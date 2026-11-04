"""Step 3: enrich a cleaned list with company data via the Parallel Group API.

Event lists repeat the same company many times, so we enrich each unique
company once (one Task run each, submitted together as a Task Group) and
then copy the results onto every lead at that company.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

import pandas as pd

from .cleaning import COMPANY, EMAIL, WEBSITE, normalize_domain


@dataclass(frozen=True)
class EnrichField:
    column: str        # Marketo column the value lands in
    key: str           # JSON key in the Task output schema
    description: str   # tells the Task processor what to find


AVAILABLE_FIELDS = [
    EnrichField(WEBSITE, "website", "Primary company website domain, e.g. acme.com. No protocol or path."),
    EnrichField("Industry", "industry", "Primary industry, e.g. 'Financial Services' or 'Computer Software'."),
    EnrichField("Number of Employees", "employee_count",
                "Best current estimate of total employees as a whole number, digits only, e.g. 1200."),
    EnrichField("Annual Revenue", "annual_revenue",
                "Most recent annual revenue in USD as a whole number, digits only, e.g. 45000000. "
                "Empty string if not publicly available."),
    EnrichField("HQ City", "hq_city", "City of the company headquarters."),
    EnrichField("HQ State", "hq_state", "State or province of the headquarters, if applicable."),
    EnrichField("HQ Country", "hq_country", "Country of the headquarters, full English name."),
    EnrichField("Company LinkedIn URL", "linkedin_url", "URL of the company's LinkedIn page."),
    EnrichField("Company Description", "description", "One-sentence description of what the company does."),
]
FIELDS_BY_COLUMN = {f.column: f for f in AVAILABLE_FIELDS}
DEFAULT_COLUMNS = ["Website", "Industry", "Number of Employees", "HQ Country"]

# Approximate list price in USD per 1,000 runs. Check https://parallel.ai/pricing.
PROCESSOR_PRICE_PER_1K = {"lite": 5, "base": 10, "core": 25, "pro": 100}

FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me", "gmx.com", "mail.com",
    "comcast.net", "att.net", "verizon.net", "sbcglobal.net", "ymail.com", "zoho.com", "yandex.com",
}

ProgressFn = Callable[[int, int, dict], None]


def recommend_processor(n_fields: int) -> str:
    """Parallel's guidance: lite ~2 fields, base ~5, core ~10, pro ~20."""
    if n_fields <= 2:
        return "lite"
    if n_fields <= 5:
        return "base"
    if n_fields <= 10:
        return "core"
    return "pro"


def estimate_cost(n_companies: int, processor: str) -> float:
    return n_companies * PROCESSOR_PRICE_PER_1K.get(processor, 0) / 1000


def build_companies(df: pd.DataFrame) -> pd.DataFrame:
    """One row per unique company: company_key, company_name, domain, lead_count.

    The domain comes from the Website column, else from a work email address.
    """
    if COMPANY not in df.columns:
        raise ValueError(f'The cleaned list needs a "{COMPANY}" column to enrich.')
    rows = []
    for _, r in df.iterrows():
        name = str(r.get(COMPANY, "")).strip()
        if not name:
            continue
        domain = normalize_domain(str(r.get(WEBSITE, "") or ""))
        if not domain:
            email = str(r.get(EMAIL, "") or "")
            email_domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
            domain = "" if email_domain in FREE_EMAIL_DOMAINS else email_domain
        rows.append({"company_key": company_key(name), "company_name": name, "domain": domain})
    if not rows:
        return pd.DataFrame(columns=["company_key", "company_name", "domain", "lead_count"])
    companies = pd.DataFrame(rows)
    counts = companies.groupby("company_key").size()
    # Prefer the first non-blank domain seen for each company.
    companies = (companies.groupby("company_key", sort=False)
                 .agg(company_name=("company_name", "first"),
                      domain=("domain", lambda s: next((d for d in s if d), "")))
                 .reset_index())
    companies["lead_count"] = companies["company_key"].map(counts)
    return companies


def company_key(name: str) -> str:
    """Normalize so 'Acme, Inc.' and 'ACME Inc' enrich once."""
    import re

    n = name.lower()
    n = re.sub(r"[^a-z0-9& ]", " ", n)
    n = re.sub(r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|gmbh|plc|sa|ag)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip() or name.lower().strip()


def build_task_spec(columns: Iterable[str]) -> dict:
    fields = [FIELDS_BY_COLUMN[c] for c in columns]
    return {
        "input_schema": {
            "type": "json",
            "json_schema": {
                "type": "object",
                "properties": {
                    "company_name": {"type": "string", "description": "Company name as written on the event list"},
                    "domain": {"type": "string", "description": "Company web domain if known, may be empty"},
                },
                "required": ["company_name"],
            },
        },
        "output_schema": {
            "type": "json",
            "json_schema": {
                "type": "object",
                "properties": {f.key: {"type": "string", "description": f.description} for f in fields},
                "required": [f.key for f in fields],
                "additionalProperties": False,
            },
        },
    }


def run_group(client, companies: pd.DataFrame, columns: list[str], processor: str,
              on_progress: ProgressFn | None = None, poll_seconds: float = 5.0,
              timeout_seconds: float = 3 * 3600) -> pd.DataFrame:
    """Enrich every company in one Task Group. Returns one row per company.

    Result columns: company_key, status, error, then for each field its value,
    "<field> confidence" and "<field> sources".
    """
    fields = [FIELDS_BY_COLUMN[c] for c in columns]
    group = client.task_group.create(metadata={"source": "list-cleanup-app"})
    group_id = group.task_group_id

    run_to_key: dict[str, str] = {}
    records = companies.to_dict("records")
    for start in range(0, len(records), 1000):  # API limit: 1,000 runs per request
        chunk = records[start:start + 1000]
        inputs = [{"input": {"company_name": r["company_name"], "domain": r["domain"]},
                   "processor": processor} for r in chunk]
        resp = client.task_group.add_runs(group_id, inputs=inputs, default_task_spec=build_task_spec(columns))
        run_to_key.update(zip(resp.run_ids, (r["company_key"] for r in chunk)))

    total = len(run_to_key)
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = client.task_group.retrieve(group_id).status
        counts = dict(status.task_run_status_counts or {})
        done = sum(v for k, v in counts.items() if k in ("completed", "failed", "cancelled"))
        if on_progress:
            on_progress(done, total, counts)
        if not status.is_active:
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"Task group {group_id} still running after {timeout_seconds / 3600:.1f}h")
        time.sleep(poll_seconds)

    results = {}
    for event in client.task_group.get_runs(group_id, include_output=True):
        if getattr(event, "type", None) != "task_run.state":
            continue
        key = run_to_key.get(event.run.run_id)
        if key is None:
            continue
        row = {"company_key": key, "status": event.run.status, "error": ""}
        if event.run.status != "completed" or event.output is None:
            err = getattr(event.run, "error", None)
            row["error"] = getattr(err, "message", None) or str(err or event.run.status)
        else:
            content = event.output.content if isinstance(event.output.content, dict) else {}
            basis = {b.field: b for b in (event.output.basis or [])}
            for f in fields:
                row[f.column] = _clean_value(content.get(f.key, ""))
                b = basis.get(f.key)
                row[f"{f.column} confidence"] = (b.confidence or "") if b else ""
                row[f"{f.column} sources"] = " ".join(c.url for c in (b.citations or [])) if b else ""
        results[key] = row
    out = pd.DataFrame(list(results.values()))
    out.attrs["task_group_id"] = group_id
    return out


def _clean_value(v) -> str:
    text = "" if v is None else str(v).strip()
    return "" if text.lower() in {"unknown", "n/a", "na", "none", "null", "not available"} else text


def apply_enrichment(df: pd.DataFrame, results: pd.DataFrame, columns: list[str],
                     overwrite: bool = False) -> tuple[pd.DataFrame, int]:
    """Copy company results onto each lead. Returns (enriched df, cells filled).

    By default only blank cells are filled, so data the attendee typed in wins.
    """
    out = df.copy()
    if results.empty or COMPANY not in out.columns:
        return out, 0
    by_key = results.set_index("company_key")
    keys = out[COMPANY].map(lambda n: company_key(str(n)) if str(n).strip() else "")
    filled = 0
    for col in columns:
        if col not in by_key.columns:
            continue
        new = keys.map(by_key[col]).fillna("")
        if col not in out.columns:
            out[col] = ""
        current = out[col].fillna("").astype(str)
        take = (new != "") & ((current == "") | overwrite) & (new != current)
        out.loc[take, col] = new[take]
        filled += int(take.sum())
    return out, filled


def audit_table(companies: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    """Per-company view with values, confidence and sources, for review."""
    if results.empty:
        return companies.copy()
    return companies.merge(results, on="company_key", how="left").drop(columns=["company_key"])

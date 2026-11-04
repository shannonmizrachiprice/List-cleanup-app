"""Event List Intake: upload -> clean for Marketo -> enrich with Parallel.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from listprep import cleaning, enrichment, ingest

load_dotenv()
st.set_page_config(page_title="Event List Intake", page_icon="📋", layout="wide")
ss = st.session_state


def api_key() -> str:
    key = ss.get("api_key_input") or os.environ.get("PARALLEL_API_KEY", "")
    if not key:
        try:
            key = st.secrets.get("PARALLEL_API_KEY", "")
        except Exception:  # no secrets.toml
            key = ""
    return key


def reset_from(step: int) -> None:
    """Clear downstream results when an earlier step changes."""
    keys = {1: ["mapping", "cleaned", "report"], 2: ["companies", "results", "enriched", "filled", "group_id"]}
    for s in (1, 2):
        if s >= step:
            for k in keys[s]:
                ss.pop(k, None)


def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def out_name(suffix: str) -> str:
    stem = Path(ss.get("file_name", "list")).stem
    return f"{stem}_{suffix}_{date.today():%Y%m%d}.csv"


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Event List Intake")
    st.caption("1. Upload  →  2. Clean for Marketo  →  3. Enrich")
    st.divider()
    st.subheader("Parallel API key")
    if os.environ.get("PARALLEL_API_KEY"):
        st.success("Loaded from environment", icon="🔑")
    else:
        st.text_input("PARALLEL_API_KEY", type="password", key="api_key_input",
                      help="Only needed for step 3. Get one at platform.parallel.ai.")
    if st.button("Start over", use_container_width=True):
        ss.clear()
        st.rerun()

# ---------------------------------------------------------------- step 1
st.header("1. Upload your event list")
upload = st.file_uploader("CSV, XLSX or XLS", type=["csv", "xlsx", "xls"])
if upload is not None:
    data = upload.getvalue()
    sheets = ingest.excel_sheets(upload.name, data)
    sheet = st.selectbox("Sheet", sheets) if len(sheets) > 1 else 0
    sig = (upload.name, len(data), sheet)
    if ss.get("upload_sig") != sig:
        try:
            ss.raw = ingest.read_upload(upload.name, data, sheet)
        except Exception as e:  # show the problem instead of a stack trace
            st.error(f"Couldn't read that file: {e}")
            st.stop()
        ss.upload_sig, ss.file_name = sig, upload.name
        reset_from(1)

if "raw" not in ss:
    st.info("Upload a file to get started.")
    st.stop()

raw: pd.DataFrame = ss.raw
st.caption(f"**{ss.file_name}**: {len(raw):,} rows × {len(raw.columns)} columns")
st.dataframe(raw.head(20), use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- step 2
st.header("2. Clean & map to Marketo fields")
if "mapping" not in ss:
    ss.mapping = cleaning.suggest_mapping(list(raw.columns))

KEEP = "(keep original name)"
options = [KEEP] + cleaning.MARKETO_FIELDS
map_df = pd.DataFrame({
    "Source column": list(raw.columns),
    "Sample value": [next((v for v in raw[c].astype(str) if v.strip() and v != "nan"), "") for c in raw.columns],
    "Marketo field": [ss.mapping.get(c) or KEEP for c in raw.columns],
})
edited = st.data_editor(
    map_df, hide_index=True, use_container_width=True, disabled=["Source column", "Sample value"],
    column_config={"Marketo field": st.column_config.SelectboxColumn(options=options, required=True)},
    key="mapping_editor",
)
mapping = {r["Source column"]: ("" if r["Marketo field"] == KEEP else r["Marketo field"]) for _, r in edited.iterrows()}

dupe_targets = pd.Series([t for t in mapping.values() if t]).value_counts()
if (dupe_targets > 1).any():
    st.warning("Several columns map to " + ", ".join(dupe_targets[dupe_targets > 1].index) +
               ". They'll be combined, keeping the first non-blank value.")

c1, c2, c3 = st.columns(3)
opts = cleaning.CleanOptions(
    merge_duplicates=c1.checkbox("Merge duplicate people", True, help="Same email (or same name + company)."),
    drop_invalid_emails=c1.checkbox("Remove rows without a valid email", True),
    fix_name_case=c2.checkbox("Fix ALL CAPS / lowercase names", True),
    normalize_countries=c2.checkbox("Standardize country names", True, help="USA, US → United States"),
    drop_unmapped=c3.checkbox("Drop columns not mapped to Marketo", False),
)

if st.button("Clean list", type="primary"):
    ss.mapping = mapping
    ss.cleaned, ss.report = cleaning.clean(raw, mapping, opts)
    reset_from(2)

if "cleaned" in ss:
    rep: cleaning.CleanReport = ss.report
    m = st.columns(4)
    m[0].metric("Rows in", f"{rep.rows_in:,}")
    m[1].metric("Rows out", f"{rep.rows_out:,}", delta=rep.rows_out - rep.rows_in or None)
    m[2].metric("Duplicates merged", f"{rep.duplicates_merged:,}")
    m[3].metric("Invalid emails removed", f"{rep.invalid_emails_removed:,}")
    for note in rep.notes:
        st.caption("• " + note)
    st.dataframe(ss.cleaned, use_container_width=True, hide_index=True)
    d1, d2 = st.columns(2)
    d1.download_button("Download cleaned CSV", csv_bytes(ss.cleaned), out_name("cleaned"), "text/csv")
    if rep.removed is not None and len(rep.removed):
        d2.download_button(f"Download {len(rep.removed):,} removed rows", csv_bytes(rep.removed),
                           out_name("removed"), "text/csv")
else:
    st.stop()

# ---------------------------------------------------------------- step 3
st.header("3. Enrich companies with Parallel")
cleaned: pd.DataFrame = ss.cleaned
if cleaning.COMPANY not in cleaned.columns:
    st.warning(f'Map a column to "{cleaning.COMPANY}" in step 2 to enable enrichment.')
    st.stop()

if "companies" not in ss:
    ss.companies = enrichment.build_companies(cleaned)
companies: pd.DataFrame = ss.companies

columns = st.multiselect("Fields to add", list(enrichment.FIELDS_BY_COLUMN), enrichment.DEFAULT_COLUMNS,
                         help="Each unique company is researched once; results are copied to all its leads.")
recommended = enrichment.recommend_processor(len(columns) or 1)
procs = list(enrichment.PROCESSOR_PRICE_PER_1K)
e1, e2 = st.columns(2)
processor = e1.selectbox("Processor", procs, index=procs.index(recommended),
                         help="lite ≈2 fields, base ≈5, core ≈10, pro ≈20. Bigger = more thorough, slower, pricier.")
overwrite = e2.checkbox("Overwrite values already in the list", False,
                        help="Off: only blank cells are filled, so data attendees entered wins.")
limit = e2.number_input("Companies to enrich (for a test run)", 1, max(len(companies), 1), max(len(companies), 1))

to_run = companies.head(int(limit))
cost = enrichment.estimate_cost(len(to_run), processor)
st.caption(f"**{len(companies):,} unique companies** across {len(cleaned):,} leads. "
           f"Running {len(to_run):,} on **{processor}** ≈ **${cost:,.2f}** at list price "
           f"([pricing](https://parallel.ai/pricing)). Recommended for {len(columns)} fields: {recommended}.")
with st.expander("Companies that will be researched"):
    st.dataframe(to_run.drop(columns=["company_key"]), use_container_width=True, hide_index=True)

if st.button("Run enrichment", type="primary", disabled=not columns or to_run.empty):
    key = api_key()
    if not key:
        st.error("Add your Parallel API key in the sidebar first.")
        st.stop()
    from parallel import Parallel

    bar = st.progress(0.0, text="Submitting to Parallel…")

    def progress(done: int, total: int, counts: dict) -> None:
        detail = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        bar.progress(done / total if total else 1.0, text=f"{done}/{total} companies done ({detail})")

    try:
        results = enrichment.run_group(Parallel(api_key=key), to_run, columns, processor, on_progress=progress)
    except Exception as e:
        st.error(f"Enrichment failed: {e}")
        st.stop()
    ss.results, ss.group_id, ss.enrich_columns = results, results.attrs.get("task_group_id"), columns
    ss.enriched, ss.filled = enrichment.apply_enrichment(cleaned, results, columns, overwrite)
    bar.progress(1.0, text="Done")

if "enriched" in ss:
    results: pd.DataFrame = ss.results
    ok = int((results["status"] == "completed").sum()) if len(results) else 0
    m = st.columns(3)
    m[0].metric("Companies enriched", f"{ok:,} / {len(results):,}")
    m[1].metric("Cells filled", f"{ss.filled:,}")
    m[2].metric("Leads", f"{len(ss.enriched):,}")
    st.caption(f"Parallel task group: `{ss.group_id}`")
    failed = results[results["status"] != "completed"] if len(results) else results
    if len(failed):
        st.warning(f"{len(failed)} companies didn't complete. They're left blank; see the audit file.")

    st.subheader("Marketo-ready list")
    st.dataframe(ss.enriched, use_container_width=True, hide_index=True)
    audit = enrichment.audit_table(companies, results)
    with st.expander("Per-company results with confidence and sources"):
        st.dataframe(audit, use_container_width=True, hide_index=True)
    d1, d2 = st.columns(2)
    d1.download_button("Download Marketo import CSV", csv_bytes(ss.enriched), out_name("marketo"),
                       "text/csv", type="primary")
    d2.download_button("Download enrichment audit (sources)", csv_bytes(audit), out_name("enrichment_audit"),
                       "text/csv")
    st.caption("Fields like HQ City or Company Description aren't Marketo standard fields. "
               "Map them to custom fields on import or skip them.")

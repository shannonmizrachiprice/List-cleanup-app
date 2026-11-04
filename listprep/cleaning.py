"""Step 2: clean a raw event list and shape it for Marketo import.

Marketo's list import auto-maps columns whose headers match field display
names, so the goal is to rename columns to those names, tidy values, and
collapse duplicate people into one row each.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

# Marketo standard lead field display names.
EMAIL = "Email Address"
FIRST = "First Name"
LAST = "Last Name"
COMPANY = "Company Name"
WEBSITE = "Website"

MARKETO_FIELDS = [
    FIRST, LAST, EMAIL, COMPANY, "Job Title", "Phone Number", "Mobile Phone Number",
    "Address", "City", "State", "Postal Code", "Country", WEBSITE, "Industry",
    "Number of Employees", "Annual Revenue", "LinkedIn Profile URL",
]

# Exact header matches, compared after lowercasing and removing non-letters.
_ALIASES = {
    EMAIL: ["email", "emailaddress", "e-mail", "workemail", "businessemail", "attendeeemail", "contactemail"],
    FIRST: ["firstname", "first", "fname", "givenname", "forename"],
    LAST: ["lastname", "last", "lname", "surname", "familyname"],
    COMPANY: ["company", "companyname", "organization", "organisation", "org", "account",
              "accountname", "employer", "business"],
    "Job Title": ["title", "jobtitle", "position", "role", "designation"],
    "Phone Number": ["phone", "phonenumber", "workphone", "telephone", "tel", "businessphone", "officephone"],
    "Mobile Phone Number": ["mobile", "mobilephone", "cell", "cellphone", "mobilenumber"],
    "Address": ["address", "street", "streetaddress", "address1", "addressline1"],
    "City": ["city", "town"],
    "State": ["state", "province", "region", "stateprovince", "county"],
    "Postal Code": ["zip", "zipcode", "postalcode", "postcode"],
    "Country": ["country", "countryregion", "nation"],
    WEBSITE: ["website", "url", "companywebsite", "web", "domain", "companyurl", "companydomain"],
    "Industry": ["industry", "sector", "vertical"],
    "Number of Employees": ["employees", "numberofemployees", "employeecount", "companysize", "headcount"],
    "Annual Revenue": ["revenue", "annualrevenue"],
    "LinkedIn Profile URL": ["linkedin", "linkedinurl", "linkedinprofile", "linkedinprofileurl"],
}
_EXACT = {alias: target for target, aliases in _ALIASES.items() for alias in aliases}

# Looser fallbacks for headers like "Attendee E-mail (work)".
_CONTAINS = [
    ("email", EMAIL), ("firstname", FIRST), ("lastname", LAST), ("jobtitle", "Job Title"),
    ("company", COMPANY), ("organization", COMPANY), ("mobile", "Mobile Phone Number"),
    ("phone", "Phone Number"), ("zip", "Postal Code"), ("postal", "Postal Code"),
    ("country", "Country"), ("linkedin", "LinkedIn Profile URL"), ("website", WEBSITE),
]

FULL_NAME_ALIASES = {"name", "fullname", "attendeename", "contactname", "registrantname"}

_COUNTRIES = {
    "us": "United States", "usa": "United States", "u.s.": "United States", "u.s.a.": "United States",
    "united states of america": "United States", "america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "gb": "United Kingdom",
    "great britain": "United Kingdom", "england": "United Kingdom",
    "uae": "United Arab Emirates", "deutschland": "Germany",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _norm(header: str) -> str:
    return re.sub(r"[^a-z]", "", str(header).lower())


def suggest_mapping(columns: list[str]) -> dict[str, str]:
    """Suggest a Marketo field for each source column ("" = keep original name).

    Each Marketo field is assigned at most once; the first matching column wins.
    """
    mapping: dict[str, str] = {}
    used: set[str] = set()
    # Exact matches first so "Email" beats "Email Opt-in" for Email Address.
    for col in columns:
        target = _EXACT.get(_norm(col))
        if target and target not in used:
            mapping[col] = target
            used.add(target)
    for col in columns:
        if col in mapping:
            continue
        n = _norm(col)
        target = next((t for key, t in _CONTAINS if key in n and t not in used), "")
        if target:
            used.add(target)
        mapping[col] = target
    return {col: mapping.get(col, "") for col in columns}


@dataclass
class CleanOptions:
    drop_invalid_emails: bool = True
    fix_name_case: bool = True
    normalize_countries: bool = True
    merge_duplicates: bool = True
    drop_unmapped: bool = False


@dataclass
class CleanReport:
    rows_in: int = 0
    rows_out: int = 0
    empty_rows_removed: int = 0
    invalid_emails_removed: int = 0
    duplicates_merged: int = 0
    names_split: int = 0
    notes: list[str] = field(default_factory=list)
    removed: pd.DataFrame | None = None  # rows dropped, with a "Reason" column


def clean(df: pd.DataFrame, mapping: dict[str, str], opts: CleanOptions | None = None):
    """Return (cleaned DataFrame, CleanReport)."""
    opts = opts or CleanOptions()
    report = CleanReport(rows_in=len(df))
    removed: list[pd.DataFrame] = []

    out = df.copy().astype(object).where(df.notna(), "")
    out = out.map(_tidy)

    # Rename columns. Unmapped columns keep their name (or are dropped).
    rename = {c: t for c, t in mapping.items() if t}
    full_name_col = next((c for c in out.columns if not mapping.get(c) and _norm(c) in FULL_NAME_ALIASES), None)
    if opts.drop_unmapped:
        keep = [c for c in out.columns if mapping.get(c) or c == full_name_col]
        out = out[keep]
    out = out.rename(columns=rename)
    out = _combine_same_named_columns(out)

    # Split "Full Name" into first/last when the file has no separate columns.
    if full_name_col and (FIRST not in out.columns or LAST not in out.columns):
        parts = out[full_name_col].map(_split_name)
        out[FIRST] = [p[0] for p in parts]
        out[LAST] = [p[1] for p in parts]
        report.names_split = int((out[full_name_col] != "").sum())
        out = out.drop(columns=[full_name_col])
        report.notes.append(f'Split "{full_name_col}" into First Name and Last Name.')

    # Drop rows with nothing in them.
    blank = (out == "").all(axis=1)
    report.empty_rows_removed = int(blank.sum())
    out = out[~blank]

    if EMAIL in out.columns:
        out[EMAIL] = out[EMAIL].str.lower().str.replace(r"^mailto:", "", regex=True)
        if opts.drop_invalid_emails:
            bad = ~out[EMAIL].str.match(EMAIL_RE)
            report.invalid_emails_removed = int(bad.sum())
            removed.append(out[bad].assign(Reason="Missing or invalid email"))
            out = out[~bad]
    else:
        report.notes.append("No email column found. Marketo needs Email Address to dedupe leads on import.")

    if opts.fix_name_case:
        for col in (FIRST, LAST):
            if col in out.columns:
                out[col] = out[col].map(_fix_case)
    if opts.normalize_countries and "Country" in out.columns:
        out["Country"] = out["Country"].map(lambda v: _COUNTRIES.get(v.lower(), v))
    if WEBSITE in out.columns:
        out[WEBSITE] = out[WEBSITE].map(normalize_domain)

    if opts.merge_duplicates:
        before = len(out)
        out, dupes = _merge_duplicates(out)
        report.duplicates_merged = before - len(out)
        if len(dupes):
            removed.append(dupes.assign(Reason="Duplicate (merged into first occurrence)"))

    # Marketo fields first in a predictable order, then anything else.
    ordered = [c for c in MARKETO_FIELDS if c in out.columns]
    out = out[ordered + [c for c in out.columns if c not in ordered]].reset_index(drop=True)

    report.rows_out = len(out)
    report.removed = pd.concat(removed, ignore_index=True) if removed else pd.DataFrame()
    return out, report


def _tidy(value) -> str:
    text = "" if value is None else str(value)
    text = text.replace(" ", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return "" if text.lower() in {"nan", "none", "null", "n/a", "na", "-", "--"} else text


def _combine_same_named_columns(df: pd.DataFrame) -> pd.DataFrame:
    """If two source columns map to one field, keep the first non-blank value."""
    if not df.columns.duplicated().any():
        return df
    combined = {}
    for col in dict.fromkeys(df.columns):
        block = df.loc[:, df.columns == col]
        combined[col] = block.apply(lambda r: next((v for v in r if v), ""), axis=1) if block.shape[1] > 1 else block.iloc[:, 0]
    return pd.DataFrame(combined, index=df.index)


def _split_name(full: str) -> tuple[str, str]:
    if not full:
        return "", ""
    if "," in full:  # "Lovelace, Ada"
        last, _, first = full.partition(",")
        return first.strip(), last.strip()
    parts = full.split(" ")
    return (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (parts[0], "")


def _fix_case(name: str) -> str:
    """Title-case names typed ALL CAPS or all lowercase; leave mixed case alone (McDonald)."""
    if name and (name.isupper() or name.islower()):
        return re.sub(r"(^|[\s\-'])(\w)", lambda m: m.group(1) + m.group(2).upper(), name.lower())
    return name


def normalize_domain(value: str) -> str:
    """'https://www.Acme.com/about' -> 'acme.com'."""
    v = value.strip().lower()
    v = re.sub(r"^[a-z]+://", "", v)
    v = re.sub(r"^www\.", "", v)
    return v.split("/")[0].split("?")[0]


def _dedupe_key(df: pd.DataFrame) -> pd.Series:
    if EMAIL in df.columns:
        key = df[EMAIL].copy()
    else:
        key = pd.Series("", index=df.index)
    fallback_cols = [c for c in (FIRST, LAST, COMPANY) if c in df.columns]
    if fallback_cols:
        fallback = df[fallback_cols].apply(lambda r: "|".join(r).lower(), axis=1)
        fallback = fallback.where(fallback.str.replace("|", "") != "", "")
        key = key.where(key != "", fallback)
    # Rows with no usable key are never treated as duplicates.
    return key.where(key != "", pd.Series([f"__row{i}" for i in df.index], index=df.index))


def _merge_duplicates(df: pd.DataFrame):
    """Collapse rows sharing a key, filling blanks from later duplicates."""
    key = _dedupe_key(df)
    dup_mask = key.duplicated(keep="first")
    if not dup_mask.any():
        return df, df.iloc[0:0]
    merged = df.groupby(key, sort=False).agg(lambda s: next((v for v in s if v), ""))
    # groupby reorders by first appearance with sort=False; restore original index.
    first_idx = df.index[~dup_mask]
    merged.index = first_idx
    return merged, df[dup_mask]

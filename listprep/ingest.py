"""Step 1: read an uploaded CSV / XLSX / XLS file into a DataFrame."""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

SUPPORTED = (".csv", ".xlsx", ".xls")


def read_upload(name: str, data: bytes, sheet: str | int = 0) -> pd.DataFrame:
    """Read file bytes into a DataFrame with every cell as text.

    Everything is read as strings so zip codes, phone numbers and IDs keep
    their leading zeros and formatting.
    """
    ext = Path(name).suffix.lower()
    if ext == ".csv":
        df = _read_csv(data)
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(io.BytesIO(data), sheet_name=sheet, dtype=str)
    else:
        raise ValueError(f"Unsupported file type {ext!r}. Upload a CSV, XLSX or XLS file.")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def excel_sheets(name: str, data: bytes) -> list[str]:
    """Sheet names of an Excel workbook (empty for CSV)."""
    if Path(name).suffix.lower() not in (".xlsx", ".xls"):
        return []
    return list(pd.ExcelFile(io.BytesIO(data)).sheet_names)


def _read_csv(data: bytes) -> pd.DataFrame:
    # Event platforms export in a mix of encodings; try the common ones.
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    # sep=None sniffs commas vs. semicolons vs. tabs.
    return pd.read_csv(io.StringIO(text), dtype=str, sep=None, engine="python", keep_default_na=False)

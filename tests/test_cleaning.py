from pathlib import Path

import pandas as pd

from listprep import cleaning, ingest

SAMPLE = Path(__file__).parent / "sample_event_list.csv"


def load():
    return ingest.read_upload(SAMPLE.name, SAMPLE.read_bytes())


def test_suggest_mapping_matches_messy_headers():
    m = cleaning.suggest_mapping(["Attendee Name", "Email", "Company", "Title", "Phone", "Country", "Registered"])
    assert m["Email"] == "Email Address"
    assert m["Company"] == "Company Name"
    assert m["Title"] == "Job Title"
    assert m["Phone"] == "Phone Number"
    assert m["Attendee Name"] == ""   # handled by full-name split
    assert m["Registered"] == ""


def test_mapping_assigns_each_field_once():
    m = cleaning.suggest_mapping(["Email", "Email Opt-in"])
    assert m == {"Email": "Email Address", "Email Opt-in": ""}


def test_clean_end_to_end():
    raw = load()
    out, rep = cleaning.clean(raw, cleaning.suggest_mapping(list(raw.columns)))

    assert rep.rows_in == 7
    assert rep.empty_rows_removed == 1
    assert rep.invalid_emails_removed == 1   # not-an-email
    assert rep.duplicates_merged == 1        # Ada twice
    assert rep.rows_out == 4

    assert list(out.columns[:4]) == ["First Name", "Last Name", "Email Address", "Company Name"]
    ada = out[out["Email Address"] == "ada@stripe.com"].iloc[0]
    assert (ada["First Name"], ada["Last Name"]) == ("Ada", "Lovelace")
    assert ada["Job Title"] == "Engineer"
    assert ada["Country"] == "United States"
    assert set(rep.removed["Reason"]) == {"Missing or invalid email", "Duplicate (merged into first occurrence)"}


def test_merge_fills_blanks_from_later_duplicate():
    df = pd.DataFrame({"Email": ["a@x.com", "A@x.com"], "Title": ["", "CTO"], "Company": ["X", ""]})
    out, rep = cleaning.clean(df, cleaning.suggest_mapping(list(df.columns)))
    assert len(out) == 1 and rep.duplicates_merged == 1
    assert out.iloc[0]["Job Title"] == "CTO" and out.iloc[0]["Company Name"] == "X"


def test_name_case_preserves_mixed_case():
    assert cleaning._fix_case("MCDONALD") == "Mcdonald"
    assert cleaning._fix_case("McDonald") == "McDonald"
    assert cleaning._fix_case("o'brien-smith") == "O'Brien-Smith"


def test_full_name_split_handles_comma():
    assert cleaning._split_name("Lovelace, Ada") == ("Ada", "Lovelace")
    assert cleaning._split_name("Mary Ann Smith") == ("Mary", "Ann Smith")


def test_normalize_domain():
    assert cleaning.normalize_domain("https://www.Acme.com/about?x=1") == "acme.com"


def test_reads_excel(tmp_path):
    p = tmp_path / "list.xlsx"
    pd.DataFrame({"Email": ["a@x.com"], "Zip": ["02134"]}).to_excel(p, index=False)
    df = ingest.read_upload(p.name, p.read_bytes())
    assert df.loc[0, "Zip"] == "02134"   # leading zero kept


def test_reads_semicolon_cp1252_csv():
    data = "Email;Company\nzoë@x.com;Zoë Co\n".encode("cp1252")
    df = ingest.read_upload("x.csv", data)
    assert df.loc[0, "Company"] == "Zoë Co"

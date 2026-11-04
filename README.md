# Event List Intake

A small web app for turning raw event attendee lists into Marketo-ready imports.

1. **Upload**: a CSV, XLSX or XLS export from any event platform. Semicolon and tab-separated files, Windows encodings and multi-sheet workbooks work. Cells are read as text, so zip codes and phone numbers keep their leading zeros.
2. **Clean**: column headers are matched to Marketo field names (`Email Address`, `First Name`, `Company Name`, …), and you can adjust the mapping in a table. Then:
   - whitespace is trimmed;
   - emails are lowercased and checked, and rows without a valid one are removed;
   - ALL CAPS and all-lowercase names are fixed;
   - "Full Name" columns are split into first and last;
   - countries like `USA` / `US` become `United States`;
   - duplicate people are merged into one row, filling blanks from the copies.

   You can download both the cleaned list and the removed rows, with the reason each was removed.
3. **Enrich**: adds company data (website, industry, employee count, revenue, headquarters, LinkedIn, description) with the [Parallel Group API](https://docs.parallel.ai/task-api/group-api). Each **unique company is researched once**, and the results are copied to every lead at that company. By default only blank cells are filled. An audit CSV records the confidence and source URLs for every value.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then paste your key from https://platform.parallel.ai
streamlit run app.py
```

Steps 1 and 2 work without a key. You can also paste the key into the sidebar instead of using `.env`.
If you deploy to Streamlit Community Cloud, put `PARALLEL_API_KEY` in the app's secrets.

## Cost

Enrichment is billed per company, not per lead. The app shows an estimate before you run. Roughly, per 1,000 companies at list price: lite $5, base $10, core $25, pro $100 ([current pricing](https://parallel.ai/pricing)).
The app suggests a processor based on how many fields you pick (lite ≈2, base ≈5, core ≈10).
Use **Companies to enrich** to try a handful first.

## Layout

```
app.py                  Streamlit UI (the three steps)
listprep/ingest.py      file reading
listprep/cleaning.py    header mapping, cleanup, dedupe
listprep/enrichment.py  company list, Task Group submit/poll/collect, merge back
tests/                  pytest suite + sample_event_list.csv
```

Run the tests with `pytest`. They use a fake Parallel client, so they cost nothing.

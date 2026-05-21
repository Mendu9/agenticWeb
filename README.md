# Agentic Web RAA Downloader

This project is a Streamlit application for discovering, tracking, and downloading regional administrative PDF documents.

## What It Does

- Provides a Streamlit UI with scraping, RAA discovery, and PDF download workflows.
- Discovers regional document pages and follows RAA-style listing, year, month, and intermediate pages.
- Extracts PDF links and groups downloads by region and year.
- Supports a daily diff workflow backed by Postgres:
  - first run creates an archive baseline,
  - later runs compare newly discovered PDF URLs against the database,
  - downloads happen only after the user clicks the daily diff download button.
- Exports daily run, inventory, new document, and download data to Excel.

## Main Files

- `scraper_app.py` - Streamlit UI and tab orchestration.
- `prefecture_raa.py` - RAA discovery, crawling, metadata, and download helpers.
- `raa_daily_tracker.py` - Postgres baseline/daily diff tracking and Excel export.
- `pdf_downloader.py` - Shared PDF/document download utilities.
- `test_prefecture_raa.py` - RAA workflow tests.

## Outputs

Downloaded files are stored under the selected download folder:

```text
<download_folder>/<region>/<year>/<pdf_file>
```

Daily tracking writes:

```text
prefecture_raa_daily_report.xlsx
```

## Configuration

Postgres settings can be provided through environment variables or Streamlit secrets:

- `RAA_DB_HOST`
- `RAA_DB_PORT`
- `RAA_DB_NAME`
- `RAA_DB_USER`
- `RAA_DB_PASSWORD`

Optional SSL setting:

- `PREFECTURE_RAA_VERIFY_SSL`

## Tests

Run the RAA-focused tests:

```bash
python -m pytest -q test_prefecture_raa.py
```

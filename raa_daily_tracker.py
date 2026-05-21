from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol, Sequence

import pandas as pd

from pdf_downloader import summarize_downloads
from prefecture_raa import (
    PREFECTURE_PORTAL_URL,
    _download_prefecture_documents,
    _normalize_url,
    discover_prefecture_raa,
)


EXCLUDED_DAILY_REGIONS = {"corse"}
MONTH_NAMES_EN = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


class RaaDailyRepository(Protocol):
    def ensure_schema(self) -> None: ...
    def has_completed_archive_baseline(self) -> bool: ...
    def create_run(self, started_at: datetime, status: str, regions_scanned: int, months_scanned: str, run_type: str = "daily_diff") -> int: ...
    def finish_run(
        self,
        run_id: int,
        finished_at: datetime,
        status: str,
        pdfs_discovered: int,
        new_pdfs: int,
        downloaded: int,
        failed: int,
        error: str = "",
        timings: Optional[Dict[str, float]] = None,
    ) -> None: ...
    def get_known_urls(self, normalized_urls: Sequence[str]) -> set[str]: ...
    def upsert_documents(self, documents: Sequence[Dict], run_id: int, seen_at: datetime) -> Dict[str, int]: ...
    def record_downloads(self, run_id: int, document_ids_by_url: Dict[str, int], documents: Sequence[Dict], downloads: Sequence[Dict]) -> None: ...
    def update_run_download_summary(self, run_id: int, downloaded: int, failed: int, timings: Optional[Dict[str, float]] = None) -> None: ...
    def fetch_new_documents_for_run(self, run_id: int) -> List[Dict]: ...
    def fetch_daily_runs(self) -> List[Dict]: ...
    def fetch_documents(self) -> List[Dict]: ...
    def fetch_downloads(self) -> List[Dict]: ...


def postgres_config_from_env(secrets: Optional[Dict] = None) -> Dict[str, str]:
    secrets = secrets or {}

    def _value(name: str, default: str = "") -> str:
        return str(os.getenv(name) or secrets.get(name) or default)

    return {
        "host": _value("RAA_DB_HOST", "localhost"),
        "port": _value("RAA_DB_PORT", "5432"),
        "dbname": _value("RAA_DB_NAME", "postgres"),
        "user": _value("RAA_DB_USER", "postgres"),
        "password": _value("RAA_DB_PASSWORD", "mendu"),
    }


class PostgresRaaDailyRepository:
    def __init__(self, config: Dict[str, str]):
        self.config = config

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Postgres daily tracking requires psycopg. Install dependencies from requirements.txt.") from exc
        return psycopg.connect(**self.config)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists raa_daily_runs (
                      id bigserial primary key,
                      run_type text not null default 'daily_diff',
                      started_at timestamptz not null,
                      finished_at timestamptz,
                      status text not null,
                      regions_scanned int not null,
                      months_scanned text not null,
                      pdfs_discovered int default 0,
                      new_pdfs int default 0,
                      downloaded int default 0,
                      failed int default 0,
                      discovery_seconds double precision default 0,
                      db_seconds double precision default 0,
                      download_seconds double precision default 0,
                      excel_seconds double precision default 0,
                      total_seconds double precision default 0,
                      error text
                    )
                    """
                )
                cur.execute("alter table raa_daily_runs add column if not exists run_type text not null default 'daily_diff'")
                cur.execute("alter table raa_daily_runs add column if not exists discovery_seconds double precision default 0")
                cur.execute("alter table raa_daily_runs add column if not exists db_seconds double precision default 0")
                cur.execute("alter table raa_daily_runs add column if not exists download_seconds double precision default 0")
                cur.execute("alter table raa_daily_runs add column if not exists excel_seconds double precision default 0")
                cur.execute("alter table raa_daily_runs add column if not exists total_seconds double precision default 0")
                cur.execute(
                    """
                    create table if not exists raa_documents (
                      id bigserial primary key,
                      normalized_url text not null unique,
                      pdf_url text not null,
                      region text not null,
                      year text,
                      month text,
                      publication_date date,
                      date_source text,
                      source_page text,
                      title text,
                      first_seen_at timestamptz not null,
                      last_seen_at timestamptz not null,
                      first_run_id bigint references raa_daily_runs(id),
                      last_run_id bigint references raa_daily_runs(id)
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists raa_downloads (
                      id bigserial primary key,
                      run_id bigint references raa_daily_runs(id),
                      document_id bigint references raa_documents(id),
                      attempted_at timestamptz not null,
                      status text not null,
                      saved_path text,
                      error text
                    )
                    """
                )

    def has_completed_archive_baseline(self) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select exists(select 1 from raa_daily_runs where run_type = 'archive_baseline' and status = 'baseline_created')")
                return bool(cur.fetchone()[0])

    def create_run(self, started_at: datetime, status: str, regions_scanned: int, months_scanned: str, run_type: str = "daily_diff") -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into raa_daily_runs (run_type, started_at, status, regions_scanned, months_scanned)
                    values (%s, %s, %s, %s, %s)
                    returning id
                    """,
                    (run_type, started_at, status, regions_scanned, months_scanned),
                )
                return int(cur.fetchone()[0])

    def finish_run(
        self,
        run_id: int,
        finished_at: datetime,
        status: str,
        pdfs_discovered: int,
        new_pdfs: int,
        downloaded: int,
        failed: int,
        error: str = "",
        timings: Optional[Dict[str, float]] = None,
    ) -> None:
        timings = timings or {}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update raa_daily_runs
                    set finished_at = %s,
                        status = %s,
                        pdfs_discovered = %s,
                        new_pdfs = %s,
                        downloaded = %s,
                        failed = %s,
                        discovery_seconds = %s,
                        db_seconds = %s,
                        download_seconds = %s,
                        excel_seconds = %s,
                        total_seconds = %s,
                        error = %s
                    where id = %s
                    """,
                    (
                        finished_at,
                        status,
                        pdfs_discovered,
                        new_pdfs,
                        downloaded,
                        failed,
                        timings.get("discovery_seconds", 0),
                        timings.get("db_seconds", 0),
                        timings.get("download_seconds", 0),
                        timings.get("excel_seconds", 0),
                        timings.get("total_seconds", 0),
                        error,
                        run_id,
                    ),
                )

    def get_known_urls(self, normalized_urls: Sequence[str]) -> set[str]:
        if not normalized_urls:
            return set()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select normalized_url from raa_documents where normalized_url = any(%s)", (list(normalized_urls),))
                return {row[0] for row in cur.fetchall()}

    def upsert_documents(self, documents: Sequence[Dict], run_id: int, seen_at: datetime) -> Dict[str, int]:
        ids_by_url: Dict[str, int] = {}
        with self._connect() as conn:
            with conn.cursor() as cur:
                for document in documents:
                    normalized_url = _normalize_url(str(document.get("url") or ""))
                    if not normalized_url:
                        continue
                    publication_date = document.get("publication_date") or None
                    cur.execute(
                        """
                        insert into raa_documents (
                            normalized_url, pdf_url, region, year, month, publication_date, date_source,
                            source_page, title, first_seen_at, last_seen_at, first_run_id, last_run_id
                        )
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        on conflict (normalized_url) do update set
                            last_seen_at = excluded.last_seen_at,
                            last_run_id = excluded.last_run_id,
                            publication_date = coalesce(raa_documents.publication_date, excluded.publication_date),
                            date_source = coalesce(nullif(raa_documents.date_source, ''), excluded.date_source),
                            source_page = excluded.source_page,
                            title = excluded.title
                        returning id
                        """,
                        (
                            normalized_url,
                            document.get("url", ""),
                            document.get("region", "") or "unknown-region",
                            document.get("year", ""),
                            document.get("month", ""),
                            publication_date,
                            document.get("date_source", ""),
                            document.get("source_page", ""),
                            document.get("title", ""),
                            seen_at,
                            seen_at,
                            run_id,
                            run_id,
                        ),
                    )
                    ids_by_url[normalized_url] = int(cur.fetchone()[0])
        return ids_by_url

    def record_downloads(self, run_id: int, document_ids_by_url: Dict[str, int], documents: Sequence[Dict], downloads: Sequence[Dict]) -> None:
        attempted_at = datetime.now(timezone.utc)
        with self._connect() as conn:
            with conn.cursor() as cur:
                for document, download in zip(documents, downloads):
                    normalized_url = _normalize_url(str(document.get("url") or ""))
                    status = str(download.get("status") or "")
                    error = status if status.startswith("failed:") else ""
                    cur.execute(
                        """
                        insert into raa_downloads (run_id, document_id, attempted_at, status, saved_path, error)
                        values (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run_id,
                            document_ids_by_url.get(normalized_url),
                            attempted_at,
                            status,
                            download.get("file_path", ""),
                            error,
                        ),
                    )

    def update_run_download_summary(self, run_id: int, downloaded: int, failed: int, timings: Optional[Dict[str, float]] = None) -> None:
        timings = timings or {}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update raa_daily_runs
                    set downloaded = %s,
                        failed = %s,
                        db_seconds = coalesce(db_seconds, 0) + %s,
                        download_seconds = coalesce(download_seconds, 0) + %s,
                        excel_seconds = coalesce(excel_seconds, 0) + %s,
                        total_seconds = coalesce(total_seconds, 0) + %s
                    where id = %s
                    """,
                    (
                        downloaded,
                        failed,
                        timings.get("db_seconds", 0),
                        timings.get("download_seconds", 0),
                        timings.get("excel_seconds", 0),
                        timings.get("total_seconds", 0),
                        run_id,
                    ),
                )

    def fetch_new_documents_for_run(self, run_id: int) -> List[Dict]:
        return self._fetch_rows(
            """
            select
                pdf_url as url,
                region,
                year,
                month,
                publication_date::text as publication_date,
                date_source,
                source_page,
                title,
                'PDF' as file_format
            from raa_documents
            where first_run_id = %s
            order by region, publication_date nulls last, title
            """,
            (run_id,),
        )

    def fetch_daily_runs(self) -> List[Dict]:
        return self._fetch_rows("select * from raa_daily_runs order by started_at desc")

    def fetch_documents(self) -> List[Dict]:
        return self._fetch_rows("select * from raa_documents order by region, year desc, title")

    def fetch_downloads(self) -> List[Dict]:
        return self._fetch_rows("select * from raa_downloads order by attempted_at desc")

    def _fetch_rows(self, query: str, params: Optional[Sequence] = None) -> List[Dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params or ())
                columns = [description.name for description in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]


def current_and_previous_months(today: Optional[date] = None) -> List[tuple[str, int, str]]:
    today = today or date.today()
    current = date(today.year, today.month, 1)
    # previous_year = current.year if current.month > 1 else current.year - 1
    # previous_month = current.month - 1 if current.month > 1 else 12
    # previous = date(previous_year, previous_month, 1)
    return [
        (MONTH_NAMES_EN[current.month], current.year, current.isoformat()[:7]),
        # (MONTH_NAMES_EN[previous.month], previous.year, previous.isoformat()[:7]),
    ]


def target_years_for_daily(today: Optional[date] = None) -> List[str]:
    today = today or date.today()
    return [str(today.year)]


def document_in_month_scope(document: Dict, month_scope: Sequence[tuple[str, int, str]]) -> bool:
    publication_date = str(document.get("publication_date") or "")
    if len(publication_date) >= 7:
        return publication_date[:7] in {item[2] for item in month_scope}
    year = str(document.get("year") or "")
    month = str(document.get("month") or "")
    return any(year == str(scope_year) and month == scope_month for scope_month, scope_year, _ in month_scope)


def filter_daily_documents(discovery_result: Dict, month_scope: Sequence[tuple[str, int, str]]) -> List[Dict]:
    documents = []
    seen = set()
    for document in discovery_result.get("documents", []):
        region = str(document.get("region") or "").lower()
        normalized_url = _normalize_url(str(document.get("url") or ""))
        if region in EXCLUDED_DAILY_REGIONS or not normalized_url or normalized_url in seen:
            continue
        if not document_in_month_scope(document, month_scope):
            continue
        seen.add(normalized_url)
        documents.append(document)
    return documents


def filter_archive_documents(discovery_result: Dict, excluded_regions: Iterable[str] = EXCLUDED_DAILY_REGIONS) -> List[Dict]:
    excluded = {str(region).lower() for region in excluded_regions}
    documents = []
    seen = set()
    for document in discovery_result.get("documents", []):
        region = str(document.get("region") or "").lower()
        normalized_url = _normalize_url(str(document.get("url") or ""))
        if region in excluded or not normalized_url or normalized_url in seen:
            continue
        seen.add(normalized_url)
        documents.append(document)
    return documents


def summarize_documents_by_region(documents: Sequence[Dict]) -> List[Dict]:
    summary: Dict[str, Dict[str, object]] = {}
    for document in documents:
        region = str(document.get("region") or "unknown-region")
        row = summary.setdefault(region, {"region": region, "new_pdfs": 0, "years": set(), "months": set()})
        row["new_pdfs"] = int(row["new_pdfs"]) + 1
        if document.get("year"):
            row["years"].add(str(document.get("year")))
        if document.get("month"):
            row["months"].add(str(document.get("month")))
    rows = []
    for row in summary.values():
        years = sorted(row["years"], reverse=True)
        months = sorted(row["months"])
        rows.append(
            {
                "region": row["region"],
                "new_pdfs": row["new_pdfs"],
                "years": ", ".join(years),
                "months": ", ".join(months),
            }
        )
    return sorted(rows, key=lambda item: (-int(item["new_pdfs"]), str(item["region"])))


def summarize_daily_scope_by_region(documents: Sequence[Dict], known_urls: Iterable[str]) -> List[Dict]:
    known = set(known_urls)
    summary: Dict[str, Dict[str, object]] = {}
    for document in documents:
        region = str(document.get("region") or "unknown-region")
        normalized_url = _normalize_url(str(document.get("url") or ""))
        row = summary.setdefault(
            region,
            {"region": region, "daily_scoped_pdfs": 0, "already_known": 0, "new_pdfs": 0, "years": set(), "months": set()},
        )
        row["daily_scoped_pdfs"] = int(row["daily_scoped_pdfs"]) + 1
        if normalized_url in known:
            row["already_known"] = int(row["already_known"]) + 1
        else:
            row["new_pdfs"] = int(row["new_pdfs"]) + 1
        if document.get("year"):
            row["years"].add(str(document.get("year")))
        if document.get("month"):
            row["months"].add(str(document.get("month")))
    rows = []
    for row in summary.values():
        rows.append(
            {
                "region": row["region"],
                "daily_scoped_pdfs": row["daily_scoped_pdfs"],
                "already_known": row["already_known"],
                "new_pdfs": row["new_pdfs"],
                "years": ", ".join(sorted(row["years"], reverse=True)),
                "months": ", ".join(sorted(row["months"])),
            }
        )
    return sorted(rows, key=lambda item: str(item["region"]))


def _excel_safe_value(value):
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is not None:
            return value.tz_convert(None).to_pydatetime()
        return value.to_pydatetime()
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _excel_safe_dataframe(rows: Sequence[Dict]) -> pd.DataFrame:
    dataframe = pd.DataFrame(rows)
    if dataframe.empty:
        return dataframe
    return dataframe.apply(lambda column: column.map(_excel_safe_value))


def _seconds_since(started_at: float) -> float:
    return round(time.perf_counter() - started_at, 2)


def write_daily_excel(workbook_path: str | Path, repo: RaaDailyRepository, new_documents: Sequence[Dict], downloads: Sequence[Dict]) -> None:
    workbook_path = Path(workbook_path)
    workbook_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(workbook_path, engine="openpyxl") as writer:
        _excel_safe_dataframe(repo.fetch_daily_runs()).to_excel(writer, sheet_name="daily_runs", index=False)
        _excel_safe_dataframe(new_documents).to_excel(writer, sheet_name="new_documents", index=False)
        _excel_safe_dataframe(downloads).to_excel(writer, sheet_name="downloads", index=False)
        _excel_safe_dataframe(repo.fetch_documents()).to_excel(writer, sheet_name="inventory_snapshot", index=False)


def create_archive_baseline(
    destination: str | Path,
    repo: RaaDailyRepository,
    start_url: str = PREFECTURE_PORTAL_URL,
    timeout: int = 30,
    discovery_func=discover_prefecture_raa,
    write_excel: bool = True,
) -> Dict:
    repo.ensure_schema()
    total_started = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    run_id = repo.create_run(started_at, "running", regions_scanned=12, months_scanned="archive-all", run_type="archive_baseline")
    timings = {"discovery_seconds": 0.0, "db_seconds": 0.0, "download_seconds": 0.0, "excel_seconds": 0.0, "total_seconds": 0.0}
    try:
        stage_started = time.perf_counter()
        discovery_result = discovery_func(
            start_url,
            timeout=timeout,
            previous_days=0,
            exclude_region_slugs=EXCLUDED_DAILY_REGIONS,
        )
        timings["discovery_seconds"] = _seconds_since(stage_started)
        archive_documents = filter_archive_documents(discovery_result)
        stage_started = time.perf_counter()
        repo.upsert_documents(archive_documents, run_id, started_at)
        timings["db_seconds"] = _seconds_since(stage_started)
        workbook_path = Path(destination) / "prefecture_raa_daily_report.xlsx"
        stage_started = time.perf_counter()
        if write_excel:
            write_daily_excel(workbook_path, repo, [], [])
        timings["excel_seconds"] = _seconds_since(stage_started)
        timings["total_seconds"] = _seconds_since(total_started)
        repo.finish_run(
            run_id,
            datetime.now(timezone.utc),
            "baseline_created",
            pdfs_discovered=len(archive_documents),
            new_pdfs=0,
            downloaded=0,
            failed=0,
            timings=timings,
        )
        return {
            "run_id": run_id,
            "status": "baseline_created",
            "baseline_exists": False,
            "months_scanned": "archive-all",
            "regions_scanned": 12,
            "pdfs_discovered": len(archive_documents),
            "already_known": 0,
            "new_pdfs": 0,
            "daily_by_region": [],
            "new_documents": [],
            "new_by_region": [],
            "downloads": [],
            "downloaded": 0,
            "failed": 0,
            **timings,
            "excel_path": str(workbook_path.resolve()),
        }
    except Exception as exc:
        repo.finish_run(run_id, datetime.now(timezone.utc), "failed", 0, 0, 0, 0, error=str(exc))
        raise


def run_daily_raa_check(
    destination: str | Path,
    repo: RaaDailyRepository,
    start_url: str = PREFECTURE_PORTAL_URL,
    timeout: int = 30,
    today: Optional[date] = None,
    discovery_func=discover_prefecture_raa,
    write_excel: bool = True,
) -> Dict:
    repo.ensure_schema()
    total_started = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    month_scope = current_and_previous_months(today)
    months_scanned = ", ".join(item[2] for item in month_scope)
    had_baseline = repo.has_completed_archive_baseline()
    if not had_baseline:
        return create_archive_baseline(
            destination=destination,
            repo=repo,
            start_url=start_url,
            timeout=timeout,
            discovery_func=discovery_func,
            write_excel=write_excel,
        )
    run_id = repo.create_run(started_at, "running", regions_scanned=12, months_scanned=months_scanned, run_type="daily_diff")
    downloads: List[Dict] = []
    error = ""
    timings = {"discovery_seconds": 0.0, "db_seconds": 0.0, "download_seconds": 0.0, "excel_seconds": 0.0, "total_seconds": 0.0}
    try:
        stage_started = time.perf_counter()
        discovery_result = discovery_func(
            start_url,
            timeout=timeout,
            extraction_page_limit=80,
            previous_days=0,
            exclude_region_slugs=EXCLUDED_DAILY_REGIONS,
            target_years=target_years_for_daily(today),
            target_months=[month for month, _, _ in month_scope],
        )
        timings["discovery_seconds"] = _seconds_since(stage_started)
        daily_documents = filter_daily_documents(discovery_result, month_scope)
        normalized_urls = [_normalize_url(str(document.get("url") or "")) for document in daily_documents]
        stage_started = time.perf_counter()
        known_urls = repo.get_known_urls(normalized_urls) if had_baseline else set()
        new_documents = [
            document
            for document in daily_documents
            if _normalize_url(str(document.get("url") or "")) not in known_urls
        ]
        daily_by_region = summarize_daily_scope_by_region(daily_documents, known_urls)
        document_ids_by_url = repo.upsert_documents(daily_documents, run_id, started_at)
        timings["db_seconds"] = _seconds_since(stage_started)

        summary = summarize_downloads(downloads, len(new_documents))
        status = "completed"
        workbook_path = Path(destination) / "prefecture_raa_daily_report.xlsx"
        stage_started = time.perf_counter()
        if write_excel:
            write_daily_excel(workbook_path, repo, new_documents if had_baseline else [], downloads)
        timings["excel_seconds"] = _seconds_since(stage_started)
        timings["total_seconds"] = _seconds_since(total_started)
        repo.finish_run(
            run_id,
            datetime.now(timezone.utc),
            status,
            pdfs_discovered=len(daily_documents),
            new_pdfs=len(new_documents),
            downloaded=summary["documents_downloaded_successfully"],
            failed=summary["failed_downloads"],
            timings=timings,
        )

        return {
            "run_id": run_id,
            "status": status,
            "baseline_exists": had_baseline,
            "months_scanned": months_scanned,
            "regions_scanned": 12,
            "pdfs_discovered": len(daily_documents),
            "already_known": len(daily_documents) - len(new_documents),
            "new_pdfs": len(new_documents),
            "daily_by_region": daily_by_region,
            "new_documents": new_documents,
            "new_by_region": summarize_documents_by_region(new_documents),
            "downloads": downloads,
            "downloaded": summary["documents_downloaded_successfully"],
            "failed": summary["failed_downloads"],
            **timings,
            "excel_path": str(workbook_path.resolve()),
        }
    except Exception as exc:
        error = str(exc)
        repo.finish_run(run_id, datetime.now(timezone.utc), "failed", 0, 0, 0, 0, error=error)
        raise


def download_daily_raa_diff(
    destination: str | Path,
    repo: RaaDailyRepository,
    run_id: int,
    documents: Optional[Sequence[Dict]] = None,
    timeout: int = 30,
    download_func=_download_prefecture_documents,
    write_excel: bool = True,
) -> Dict:
    repo.ensure_schema()
    total_started = time.perf_counter()
    timings = {"db_seconds": 0.0, "download_seconds": 0.0, "excel_seconds": 0.0, "total_seconds": 0.0}
    stage_started = time.perf_counter()
    documents_to_download = list(documents) if documents is not None else repo.fetch_new_documents_for_run(run_id)
    seen_at = datetime.now(timezone.utc)
    document_ids_by_url = repo.upsert_documents(documents_to_download, run_id, seen_at)
    timings["db_seconds"] = _seconds_since(stage_started)
    stage_started = time.perf_counter()
    downloads = download_func(documents_to_download, destination, timeout=timeout) if documents_to_download else []
    timings["download_seconds"] = _seconds_since(stage_started)
    if documents_to_download:
        stage_started = time.perf_counter()
        repo.record_downloads(run_id, document_ids_by_url, documents_to_download, downloads)
        timings["db_seconds"] = round(timings["db_seconds"] + _seconds_since(stage_started), 2)
    summary = summarize_downloads(downloads, len(documents_to_download))
    workbook_path = Path(destination) / "prefecture_raa_daily_report.xlsx"
    stage_started = time.perf_counter()
    if write_excel:
        write_daily_excel(workbook_path, repo, documents_to_download, downloads)
    timings["excel_seconds"] = _seconds_since(stage_started)
    timings["total_seconds"] = _seconds_since(total_started)
    repo.update_run_download_summary(
        run_id,
        summary["documents_downloaded_successfully"],
        summary["failed_downloads"],
        timings=timings,
    )
    return {
        "run_id": run_id,
        "documents_selected_for_download": len(documents_to_download),
        "downloads": downloads,
        "downloaded": summary["documents_downloaded_successfully"],
        "failed": summary["failed_downloads"],
        **timings,
        "excel_path": str(workbook_path.resolve()),
    }

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, Iterable, List, Optional, Sequence
from urllib.parse import unquote, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

from pdf_downloader import collect_pdf_links, dedupe_links, download_document_files, same_path_family, summarize_downloads


PREFECTURE_PORTAL_URL = "https://www.prefectures-regions.gouv.fr/"

REGION_EXCLUDE_SEGMENTS = {
    "",
    "contact",
    "informations",
    "le-savez-vous",
    "outils",
}

DOCUMENTS_PATTERNS = (
    "/documents-publications",
    "/documents-publications/",
    "/documents-publications?",
    "/tags/view/",
)

RaaPhrase = "recueil des actes administratifs"
RAA_TEXT_HINTS = (
    "recueil des actes administratifs",
    "recueils des actes administratifs",
    "recueil regional des actes administratifs",
    "recueils regionaux des actes administratifs",
)
RAA_PATH_HINTS = (
    "recueil-des-actes-administratifs",
    "recueils-des-actes-administratifs",
    "recueil-regional-des-actes-administratifs",
    "recueils-regionaux-des-actes-administratifs",
    "recueil+des+actes+administratifs",
    "recueils+des+actes+administratifs",
    "recueil+regional+des+actes+administratifs",
)
NOISE_PATH_HINTS = (
    "/outils/",
    "/glossaire",
    "/faq",
    "/contact",
    "/mentions-legales",
)
NON_HTML_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".zip",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
)
MONTH_HINTS = (
    "janvier",
    "fevrier",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "aout",
    "septembre",
    "octobre",
    "novembre",
    "decembre",
)
MONTH_LABELS = {
    "janvier": "January",
    "fevrier": "February",
    "mars": "March",
    "avril": "April",
    "mai": "May",
    "juin": "June",
    "juillet": "July",
    "aout": "August",
    "septembre": "September",
    "octobre": "October",
    "novembre": "November",
    "decembre": "December",
}
MONTH_NUMBERS = {
    "janvier": 1,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
}


def _normalize_month_scope(target_months: Optional[Iterable[str]] = None) -> set[str]:
    months = set()
    for month in target_months or []:
        normalized = _normalize_text(str(month))
        if not normalized:
            continue
        months.add(normalized)
        for french_hint, english_label in MONTH_LABELS.items():
            if normalized == _normalize_text(english_label):
                months.add(french_hint)
    return months


PAGINATION_HINTS = ("suivant", "precedent", "page suivante", "page precedente")
REGION_DISCOVERY_WORKERS = 4
PDF_EXTRACTION_WORKERS = 2
RAA_EXPANSION_MAX_DEPTH = 3
RAA_MAX_SCANNED_PAGES_PER_ENTRY = 80
RAA_MAX_EXTRACTION_PAGES_PER_REGION = 120
RAA_MAX_TRUSTED_EXTERNAL_ROOTS = 3
BLOCKED_EXTERNAL_NETLOC_HINTS = (
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
)
_BROWSER_FETCH_CACHE: Dict[str, str] = {}
_BROWSER_FETCH_LOCK = Lock()
_BROWSER_FALLBACK_DISABLED = False


def _prefecture_verify_ssl() -> bool:
    value = os.getenv("PREFECTURE_RAA_VERIFY_SSL", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _new_http_session() -> requests.Session:
    session = requests.Session()
    if not _prefecture_verify_ssl():
        session.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_text.lower().split())


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        fragment="",
    )
    path = normalized.path.rstrip("/") or "/"
    return normalized._replace(path=path).geturl()


def _same_domain(url: str, base: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(base).netloc.lower()


def _is_french_government_host(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return netloc == "gouv.fr" or netloc.endswith(".gouv.fr")


def _is_prefectures_regions_host(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return netloc == "www.prefectures-regions.gouv.fr" or netloc == "prefectures-regions.gouv.fr"


def _is_blocked_external_host(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return any(hint == netloc or netloc.endswith(f".{hint}") for hint in BLOCKED_EXTERNAL_NETLOC_HINTS)


def _path_text(url: str) -> str:
    parsed = urlparse(url)
    combined = f"{parsed.path} {parsed.query}"
    return _normalize_text(unquote(combined).replace("/", " ").replace("-", " ").replace("_", " ").replace("+", " "))


def _is_non_http_href(raw_href: str) -> bool:
    href = (raw_href or "").strip().lower()
    return href.startswith(("mailto:", "javascript:", "tel:", "#"))


def _looks_like_bare_email_href(raw_href: str) -> bool:
    href = (raw_href or "").strip()
    lowered = href.lower()
    if "@" not in href:
        return False
    if lowered.startswith(("http://", "https://", "mailto:")):
        return False
    if href.startswith("/"):
        return False
    return True


def _is_html_navigation_candidate(url: str) -> bool:
    parsed = urlparse(url)
    lowered_path = unquote(parsed.path).lower()
    if any(lowered_path.endswith(ext) for ext in NON_HTML_EXTENSIONS):
        return False
    if any(hint in lowered_path for hint in NOISE_PATH_HINTS):
        return False
    return parsed.scheme in {"http", "https"}


def _absolute_links(page_url: str, html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links: List[Dict[str, str]] = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        raw_href = anchor["href"].strip()
        if _is_non_http_href(raw_href) or _looks_like_bare_email_href(raw_href):
            continue
        absolute = urljoin(page_url, raw_href)
        normalized = _normalize_url(absolute)
        if urlparse(normalized).scheme not in {"http", "https"}:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(
            {
                "url": normalized,
                "text": " ".join(anchor.get_text(" ", strip=True).split()),
            }
        )
    return links


def _absolute_links_with_heading_context(page_url: str, html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links: List[Dict[str, str]] = []
    current_year = ""
    current_month = ""

    def _nearest_previous_block_text(anchor) -> str:
        previous = anchor.find_previous(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li"])
        if previous is None:
            return ""
        return " ".join(previous.get_text(" ", strip=True).split())

    for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "a"]):
        element_text = " ".join(element.get_text(" ", strip=True).split())
        if element.name != "a":
            heading_year = _extract_year(element_text)
            heading_month = _extract_month(element_text)
            if heading_year:
                current_year = heading_year
            if heading_month:
                current_month = element_text
            continue
        if not element.has_attr("href"):
            continue
        raw_href = element["href"].strip()
        if _is_non_http_href(raw_href) or _looks_like_bare_email_href(raw_href):
            continue
        absolute = urljoin(page_url, raw_href)
        normalized = _normalize_url(absolute)
        if urlparse(normalized).scheme not in {"http", "https"}:
            continue
        attribute_context = " ".join(
            str(element.get(attribute, "") or "").strip()
            for attribute in ("title", "aria-label")
            if element.get(attribute)
        )
        link_text = " ".join(
            value for value in (element_text, attribute_context) if value
        )
        heading_context = " ".join(
            value
            for value in (current_year, current_month, _nearest_previous_block_text(element))
            if value
        )
        links.append(
            {
                "url": normalized,
                "text": link_text,
                "heading_context": heading_context,
            }
        )
    return links


def _source_page_date_context(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    contexts: List[str] = []
    for element in soup.find_all(class_=lambda value: value and "page-title-maj" in " ".join(value if isinstance(value, list) else [value])):
        text = " ".join(element.get_text(" ", strip=True).split())
        if text:
            contexts.append(text)
    for element in soup.find_all("time"):
        text = " ".join(element.get_text(" ", strip=True).split())
        datetime_value = str(element.get("datetime", "") or "").strip()
        combined = " ".join(value for value in (text, datetime_value) if value)
        if combined:
            contexts.append(combined)
    for meta_name in ("article:published_time", "article:modified_time", "date", "dc.date", "dcterms.date"):
        for meta in soup.find_all("meta", attrs={"property": meta_name}) + soup.find_all("meta", attrs={"name": meta_name}):
            content = str(meta.get("content", "") or "").strip()
            if content:
                contexts.append(content)
    return " ".join(dict.fromkeys(contexts))


def _fetch_text(
    url: str,
    timeout: int,
    session: requests.Session,
    referer: str = "",
    allow_browser_fallback: bool = False,
) -> str:
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"}
    if referer:
        headers["Referer"] = referer
    try:
        response = session.get(
            url,
            timeout=timeout,
            headers=headers,
        )
        response.raise_for_status()
        return response.text
    except requests.exceptions.SSLError:
        if _is_french_government_host(url) and not _is_blocked_external_host(url):
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            response = session.get(
                url,
                timeout=timeout,
                headers=headers,
                verify=False,
            )
            response.raise_for_status()
            return response.text
        raise
    except requests.RequestException:
        if not allow_browser_fallback or not _is_french_government_host(url) or _is_blocked_external_host(url):
            raise
        return _fetch_text_with_browser(url, timeout, referer=referer)


def _fetch_text_with_browser(url: str, timeout: int, referer: str = "") -> str:
    global _BROWSER_FALLBACK_DISABLED
    if _BROWSER_FALLBACK_DISABLED:
        raise RuntimeError("browser fallback disabled after Playwright subprocess startup failed")
    normalized = _normalize_url(url)
    with _BROWSER_FETCH_LOCK:
        if normalized in _BROWSER_FETCH_CACHE:
            return _BROWSER_FETCH_CACHE[normalized]
        try:
            _ensure_windows_subprocess_event_loop_policy()
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError(f"browser fallback unavailable for {url}: {exc}") from exc

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page(
                        extra_http_headers={
                            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                        }
                    )
                    referer = referer or PREFECTURE_PORTAL_URL
                    warmed_up = False
                    if referer and not _same_domain(url, referer):
                        page.goto(referer, wait_until="domcontentloaded", timeout=max(1000, int(timeout) * 1000))
                        warmed_up = True
                    if warmed_up:
                        page.goto(url, wait_until="domcontentloaded", timeout=max(1000, int(timeout) * 1000))
                    else:
                        page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=max(1000, int(timeout) * 1000),
                            referer=referer,
                        )
                    html = page.content()
                finally:
                    browser.close()
        except (NotImplementedError, OSError, PermissionError) as exc:
            _BROWSER_FALLBACK_DISABLED = True
            raise RuntimeError(
                "browser fallback unavailable because the active Windows asyncio event loop "
                "does not support subprocesses or subprocess startup was blocked"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"browser fallback failed for {url}: {exc}") from exc
        _BROWSER_FETCH_CACHE[normalized] = html
        return html


def _ensure_windows_subprocess_event_loop_policy() -> None:
    if sys.platform != "win32":
        return
    proactor_policy = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if proactor_policy is None:
        return
    if not isinstance(asyncio.get_event_loop_policy(), proactor_policy):
        asyncio.set_event_loop_policy(proactor_policy())


def _region_slug(url: str, portal_url: str) -> str:
    parsed = urlparse(url)
    portal = urlparse(portal_url)
    if parsed.netloc.lower() != portal.netloc.lower():
        return ""
    segments = [segment for segment in parsed.path.split("/") if segment]
    return segments[0].lower() if segments else ""


def _extract_year(*values: str) -> Optional[str]:
    for value in values:
        if not value:
            continue
        normalized = unquote(value)
        for year in range(2035, 2009, -1):
            if str(year) in normalized:
                return str(year)
    return None


def _extract_month(*values: str) -> str:
    for value in values:
        normalized = _normalize_text(unquote(value or ""))
        for month in MONTH_HINTS:
            if month in normalized:
                return MONTH_LABELS.get(month, month.title())
    return ""


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_exact_publication_date(value: str) -> Optional[date]:
    normalized = _normalize_text(unquote(value or ""))
    if not normalized:
        return None

    compact_year_first = re.search(
        r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)",
        normalized,
    )
    if compact_year_first:
        parsed = _safe_date(
            int(compact_year_first.group(1)),
            int(compact_year_first.group(2)),
            int(compact_year_first.group(3)),
        )
        if parsed:
            return parsed

    separated_numeric = re.search(
        r"(?<!\d)(0?[1-9]|[12]\d|3[01])[\s._/-]+(0?[1-9]|1[0-2])[\s._/-]+(20\d{2}|\d{2})(?!\d)",
        normalized,
    )
    if separated_numeric:
        year_text = separated_numeric.group(3)
        year = int(f"20{year_text}") if len(year_text) == 2 else int(year_text)
        parsed = _safe_date(year, int(separated_numeric.group(2)), int(separated_numeric.group(1)))
        if parsed:
            return parsed

    compact_day_first = re.search(
        r"(?<!\d)(0[1-9]|[12]\d|3[01])(0[1-9]|1[0-2])(20\d{2})(?!\d)",
        normalized,
    )
    if compact_day_first:
        parsed = _safe_date(
            int(compact_day_first.group(3)),
            int(compact_day_first.group(2)),
            int(compact_day_first.group(1)),
        )
        if parsed:
            return parsed

    for month_name, month_number in MONTH_NUMBERS.items():
        text_date = re.search(
            rf"(?<!\d)(0?[1-9]|[12]\d|3[01])\s+{re.escape(month_name)}\s+(20\d{{2}})(?!\d)",
            normalized,
        )
        if text_date:
            parsed = _safe_date(int(text_date.group(2)), month_number, int(text_date.group(1)))
            if parsed:
                return parsed

    return None


def _extract_publication_date_with_source(*source_values: tuple[str, str]) -> tuple[str, str]:
    for source, value in source_values:
        parsed = _parse_exact_publication_date(value)
        if parsed:
            return parsed.isoformat(), source
    return "", "unknown"


def _date_window_bounds(previous_days: Optional[int], today: Optional[date] = None) -> tuple[Optional[date], Optional[date]]:
    if previous_days is None or int(previous_days) <= 0:
        return None, None
    end_date = today or date.today()
    start_date = end_date - timedelta(days=max(0, int(previous_days) - 1))
    return start_date, end_date


def _is_publication_date_in_window(publication_date: str, previous_days: Optional[int], today: Optional[date] = None) -> bool:
    start_date, end_date = _date_window_bounds(previous_days, today)
    if not start_date or not end_date:
        return True
    if not publication_date:
        return False
    try:
        parsed = datetime.strptime(publication_date, "%Y-%m-%d").date()
    except ValueError:
        return False
    return start_date <= parsed <= end_date


def _is_region_url(url: str, portal_url: str) -> bool:
    parsed = urlparse(url)
    portal = urlparse(portal_url)
    if parsed.netloc.lower() != portal.netloc.lower():
        return False
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 1:
        return False
    return segments[0].lower() not in REGION_EXCLUDE_SEGMENTS


def _looks_like_documents_publications_url(url: str) -> bool:
    parsed = urlparse(url)
    lowered = unquote(parsed.path).lower()
    normalized_text = _path_text(url)
    return (
        "documents publications" in normalized_text
        or "documents-publications" in lowered
        or ("tags/view" in lowered and "documents et publications" in normalized_text)
    )


def _combined_raa_text(url: str, text: str = "") -> str:
    return f"{_normalize_text(text)} {_path_text(url)}".strip()


def _has_raa_context(url: str, text: str = "") -> bool:
    combined = _combined_raa_text(url, text)
    lowered_path = unquote(urlparse(url).path).lower()
    if any(hint in lowered_path for hint in RAA_PATH_HINTS):
        return True
    if any(hint in combined for hint in RAA_TEXT_HINTS):
        return True
    tokens = set(combined.replace(".", " ").split())
    has_raa_token = "raa" in tokens
    has_publication_context = "documents publications" in combined or "/documents-publications" in lowered_path
    has_recueil_context = "recueil" in combined or "actes administratifs" in combined
    return has_raa_token and (has_publication_context or has_recueil_context)


def _is_raa_exact_match(url: str, text: str = "") -> bool:
    return _has_raa_context(url, text)


def _is_raa_branch_url(url: str) -> bool:
    return _has_raa_context(url)


def _is_raa_listing_like(url: str, text: str = "") -> bool:
    combined = _combined_raa_text(url, text)
    if "dossiers" in combined and _has_raa_context(url, text):
        return True
    return _has_raa_context(url, text)


def _is_pagination_link(url: str, text: str) -> bool:
    normalized_text = _normalize_text(text)
    lowered_path = unquote(urlparse(url).path).lower()
    return "/(offset)/" in lowered_path or any(hint in normalized_text for hint in PAGINATION_HINTS)


def _is_year_or_month_page(url: str, text: str = "") -> bool:
    combined = f"{_normalize_text(text)} {_path_text(url)}".strip()
    if any(month in combined for month in MONTH_HINTS):
        return True
    return any(str(year) in combined for year in range(2010, 2036))


def _matches_discovery_scope(
    url: str,
    text: str = "",
    target_years: Optional[Iterable[str]] = None,
    target_months: Optional[Iterable[str]] = None,
) -> bool:
    years = {str(year) for year in (target_years or [])}
    months = _normalize_month_scope(target_months)
    if not years and not months:
        return True
    combined = f"{_normalize_text(text)} {_path_text(url)}".strip()
    years_in_value = {str(year) for year in range(2010, 2036) if str(year) in combined}
    months_in_value = {month for month in MONTH_HINTS if month in combined}
    if years_in_value and years and not years_in_value.intersection(years):
        return False
    if months_in_value and months and not months_in_value.intersection(months):
        return False
    if years_in_value and years_in_value.intersection(years):
        return True
    if months_in_value and months_in_value.intersection(months):
        return True
    return not years_in_value and not months_in_value


def _is_same_region(url: str, region_slug: str) -> bool:
    lowered_path = unquote(urlparse(url).path).lower()
    return bool(region_slug) and lowered_path.startswith(f"/{region_slug}/")


def _is_trusted_external_raa_branch_candidate(
    url: str,
    text: str,
    source_url: str,
    source_is_raa_branch: bool = False,
) -> bool:
    if _same_domain(url, source_url):
        return False
    if not _is_french_government_host(url) or _is_blocked_external_host(url):
        return False
    if not _is_html_navigation_candidate(url):
        return False
    if not source_is_raa_branch and not _is_raa_listing_like(source_url):
        return False
    return _has_raa_context(url, text)


def _is_within_trusted_external_branch(url: str, trusted_external_roots: Iterable[str]) -> bool:
    return any(same_path_family(root, url) for root in trusted_external_roots)


def _is_trusted_external_source(url: str, trusted_external_roots: Iterable[str]) -> bool:
    return _is_within_trusted_external_branch(url, trusted_external_roots)


def _should_register_trusted_external_root(
    url: str,
    text: str,
    source_url: str,
    branch_root: str,
    trusted_external_roots: Iterable[str],
) -> bool:
    if _same_domain(url, branch_root):
        return False
    if _is_within_trusted_external_branch(url, trusted_external_roots):
        return False
    source_is_raa_branch = (
        _normalize_url(source_url) == _normalize_url(branch_root)
        or _is_raa_listing_like(source_url)
        or _is_trusted_external_source(source_url, trusted_external_roots)
    )
    if _is_trusted_external_raa_branch_candidate(url, text, source_url, source_is_raa_branch):
        return True
    if _is_trusted_external_source(source_url, trusted_external_roots) and _same_domain(url, source_url):
        return _has_raa_context(url, text)
    return False


def discover_region_pages(
    portal_url: str = PREFECTURE_PORTAL_URL,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
) -> List[str]:
    http = session or _new_http_session()
    html = _fetch_text(portal_url, timeout, http)
    regions = [
        link["url"]
        for link in _absolute_links(portal_url, html)
        if _is_region_url(link["url"], portal_url)
    ]
    return list(dict.fromkeys(regions))


def _documents_publications_candidates(region_url: str) -> List[str]:
    normalized_region = _normalize_url(region_url)
    parsed = urlparse(normalized_region)
    base_path = parsed.path.rstrip("/")
    if not base_path:
        return []
    candidates = []
    for suffix in ("Documents-publications", "DOCUMENTS-PUBLICATIONS"):
        candidates.append(
            parsed._replace(path=f"{base_path}/{suffix}", query="", fragment="").geturl()
        )
    return candidates


def find_documents_publications_url(
    region_url: str,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    if _looks_like_documents_publications_url(region_url):
        return _normalize_url(region_url)
    http = session or _new_http_session()
    html = _fetch_text(region_url, timeout, http)
    region_slug = _region_slug(region_url, PREFECTURE_PORTAL_URL)
    scored_candidates: List[tuple[int, str]] = []
    for link in _absolute_links(region_url, html):
        if not _same_domain(link["url"], region_url):
            continue
        if region_slug and not _is_same_region(link["url"], region_slug):
            continue
        if not _is_html_navigation_candidate(link["url"]):
            continue
        lowered_path = unquote(urlparse(link["url"]).path).lower()
        normalized_text = _normalize_text(link["text"])
        if "documents publications" in normalized_text or _looks_like_documents_publications_url(link["url"]):
            score = 0
            if "documents-publications" in lowered_path:
                score += 100
            if lowered_path.endswith("/documents-publications") or lowered_path.endswith("/documents-publications/"):
                score += 50
            if "documents publications" in normalized_text:
                score += 20
            if "/tags/view/" in lowered_path:
                score -= 25
            scored_candidates.append((score, link["url"]))
    if scored_candidates:
        scored_candidates.sort(key=lambda item: (-item[0], item[1]))
        return scored_candidates[0][1]
    candidates = _documents_publications_candidates(region_url)
    return candidates[0] if candidates else None


def find_raa_entry_urls(
    documents_url: str,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
) -> List[str]:
    http = session or _new_http_session()
    html = _fetch_text(documents_url, timeout, http)
    region_slug = _region_slug(documents_url, PREFECTURE_PORTAL_URL)
    entries: List[str] = []
    for link in _absolute_links(documents_url, html):
        if _same_domain(link["url"], documents_url):
            if region_slug and not _is_same_region(link["url"], region_slug):
                continue
        elif not _is_trusted_external_raa_branch_candidate(
            link["url"],
            link["text"],
            documents_url,
            source_is_raa_branch=True,
        ):
            continue
        if not _is_html_navigation_candidate(link["url"]):
            continue
        if _is_raa_exact_match(link["url"], link["text"]):
            entries.append(link["url"])
    return list(dict.fromkeys(entries))


def _should_follow_raa_child(
    url: str,
    text: str,
    region_slug: str,
    branch_root: str,
    source_url: str,
    trusted_external_roots: Iterable[str],
    target_years: Optional[Iterable[str]] = None,
    target_months: Optional[Iterable[str]] = None,
) -> bool:
    if not _is_html_navigation_candidate(url):
        return False
    if not _matches_discovery_scope(url, text, target_years, target_months):
        return False

    source_is_raa_branch = (
        _normalize_url(source_url) == _normalize_url(branch_root)
        or _is_raa_listing_like(source_url)
        or _is_trusted_external_source(source_url, trusted_external_roots)
    )
    if _is_trusted_external_raa_branch_candidate(url, text, source_url, source_is_raa_branch):
        return True

    lowered_path = unquote(urlparse(url).path).lower()

    if _same_domain(url, branch_root):
        if not _is_prefectures_regions_host(branch_root):
            if same_path_family(branch_root, url):
                return (
                    _has_raa_context(url, text)
                    or _is_year_or_month_page(url, text)
                    or _is_pagination_link(url, text)
                )
            return _has_raa_context(url, text) and (_is_year_or_month_page(url, text) or _is_pagination_link(url, text))

        if region_slug and not _is_same_region(url, region_slug):
            return False
        if "/documents-publications/" not in lowered_path and "/tags/view/" not in lowered_path:
            return False
        if _is_raa_branch_url(url):
            return True
        if same_path_family(branch_root, url):
            return _is_year_or_month_page(url, text) or _is_pagination_link(url, text)
        if "/documents-publications/" in lowered_path and _has_raa_context(url, text):
            return _is_year_or_month_page(url, text)
        return False

    if _is_within_trusted_external_branch(url, trusted_external_roots):
        if _has_raa_context(url, text):
            return True
        return _is_year_or_month_page(url, text) or _is_pagination_link(url, text)

    if _is_trusted_external_source(source_url, trusted_external_roots) and _same_domain(url, source_url):
        return _has_raa_context(url, text)

    return False


def expand_raa_pages(
    raa_entry_url: str,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
    max_depth: int = RAA_EXPANSION_MAX_DEPTH,
    include_scanned_pages: bool = False,
    include_errors: bool = False,
    region_slug_override: str = "",
    max_scanned_pages: int = RAA_MAX_SCANNED_PAGES_PER_ENTRY,
    max_trusted_external_roots: int = RAA_MAX_TRUSTED_EXTERNAL_ROOTS,
    target_years: Optional[Iterable[str]] = None,
    target_months: Optional[Iterable[str]] = None,
) -> List[str] | tuple[List[str], List[str]]:
    http = session or _new_http_session()
    region_slug = region_slug_override or _region_slug(raa_entry_url, PREFECTURE_PORTAL_URL)
    queue: List[tuple[str, int, str]] = [(_normalize_url(raa_entry_url), 0, "")]
    queued = {_normalize_url(raa_entry_url)}
    visited = set()
    trusted_external_roots: List[str] = []
    found: List[str] = []
    scanned_pages: List[str] = []
    errors: List[str] = []

    while queue:
        if max_scanned_pages and len(scanned_pages) >= max_scanned_pages:
            errors.append(f"raa_scan_cap_reached:{raa_entry_url}:{max_scanned_pages}")
            break
        current_url, depth, referer = queue.pop(0)
        if current_url in visited:
            continue
        visited.add(current_url)
        scanned_pages.append(current_url)

        try:
            html = _fetch_text(current_url, timeout, http, referer=referer)
        except Exception as exc:
            errors.append(f"raa_fetch_failed:{current_url}:{exc}")
            continue

        if _matches_discovery_scope(current_url, "", target_years, target_months) and _is_raa_listing_like(current_url):
            found.append(current_url)

        for link in _absolute_links(current_url, html):
            link_url = link["url"]
            if not _should_follow_raa_child(
                link_url,
                link["text"],
                region_slug,
                raa_entry_url,
                current_url,
                trusted_external_roots,
                target_years,
                target_months,
            ):
                continue
            if _should_register_trusted_external_root(
                link_url,
                link["text"],
                current_url,
                raa_entry_url,
                trusted_external_roots,
            ):
                if len(trusted_external_roots) < max_trusted_external_roots:
                    trusted_external_roots.append(link_url)
                else:
                    errors.append(f"trusted_external_root_cap_reached:{link_url}")
                    continue
            if (
                _matches_discovery_scope(link_url, link["text"], target_years, target_months)
                and (_is_raa_listing_like(link_url, link["text"]) or _is_year_or_month_page(link_url, link["text"]))
            ):
                found.append(link_url)
            normalized_link = _normalize_url(link_url)
            if depth < max_depth and normalized_link not in visited and normalized_link not in queued:
                queued.add(normalized_link)
                queue.append((link_url, depth + 1, current_url))

    unique_found = list(dict.fromkeys(found))
    unique_scanned = list(dict.fromkeys(scanned_pages))
    unique_errors = list(dict.fromkeys(errors))
    if include_errors:
        return unique_found, unique_scanned, unique_errors
    if include_scanned_pages:
        return unique_found, unique_scanned
    return unique_found


def extract_pdf_links_from_raa_pages(
    raa_pages: Sequence[str],
    timeout: int = 30,
    session: Optional[requests.Session] = None,
    region_slug: str = "",
    max_workers: int = PDF_EXTRACTION_WORKERS,
    previous_days: Optional[int] = None,
    today: Optional[date] = None,
) -> Dict[str, object]:
    seen_pdf_urls = set()
    duplicate_count = 0
    pdf_links: List[str] = []
    documents: List[Dict[str, str]] = []
    successful_pages: List[str] = []
    errors: List[str] = []

    def _extract_from_page(raa_page: str) -> Dict[str, object]:
        http = session or _new_http_session()
        page_pdf_links: List[str] = []
        page_documents: List[Dict[str, str]] = []
        try:
            html = _fetch_text(raa_page, timeout, http)
        except Exception as exc:
            return {"page": raa_page, "pdf_links": [], "documents": [], "error": f"raa_fetch_failed:{raa_page}:{exc}"}

        source_page_context = _source_page_date_context(html)
        page_links = _absolute_links_with_heading_context(raa_page, html)
        link_context_by_pdf_url: Dict[str, Dict[str, str]] = {}
        for link in page_links:
            for pdf_candidate in collect_pdf_links(raa_page, [link["url"]]):
                candidate_context = {"text": link["text"], "heading_context": link.get("heading_context", "")}
                existing_context = link_context_by_pdf_url.get(pdf_candidate)
                if existing_context is None:
                    link_context_by_pdf_url[pdf_candidate] = candidate_context
                    continue
                existing_date, _ = _extract_publication_date_with_source(
                    ("link_text", existing_context.get("text", "")),
                    ("heading_context", existing_context.get("heading_context", "")),
                )
                candidate_date, _ = _extract_publication_date_with_source(
                    ("link_text", candidate_context.get("text", "")),
                    ("heading_context", candidate_context.get("heading_context", "")),
                )
                if candidate_date and not existing_date:
                    link_context_by_pdf_url[pdf_candidate] = candidate_context
        discovered_pdf_links = list(link_context_by_pdf_url)
        for pdf_link in discovered_pdf_links:
            page_pdf_links.append(pdf_link)
            pdf_filename = Path(unquote(urlparse(pdf_link).path)).name
            link_context = link_context_by_pdf_url.get(pdf_link, {})
            link_text = link_context.get("text", "")
            heading_context = link_context.get("heading_context", "")
            year = _extract_year(raa_page, heading_context, link_text, pdf_filename)
            publication_date, date_source = _extract_publication_date_with_source(
                ("link_text", link_text),
                ("heading_context", heading_context),
                ("filename", pdf_filename),
                ("pdf_url", pdf_link),
                ("source_page", source_page_context),
                ("source_page", raa_page),
            )
            page_documents.append(
                {
                    "url": pdf_link,
                    "title": pdf_filename,
                    "file_format": "PDF",
                    "source": "prefecture_raa",
                    "source_page": raa_page,
                    "region": region_slug or _region_slug(raa_page, PREFECTURE_PORTAL_URL),
                    "year": year or "unknown-year",
                    "month": _extract_month(raa_page, heading_context, link_text, pdf_filename),
                    "publication_date": publication_date,
                    "date_source": date_source,
                    "is_within_date_window": _is_publication_date_in_window(publication_date, previous_days, today),
                }
            )
        return {"page": raa_page, "pdf_links": page_pdf_links, "documents": page_documents, "error": ""}

    if session is None and max_workers > 1 and len(raa_pages) > 1:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(raa_pages))) as executor:
            page_results = list(executor.map(_extract_from_page, raa_pages))
    else:
        page_results = [_extract_from_page(raa_page) for raa_page in raa_pages]

    for page_result in page_results:
        error = str(page_result.get("error") or "")
        if error:
            errors.append(error)
            continue
        page_pdf_links = list(page_result.get("pdf_links") or [])
        page_documents = list(page_result.get("documents") or [])
        if page_pdf_links:
            successful_pages.append(str(page_result.get("page") or ""))
        for pdf_link, document in zip(page_pdf_links, page_documents):
            if pdf_link in seen_pdf_urls:
                duplicate_count += 1
                continue
            seen_pdf_urls.add(pdf_link)
            pdf_links.append(pdf_link)
            documents.append(document)

    return {
        "raa_pages_with_pdfs": successful_pages,
        "pdf_links": pdf_links,
        "documents": documents,
        "duplicate_pdf_links": duplicate_count,
        "errors": errors,
    }


def _rank_raa_page_for_extraction(url: str) -> tuple[int, str]:
    path_text = _path_text(url)
    score = 0
    year = _extract_year(url)
    if year:
        score += 100 + max(0, int(year) - 2010)
    if any(month in path_text for month in MONTH_HINTS):
        score += 25
    if _has_raa_context(url):
        score += 20
    if _is_pagination_link(url, ""):
        score -= 25
    return (-score, url)


def _select_raa_pages_for_extraction(
    pages: Sequence[str],
    max_pages: int = RAA_MAX_EXTRACTION_PAGES_PER_REGION,
) -> List[str]:
    unique_pages = list(dict.fromkeys(pages))
    if not max_pages or len(unique_pages) <= max_pages:
        return unique_pages
    return sorted(unique_pages, key=_rank_raa_page_for_extraction)[:max_pages]


def discover_region_raa_pdfs(
    region_url: str,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
    documents_url: Optional[str] = None,
    parallel_pdf_extraction: bool = False,
    extraction_page_limit: int = RAA_MAX_EXTRACTION_PAGES_PER_REGION,
    previous_days: Optional[int] = None,
    today: Optional[date] = None,
    target_years: Optional[Iterable[str]] = None,
    target_months: Optional[Iterable[str]] = None,
) -> Dict:
    started_at = time.perf_counter()
    http = session or _new_http_session()
    resolved_documents_url = documents_url or find_documents_publications_url(region_url, timeout=timeout, session=http)
    result = {
        "region_url": region_url,
        "region": _region_slug(region_url, PREFECTURE_PORTAL_URL) or "unknown-region",
        "documents_publications_url": resolved_documents_url,
        "raa_entry_urls": [],
        "raa_pages": [],
        "pdf_links": [],
        "documents": [],
        "available_years": [],
        "has_raa_pdfs": False,
        "pdfs_in_date_window": 0,
        "unknown_date_pdfs": 0,
        "pages_scanned": [],
        "raa_pages_considered": 0,
        "raa_pages_extracted": 0,
        "elapsed_s": 0.0,
        "errors": [],
    }
    if not resolved_documents_url:
        result["errors"].append("documents_publications_not_found")
        result["elapsed_s"] = round(time.perf_counter() - started_at, 2)
        return result

    try:
        raa_entry_urls = find_raa_entry_urls(resolved_documents_url, timeout=timeout, session=http)
    except Exception as exc:
        result["errors"].append(f"raa_entry_discovery_failed:{exc}")
        result["elapsed_s"] = round(time.perf_counter() - started_at, 2)
        return result

    if _has_raa_context(resolved_documents_url):
        raa_entry_urls = list(dict.fromkeys([resolved_documents_url] + raa_entry_urls))
    result["raa_entry_urls"] = raa_entry_urls
    if not raa_entry_urls:
        result["errors"].append("raa_entry_not_found")
        result["elapsed_s"] = round(time.perf_counter() - started_at, 2)
        return result

    expanded_pages: List[str] = []
    for entry_url in raa_entry_urls:
        raa_pages, scanned_pages, expand_errors = expand_raa_pages(
            entry_url,
            timeout=timeout,
            session=http,
            include_errors=True,
            region_slug_override=result["region"],
            target_years=target_years,
            target_months=target_months,
        )
        result["errors"].extend(expand_errors)
        for page in scanned_pages:
            if page not in result["pages_scanned"]:
                result["pages_scanned"].append(page)
        for page in raa_pages:
            if page not in expanded_pages:
                expanded_pages.append(page)

    result["raa_pages_considered"] = len(expanded_pages)
    extraction_pages = _select_raa_pages_for_extraction(expanded_pages, extraction_page_limit)
    result["raa_pages_extracted"] = len(extraction_pages)
    if len(extraction_pages) < len(expanded_pages):
        result["errors"].append(
            f"raa_extraction_page_cap_applied:{len(extraction_pages)}/{len(expanded_pages)}"
        )

    extraction = extract_pdf_links_from_raa_pages(
        extraction_pages,
        timeout=timeout,
        session=None if parallel_pdf_extraction else http,
        region_slug=result["region"],
        max_workers=PDF_EXTRACTION_WORKERS if parallel_pdf_extraction else 1,
        previous_days=previous_days,
        today=today,
    )
    result["raa_pages"] = extraction["raa_pages_with_pdfs"]
    result["pdf_links"] = extraction["pdf_links"]
    result["documents"] = extraction["documents"]
    result["duplicate_pdf_links"] = extraction["duplicate_pdf_links"]
    result["available_years"] = sorted(
        {str(document.get("year")) for document in extraction["documents"] if document.get("year")},
        reverse=True,
    )
    result["has_raa_pdfs"] = bool(extraction["documents"])
    result["pdfs_in_date_window"] = sum(1 for document in extraction["documents"] if document.get("is_within_date_window"))
    result["unknown_date_pdfs"] = sum(1 for document in extraction["documents"] if not document.get("publication_date"))
    result["errors"].extend(extraction["errors"])
    result["elapsed_s"] = round(time.perf_counter() - started_at, 2)
    return result


def bulk_download_prefecture_raa(
    portal_url: str,
    destination: str | Path,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
    download_session: Optional[requests.Session] = None,
    region_urls: Optional[Sequence[str]] = None,
) -> Dict:
    http = session or _new_http_session()
    regions = list(region_urls) if region_urls is not None else discover_region_pages(
        portal_url=portal_url,
        timeout=timeout,
        session=http,
    )

    region_results = []
    all_pdfs: List[str] = []
    all_documents: List[Dict] = []
    seen_pdfs = set()
    duplicate_count = 0
    raa_pages_seen = set()

    for region_url in regions:
        region_result = discover_region_raa_pdfs(region_url, timeout=timeout, session=http)
        region_results.append(region_result)
        for page_url in region_result["raa_pages"]:
            raa_pages_seen.add(page_url)
        for document in region_result.get("documents", []):
            all_documents.append(document)
        for pdf_url in region_result["pdf_links"]:
            if pdf_url in seen_pdfs:
                duplicate_count += 1
                continue
            seen_pdfs.add(pdf_url)
            all_pdfs.append(pdf_url)
        duplicate_count += region_result.get("duplicate_pdf_links", 0)

    downloads = _download_prefecture_documents(
        all_documents,
        destination,
        timeout=timeout,
        session=download_session or http,
    )
    download_summary = summarize_downloads(downloads, len(all_documents))

    return {
        "portal_url": portal_url,
        "regions": regions,
        "region_results": region_results,
        "pdf_links": all_pdfs,
        "documents": all_documents,
        "downloads": downloads,
        "regions_discovered": len(regions),
        "raa_pages_found": len(raa_pages_seen),
        "pdfs_discovered": len(all_pdfs),
        "pdfs_attempted": download_summary["download_attempts"],
        "pdfs_downloaded_successfully": download_summary["documents_downloaded_successfully"],
        "failed_downloads": download_summary["failed_downloads"],
        "skipped_or_duplicate_downloads": duplicate_count,
    }


def _resolve_documents_urls_from_start(
    start_url: str,
    timeout: int,
    session: requests.Session,
) -> List[Dict[str, str]]:
    normalized = _normalize_url(start_url)
    segments = [segment for segment in urlparse(normalized).path.split("/") if segment]

    if _looks_like_documents_publications_url(normalized):
        region_slug = _region_slug(normalized, PREFECTURE_PORTAL_URL)
        region_root = urlparse(normalized)._replace(path=f"/{region_slug}", query="", fragment="").geturl() if region_slug else normalized
        return [{"region_url": region_root, "documents_url": normalized}]

    if _has_raa_context(normalized) and _is_french_government_host(normalized):
        return [{"region_url": normalized, "documents_url": normalized}]

    if len(segments) == 1:
        documents_url = find_documents_publications_url(normalized, timeout=timeout, session=session)
        return [{"region_url": normalized, "documents_url": documents_url}] if documents_url else []

    regions = discover_region_pages(portal_url=normalized, timeout=timeout, session=session)
    resolved: List[Dict[str, str]] = []
    for region_url in regions:
        documents_url = find_documents_publications_url(region_url, timeout=timeout, session=session)
        if documents_url:
            resolved.append({"region_url": region_url, "documents_url": documents_url})
    return resolved


def discover_prefecture_raa(
    start_url: str = PREFECTURE_PORTAL_URL,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
    progress_callback: Optional[Callable[[Dict], None]] = None,
    extraction_page_limit: int = RAA_MAX_EXTRACTION_PAGES_PER_REGION,
    previous_days: Optional[int] = None,
    today: Optional[date] = None,
    exclude_region_slugs: Optional[Iterable[str]] = None,
    target_years: Optional[Iterable[str]] = None,
    target_months: Optional[Iterable[str]] = None,
) -> Dict:
    http = session or _new_http_session()
    normalized = _normalize_url(start_url)
    region_mappings = _resolve_documents_urls_from_start(normalized, timeout, http)
    excluded_regions = {str(region).lower() for region in (exclude_region_slugs or [])}
    if excluded_regions:
        region_mappings = [
            mapping
            for mapping in region_mappings
            if _region_slug(mapping.get("region_url", ""), PREFECTURE_PORTAL_URL) not in excluded_regions
        ]

    region_results = []
    all_raa_pages: List[str] = []
    all_pdfs: List[str] = []
    all_documents: List[Dict] = []
    pages_scanned: List[str] = []
    seen_pdfs = set()
    duplicate_count = 0

    multiple_regions = len(region_mappings) > 1

    def _discover_mapping(mapping: Dict[str, str]) -> Dict:
        mapping_session = http if session is not None else _new_http_session()
        return discover_region_raa_pdfs(
            mapping["region_url"],
            timeout=timeout,
            session=mapping_session,
            documents_url=mapping.get("documents_url"),
            parallel_pdf_extraction=session is None and not multiple_regions,
            extraction_page_limit=extraction_page_limit,
            previous_days=previous_days,
            today=today,
            target_years=target_years,
            target_months=target_months,
        )

    if session is None and len(region_mappings) > 1:
        with ThreadPoolExecutor(max_workers=min(REGION_DISCOVERY_WORKERS, len(region_mappings))) as executor:
            future_by_index = {
                executor.submit(_discover_mapping, mapping): index
                for index, mapping in enumerate(region_mappings)
            }
            indexed_results: List[tuple[int, Dict]] = []
            for future in as_completed(future_by_index):
                index = future_by_index[future]
                region_result = future.result()
                indexed_results.append((index, region_result))
                if progress_callback:
                    progress_callback(region_result)
            discovered_region_results = [
                result for _, result in sorted(indexed_results, key=lambda item: item[0])
            ]
    else:
        discovered_region_results = []
        for mapping in region_mappings:
            region_result = _discover_mapping(mapping)
            discovered_region_results.append(region_result)
            if progress_callback:
                progress_callback(region_result)

    for region_result in discovered_region_results:
        region_results.append(region_result)
        documents_url = region_result.get("documents_publications_url")
        if documents_url and documents_url not in pages_scanned:
            pages_scanned.append(documents_url)
        for page in region_result.get("pages_scanned", []):
            if page not in pages_scanned:
                pages_scanned.append(page)
        for page in region_result.get("raa_pages", []):
            if page not in all_raa_pages:
                all_raa_pages.append(page)
        for document in region_result.get("documents", []):
            all_documents.append(document)
        for pdf_link in region_result.get("pdf_links", []):
            if pdf_link in seen_pdfs:
                duplicate_count += 1
                continue
            seen_pdfs.add(pdf_link)
            all_pdfs.append(pdf_link)
        duplicate_count += region_result.get("duplicate_pdf_links", 0)

    date_filter_start, date_filter_end = _date_window_bounds(previous_days, today)
    pdfs_in_date_window = sum(1 for document in all_documents if document.get("is_within_date_window"))
    unknown_date_pdfs = sum(1 for document in all_documents if not document.get("publication_date"))

    return {
        "page_url": normalized,
        "region_pages": [mapping["documents_url"] for mapping in region_mappings if mapping.get("documents_url")],
        "region_results": region_results,
        "raa_pages": all_raa_pages,
        "pdf_links": all_pdfs,
        "documents": all_documents,
        "pages_scanned": pages_scanned,
        "regions_discovered": len(region_mappings),
        "raa_pages_found": len(all_raa_pages),
        "pdfs_discovered": len(all_pdfs),
        "date_filter_days": previous_days,
        "date_filter_start": date_filter_start.isoformat() if date_filter_start else "",
        "date_filter_end": date_filter_end.isoformat() if date_filter_end else "",
        "pdfs_in_date_window": pdfs_in_date_window,
        "unknown_date_pdfs": unknown_date_pdfs,
        "skipped_duplicates": duplicate_count,
    }


def _download_prefecture_documents(
    documents: Sequence[Dict],
    destination: str | Path,
    timeout: int = 60,
    session: Optional[requests.Session] = None,
) -> List[Dict]:
    downloads: List[Dict] = []
    root = Path(destination)
    http = session or _new_http_session()
    for document in documents:
        region = str(document.get("region") or "unknown-region")
        year = str(document.get("year") or "unknown-year")
        target_dir = root / region / year
        item_downloads = download_document_files(
            [document],
            target_dir,
            timeout=timeout,
            session=http,
            organize_subfolders=False,
        )
        downloads.extend(item_downloads)
    return downloads


def _limit_documents_per_region(documents: Sequence[Dict], max_downloads_per_region: Optional[int]) -> List[Dict]:
    if max_downloads_per_region is None or int(max_downloads_per_region) <= 0:
        return list(documents)
    capped: List[Dict] = []
    counts: Dict[str, int] = {}
    for document in documents:
        region = str(document.get("region") or "unknown-region")
        current = counts.get(region, 0)
        if current >= int(max_downloads_per_region):
            continue
        counts[region] = current + 1
        capped.append(document)
    return capped


def download_prefecture_raa_pdfs(
    discovery_result: Dict,
    destination: str | Path,
    timeout: int = 60,
    session: Optional[requests.Session] = None,
    max_downloads_per_region: Optional[int] = None,
) -> Dict:
    date_filter_days = discovery_result.get("date_filter_days")
    documents = list(discovery_result.get("documents") or [])
    if not documents:
        fallback_documents = []
        for link in dedupe_links(
            discovery_result.get("page_url", PREFECTURE_PORTAL_URL),
            discovery_result.get("pdf_links", []),
        ):
            publication_date, date_source = _extract_publication_date_with_source(("pdf_url", link))
            fallback_documents.append({
                "url": link,
                "title": Path(unquote(urlparse(link).path)).name,
                "file_format": "PDF",
                "source": "prefecture_raa_fallback",
                "region": "unknown-region",
                "year": _extract_year(link) or "unknown-year",
                "publication_date": publication_date,
                "date_source": date_source,
            })
        documents = fallback_documents
        for document in documents:
            document["is_within_date_window"] = _is_publication_date_in_window(
                str(document.get("publication_date") or ""),
                date_filter_days,
            )
    else:
        seen = set()
        deduped_documents = []
        for document in documents:
            normalized_url = _normalize_url(str(document.get("url") or ""))
            if not normalized_url or normalized_url in seen:
                continue
            seen.add(normalized_url)
            deduped_documents.append(document)
        documents = deduped_documents

    date_eligible_documents = [
        document
        for document in documents
        if not date_filter_days or int(date_filter_days) <= 0 or document.get("is_within_date_window")
    ]
    skipped_by_date_filter = max(0, len(documents) - len(date_eligible_documents))
    unknown_date_pdfs = sum(1 for document in documents if not document.get("publication_date"))
    pdfs_in_date_window = sum(1 for document in documents if document.get("is_within_date_window"))
    selected_documents = _limit_documents_per_region(date_eligible_documents, max_downloads_per_region)
    duplicate_pdf_links_skipped = max(0, len(discovery_result.get("pdf_links", [])) - len(documents))
    skipped_by_region_limit = max(0, len(date_eligible_documents) - len(selected_documents))
    downloads = _download_prefecture_documents(selected_documents, destination, timeout=timeout, session=session)
    summary = summarize_downloads(downloads, len(selected_documents))
    upstream_duplicate_pdf_links = discovery_result.get("skipped_duplicates", 0)
    duplicate_pdf_links_skipped += upstream_duplicate_pdf_links
    return {
        **discovery_result,
        "documents": documents,
        "date_eligible_documents": date_eligible_documents,
        "selected_documents": selected_documents,
        "max_downloads_per_region": max_downloads_per_region,
        "documents_selected_for_download": len(selected_documents),
        "downloads": downloads,
        "attempted": summary["download_attempts"],
        "downloaded": summary["documents_downloaded_successfully"],
        "failed": summary["failed_downloads"],
        "pdfs_in_date_window": pdfs_in_date_window,
        "unknown_date_pdfs": unknown_date_pdfs,
        "skipped_by_date_filter": skipped_by_date_filter,
        "duplicate_pdf_links_skipped": duplicate_pdf_links_skipped,
        "skipped_by_region_limit": skipped_by_region_limit,
        "skipped_duplicates": duplicate_pdf_links_skipped + skipped_by_region_limit + skipped_by_date_filter,
    }


def run_prefecture_raa_workflow(
    start_url: str = PREFECTURE_PORTAL_URL,
    destination: str | Path = "prefecture_raa_downloads",
    timeout: int = 30,
    session: Optional[requests.Session] = None,
    max_downloads_per_region: Optional[int] = None,
    previous_days: Optional[int] = None,
    today: Optional[date] = None,
) -> Dict:
    discovery = discover_prefecture_raa(
        start_url=start_url,
        timeout=timeout,
        session=session,
        previous_days=previous_days,
        today=today,
    )
    downloads = download_prefecture_raa_pdfs(
        discovery,
        destination,
        timeout=timeout,
        session=session,
        max_downloads_per_region=max_downloads_per_region,
    )
    return {
        **downloads,
        "regions_discovered": discovery["regions_discovered"],
        "raa_pages_found": discovery["raa_pages_found"],
        "pdfs_discovered": discovery["pdfs_discovered"],
        "pdfs_in_date_window": downloads.get("pdfs_in_date_window", discovery.get("pdfs_in_date_window", 0)),
        "unknown_date_pdfs": downloads.get("unknown_date_pdfs", discovery.get("unknown_date_pdfs", 0)),
        "date_filter_days": discovery.get("date_filter_days"),
        "date_filter_start": discovery.get("date_filter_start", ""),
        "date_filter_end": discovery.get("date_filter_end", ""),
        "pdfs_attempted": downloads["attempted"],
        "pdfs_downloaded_successfully": downloads["downloaded"],
        "failed_downloads": downloads["failed"],
        "skipped_or_duplicate_downloads": downloads["skipped_duplicates"],
        "duplicate_pdf_links_skipped": downloads.get("duplicate_pdf_links_skipped", 0),
        "skipped_by_region_limit": downloads.get("skipped_by_region_limit", 0),
        "skipped_by_date_filter": downloads.get("skipped_by_date_filter", 0),
    }

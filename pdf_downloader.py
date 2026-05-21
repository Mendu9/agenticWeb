from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".csv",
    ".txt",
    ".rtf",
    ".zip",
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".tif",
    ".tiff",
    ".bmp",
}

SUPPORTED_FILE_EXTENSIONS = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS

DISCOVERY_PAGE_TERMS = (
    "document",
    "publication",
    "download",
    "file",
    "attachment",
    "recueil",
    "arrete",
    "arrêté",
)

CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
}


def _sanitize_name(value: str, fallback: str = "download") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" .")
    return cleaned or fallback


def _extract_filename_from_header(content_disposition: str) -> Optional[str]:
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition, re.IGNORECASE)
    if not match:
        return None
    return unquote(match.group(1)).strip()


def _extension_from_url(file_url: str) -> str:
    parsed = urlparse(file_url)
    path_suffix = Path(unquote(parsed.path)).suffix.lower()
    if path_suffix in SUPPORTED_FILE_EXTENSIONS:
        return path_suffix

    for values in parse_qs(parsed.query, keep_blank_values=True).values():
        for value in values:
            query_suffix = Path(unquote(value)).suffix.lower()
            if query_suffix in SUPPORTED_FILE_EXTENSIONS:
                return query_suffix
    return ""


def _srcset_candidates(srcset: str) -> List[str]:
    candidates = []
    for source in srcset.split(","):
        url = source.strip().split(None, 1)[0]
        if url:
            candidates.append(url)
    return candidates


def _extension_from_content_type(content_type: str) -> str:
    bare_type = (content_type or "").split(";", 1)[0].strip().lower()
    return CONTENT_TYPE_EXTENSIONS.get(bare_type, "")


def _filename_from_url(file_url: str, index: int, default_extension: str = ".bin") -> str:
    parsed = urlparse(file_url)
    name = Path(unquote(parsed.path)).name
    if not name:
        name = f"document_{index}{default_extension}"
    if not Path(name).suffix and default_extension:
        name = f"{name}{default_extension}"
    return _sanitize_name(name, fallback=f"document_{index}{default_extension}")


def build_pdf_download_dir(source_url: str, root_dir: str | Path = "downloads/pdfs") -> Path:
    parsed = urlparse(source_url)
    domain = _sanitize_name(parsed.netloc or "unknown-domain")
    path_bits = [bit for bit in parsed.path.split("/") if bit]
    suffix = "_".join(_sanitize_name(bit) for bit in path_bits[:2]) if path_bits else "root"
    return Path(root_dir) / domain / suffix


def discover_pdf_links(page_url: str, html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = [anchor["href"].strip() for anchor in soup.find_all("a", href=True)]
    return collect_pdf_links(page_url, candidates)


def discover_file_links(page_url: str, html: str, include_images: bool = True) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = [anchor["href"].strip() for anchor in soup.find_all("a", href=True)]
    if include_images:
        candidates.extend(img["src"].strip() for img in soup.find_all("img", src=True))
        for image in soup.find_all(["img", "source"], srcset=True):
            candidates.extend(_srcset_candidates(image["srcset"]))
    return collect_file_links(page_url, candidates)


def dedupe_links(page_url: str, candidates: Iterable[str]) -> List[str]:
    found: List[str] = []
    seen = set()

    for candidate in candidates:
        if not candidate:
            continue
        absolute = urljoin(page_url, str(candidate).strip())
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        normalized = absolute.split("#", 1)[0]
        if normalized in seen:
            continue
        seen.add(normalized)
        found.append(normalized)

    return found


def same_path_family(candidate_url: str, source_url: str) -> bool:
    source = urlparse(source_url)
    candidate = urlparse(candidate_url)
    if source.netloc.lower() != candidate.netloc.lower():
        return False
    source_path = source.path.rstrip("/") or "/"
    candidate_path = candidate.path.rstrip("/") or "/"
    if source_path == "/":
        return True
    return candidate_path == source_path or candidate_path.startswith(source_path + "/")


def is_relevant_discovery_page(candidate_url: str, source_url: str) -> bool:
    if same_path_family(candidate_url, source_url):
        return True
    source = urlparse(source_url)
    candidate = urlparse(candidate_url)
    if source.netloc.lower() != candidate.netloc.lower():
        return False
    candidate_path = unquote(candidate.path).lower()
    return any(term in candidate_path for term in DISCOVERY_PAGE_TERMS)


def collect_pdf_links(page_url: str, candidates: Iterable[str]) -> List[str]:
    return [link for link in dedupe_links(page_url, candidates) if ".pdf" in link.lower()]


def collect_file_links(page_url: str, candidates: Iterable[str]) -> List[str]:
    return [
        link
        for link in dedupe_links(page_url, candidates)
        if _extension_from_url(link) in SUPPORTED_FILE_EXTENSIONS
    ]


def _typed_value(value, default=None):
    if isinstance(value, list) and len(value) == 2:
        return value[1]
    return value if value is not None else default


def _extract_preload_dump_script_urls(page_url: str, html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    script_urls = []
    for script in soup.find_all("script", src=True):
        src = script["src"].strip()
        if "preloadDumps" not in src:
            continue
        script_urls.append(urljoin(page_url, src))
    return list(dict.fromkeys(script_urls))


def _parse_preload_dump(preload_js: str) -> Optional[Dict]:
    match = re.search(r"window\.preloadDump = (.*);\s*$", preload_js, flags=re.S)
    if not match:
        return None
    outer = json.loads(match.group(1))
    return json.loads(outer)


def discover_pdf_links_from_preload_dump(
    page_url: str,
    html: str,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
) -> List[str]:
    http = session or requests.Session()
    page_path = urlparse(page_url).path.rstrip("/")
    preload_urls = _extract_preload_dump_script_urls(page_url, html)
    if not preload_urls:
        return []

    pdf_candidates: List[str] = []
    for preload_url in preload_urls:
        response = http.get(
            preload_url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
        )
        response.raise_for_status()
        payload = _parse_preload_dump(response.text)
        if not payload:
            continue

        objects = {}
        for entry in payload.get("recording", []):
            if entry[0] == "baseobj":
                objects[entry[1][1]] = entry[2]

        for obj in objects.values():
            if obj.get("_obj_class") != "DownloadItem":
                continue

            item_path = obj.get("_path", "")
            if page_path and f"{page_path}/" not in item_path:
                continue

            download_obj_id = _typed_value(obj.get("link"), {}).get("obj_id")
            if not download_obj_id:
                continue

            download_obj = objects.get(download_obj_id, {})
            blob_id = _typed_value(download_obj.get("blob"), {}).get("id")
            if not blob_id or not blob_id.lower().endswith(".pdf"):
                continue

            pdf_candidates.append(f"https://marketing.webassets.siemens-healthineers.com/{blob_id}")

    return collect_pdf_links(page_url, pdf_candidates)


def discover_pdf_links_with_strategies(
    page_url: str,
    html: str,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
    extra_candidates: Optional[Iterable[str]] = None,
) -> List[str]:
    candidates: List[str] = []
    candidates.extend(discover_pdf_links(page_url, html))
    candidates.extend(discover_pdf_links_from_preload_dump(page_url, html, timeout=timeout, session=session))
    if extra_candidates:
        candidates.extend(collect_pdf_links(page_url, extra_candidates))
    return collect_pdf_links(page_url, candidates)


def discover_file_links_with_strategies(
    page_url: str,
    html: str,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
    extra_candidates: Optional[Iterable[str]] = None,
) -> List[str]:
    candidates: List[str] = []
    candidates.extend(discover_file_links(page_url, html))
    candidates.extend(discover_pdf_links_from_preload_dump(page_url, html, timeout=timeout, session=session))
    if extra_candidates:
        candidates.extend(collect_file_links(page_url, extra_candidates))
    return collect_file_links(page_url, candidates)


def _map_doclib_query_params(source_url: str) -> Dict[str, str]:
    query = parse_qs(urlparse(source_url).query)
    mapping = {
        "productgroups": "product-groups",
        "products": "products",
        "documenttypes": "document-types",
        "languages": "document-languages",
        "countries": "countries",
        "sortingby": "sort",
        "search": "search",
        "partnumber": "part-number",
        "productcode": "product-code",
        "version": "version",
        "lotnumber": "lot-number",
    }

    params: Dict[str, str] = {}
    for source_key, target_key in mapping.items():
        values = query.get(source_key)
        if values:
            params[target_key] = values[-1]
    return params


def _document_url(document: Dict) -> str:
    return str(document.get("url") or document.get("pdf_url") or "")


def select_documents_for_download(discovery_result: Dict, limit: Optional[int] = None) -> Dict:
    documents = list(discovery_result.get("documents") or [])
    if not documents:
        documents = [
            {"url": link, "title": "", "source": "pdf_links"}
            for link in discovery_result.get("pdf_links", [])
        ]

    selected_documents = documents[:limit] if limit is not None else documents
    selected_links = [_document_url(document) for document in selected_documents if _document_url(document)]
    selected_pdf_links = [
        _document_url(document)
        for document in selected_documents
        if _document_url(document) and str(document.get("file_format") or "PDF").upper() == "PDF"
    ]
    return {
        **discovery_result,
        "selected_documents": selected_documents,
        "selected_file_links": selected_links,
        "selected_pdf_links": selected_pdf_links,
        "documents_selected_for_download": len(selected_links),
    }


def summarize_downloads(downloads: List[Dict], selected_count: Optional[int] = None) -> Dict:
    success_count = sum(1 for item in downloads if item.get("status") == "downloaded")
    return {
        "documents_selected_for_download": selected_count if selected_count is not None else len(downloads),
        "download_attempts": len(downloads),
        "documents_downloaded_successfully": success_count,
        "failed_downloads": len(downloads) - success_count,
    }


def fetch_doclib_document_links(
    page_url: str,
    timeout: int = 30,
    max_results: int = 150,
    session: Optional[requests.Session] = None,
) -> Dict:
    http = session or requests.Session()
    parsed = urlparse(page_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    params = _map_doclib_query_params(page_url)
    params.setdefault("sort", "-modified-at")
    page_size = 40

    links: List[str] = []
    documents: List[Dict] = []
    scanned_pages = []
    total_available = None
    records_scanned = 0
    skipped_count = 0
    duplicate_count = 0
    max_results = max(0, int(max_results or 0))
    max_pages = (max_results + page_size - 1) // page_size if max_results else 0

    for page_number in range(1, max_pages + 1):
        page_params = {**params, "page": str(page_number), "pagesize": str(page_size)}
        response = http.get(
            f"{base_url}/rest/v1/documents",
            params=page_params,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        payload = payload if isinstance(payload, dict) else {}
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        total_available = meta.get("total", total_available)
        records = payload.get("data", [])
        records = records if isinstance(records, list) else []
        if not records:
            break

        page_links = []
        page_pdf_count = 0
        for record in records[: max_results - records_scanned]:
            records_scanned += 1
            if not isinstance(record, dict):
                skipped_count += 1
                continue
            attrs = record.get("attributes", {})
            if not isinstance(attrs, dict):
                skipped_count += 1
                continue
            if attrs.get("accessible") is False or attrs.get("blockedNotOwner") is True:
                skipped_count += 1
                continue
            document_id = record.get("id")
            if not document_id:
                skipped_count += 1
                continue
            file_format = str(attrs.get("fileFormat") or "").upper()
            link = f"{base_url}/download?document-ids={document_id}"
            page_links.append(link)
            if file_format == "PDF":
                page_pdf_count += 1
            documents.append(
                {
                    "id": document_id,
                    "title": attrs.get("title") or attrs.get("name") or "",
                    "file_format": file_format,
                    "url": link,
                    "source": "doclib_listing_current_endpoint",
                }
            )

        links.extend(page_links)
        scanned_pages.append(
            {
                "url": f"{base_url}/rest/v1/documents?page={page_number}",
                "title": f"DocLib listing page {page_number} (current implementation endpoint)",
                "files_found": len(page_links),
                "pdf_links_found": page_pdf_count,
                "records_scanned": records_scanned,
            }
        )

        if records_scanned >= max_results or len(records) < page_size:
            break

    deduped_links = dedupe_links(page_url, links)
    seen_document_links = set()
    deduped_documents = []
    for document in documents:
        link = _document_url(document)
        if link in seen_document_links:
            duplicate_count += 1
            continue
        seen_document_links.add(link)
        deduped_documents.append(document)

    return {
        "page_url": page_url,
        "pdf_links": [document["url"] for document in deduped_documents if document.get("file_format") == "PDF"],
        "file_links": deduped_links,
        "count": len(deduped_links),
        "documents": deduped_documents,
        "pages_scanned": scanned_pages,
        "pages_count": len(scanned_pages),
        "total_available": total_available,
        "total_available_documents": total_available,
        "scan_limit": max_results,
        "pages_or_results_scanned": records_scanned,
        "documents_discovered": len(deduped_documents),
        "documents_selected_for_download": 0,
        "download_attempts": 0,
        "documents_downloaded_successfully": 0,
        "failed_downloads": 0,
        "skipped_or_deduplicated_documents": skipped_count + duplicate_count,
        "discovery_method": "doclib_listing",
        "listing_source": "doclib_rest_listing_current_implementation",
        "download_hint": (
            "DocLib is treated as a paginated document listing. This implementation currently "
            "reads the listing through the site's document endpoint; direct downloads may still "
            "require an authenticated browser session."
        ),
    }


def fetch_file_links(page_url: str, timeout: int = 30, session: Optional[requests.Session] = None) -> Dict:
    http = session or requests.Session()
    parsed = urlparse(page_url)
    if "doclib.siemens-healthineers.com" in parsed.netloc.lower() and parsed.path.startswith("/documents"):
        return fetch_doclib_document_links(page_url, timeout=timeout, session=http)

    response = http.get(
        page_url,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
    )
    response.raise_for_status()
    links = discover_file_links_with_strategies(page_url, response.text, timeout=timeout, session=http)
    documents = [
        {
            "url": link,
            "title": Path(unquote(urlparse(link).path)).name,
            "file_format": _extension_from_url(link).lstrip(".").upper(),
            "source": "html_or_image_link",
        }
        for link in links
    ]
    return {
        "page_url": page_url,
        "pdf_links": [link for link in links if _extension_from_url(link) == ".pdf"],
        "file_links": links,
        "documents": documents,
        "count": len(links),
        "documents_discovered": len(documents),
        "discovery_method": "html",
    }


def fetch_pdf_links(page_url: str, timeout: int = 30, session: Optional[requests.Session] = None) -> Dict:
    http = session or requests.Session()
    parsed = urlparse(page_url)
    if "doclib.siemens-healthineers.com" in parsed.netloc.lower() and parsed.path.startswith("/documents"):
        return fetch_doclib_document_links(page_url, timeout=timeout, session=http)

    response = http.get(
        page_url,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
    )
    response.raise_for_status()
    links = discover_pdf_links_with_strategies(page_url, response.text, timeout=timeout, session=http)
    return {
        "page_url": page_url,
        "pdf_links": links,
        "file_links": links,
        "documents": [{"url": link, "title": Path(unquote(urlparse(link).path)).name, "file_format": "PDF", "source": "html_link"} for link in links],
        "count": len(links),
        "documents_discovered": len(links),
        "discovery_method": "html",
    }


def _document_folder_name(document: Dict, index: int) -> str:
    title = document.get("title") or Path(unquote(urlparse(_document_url(document)).path)).stem
    document_id = document.get("id") or f"{index:04d}"
    return _sanitize_name(f"{index:04d}_{document_id}_{title}", fallback=f"{index:04d}_document")


def _document_category_folder(document: Dict, file_url: str, content_type: str = "") -> str:
    file_format = str(document.get("file_format") or "").lower().lstrip(".")
    extension = _extension_from_content_type(content_type) or _extension_from_url(file_url)
    if extension in IMAGE_EXTENSIONS or file_format in {ext.lstrip(".") for ext in IMAGE_EXTENSIONS}:
        return "images"
    return "documents"


def download_document_files(
    documents_or_urls: List[Dict | str],
    destination: str | Path,
    timeout: int = 60,
    session: Optional[requests.Session] = None,
    organize_subfolders: bool = True,
) -> List[Dict]:
    http = session or requests.Session()
    target_dir = Path(destination)
    target_dir.mkdir(parents=True, exist_ok=True)

    downloads: List[Dict] = []
    for index, item in enumerate(documents_or_urls, start=1):
        document = item if isinstance(item, dict) else {"url": str(item)}
        file_url = _document_url(document)
        try:
            response = http.get(
                file_url,
                timeout=timeout,
                stream=True,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
            )
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/html" in content_type:
                raise ValueError("download endpoint returned HTML instead of a downloadable file")

            header_name = _extract_filename_from_header(response.headers.get("Content-Disposition", ""))
            default_extension = (
                _extension_from_content_type(content_type)
                or _extension_from_url(file_url)
                or f".{str(document.get('file_format') or 'bin').lower().lstrip('.')}"
            )
            filename = _sanitize_name(header_name) if header_name else _filename_from_url(file_url, index, default_extension)
            if not Path(filename).suffix and default_extension:
                filename = f"{filename}{default_extension}"

            item_dir = target_dir / _document_category_folder(document, file_url, content_type) if organize_subfolders else target_dir
            item_dir.mkdir(parents=True, exist_ok=True)
            file_path = item_dir / filename
            counter = 1
            while file_path.exists():
                file_path = item_dir / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
                counter += 1

            with file_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        handle.write(chunk)

            downloads.append(
                {
                    "url": file_url,
                    "file_path": str(file_path.resolve()),
                    "filename": file_path.name,
                    "folder": str(item_dir.resolve()),
                    "bytes": file_path.stat().st_size,
                    "status": "downloaded",
                }
            )
        except Exception as exc:
            downloads.append(
                {
                    "url": file_url,
                    "file_path": "",
                    "filename": "",
                    "folder": "",
                    "bytes": 0,
                    "status": f"failed: {exc}",
                }
            )

    return downloads


def download_pdf_files(
    pdf_urls: List[str],
    destination: str | Path,
    timeout: int = 60,
    session: Optional[requests.Session] = None,
) -> List[Dict]:
    return download_document_files(
        list(pdf_urls),
        destination,
        timeout=timeout,
        session=session,
        organize_subfolders=False,
    )

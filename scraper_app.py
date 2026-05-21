import asyncio
import io
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import pandas as pd
import streamlit as st

try:
    from entity_extractor import extract_product_records
    from focused_crawler import crawl_focused_tree
    from page_parser import parse_structured_page
    from prefecture_raa import (
        PREFECTURE_PORTAL_URL,
        discover_prefecture_raa,
        download_prefecture_raa_pdfs,
        run_prefecture_raa_workflow,
    )
    from raa_daily_tracker import (
        PostgresRaaDailyRepository,
        download_daily_raa_diff,
        postgres_config_from_env,
        run_daily_raa_check,
    )
    from scraper import FireScraper
    import pdf_downloader as pdf_tools
    from preprocessing import preprocess_page_result
except ImportError as e:
    st.error(f"Could not import firescrape: {e}")
    st.stop()

build_pdf_download_dir = pdf_tools.build_pdf_download_dir
collect_pdf_links = pdf_tools.collect_pdf_links
collect_file_links = pdf_tools.collect_file_links
download_document_files = pdf_tools.download_document_files
download_pdf_files = pdf_tools.download_pdf_files
fetch_doclib_document_links = pdf_tools.fetch_doclib_document_links
fetch_file_links = pdf_tools.fetch_file_links
fetch_pdf_links = pdf_tools.fetch_pdf_links
select_documents_for_download = pdf_tools.select_documents_for_download
summarize_downloads = pdf_tools.summarize_downloads
same_path_family = pdf_tools.same_path_family
is_relevant_discovery_page = pdf_tools.is_relevant_discovery_page
discover_file_links_with_strategies = pdf_tools.discover_file_links_with_strategies
discover_pdf_links_with_strategies = getattr(
    pdf_tools,
    "discover_pdf_links_with_strategies",
    lambda page_url, html, timeout=30, session=None, extra_candidates=None: collect_pdf_links(
        page_url,
        list(pdf_tools.discover_pdf_links(page_url, html)) + list(extra_candidates or []),
    ),
)


def _streamlit_secrets_or_empty():
    try:
        return dict(st.secrets)
    except Exception:
        return {}


def _apply_prefecture_network_settings(secrets=None):
    secrets = secrets or _streamlit_secrets_or_empty()
    if "PREFECTURE_RAA_VERIFY_SSL" in secrets:
        os.environ["PREFECTURE_RAA_VERIFY_SSL"] = str(secrets["PREFECTURE_RAA_VERIFY_SSL"])


st.set_page_config(page_title="web scraper", layout="wide")

def _file_extension(link: str) -> str:
    return pdf_tools._extension_from_url(link)


def _is_pdf_link(link: str) -> bool:
    return _file_extension(link) == ".pdf"


def _normalize_url(url: str) -> str:
    normalized = url.strip()
    if normalized and not normalized.startswith("http"):
        normalized = "https://" + normalized
    return normalized


def _default_document_rows(file_links: list[str]) -> list[dict]:
    return [
        {
            "url": link,
            "title": Path(unquote(urlparse(link).path)).name,
            "file_format": _file_extension(link).lstrip(".").upper(),
            "source": "discovered_link",
        }
        for link in file_links
    ]


def _records_to_rows(records: list) -> list[dict]:
    rows = []
    for record in records:
        rows.append(
            {
                "Product": record.product_name,
                "Modality": record.modality,
                "Family": record.product_family,
                "Summary": record.summary,
                "Specs": "; ".join(
                    f"{key}: {', '.join(values)}" for key, values in record.technical_specs.items()
                ),
                "Clinical Parameters": "; ".join(
                    f"{parameter.name}: {parameter.value}" for parameter in record.clinical_parameters
                ),
            }
        )
    return rows


def _blocks_to_rows(blocks: list) -> list[dict]:
    rows = []
    for block in blocks:
        rows.append(
            {
                "Block ID": block.block_id,
                "Type": block.block_type,
                "Heading": block.heading,
                "Text": block.text,
                "Links": ", ".join(block.links[:5]),
            }
        )
    return rows


def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            def _run():
                _l = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
                asyncio.set_event_loop(_l)
                try:
                    return _l.run_until_complete(coro)
                finally:
                    _l.close()
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(_run).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


async def do_scrape(url: str, timeout: int) -> dict:
    t0 = time.time()
    async with FireScraper(timeout=timeout) as s:
        r = await s.fetch(url)
    structured = parse_structured_page(r.html or r.markdown, r.url, r.title)
    records = extract_product_records(structured)
    etl = preprocess_page_result(r)
    return {
        "url": r.url, "title": r.title, "success": r.success, "error": r.error,
        "content": r.markdown, "content_chars": len(r.markdown),
        "clean_content": etl.clean_text,
        "clean_content_chars": len(etl.clean_text),
        "structured_blocks": structured.blocks,
        "structured_tables": structured.tables,
        "product_records": records,
        "links": r.links, "links_count": len(r.links),
        "images_count": len(r.images),
        "fetch_method": r.metadata.get("fetched_via", "unknown"),
        "block_signals": r.metadata.get("block_signals", []),
        "elapsed_s": round(time.time() - t0, 2),
    }


async def do_crawl(url: str, depth: int, max_pages: int, timeout: int) -> dict:
    t0 = time.time()
    async with FireScraper(timeout=timeout) as s:
        pages = await crawl_focused_tree(
            start_url=url, max_depth=depth, max_pages=max_pages,
            concurrency=2, respect_robots=True, scraper=s, verbose=False,
        )
    rows = []
    for p in pages:
        structured = parse_structured_page(p.html or p.markdown, p.url, p.title)
        records = extract_product_records(structured)
        etl = preprocess_page_result(p)
        rows.append({
            "url": p.url,
            "title": p.title,
            "success": p.success,
            "content_chars": len(p.markdown),
            "clean_content_chars": len(etl.clean_text),
            "links_found": len(p.links),
            "images_found": len(p.images),
            "fetch_method": p.metadata.get("fetched_via", "unknown"),
            "source_quality": p.metadata.get("source_quality", "unknown"),
            "bad_source_reasons": p.metadata.get("bad_source_reasons", []),
            "content_preview": p.markdown[:300].replace("\n", " ").strip(),
            "clean_preview": etl.clean_text[:300].replace("\n", " ").strip(),
            "full_content": p.markdown,
            "clean_content": etl.clean_text,
            "structured_blocks": structured.blocks,
            "product_records": records,
        })
    return {
        "pages": rows, "total_pages": len(rows),
        "total_chars": sum(r["content_chars"] for r in rows),
        "total_clean_chars": sum(r["clean_content_chars"] for r in rows),
        "elapsed_s": round(time.time() - t0, 2),
    }


async def do_discover_pdfs(url: str, depth: int, max_pages: int, timeout: int) -> dict:
    t0 = time.time()
    parsed = urlparse(url)
    if "doclib.siemens-healthineers.com" in parsed.netloc.lower() and parsed.path.startswith("/documents"):
        result = await asyncio.to_thread(fetch_doclib_document_links, url, timeout, max_pages)
        result["elapsed_s"] = round(time.time() - t0, 2)
        return result

    initial_result = await asyncio.to_thread(fetch_file_links, url, timeout)

    async with FireScraper(timeout=timeout) as scraper:
        start_page = await scraper.fetch(url)
        file_links = collect_file_links(
            start_page.url or url,
            initial_result.get("file_links", []) + collect_file_links(start_page.url or url, start_page.links),
        )
        scanned_pages = [
            {
                "url": start_page.url or url,
                "title": start_page.title,
                "files_found": len(file_links),
                "pdf_links_found": len([link for link in file_links if _is_pdf_link(link)]),
            }
        ]

        if depth > 0 and max_pages > 1:
            pages = await crawl_focused_tree(
                start_url=url,
                max_depth=depth,
                max_pages=max_pages,
                include_patterns=[
                    "*[Dd]ocument*",
                    "*[Pp]ublication*",
                    "*[Dd]ownload*",
                    "*[Ff]ile*",
                    "*[Rr]ecueil*",
                    "*[Aa]rrete*",
                ],
                concurrency=2,
                respect_robots=True,
                scraper=scraper,
                verbose=False,
            )
            discovery_root = start_page.url or url
            scanned_urls = {discovery_root.split("#", 1)[0].rstrip("/") or discovery_root}
            for page in pages:
                if not is_relevant_discovery_page(page.url, discovery_root):
                    continue
                normalized_page_url = page.url.split("#", 1)[0].rstrip("/") or page.url
                if normalized_page_url in scanned_urls:
                    continue
                scanned_urls.add(normalized_page_url)
                page_files = discover_file_links_with_strategies(
                    page.url,
                    page.html or page.markdown or "",
                    timeout=timeout,
                    extra_candidates=page.links,
                )
                if page_files:
                    file_links.extend(page_files)
                scanned_pages.append(
                    {
                        "url": page.url,
                        "title": page.title,
                        "files_found": len(page_files),
                        "pdf_links_found": len([link for link in page_files if _is_pdf_link(link)]),
                    }
                )

    deduped_links = collect_file_links(url, file_links)
    documents = [
        {
            "url": link,
            "title": Path(link.split("?", 1)[0]).name,
            "file_format": _file_extension(link).lstrip(".").upper(),
            "source": "crawl_file_link",
        }
        for link in deduped_links
    ]
    return {
        "page_url": url,
        "pdf_links": [link for link in deduped_links if _is_pdf_link(link)],
        "file_links": deduped_links,
        "documents": documents,
        "count": len(deduped_links),
        "documents_discovered": len(documents),
        "pages_scanned": scanned_pages,
        "pages_count": len(scanned_pages),
        "elapsed_s": round(time.time() - t0, 2),
    }


def to_excel(scrape: dict, crawl: dict, url: str) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        pd.DataFrame({
            "Field": ["URL", "Title", "Run Date", "Success", "Method",
                      "Content (chars)", "Clean Content (chars)", "Links", "Images", "Scrape Time (s)",
                      "Crawl Pages", "Crawl Total Chars", "Crawl Time (s)", "Block Signals"],
            "Value": [
                url, scrape.get("title", ""), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(scrape.get("success", "")), scrape.get("fetch_method", ""),
                scrape.get("content_chars", 0), scrape.get("clean_content_chars", 0), scrape.get("links_count", 0),
                scrape.get("images_count", 0), scrape.get("elapsed_s", ""),
                crawl["total_pages"] if crawl else "—",
                crawl["total_chars"] if crawl else "—",
                crawl["elapsed_s"] if crawl else "—",
                ", ".join(scrape.get("block_signals", [])) or "None",
            ],
        }).to_excel(w, sheet_name="Summary", index=False)

        pd.DataFrame({
            "URL": [scrape.get("url", "")], "Title": [scrape.get("title", "")],
            "Content": [scrape.get("content", "")],
            "Content Chars": [scrape.get("content_chars", 0)],
            "Clean Content": [scrape.get("clean_content", "")],
            "Clean Content Chars": [scrape.get("clean_content_chars", 0)],
            "Links Count": [scrape.get("links_count", 0)],
            "Images Count": [scrape.get("images_count", 0)],
            "Fetch Method": [scrape.get("fetch_method", "")],
            "Time (s)": [scrape.get("elapsed_s", "")],
        }).to_excel(w, sheet_name="Scraped Content", index=False)

        if scrape.get("links"):
            pd.DataFrame({"Links Found": scrape["links"]}).to_excel(
                w, sheet_name="Links", index=False)

        if crawl and crawl.get("pages"):
            pd.DataFrame([{
                "URL": p["url"], "Title": p["title"],
                "Content (chars)": p["content_chars"],
                "Clean Content (chars)": p["clean_content_chars"],
                "Links Found": p["links_found"], "Images Found": p["images_found"],
                "Fetch Method": p["fetch_method"],
                "Content Preview": p["content_preview"],
                "Clean Preview": p["clean_preview"],
            } for p in crawl["pages"]]).to_excel(w, sheet_name="Crawl Pages", index=False)

            pd.DataFrame([{
                "URL": p["url"], "Title": p["title"],
                "Full Content": p["full_content"], "Clean Content": p["clean_content"],
            } for p in crawl["pages"]]).to_excel(w, sheet_name="Full Content", index=False)

    return out.getvalue()


def render_scraper_tab(mode: str, crawl_depth: int, max_pages: int, timeout: int):
    url_input = st.text_input("URL", key="scrape_url")
    run_btn = st.button("Run", type="primary", width="stretch", key="scrape_run")

    if not run_btn:
        return
    if not url_input.strip():
        st.warning("Please enter a URL.")
        return

    url = url_input.strip()
    if not url.startswith("http"):
        url = "https://" + url
    domain = urlparse(url).netloc

    scrape_result = crawl_result = None

    with st.spinner(f"Scraping {url} ..."):
        try:
            scrape_result = run_async(do_scrape(url, timeout))
        except Exception as e:
            st.error(f"Scrape failed: {e}")

    if scrape_result and mode == "scrape and crawl":
        with st.spinner(f"Crawling {domain} - depth {crawl_depth}, max {max_pages} pages ..."):
            try:
                crawl_result = run_async(do_crawl(url, crawl_depth, max_pages, timeout))
            except Exception as e:
                st.error(f"Crawl failed: {e}")

    if not scrape_result:
        return

    st.divider()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("site title", scrape_result["title"] or domain)
    c2.metric("chars", f"{scrape_result['content_chars']:,} chars")
    c3.metric("links", scrape_result["links_count"])
    c4.metric("time", f"{scrape_result['elapsed_s']}s")

    if scrape_result.get("block_signals"):
        st.warning(f"Block signals: {', '.join(scrape_result['block_signals'])}")
    if not scrape_result["success"]:
        st.error(f"Error: {scrape_result['error']}")

    st.caption(f"URL: {scrape_result['url']} | Method: `{scrape_result['fetch_method']}`")

    col_a, col_b = st.columns([2, 1])
    with col_a:
        with st.expander("Scraped text", expanded=True):
            st.text_area(
                "content",
                value=scrape_result["content"] or "(no content)",
                height=300,
                label_visibility="collapsed",
            )
        with st.expander("ETL cleaned text", expanded=True):
            st.text_area(
                "clean_content",
                value=scrape_result["clean_content"] or "(no useful content retained)",
                height=300,
                label_visibility="collapsed",
            )
        with st.expander("Structured blocks"):
            block_rows = _blocks_to_rows(scrape_result.get("structured_blocks", []))
            if block_rows:
                st.dataframe(pd.DataFrame(block_rows), width="stretch", height=320)
            else:
                st.caption("No structured blocks found.")
        with st.expander("Extracted specs and parameters", expanded=True):
            record_rows = _records_to_rows(scrape_result.get("product_records", []))
            if record_rows:
                st.dataframe(pd.DataFrame(record_rows), width="stretch", height=260)
            else:
                st.caption("No product records extracted.")
    with col_b:
        st.markdown("**Links discovered**")
        if scrape_result.get("links"):
            st.dataframe(pd.DataFrame({"URL": scrape_result["links"]}), height=300, width="stretch")
        else:
            st.caption("No links found.")

    if crawl_result:
        st.divider()
        st.subheader("Crawl Results")
        c1, c2, c3 = st.columns(3)
        c1.metric("Pages Crawled", crawl_result["total_pages"])
        c2.metric("Total Content", f"{crawl_result['total_chars']:,} chars")
        c3.metric("Crawl Time", f"{crawl_result['elapsed_s']}s")

        if crawl_result["pages"]:
            st.dataframe(
                pd.DataFrame([
                    {
                        "URL": p["url"],
                        "Title": p["title"],
                        "Content (chars)": p["content_chars"],
                        "Clean (chars)": p["clean_content_chars"],
                        "Links": p["links_found"],
                        "Images": p["images_found"],
                        "Method": p["fetch_method"],
                        "Preview": p["content_preview"],
                        "Clean Preview": p["clean_preview"],
                    }
                    for p in crawl_result["pages"]
                ]),
                width="stretch",
                height=400,
            )

            with st.expander("Full content per crawled page"):
                for idx, p in enumerate(crawl_result["pages"]):
                    st.markdown(f"**{p['title'] or p['url']}** `{p['content_chars']:,}c`")
                    st.text_area(
                        "fc",
                        value=p["full_content"] or "(empty)",
                        height=150,
                        label_visibility="collapsed",
                        key=f"crawl_page_{idx}",
                    )
                    st.text_area(
                        "cc",
                        value=p["clean_content"] or "(no useful content retained)",
                        height=150,
                        label_visibility="collapsed",
                        key=f"crawl_clean_page_{idx}",
                    )
                    record_rows = _records_to_rows(p.get("product_records", []))
                    if record_rows:
                        st.dataframe(pd.DataFrame(record_rows), width="stretch", height=180)
                    st.divider()

    st.divider()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        "Download Excel",
        data=to_excel(scrape_result, crawl_result, url),
        file_name=f"scrape_{domain}_{ts}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
        type="primary",
    )


def select_folder_dialog(initial_dir: str = "") -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(initialdir=initial_dir or None)
        root.destroy()
        return selected or ""
    except Exception:
        return ""


def _render_download_directory_input(state_prefix: str, normalized_url: str, root_dir: str) -> tuple[bool, Path]:
    default_dir = str(build_pdf_download_dir(normalized_url, root_dir=root_dir)) if normalized_url else root_dir
    target_key = f"{state_prefix}_target_dir"
    default_key = f"{state_prefix}_default_target_dir"
    previous_default_dir = st.session_state.get(default_key)
    if (
        target_key not in st.session_state
        or not st.session_state[target_key]
        or st.session_state[target_key] in {"downloads/pdfs", "downloads/files", previous_default_dir}
    ):
        st.session_state[target_key] = default_dir
    st.session_state[default_key] = default_dir

    folder_col, browse_col = st.columns([5, 1])
    with browse_col:
        st.write("")
        browse_clicked = st.button("Browse", width="stretch", key=f"{state_prefix}_browse")
        if browse_clicked:
            selected_dir = select_folder_dialog(st.session_state[target_key])
            if selected_dir:
                st.session_state[target_key] = selected_dir
            st.rerun()

    with folder_col:
        legacy_key = "C:/Users/z0055sbr/Downloads/Test2"
        if legacy_key in st.session_state and st.session_state.get(legacy_key) and st.session_state.get(target_key) == default_dir:
            st.session_state[target_key] = st.session_state[legacy_key]
        target_dir = st.text_input("Download folder", key=target_key)

    save_dir = Path(target_dir.strip()) if target_dir.strip() else build_pdf_download_dir(normalized_url, root_dir=root_dir)
    return browse_clicked, save_dir


def _download_dir_from_state(state_prefix: str, fallback: Path) -> Path:
    value = str(st.session_state.get(f"{state_prefix}_target_dir") or "").strip()
    return Path(value) if value else Path(fallback)


def render_pdf_tab(timeout: int):
    pdf_source = st.text_input("url for documents/images", key="pdf_source")
    normalized_url = _normalize_url(pdf_source)

    previous_source = st.session_state.get("pdf_current_source")
    source_changed_with_discovery = False
    if previous_source != normalized_url:
        st.session_state["pdf_current_source"] = normalized_url
        st.session_state["pdf_download_limit"] = 0
        if st.session_state.get("pdf_discovery_source") != normalized_url:
            source_changed_with_discovery = st.session_state.get("pdf_discovery_result") is not None
            st.session_state.pop("pdf_discovery_result", None)
            st.session_state.pop("pdf_discovery_source", None)

    crawl_pdfs = True #st.checkbox("Scan linked pages for more files", value=True, key="pdf_crawl_enabled")
    pdf_crawl_depth = 3#st.slider("File crawl depth", 0, 3, 2, key="pdf_crawl_depth")
    pdf_max_pages = st.number_input(
        "Discovery scan limit",
        min_value=1,
        max_value=250000,
        value=250000,
        step=50,
        key="pdf_max_pages",
        help=(
            "For DocLib this is the number of listing results to inspect. "
            "For regular websites this is the maximum number of pages to crawl."
        ),
    )
    browse_clicked, _ = _render_download_directory_input("pdf", normalized_url, "downloads/files")
    save_dir ='C:/Users/z0055sbr/Downloads/Test2'
    if browse_clicked:
        return

    pdf_run_btn = st.button("Discover files", type="primary", width="stretch", key="pdf_run")

    if pdf_run_btn:
        if not normalized_url:
            st.warning("Please enter a page URL.")
            return

        try:
            if crawl_pdfs:
                with st.spinner(f"Scanning {normalized_url} and linked pages for downloadable files ..."):
                    pdf_result = run_async(
                        do_discover_pdfs(
                            normalized_url,
                            depth=pdf_crawl_depth,
                            max_pages=int(pdf_max_pages),
                            timeout=timeout,
                        )
                    )
            else:
                with st.spinner(f"Scanning {normalized_url} for downloadable files ..."):
                    pdf_result = fetch_file_links(normalized_url, timeout=timeout)
        except Exception as e:
            st.error(f"PDF discovery failed: {e}")
            return

        st.session_state["pdf_discovery_result"] = pdf_result
        st.session_state["pdf_discovery_source"] = normalized_url
        st.session_state["pdf_download_limit"] = 0

    pdf_result = st.session_state.get("pdf_discovery_result")
    if not pdf_result:
        if source_changed_with_discovery:
            st.caption("URL changed. Click Discover files to scan the new source.")
        return
    if normalized_url and st.session_state.get("pdf_discovery_source") != normalized_url:
        st.caption("URL changed. Click Discover files to scan the new source.")
        return

    if crawl_pdfs:
        method = pdf_result.get("discovery_method", "crawl")
        if method == "doclib_listing":
            message = (
                f"DocLib source reports {pdf_result.get('total_available_documents', 'unknown')} total matching document(s). "
                f"This discovery scanned {pdf_result.get('pages_or_results_scanned', 0)} result(s) "
                f"within the configured scan limit of {pdf_result.get('scan_limit', 0)} and found "
                f"{pdf_result.get('documents_discovered', pdf_result['count'])} downloadable file/document record(s) in that scanned window."
            )
            if (
                pdf_result.get("total_available_documents") is not None
                and pdf_result.get("scan_limit", 0) < pdf_result.get("total_available_documents", 0)
            ):
                st.caption(
                    "Only the scanned window is available to download. Increase the discovery scan limit "
                    "and click Discover files again to inspect more DocLib records."
                )
        else:
            message = (
                f"Found {pdf_result['count']} downloadable file link(s) across {pdf_result['pages_count']} scanned page(s) "
                f"in {pdf_result['elapsed_s']}s."
            )
        st.info(message)
        if pdf_result.get("download_hint"):
            st.caption(pdf_result["download_hint"])
        if pdf_result.get("pages_scanned"):
            with st.expander("Scanned pages"):
                st.dataframe(pd.DataFrame(pdf_result["pages_scanned"]), width="stretch", height=240)
    else:
        st.info(f"Found {pdf_result['count']} downloadable file link(s).")
    file_links = pdf_result.get("file_links") or pdf_result.get("pdf_links", [])
    if not file_links:
        st.caption("No downloadable files were discovered on that page.")
        return

    rows = pdf_result.get("documents") or _default_document_rows(file_links)
    st.dataframe(pd.DataFrame(rows), width="stretch", height=260)
    discovered_count = len(file_links)
    pdf_download_limit = st.number_input(
        "Files to download now",
        min_value=0,
        max_value=discovered_count,
        value=0,
        step=1,
        key="pdf_download_limit",
        help="Discovery does not download files. Choose a number and click Download selected files.",
    )
    selected_limit = None if int(pdf_download_limit) == 0 else int(pdf_download_limit)
    selected_result = select_documents_for_download(pdf_result, selected_limit)
    if selected_result["documents_selected_for_download"] > len(file_links):
        selected_result = select_documents_for_download(pdf_result, len(file_links))
    st.caption(
        f"Selected {selected_result['documents_selected_for_download']} of "
        f"{len(file_links)} discovered file(s) for download."
    )
    if int(pdf_download_limit) == 0:
        st.caption("Set Files to download now above 0 when you are ready to start downloading.")
        return

    download_btn = st.button("Download selected files", type="primary", width="stretch", key="pdf_download_selected")
    if not download_btn:
        return

    try:
        with st.spinner(f"Downloading files to {save_dir} ..."):
            downloaded = download_document_files(selected_result["selected_documents"], save_dir, timeout=timeout)
    except Exception as e:
        st.error(f"PDF download failed: {e}")
        return

    st.dataframe(pd.DataFrame(downloaded), width="stretch", height=320)
    download_summary = summarize_downloads(downloaded, selected_result["documents_selected_for_download"])
    success_count = download_summary["documents_downloaded_successfully"]
    failed_count = download_summary["failed_downloads"]
    html_blocked_count = sum(
        1
        for item in downloaded
        if "returned HTML instead of a downloadable file" in item["status"]
    )
    if html_blocked_count:
        st.warning(
            f"{html_blocked_count} link(s) resolved to an HTML page instead of a downloadable file. "
            "That usually means the site needs a browser session, extra click flow, or access rights "
            "before the real file URL is issued."
        )
    if failed_count:
        st.warning(f"{failed_count} selected document(s) failed to download.")
    st.success(
        f"Selected {download_summary['documents_selected_for_download']} document(s); "
        f"attempted {download_summary['download_attempts']} download(s); "
        f"saved {success_count} file(s) under `{save_dir.resolve()}`."
    )


def render_prefecture_tab(timeout: int):
    # st.caption(
    #     "This site-specific workflow starts from the prefecture portal or a region page, "
    #     "finds Documents & publications, follows Recueil des actes administratifs pages, "
    #     "and downloads all discovered RAA PDFs in one run."
    # )

    prefecture_source = st.text_input(
        "url",
        key="prefecture_source",
        value=PREFECTURE_PORTAL_URL,
        placeholder=PREFECTURE_PORTAL_URL,
    )
    normalized_url = _normalize_url(prefecture_source)

    previous_source = st.session_state.get("prefecture_current_source")
    source_changed_with_result = False
    if previous_source != normalized_url:
        st.session_state["prefecture_current_source"] = normalized_url
        if st.session_state.get("prefecture_result_source") != normalized_url:
            source_changed_with_result = (
                st.session_state.get("prefecture_run_result") is not None
                or st.session_state.get("prefecture_discovery_result") is not None
                or st.session_state.get("prefecture_download_result") is not None
            )
            st.session_state.pop("prefecture_run_result", None)
            st.session_state.pop("prefecture_discovery_result", None)
            st.session_state.pop("prefecture_download_result", None)
            st.session_state.pop("prefecture_result_source", None)

    browse_clicked, save_dir = _render_download_directory_input("prefecture", normalized_url, "prefecture_raa_downloads")
    if browse_clicked:
        return
    download_cap_per_region = 5 #st.number_input(
    #     "PDFs to download per region (testing)",
    #     min_value=0,
    #     max_value=50,
    #     value=5,
    #     step=1,
     #   key="prefecture_download_cap_per_region",
        #help="This only limits downloads after discovery. Set 0 to download all PDFs that were discovered.",
    #)
    extraction_page_limit = 0 #st.number_input(
    #     "RAA pages to extract per region (0 = all)",
    #     min_value=0,
    #     max_value=10000,
    #     value=120,
    #     step=10,
    #     key="prefecture_extraction_page_limit",
    #     help=(
    #         "Limits how many expanded RAA listing/year/month pages are parsed for PDF links per region. "
    #         "Use 0 for exhaustive discovery; a bounded value is faster for testing."
    #     ),
    #)
    previous_days = 0# st.number_input(
    #     "Only include PDFs from previous N days",
    #     min_value=0,
    #     max_value=3650,
    #     value=10,
    #     step=1,
    #     key="prefecture_previous_days",
    #     help="Use 0 to disable date filtering. Unknown-date PDFs are shown but excluded from download selection when filtering is active.",
    # )

    discover_btn = st.button("search", type="primary", width="stretch", key="prefecture_discover")
    if discover_btn:
        if not normalized_url:
            st.warning("Please enter a prefecture portal or region URL.")
            return

        try:
            status_placeholder = st.empty()
            progress_placeholder = st.empty()
            progress_rows = []

            def _on_region_result(region_result: dict):
                progress_rows.append(
                    {
                        "region": region_result.get("region", ""),
                        "seconds": region_result.get("elapsed_s", 0),
                        "pages_scanned": len(region_result.get("pages_scanned", [])),
                        #"raa_pages_extracted": region_result.get("raa_pages_extracted", 0),
                        #"raa_pages_with_pdfs": len(region_result.get("raa_pages", [])),
                        "pdfs_found": len(region_result.get("pdf_links", [])),
                        #"pdfs_in_date_window": region_result.get("pdfs_in_date_window", 0),
                        #"unknown_date_pdfs": region_result.get("unknown_date_pdfs", 0),
                        #"status": "PDFs found" if region_result.get("has_raa_pdfs") else "no PDFs or fetch issue",
                        #"errors": "; ".join(region_result.get("errors", [])[:3]),
                    }
                )
                completed = len(progress_rows)
                status_placeholder.info(f"searching->completed {completed} regions")
                progress_placeholder.dataframe(pd.DataFrame(progress_rows), width="stretch", height=220)

            # status_placeholder.info("Discovering RAA regions, pages, years, and PDF links ...")
            search_started = time.perf_counter()
            discovery_result = discover_prefecture_raa(
                normalized_url,
                timeout,
                progress_callback=_on_region_result,
                extraction_page_limit=int(extraction_page_limit),
                previous_days=int(previous_days),
            )
            discovery_result["search_seconds"] = round(time.perf_counter() - search_started, 2)
            status_placeholder.success(
                f"found {discovery_result.get('pdfs_discovered', 0)} pdfs "
                f"on {discovery_result.get('raa_pages_found', 0)} RAA pages "
                #f"{discovery_result.get('pdfs_in_date_window', 0)} inside the date window."
            )
            st.session_state["prefecture_discovery_result"] = discovery_result
            st.session_state.pop("prefecture_download_result", None)
            st.session_state["prefecture_result_source"] = normalized_url
        except Exception as e:
            st.error(f"Prefecture discovery failed: {e}")
            return

    def _render_daily_check_section():
        st.markdown("**Daily RAA diff check**")
        daily_btn = st.button("Check updated", type="secondary", width="stretch", key="prefecture_daily_check")
        if daily_btn:
            try:
                secrets = _streamlit_secrets_or_empty()
                repo = PostgresRaaDailyRepository(postgres_config_from_env(secrets))
                with st.spinner("checking RAA regions"):
                    daily_result = run_daily_raa_check(
                        destination=save_dir,
                        repo=repo,
                        start_url=normalized_url or PREFECTURE_PORTAL_URL,
                        timeout=timeout,
                    )
                st.session_state["prefecture_daily_result"] = daily_result
                st.session_state.pop("prefecture_daily_download_result", None)
            except Exception as e:
                st.error(f"Daily RAA check failed: {e}")
                return
        daily_result = st.session_state.get("prefecture_daily_result") or {}
        if not daily_result:
            return
        # if daily_result.get("status") == "baseline_created":
        #     st.success("Baseline created. Future daily checks will show new PDFs.")
        # else:
        #     st.success(
        #         f"Daily check finished: {daily_result.get('new_pdfs', 0)} new PDF(s) ready for download."
        #     )

        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Regions scanned", daily_result.get("regions_scanned", 0))
        d2.metric("Daily check PDFs", daily_result.get("pdfs_discovered", 0))
        d3.metric("New PDFs", daily_result.get("new_pdfs", 0))
        d4.metric("Already in db", daily_result.get("already_known", 0))
        st.caption(
            f"Months scanned: {daily_result.get('months_scanned', '-')}. "
            f"Excel report: {daily_result.get('excel_path', '-')}"
        )
        timing_rows = [
            {"stage": "search", "seconds": daily_result.get("discovery_seconds", 0)},
            {"stage": "db compare", "seconds": daily_result.get("db_seconds", 0)},
            {"stage": "excel report gen", "seconds": daily_result.get("excel_seconds", 0)},
            {"stage": "total daily check", "seconds": daily_result.get("total_seconds", 0)},
        ]
        st.dataframe(pd.DataFrame(timing_rows), width="stretch", height=180)
        daily_by_region = daily_result.get("daily_by_region", [])
        if daily_by_region:
            st.markdown("**Daily PDFs by region**")
            #st.caption("This is the current daily-check window, not the full archive: current month plus previous month, excluding Corse.")
            st.dataframe(pd.DataFrame(daily_by_region), width="stretch", height=260)
        new_by_region = daily_result.get("new_by_region", [])
        if new_by_region:
            st.markdown("**New PDFs by region**")
            st.dataframe(pd.DataFrame(new_by_region), width="stretch", height=180)
        new_documents = daily_result.get("new_documents", [])
        if new_documents:
            download_daily_btn = st.button(
                "Download daily diff PDFs",
                type="primary",
                width="stretch",
                key="prefecture_download_daily_diff",
            )
            if download_daily_btn:
                try:
                    download_dir = _download_dir_from_state("prefecture", save_dir)
                    secrets = _streamlit_secrets_or_empty()
                    repo = PostgresRaaDailyRepository(postgres_config_from_env(secrets))
                    with st.spinner(f"Downloading {len(new_documents)} new RAA PDF(s) to {download_dir} ..."):
                        daily_download_result = download_daily_raa_diff(
                            destination=download_dir,
                            repo=repo,
                            run_id=int(daily_result.get("run_id", 0)),
                            documents=new_documents,
                            timeout=timeout,
                        )
                    st.session_state["prefecture_daily_download_result"] = daily_download_result
                except Exception as e:
                    st.error(f"Daily diff download failed: {e}")
                    return
            daily_download_result = st.session_state.get("prefecture_daily_download_result") or {}
            if daily_download_result:
                st.success(
                    f"Downloaded {daily_download_result.get('downloaded', 0)} "
                    f"of {daily_download_result.get('documents_selected_for_download', 0)} daily diff PDF(s); "
                    f"{daily_download_result.get('failed', 0)} failed."
                )
                st.dataframe(
                    pd.DataFrame(
                        [
                            {"stage": "database register downloads", "seconds": daily_download_result.get("db_seconds", 0)},
                            {"stage": "download files", "seconds": daily_download_result.get("download_seconds", 0)},
                            {"stage": "excel report", "seconds": daily_download_result.get("excel_seconds", 0)},
                            {"stage": "total download click", "seconds": daily_download_result.get("total_seconds", 0)},
                        ]
                    ),
                    width="stretch",
                    height=180,
                )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "region": document.get("region", ""),
                            "year": document.get("year", ""),
                            "month": document.get("month", ""),
                            "publication_date": document.get("publication_date", ""),
                            "file": document.get("title", ""),
                            "pdf_url": document.get("url", ""),
                        }
                        for document in new_documents
                    ]
                ),
                width="stretch",
                height=260,
            )

    discovery_result = st.session_state.get("prefecture_discovery_result")
    download_result = st.session_state.get("prefecture_download_result")
    if not discovery_result:
        _render_daily_check_section()
        if source_changed_with_result:
            st.caption("URL changed. Run the prefecture workflow again to scan the new source.")
        return
    if normalized_url and st.session_state.get("prefecture_result_source") != normalized_url:
        st.caption("URL changed. Run the prefecture workflow again to scan the new source.")
        return

    if discovery_result.get("pdfs_discovered", 0):
        #st.caption("Discovery results are ready. Review the tables below, then download selected PDFs when ready.")
        download_btn = st.button("download", type="primary", width="stretch", key="prefecture_download_selected")
        if download_btn:
            try:
                download_dir = _download_dir_from_state("prefecture", save_dir)
                with st.spinner(f"Downloading selected RAA PDFs to {download_dir} ..."):
                    download_result = download_prefecture_raa_pdfs(
                        discovery_result,
                        download_dir,
                        timeout,
                        None,
                        int(download_cap_per_region),
                    )
                download_result = {
                    **download_result,
                    "regions_discovered": discovery_result["regions_discovered"],
                    "raa_pages_found": discovery_result["raa_pages_found"],
                    "pdfs_discovered": discovery_result["pdfs_discovered"],
                    "pdfs_in_date_window": download_result.get("pdfs_in_date_window", discovery_result.get("pdfs_in_date_window", 0)),
                    "unknown_date_pdfs": download_result.get("unknown_date_pdfs", discovery_result.get("unknown_date_pdfs", 0)),
                    "date_filter_days": discovery_result.get("date_filter_days"),
                    "date_filter_start": discovery_result.get("date_filter_start", ""),
                    "date_filter_end": discovery_result.get("date_filter_end", ""),
                    "pdfs_attempted": download_result["attempted"],
                    "pdfs_downloaded_successfully": download_result["downloaded"],
                    "failed_downloads": download_result["failed"],
                    "skipped_or_duplicate_downloads": download_result["skipped_duplicates"],
                    "duplicate_pdf_links_skipped": download_result.get("duplicate_pdf_links_skipped", 0),
                    "skipped_by_region_limit": download_result.get("skipped_by_region_limit", 0),
                    "skipped_by_date_filter": download_result.get("skipped_by_date_filter", 0),
                }
                st.session_state["prefecture_download_result"] = download_result
                st.success(
                    f"downloaded {download_result.get('pdfs_downloaded_successfully', 0)} "
                    f"of {download_result.get('pdfs_attempted', 0)}"
                )
            except Exception as e:
                st.error(f"Prefecture download failed: {e}")
                return

    _render_daily_check_section()

    workflow_result = download_result or {
        **discovery_result,
        "downloads": [],
        "selected_documents": [],
        "max_downloads_per_region": int(download_cap_per_region),
        "documents_selected_for_download": 0,
        "pdfs_attempted": 0,
        "pdfs_downloaded_successfully": 0,
        "failed_downloads": 0,
        "skipped_or_duplicate_downloads": discovery_result.get("skipped_duplicates", 0),
        "duplicate_pdf_links_skipped": discovery_result.get("skipped_duplicates", 0),
        "skipped_by_region_limit": 0,
        "skipped_by_date_filter": 0,
    }
    downloads = workflow_result.get("downloads", [])
    pdf_links = workflow_result.get("pdf_links", [])
    discovered_documents = workflow_result.get("documents", [])
    selected_documents = workflow_result.get("selected_documents", [])
    region_download_stats = {}
    for document, download in zip(selected_documents, downloads):
        region = document.get("region", "") or "unknown-region"
        stats = region_download_stats.setdefault(region, {"selected": 0, "downloaded": 0, "failed": 0})
        stats["selected"] += 1
        if download.get("status") == "downloaded":
            stats["downloaded"] += 1
        elif str(download.get("status", "")).startswith("failed:"):
            stats["failed"] += 1
    documents_pages_resolved = sum(
        1 for region_result in workflow_result.get("region_results", []) if region_result.get("documents_publications_url")
    )
    raa_entry_count = sum(
        len(region_result.get("raa_entry_urls", []))
        for region_result in workflow_result.get("region_results", [])
    )
    region_rows = [
        {
            "region": region_result.get("region", ""),
            "region_url": region_result.get("region_url", ""),
            "has_raa_pdfs": "yes" if region_result.get("has_raa_pdfs") else "no",
            "available_years": ", ".join(region_result.get("available_years", [])),
            "documents_publications_url": region_result.get("documents_publications_url", ""),
            "raa_entry_urls": "\n".join(region_result.get("raa_entry_urls", [])),
            "seconds": region_result.get("elapsed_s", 0),
            "pages_scanned": len(region_result.get("pages_scanned", [])),
            "raa_pages_considered": region_result.get("raa_pages_considered", 0),
            #"raa_pages_extracted": region_result.get("raa_pages_extracted", 0),
            "raa_pages_with_pdfs": len(region_result.get("raa_pages", [])),
            "pdfs_found": len(region_result.get("pdf_links", [])),
            #"pdfs_in_date_window": region_result.get("pdfs_in_date_window", 0),
            #"unknown_date_pdfs": region_result.get("unknown_date_pdfs", 0),
            #"pdfs_selected": region_download_stats.get(region_result.get("region", ""), {}).get("selected", 0),
            #"pdfs_downloaded": region_download_stats.get(region_result.get("region", ""), {}).get("downloaded", 0),
            #"pdfs_failed": region_download_stats.get(region_result.get("region", ""), {}).get("failed", 0),
            #"errors": "; ".join(region_result.get("errors", [])),
        }
        for region_result in workflow_result.get("region_results", [])
    ]
    availability_rows = [
        {
            "region": region_result.get("region", ""),
            "raa_pdfs_available": "yes" if region_result.get("has_raa_pdfs") else "no",
            "available_years": ", ".join(region_result.get("available_years", [])) or "-",
            "pdfs_found": len(region_result.get("pdf_links", [])),
            #"pdfs_in_date_window": region_result.get("pdfs_in_date_window", 0),
            #"unknown_date_pdfs": region_result.get("unknown_date_pdfs", 0),
            "seconds": region_result.get("elapsed_s", 0),
            #"errors": "; ".join(region_result.get("errors", [])[:2]),
        }
        for region_result in workflow_result.get("region_results", [])
    ]
    discovered_rows = [
        {
            "region": document.get("region", ""),
            "year": document.get("year", ""),
            "month": document.get("month", "") or "-",
            "publication_date": document.get("publication_date", ""),
            #"date_source": document.get("date_source", "unknown"),
            #"inside_date_window": bool(document.get("is_within_date_window")),
            "file": document.get("title", ""),
            "source_page": document.get("source_page", ""),
            "pdf_url": document.get("url", ""),
        }
        for document in discovered_documents
    ]
    month_rows = (
        pd.DataFrame(discovered_rows)
        .groupby(["region", "year", "month"], dropna=False)
        .size()
        .reset_index(name="pdfs_found")
        .sort_values(["region", "year", "month"], ascending=[True, False, True])
        if discovered_rows
        else pd.DataFrame(columns=["region", "year", "month", "pdfs_found"])
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Regions", workflow_result.get("regions_discovered", 0))
    #c4.metric("Docs Pages", documents_pages_resolved)
    #c3.metric("RAA Entries", raa_entry_count)
    c3.metric("RAA PDF Pages", workflow_result.get("raa_pages_found", 0))
    c4.metric("Search seconds", workflow_result.get("search_seconds", 0))
    c5, c6, c7, c8 = st.columns(4)
    c2.metric("Archive PDFs Found", workflow_result.get("pdfs_discovered", 0))
    # c6.metric("PDFs In Date Window", workflow_result.get("pdfs_in_date_window", 0))
    # c7.metric("Unknown-Date PDFs", workflow_result.get("unknown_date_pdfs", 0))
    # c8.metric("Downloaded", workflow_result.get("pdfs_downloaded_successfully", 0))

    # if workflow_result.get("date_filter_start") and workflow_result.get("date_filter_end"):
    #     st.caption(
    #         "Date filter uses RAA publication dates parsed from link text, headings, URLs, and filenames. "
    #         f"Current window: {workflow_result.get('date_filter_start')} to {workflow_result.get('date_filter_end')}. "
    #         "Unknown-date PDFs are excluded from download selection."
    #     )
    # else:
    #     st.caption(
    #         "Date filtering is disabled. RAA publication dates are still parsed from link text, headings, URLs, and filenames when available."
    #     )

#   #  st.caption(
#         f"Resolved {documents_pages_resolved} Documents & publications page(s), "
#         f"matched {raa_entry_count} RAA entry page(s), "
#         f"found {workflow_result.get('raa_pages_found', 0)} RAA page(s) with PDFs, "
#         f"discovered {workflow_result.get('pdfs_discovered', 0)} PDF(s), "
#         f"selected {workflow_result.get('documents_selected_for_download', 0)} for download "
#         f"(cap per region: {workflow_result.get('max_downloads_per_region', 0)}), "
#         f"then attempted {workflow_result.get('pdfs_attempted', 0)} download(s); "
#         f"failed {workflow_result.get('failed_downloads', 0)}; "
#         f"duplicate PDF links skipped {workflow_result.get('duplicate_pdf_links_skipped', 0)}; "
#         f"skipped by per-region cap {workflow_result.get('skipped_by_region_limit', 0)}."
#     )
    #st.caption("Files are stored as `prefecture_raa_downloads/<region>/<year>/<pdf_file>`.")

    if availability_rows:
        with st.expander("Region and year availability", expanded=True):
            st.dataframe(pd.DataFrame(availability_rows), width="stretch", height=220)
    if not month_rows.empty:
        with st.expander("Region, year, and month availability"):
            st.dataframe(month_rows, width="stretch", height=240)

    if region_rows:
        with st.expander("Region results", expanded=True):
            st.dataframe(pd.DataFrame(region_rows), width="stretch", height=260)

    if discovered_rows:
        with st.expander("Discovered PDFs", expanded=True):
            st.dataframe(pd.DataFrame(discovered_rows), width="stretch", height=320)

    if not pdf_links:
        st.warning(
            "No RAA PDFs were discovered after scanning Documents & publications, "
            "RAA tag/direct pages, and intermediate year pages for that prefecture source."
        )
        return

    if not downloads:
        #st.warning("no download yet")
        return

    st.dataframe(pd.DataFrame(downloads), width="stretch", height=320)
    failed_count = workflow_result.get("failed_downloads", 0)
    if failed_count:
        st.warning(f"{failed_count} failed to download")
    # st.success(
    #     f"Discovered {workflow_result.get('pdfs_discovered', 0)} prefecture PDF(s); "
    #     f"{workflow_result.get('pdfs_in_date_window', 0)} inside the date window; "
    #     f"attempted {workflow_result.get('pdfs_attempted', 0)} download(s); "
    #     f"saved {workflow_result.get('pdfs_downloaded_successfully', 0)} file(s) under `{save_dir.resolve()}`."
    # )


st.title("demoweb")

with st.sidebar:
    st.header("Settings")
    mode = st.radio("Mode", ["scrape", "scrape and crawl"])
    crawl_depth, max_pages, timeout = 3, 20, 40

scrape_tab, prefecture_tab, pdf_tab = st.tabs(["Scraper", "RAA", "PDF Downloader"])

with scrape_tab:
    render_scraper_tab(mode, crawl_depth, max_pages, timeout)

with prefecture_tab:
    render_prefecture_tab(timeout)

with pdf_tab:
    render_pdf_tab(timeout)

st.stop()

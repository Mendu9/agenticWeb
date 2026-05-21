from pathlib import Path
import asyncio
import sys
import types
from uuid import uuid4

from pdf_downloader import (
    build_pdf_download_dir,
    collect_file_links,
    collect_pdf_links,
    dedupe_links,
    discover_file_links,
    discover_file_links_with_strategies,
    discover_pdf_links,
    discover_pdf_links_from_preload_dump,
    download_document_files,
    download_pdf_files,
    fetch_doclib_document_links,
    is_relevant_discovery_page,
    same_path_family,
    select_documents_for_download,
    summarize_downloads,
)


def _local_test_dir(name: str) -> Path:
    path = Path(".codex_tmp_test") / f"{name}_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_discover_pdf_links_normalizes_and_deduplicates():
    html = """
    <html>
      <body>
        <a href="/docs/a.pdf">A</a>
        <a href="https://example.com/docs/a.pdf#page=2">A duplicate</a>
        <a href="brochure.PDF">B</a>
        <a href="/docs/not-a-doc">Ignore</a>
      </body>
    </html>
    """

    links = discover_pdf_links("https://example.com/products", html)

    assert links == [
        "https://example.com/docs/a.pdf",
        "https://example.com/brochure.PDF",
    ]


def test_build_pdf_download_dir_uses_domain_and_path():
    target = build_pdf_download_dir("https://example.com/library/reports")
    assert target == Path("downloads/pdfs/example.com/library_reports")


def test_collect_pdf_links_deduplicates_across_multiple_sources():
    links = collect_pdf_links(
        "https://example.com/start",
        [
            "/files/a.pdf",
            "https://example.com/files/a.pdf#page=2",
            "/files/b.pdf",
            "/files/ignore.html",
        ],
    )

    assert links == [
        "https://example.com/files/a.pdf",
        "https://example.com/files/b.pdf",
    ]


def test_collect_file_links_accepts_query_string_file_urls_and_rejects_invalid_links():
    links = collect_file_links(
        "https://example.com/products/index.html",
        [
            "/download?file=guide.pdf",
            "/download?asset=manual.DOCX#section",
            "https://cdn.example.net/image?id=hero.png",
            "javascript:void(0)",
            "mailto:support@example.com",
            "not-a-file",
        ],
    )

    assert links == [
        "https://example.com/download?file=guide.pdf",
        "https://example.com/download?asset=manual.DOCX",
        "https://cdn.example.net/image?id=hero.png",
    ]


def test_same_path_family_blocks_redirected_homepage_but_allows_listing_children():
    source = "https://example.com/tags/view/Centre-Val+de+Loire/Documents+et+publications"

    assert same_path_family(
        "https://example.com/tags/view/Centre-Val+de+Loire/Documents+et+publications/(offset)/10",
        source,
    )
    assert same_path_family(
        "https://example.com/tags/view/Centre-Val+de+Loire/Documents+et+publications/Arrete",
        source,
    )
    assert not same_path_family("https://example.com/", source)
    assert not same_path_family(
        "https://other.example.com/tags/view/Centre-Val+de+Loire/Documents+et+publications",
        source,
    )


def test_relevant_discovery_page_allows_document_detail_pages_but_blocks_homepage():
    source = "https://example.com/tags/view/Centre-Val+de+Loire/Documents+et+publications"

    assert is_relevant_discovery_page(
        "https://example.com/centre-val-de-loire/Documents-publications/Recueil-des-Actes-Administratifs-2026",
        source,
    )
    assert is_relevant_discovery_page(
        "https://example.com/centre-val-de-loire/Documents-publications/Contrat-de-Plan-Etat-Region",
        source,
    )
    assert not is_relevant_discovery_page("https://example.com/", source)
    assert not is_relevant_discovery_page("https://example.com/careers", source)
    assert not is_relevant_discovery_page(
        "https://other.example.com/Documents-publications/Recueil-des-Actes-Administratifs-2026",
        source,
    )


def test_discover_file_links_includes_documents_and_images():
    html = """
    <html>
      <body>
        <a href="/docs/a.pdf">PDF</a>
        <a href="/docs/b.docx">DOCX</a>
        <a href="/docs/c.xlsx">XLSX</a>
        <img src="/images/photo.png" />
        <a href="/page.html">HTML page</a>
      </body>
    </html>
    """

    links = discover_file_links("https://example.com/start", html)

    assert links == [
        "https://example.com/docs/a.pdf",
        "https://example.com/docs/b.docx",
        "https://example.com/docs/c.xlsx",
        "https://example.com/images/photo.png",
    ]


def test_discover_file_links_includes_image_srcset_candidates():
    html = """
    <html>
      <body>
        <picture>
          <source srcset="/images/wide.webp 1x, https://img.example.net/wide@2x.webp 2x" />
          <img src="/images/fallback.jpg" srcset="/images/small.png 400w, /images/large.png 800w" />
        </picture>
      </body>
    </html>
    """

    links = discover_file_links("https://example.com/gallery/", html)

    assert links == [
        "https://example.com/images/fallback.jpg",
        "https://example.com/images/wide.webp",
        "https://img.example.net/wide@2x.webp",
        "https://example.com/images/small.png",
        "https://example.com/images/large.png",
    ]


def test_discover_file_links_with_strategies_merges_extra_candidates_and_dedupes_fragments():
    html = """
    <html>
      <body>
        <a href="/files/a.pdf#first">A</a>
        <img src="/images/chart.svg" />
      </body>
    </html>
    """

    links = discover_file_links_with_strategies(
        "https://example.com/root/page",
        html,
        extra_candidates=[
            "/files/a.pdf#second",
            "https://assets.example.org/download?name=report.xlsx",
            "ftp://example.com/ignored.pdf",
        ],
    )

    assert links == [
        "https://example.com/files/a.pdf",
        "https://example.com/images/chart.svg",
        "https://assets.example.org/download?name=report.xlsx",
    ]


def test_dedupe_links_keeps_non_pdf_document_actions():
    links = dedupe_links(
        "https://doclib.siemens-healthineers.com/documents?countries=78",
        [
            "/download?document-ids=123",
            "https://doclib.siemens-healthineers.com/download?document-ids=123#fragment",
            "/view?document-id=123",
        ],
    )

    assert links == [
        "https://doclib.siemens-healthineers.com/download?document-ids=123",
        "https://doclib.siemens-healthineers.com/view?document-id=123",
    ]


def _load_scraper_app_definitions(monkeypatch):
    fake_streamlit = types.SimpleNamespace(
        set_page_config=lambda **kwargs: None,
        error=lambda message: None,
        stop=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "streamlit", fake_streamlit)

    source = Path("scraper_app.py").read_text(encoding="utf-8")
    definitions_only = source.split('\nst.title("demoweb")', 1)[0]
    module = types.ModuleType("scraper_app_under_test")
    module.__dict__["__file__"] = str(Path("scraper_app.py").resolve())
    exec(compile(definitions_only, "scraper_app.py", "exec"), module.__dict__)
    return module


class _FakeScraper:
    def __init__(self, page):
        self.page = page

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def fetch(self, url):
        return self.page


def test_do_discover_pdfs_aggregates_start_and_child_pages_without_downloads(monkeypatch):
    app = _load_scraper_app_definitions(monkeypatch)
    start_page = types.SimpleNamespace(
        url="https://example.com/start",
        title="Start",
        links=[
            "/files/from-start.pdf#page=2",
            "https://external.example.org/spec.docx",
            "javascript:void(0)",
        ],
        html="",
        markdown="",
    )
    child_page = types.SimpleNamespace(
        url="https://example.com/start/child",
        title="Child",
        links=[
            "/download?file=child.pdf#duplicate",
            "/download?file=child.pdf#again",
            "mailto:docs@example.com",
        ],
        html="""
        <html>
          <body>
            <a href="/download?file=child.pdf">Child PDF</a>
            <img src="/images/diagram.png" />
            <img srcset="/images/thumb.webp 1x, https://cdn.example.net/full.webp 2x" />
          </body>
        </html>
        """,
        markdown="",
    )
    document_detail_page = types.SimpleNamespace(
        url="https://example.com/Documents-publications/report-page",
        title="Document detail",
        links=["/files/detail.pdf"],
        html='<html><body><a href="/files/detail.pdf">Detail PDF</a></body></html>',
        markdown="",
    )
    homepage = types.SimpleNamespace(
        url="https://example.com/",
        title="Home",
        links=["/images/home.png"],
        html='<html><body><img src="/images/home.png" /></body></html>',
        markdown="",
    )

    monkeypatch.setattr(
        app,
        "fetch_file_links",
        lambda url, timeout: {
            "file_links": [
                "https://example.com/download?file=start-query.pdf",
                "https://example.com/files/from-start.pdf#intro",
            ]
        },
    )
    monkeypatch.setattr(app, "FireScraper", lambda timeout: _FakeScraper(start_page))

    captured_crawl_kwargs = {}

    async def fake_crawl_focused_tree(**kwargs):
        captured_crawl_kwargs.update(kwargs)
        return [start_page, child_page, document_detail_page, homepage]

    monkeypatch.setattr(app, "crawl_focused_tree", fake_crawl_focused_tree)

    result = asyncio.run(app.do_discover_pdfs("https://example.com/start", depth=1, max_pages=3, timeout=5))

    assert result["file_links"] == [
        "https://example.com/download?file=start-query.pdf",
        "https://example.com/files/from-start.pdf",
        "https://external.example.org/spec.docx",
        "https://example.com/download?file=child.pdf",
        "https://example.com/images/diagram.png",
        "https://example.com/images/thumb.webp",
        "https://cdn.example.net/full.webp",
        "https://example.com/files/detail.pdf",
    ]
    assert result["pdf_links"] == [
        "https://example.com/download?file=start-query.pdf",
        "https://example.com/files/from-start.pdf",
        "https://example.com/download?file=child.pdf",
        "https://example.com/files/detail.pdf",
    ]
    assert [page["url"] for page in result["pages_scanned"]] == [
        "https://example.com/start",
        "https://example.com/start/child",
        "https://example.com/Documents-publications/report-page",
    ]
    assert [page["files_found"] for page in result["pages_scanned"]] == [3, 4, 1]
    assert [page["pdf_links_found"] for page in result["pages_scanned"]] == [2, 1, 1]
    assert result["documents_discovered"] == 8
    assert "*[Dd]ocument*" in captured_crawl_kwargs["include_patterns"]


class _FakeResponse:
    def __init__(self, content: bytes, headers=None, json_payload=None):
        self._content = content
        self.headers = headers or {}
        self._json_payload = json_payload
        self.text = content.decode("utf-8") if isinstance(content, bytes) else content

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=8192):
        yield self._content

    def json(self):
        return self._json_payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, timeout=0, stream=False, headers=None, params=None):
        self.calls.append({"url": url, "params": params or {}, "stream": stream, "headers": headers or {}})
        if params:
            page = params.get("page")
            if (url, page) in self.responses:
                return self.responses[(url, page)]
        return self.responses[url]


def test_download_pdf_files_saves_with_unique_names():
    target_dir = _local_test_dir("pdf_unique_names")
    session = _FakeSession(
        {
            "https://example.com/a.pdf": _FakeResponse(b"first", {"Content-Disposition": 'attachment; filename="report.pdf"'}),
            "https://example.com/b.pdf": _FakeResponse(b"second", {"Content-Disposition": 'attachment; filename="report.pdf"'}),
        }
    )

    result = download_pdf_files(
        ["https://example.com/a.pdf", "https://example.com/b.pdf"],
        target_dir,
        session=session,
    )

    assert [item["filename"] for item in result] == ["report.pdf", "report_1.pdf"]
    assert (target_dir / "report.pdf").read_bytes() == b"first"
    assert (target_dir / "report_1.pdf").read_bytes() == b"second"


def test_discover_pdf_links_from_preload_dump_recovers_hidden_pdf_urls():
    html = '<html><head><script src="/assets/preloadDumps/example.js"></script></head></html>'
    preload = (
        'window.preloadDump = "{\\"recording\\":[['
        '\\"baseobj\\",[[\\"workspace\\",\\"published\\"],\\"item1\\"],{'
        '\\"_obj_class\\":\\"DownloadItem\\",'
        '\\"_path\\":\\"/dawn_country/corporate/en/education/clinical-specialty-educational-resources/abc\\",'
        '\\"link\\":[\\"link\\",{\\"obj_id\\":\\"download1\\"}]'
        '}],['
        '\\"baseobj\\",[[\\"workspace\\",\\"published\\"],\\"download1\\"],{'
        '\\"_obj_class\\":\\"Download\\",'
        '\\"blob\\":[\\"binary\\",{\\"id\\":\\"asset123/hash456/file.pdf\\"}]'
        '}] ]}";'
    )
    session = _FakeSession({"https://example.com/assets/preloadDumps/example.js": _FakeResponse(preload)})

    links = discover_pdf_links_from_preload_dump(
        "https://example.com/education/clinical-specialty-educational-resources",
        html,
        session=session,
    )

    assert links == ["https://marketing.webassets.siemens-healthineers.com/asset123/hash456/file.pdf"]


def _doclib_record(document_id, file_format="PDF", accessible=True, blocked=False, title=""):
    return {
        "id": document_id,
        "attributes": {
            "fileFormat": file_format,
            "accessible": accessible,
            "blockedNotOwner": blocked,
            "title": title or f"Document {document_id}",
        },
    }


def test_doclib_discovery_tracks_source_total_scan_window_and_skips():
    endpoint = "https://doclib.siemens-healthineers.com/rest/v1/documents"
    session = _FakeSession(
        {
            (endpoint, "1"): _FakeResponse(
                b"",
                json_payload={
                    "meta": {"total": 999},
                    "data": [
                        _doclib_record("1", title="First"),
                        _doclib_record("2", file_format="DOCX"),
                        _doclib_record("3", accessible=False),
                        _doclib_record("4", blocked=True),
                        _doclib_record("1", title="Duplicate"),
                    ],
                },
            )
        }
    )

    result = fetch_doclib_document_links(
        "https://doclib.siemens-healthineers.com/documents?productgroups=1&countries=78&sortingby=-modified-at",
        max_results=5,
        session=session,
    )

    assert result["discovery_method"] == "doclib_listing"
    assert result["listing_source"] == "doclib_rest_listing_current_implementation"
    assert result["total_available_documents"] == 999
    assert result["scan_limit"] == 5
    assert result["pages_or_results_scanned"] == 5
    assert result["documents_discovered"] == 2
    assert result["documents_selected_for_download"] == 0
    assert result["skipped_or_deduplicated_documents"] == 3
    assert result["pdf_links"] == ["https://doclib.siemens-healthineers.com/download?document-ids=1"]
    assert result["file_links"] == [
        "https://doclib.siemens-healthineers.com/download?document-ids=1",
        "https://doclib.siemens-healthineers.com/download?document-ids=2",
    ]
    assert [document["title"] for document in result["documents"]] == ["First", "Document 2"]
    assert session.calls[0]["params"]["product-groups"] == "1"
    assert session.calls[0]["params"]["countries"] == "78"
    assert session.calls[0]["params"]["sort"] == "-modified-at"


def test_doclib_scan_limit_is_not_reported_as_total_available():
    endpoint = "https://doclib.siemens-healthineers.com/rest/v1/documents"
    session = _FakeSession(
        {
            (endpoint, "1"): _FakeResponse(
                b"",
                json_payload={"meta": {"total": 321}, "data": [_doclib_record(str(i)) for i in range(1, 41)]},
            ),
            (endpoint, "2"): _FakeResponse(
                b"",
                json_payload={"meta": {"total": 321}, "data": [_doclib_record(str(i)) for i in range(41, 81)]},
            ),
        }
    )

    result = fetch_doclib_document_links(
        "https://doclib.siemens-healthineers.com/documents?productgroups=1",
        max_results=50,
        session=session,
    )

    assert result["total_available_documents"] == 321
    assert result["scan_limit"] == 50
    assert result["pages_or_results_scanned"] == 50
    assert result["documents_discovered"] == 50
    assert result["total_available_documents"] != result["scan_limit"]
    assert len(result["pages_scanned"]) == 2


def test_doclib_discovery_tolerates_malformed_records_and_missing_metadata():
    endpoint = "https://doclib.siemens-healthineers.com/rest/v1/documents"
    session = _FakeSession(
        {
            (endpoint, "1"): _FakeResponse(
                b"",
                json_payload={
                    "meta": None,
                    "data": [
                        None,
                        {"id": "missing-attributes"},
                        {"id": "bad-attributes", "attributes": None},
                        _doclib_record("good", file_format="DOCX", title="Usable"),
                    ],
                },
            )
        }
    )

    result = fetch_doclib_document_links(
        "https://doclib.siemens-healthineers.com/documents?productgroups=1",
        max_results=4,
        session=session,
    )

    assert result["total_available_documents"] is None
    assert result["scan_limit"] == 4
    assert result["pages_or_results_scanned"] == 4
    assert result["documents_discovered"] == 2
    assert result["skipped_or_deduplicated_documents"] == 2
    assert result["pdf_links"] == []
    assert result["file_links"] == [
        "https://doclib.siemens-healthineers.com/download?document-ids=missing-attributes",
        "https://doclib.siemens-healthineers.com/download?document-ids=good",
    ]


def test_selection_is_separate_from_discovery_and_acquisition_summary():
    discovery = {
        "pdf_links": ["https://example.com/1.pdf", "https://example.com/2.pdf", "https://example.com/3.pdf"],
        "documents": [
            {"url": "https://example.com/1.pdf", "title": "One"},
            {"url": "https://example.com/2.pdf", "title": "Two"},
            {"url": "https://example.com/3.pdf", "title": "Three"},
        ],
        "documents_discovered": 3,
    }

    selected = select_documents_for_download(discovery, limit=2)
    summary = summarize_downloads(
        [
            {"url": "https://example.com/1.pdf", "status": "downloaded"},
            {"url": "https://example.com/2.pdf", "status": "failed: download endpoint returned HTML instead of a PDF file"},
        ],
        selected["documents_selected_for_download"],
    )

    assert selected["documents_discovered"] == 3
    assert selected["documents_selected_for_download"] == 2
    assert selected["selected_pdf_links"] == ["https://example.com/1.pdf", "https://example.com/2.pdf"]
    assert summary == {
        "documents_selected_for_download": 2,
        "download_attempts": 2,
        "documents_downloaded_successfully": 1,
        "failed_downloads": 1,
    }


def test_selection_keeps_pdf_links_separate_from_selected_file_links():
    discovery = {
        "pdf_links": ["https://example.com/1.pdf"],
        "file_links": ["https://example.com/1.pdf", "https://example.com/2.docx"],
        "documents": [
            {"url": "https://example.com/1.pdf", "title": "One", "file_format": "PDF"},
            {"url": "https://example.com/2.docx", "title": "Two", "file_format": "DOCX"},
        ],
        "documents_discovered": 2,
    }

    selected = select_documents_for_download(discovery)

    assert selected["documents_selected_for_download"] == 2
    assert selected["selected_file_links"] == ["https://example.com/1.pdf", "https://example.com/2.docx"]
    assert selected["selected_pdf_links"] == ["https://example.com/1.pdf"]


def test_download_pdf_files_reports_mixed_failures_and_existing_collision():
    target_dir = _local_test_dir("pdf_mixed_failures")
    (target_dir / "report.pdf").write_bytes(b"existing")
    session = _FakeSession(
        {
            "https://example.com/a.pdf": _FakeResponse(
                b"first",
                {"Content-Disposition": 'attachment; filename="report.pdf"', "Content-Type": "application/pdf"},
            ),
            "https://example.com/login": _FakeResponse(b"<html>login</html>", {"Content-Type": "text/html"}),
        }
    )

    result = download_pdf_files(["https://example.com/a.pdf", "https://example.com/login"], target_dir, session=session)

    assert result[0]["status"] == "downloaded"
    assert result[0]["filename"] == "report_1.pdf"
    assert result[1]["status"].startswith("failed:")
    assert "returned HTML instead of a downloadable file" in result[1]["status"]


def test_download_document_files_groups_documents_and_images():
    target_dir = _local_test_dir("document_grouped_folders")
    session = _FakeSession(
        {
            "https://example.com/a.docx": _FakeResponse(
                b"docx",
                {
                    "Content-Disposition": 'attachment; filename="manual.docx"',
                    "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                },
            ),
            "https://example.com/image.png": _FakeResponse(
                b"png",
                {"Content-Disposition": 'attachment; filename="image.png"', "Content-Type": "image/png"},
            ),
        }
    )

    result = download_document_files(
        [
            {"id": "doc-a", "title": "Manual", "url": "https://example.com/a.docx", "file_format": "DOCX"},
            {"id": "img-b", "title": "Image", "url": "https://example.com/image.png", "file_format": "PNG"},
        ],
        target_dir,
        session=session,
    )

    assert [item["status"] for item in result] == ["downloaded", "downloaded"]
    assert Path(result[0]["folder"]).name == "documents"
    assert Path(result[1]["folder"]).name == "images"
    assert Path(result[0]["file_path"]).read_bytes() == b"docx"
    assert Path(result[1]["file_path"]).read_bytes() == b"png"

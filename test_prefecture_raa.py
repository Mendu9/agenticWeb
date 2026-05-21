from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd

import prefecture_raa as raa_module
from prefecture_raa import (
    discover_prefecture_raa,
    extract_pdf_links_from_raa_pages,
    find_documents_publications_url,
    download_prefecture_raa_pdfs,
    find_raa_entry_urls,
    run_prefecture_raa_workflow,
)
from raa_daily_tracker import create_archive_baseline, current_and_previous_months, download_daily_raa_diff, run_daily_raa_check


def _local_test_dir(name: str) -> Path:
    path = Path(".codex_tmp_test") / f"{name}_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_parse_exact_publication_date_variants():
    cases = {
        "20260429_recueil.pdf": date(2026, 4, 29),
        "RAA n°075 du 07.01.2026_Spécial.pdf": date(2026, 1, 7),
        "recueil regional du 02.01.26.pdf": date(2026, 1, 2),
        "RAA spécial 21012026.pdf": date(2026, 1, 21),
        "Recueil n°180 du 30 avril 2026.pdf": date(2026, 4, 30),
        "RAA du 3 février 2026.pdf": date(2026, 2, 3),
        "RAA du 14 août 2026.pdf": date(2026, 8, 14),
        "RAA du 25 decembre 2026.pdf": date(2026, 12, 25),
    }

    for text, expected in cases.items():
        assert raa_module._parse_exact_publication_date(text) == expected


def test_publication_date_source_precedence_prefers_link_text():
    publication_date, date_source = raa_module._extract_publication_date_with_source(
        ("link_text", "Recueil n°180 du 30 avril 2026"),
        ("filename", "20260401_recueil.pdf"),
        ("source_page", "https://example.test/2026"),
    )

    assert publication_date == "2026-04-30"
    assert date_source == "link_text"


def test_publication_date_unknown_when_only_year_is_available():
    publication_date, date_source = raa_module._extract_publication_date_with_source(
        ("filename", "recueil-r76-2026-189-recueil-des-actes-administratifs.pdf"),
        ("source_page", "https://example.test/recueils-2026"),
    )

    assert publication_date == ""
    assert date_source == "unknown"


class _FakeResponse:
    def __init__(self, text: str = "", content: bytes | None = None, headers=None):
        self.text = text
        self._content = content if content is not None else text.encode("utf-8")
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=8192):
        yield self._content


class _FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, timeout=0, stream=False, headers=None, params=None):
        self.calls.append({"url": url, "stream": stream, "timeout": timeout})
        return self.responses[url]


class _FakeSslRetrySession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, timeout=0, stream=False, headers=None, params=None, verify=True):
        self.calls.append({"url": url, "verify": verify, "timeout": timeout})
        if len(self.calls) == 1:
            raise raa_module.requests.exceptions.SSLError("certificate verify failed")
        return self.response


class _FakeDailyRepo:
    def __init__(self):
        self.documents = {}
        self.downloads = []
        self.runs = []

    def ensure_schema(self):
        return None

    def has_completed_archive_baseline(self):
        return any(run.get("run_type") == "archive_baseline" and run.get("status") == "baseline_created" for run in self.runs)

    def create_run(self, started_at, status, regions_scanned, months_scanned, run_type="daily_diff"):
        run_id = len(self.runs) + 1
        self.runs.append(
            {
                "id": run_id,
                "run_type": run_type,
                "started_at": started_at,
                "status": status,
                "regions_scanned": regions_scanned,
                "months_scanned": months_scanned,
            }
        )
        return run_id

    def finish_run(self, run_id, finished_at, status, pdfs_discovered, new_pdfs, downloaded, failed, error="", timings=None):
        timings = timings or {}
        self.runs[run_id - 1].update(
            {
                "finished_at": finished_at,
                "status": status,
                "pdfs_discovered": pdfs_discovered,
                "new_pdfs": new_pdfs,
                "downloaded": downloaded,
                "failed": failed,
                "error": error,
                **timings,
            }
        )

    def get_known_urls(self, normalized_urls):
        return {url for url in normalized_urls if url in self.documents}

    def upsert_documents(self, documents, run_id, seen_at):
        ids_by_url = {}
        for document in documents:
            normalized_url = raa_module._normalize_url(document["url"])
            if normalized_url not in self.documents:
                self.documents[normalized_url] = {
                    **document,
                    "id": len(self.documents) + 1,
                    "normalized_url": normalized_url,
                    "first_run_id": run_id,
                }
            self.documents[normalized_url]["last_run_id"] = run_id
            ids_by_url[normalized_url] = self.documents[normalized_url]["id"]
        return ids_by_url

    def record_downloads(self, run_id, document_ids_by_url, documents, downloads):
        for document, download in zip(documents, downloads):
            normalized_url = raa_module._normalize_url(document["url"])
            self.downloads.append(
                {
                    "run_id": run_id,
                    "document_id": document_ids_by_url[normalized_url],
                    "status": download.get("status", ""),
                    "saved_path": download.get("file_path", ""),
                }
            )

    def update_run_download_summary(self, run_id, downloaded, failed, timings=None):
        self.runs[run_id - 1]["downloaded"] = downloaded
        self.runs[run_id - 1]["failed"] = failed
        self.runs[run_id - 1].update(timings or {})

    def fetch_new_documents_for_run(self, run_id):
        return [document for document in self.documents.values() if document.get("first_run_id") == run_id]

    def fetch_daily_runs(self):
        return list(self.runs)

    def fetch_documents(self):
        return list(self.documents.values())

    def fetch_downloads(self):
        return list(self.downloads)


def test_discover_prefecture_raa_finds_region_pages_raa_pages_and_pdfs():
    start_url = "https://www.prefectures-regions.gouv.fr/"
    region_root = "https://www.prefectures-regions.gouv.fr/centre-val-de-loire"
    region_url = f"{region_root}/Documents-publications"
    raa_tag_url = "https://www.prefectures-regions.gouv.fr/centre-val-de-loire/tags/view/Centre-Val+de+Loire/Documents+et+publications/Recueil+des+actes+administratifs"
    year_url = f"{region_url}/Recueil-des-Actes-Administratifs/Recueil-des-actes-administratifs-pour-l-annee-2026"
    jan_url = f"{year_url}/Janvier-2026"
    feb_url = f"{year_url}/Fevrier-2026"

    session = _FakeSession(
        {
            start_url: _FakeResponse(
                """
                <html><body>
                  <a href="/centre-val-de-loire">Centre-Val de Loire</a>
                  <a href="/contact">Contact</a>
                </body></html>
                """
            ),
            region_root: _FakeResponse(
                f"""
                <html><body>
                  <a href="{region_url}">Documents & publications</a>
                </body></html>
                """
            ),
            region_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{raa_tag_url}">Recueil des actes administratifs</a>
                  <a href="/other-page">Ignore me</a>
                </body></html>
                """
            ),
            raa_tag_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{year_url}">Recueil des actes administratifs pour l'annee 2026</a>
                  <a href="/centre-val-de-loire/Documents-publications/Budget">Budget</a>
                </body></html>
                """
            ),
            year_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{jan_url}">Janvier 2026</a>
                  <a href="{feb_url}">Fevrier 2026</a>
                  <a href="raa-regional@centre-val-de-loire.pref.gouv.fr">Contact email</a>
                </body></html>
                """
            ),
            jan_url: _FakeResponse(
                """
                <html><body>
                  <a href="/files/raa-jan-001.pdf">Jan 001</a>
                  <a href="/files/raa-jan-001.pdf#page=2">Jan duplicate</a>
                  <a href="/files/raa-jan-002.pdf">Jan 002</a>
                </body></html>
                """
            ),
            feb_url: _FakeResponse(
                """
                <html><body>
                  <a href="/files/raa-feb-001.pdf">Feb 001</a>
                  <a href="/files/notice.html">Notice</a>
                </body></html>
                """
            ),
        }
    )

    result = discover_prefecture_raa(start_url, session=session)

    assert result["region_pages"] == [region_url]
    assert result["raa_pages"] == [jan_url, feb_url]
    assert result["pdf_links"] == [
        "https://www.prefectures-regions.gouv.fr/files/raa-jan-001.pdf",
        "https://www.prefectures-regions.gouv.fr/files/raa-jan-002.pdf",
        "https://www.prefectures-regions.gouv.fr/files/raa-feb-001.pdf",
    ]
    assert result["documents"][0]["region"] == "centre-val-de-loire"
    assert result["documents"][0]["year"] == "2026"
    assert result["documents"][0]["month"] == "January"
    assert result["regions_discovered"] == 1
    assert result["raa_pages_found"] == 2
    assert result["pdfs_discovered"] == 3
    assert result["pages_scanned"] == [region_url, raa_tag_url, year_url, jan_url, feb_url]
    assert not any("@" in call["url"] for call in session.calls)


def test_prefecture_fetch_retries_french_government_ssl_error_without_verification():
    session = _FakeSslRetrySession(_FakeResponse("<html><body>ok</body></html>"))

    html = raa_module._fetch_text("https://www.prefectures-regions.gouv.fr/", 10, session)

    assert html == "<html><body>ok</body></html>"
    assert session.calls == [
        {"url": "https://www.prefectures-regions.gouv.fr/", "verify": True, "timeout": 10},
        {"url": "https://www.prefectures-regions.gouv.fr/", "verify": False, "timeout": 10},
    ]


def test_discover_prefecture_raa_uses_region_url_directly():
    region_url = "https://www.prefectures-regions.gouv.fr/centre-val-de-loire/Documents-publications"
    raa_url = f"{region_url}/Recueil-des-Actes-Administratifs/Recueil-des-actes-administratifs-pour-l-annee-2026"
    session = _FakeSession(
        {
            region_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{raa_url}">Recueil des actes administratifs</a>
                </body></html>
                """
            ),
            raa_url: _FakeResponse(
                """
                <html><body>
                  <a href="/files/raa-2026.pdf">PDF</a>
                </body></html>
                """
            ),
        }
    )

    result = discover_prefecture_raa(region_url, session=session)

    assert result["region_pages"] == [region_url]
    assert result["raa_pages"] == [raa_url]
    assert result["pdf_links"] == ["https://www.prefectures-regions.gouv.fr/files/raa-2026.pdf"]
    assert result["documents"][0]["year"] == "2026"


def test_discover_prefecture_raa_handles_regional_raa_page_variant():
    region_root = "https://www.prefectures-regions.gouv.fr/auvergne-rhone-alpes"
    documents_url = f"{region_root}/Documents-publications"
    generated_tag_url = f"{region_root}/tags/view/Auvergne-Rhone-Alpes/Documents+et+publications/Recueil+des+actes+administratifs"
    regional_raa_url = f"{documents_url}/Recueil-regional-des-actes-administratifs-RAA"
    pdf_url = "https://www.prefectures-regions.gouv.fr/auvergne-rhone-alpes/irecontenu/telechargement/137331/1003403/file/raa-regional-special.pdf"
    session = _FakeSession(
        {
            region_root: _FakeResponse(f'<html><body><a href="{documents_url}">Documents & publications</a></body></html>'),
            documents_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{generated_tag_url}">Recueil des actes administratifs</a>
                  <a href="{regional_raa_url}">Recueil régional des actes administratifs (RAA)</a>
                </body></html>
                """
            ),
            generated_tag_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{regional_raa_url}">Recueil régional des actes administratifs (RAA)</a>
                </body></html>
                """
            ),
            regional_raa_url: _FakeResponse(
                f"""
                <html><body>
                  <h4>2026</h4>
                  <h4>Avril</h4>
                  <a href="{pdf_url}">RAA régional spécial PDF</a>
                </body></html>
                """
            ),
        }
    )

    result = discover_prefecture_raa(region_root, session=session)

    assert regional_raa_url in result["raa_pages"]
    assert result["pdf_links"] == [pdf_url]
    assert result["documents"][0]["region"] == "auvergne-rhone-alpes"
    assert result["documents"][0]["year"] == "2026"
    assert result["documents"][0]["month"] == "April"
    assert result["region_results"][0]["available_years"] == ["2026"]


def test_run_prefecture_raa_workflow_crawls_generic_external_raa_branch_under_region_year():
    region_root = "https://www.prefectures-regions.gouv.fr/occitanie"
    documents_url = f"{region_root}/Documents-publications"
    same_domain_raa_url = f"{documents_url}/Recueil-des-actes-administratifs"
    external_collection_url = "https://www.herault.gouv.fr/Publications/RAA/Recueil-des-actes-administratifs"
    external_year_url = f"{external_collection_url}/RAA-2026"
    pdf_1_url = "https://www.herault.gouv.fr/contenu/telechargement/12345/67890/file/raa-2026-001.pdf"
    pdf_2_url = "https://www.herault.gouv.fr/contenu/telechargement/12346/67891/file/raa-2026-002.pdf"
    target_dir = _local_test_dir("prefecture_raa_external_branch")
    session = _FakeSession(
        {
            region_root: _FakeResponse(f'<html><body><a href="{documents_url}">Documents & publications</a></body></html>'),
            documents_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{same_domain_raa_url}">Recueil des actes administratifs</a>
                </body></html>
                """
            ),
            same_domain_raa_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{external_collection_url}">Recueil des actes administratifs - prefecture de l'Herault</a>
                </body></html>
                """
            ),
            external_collection_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{external_year_url}">Recueil des actes administratifs 2026</a>
                </body></html>
                """
            ),
            external_year_url: _FakeResponse(
                f"""
                <html><body>
                  <h2>2026</h2>
                  <a href="{pdf_1_url}">RAA 2026 numero 001</a>
                  <a href="{pdf_2_url}">RAA 2026 numero 002</a>
                </body></html>
                """
            ),
            pdf_1_url: _FakeResponse(
                content=b"external-raa-001",
                headers={
                    "Content-Disposition": 'attachment; filename="raa-2026-001.pdf"',
                    "Content-Type": "application/pdf",
                },
            ),
            pdf_2_url: _FakeResponse(
                content=b"external-raa-002",
                headers={
                    "Content-Disposition": 'attachment; filename="raa-2026-002.pdf"',
                    "Content-Type": "application/pdf",
                },
            ),
        }
    )

    result = run_prefecture_raa_workflow(region_root, destination=target_dir, session=session)

    assert result["region_pages"] == [documents_url]
    assert result["raa_pages"] == [external_year_url]
    assert result["pdf_links"] == [pdf_1_url, pdf_2_url]
    assert [(document["region"], document["year"]) for document in result["documents"]] == [
        ("occitanie", "2026"),
        ("occitanie", "2026"),
    ]
    assert result["downloaded"] == 2
    assert (target_dir / "occitanie" / "2026" / "raa-2026-001.pdf").read_bytes() == b"external-raa-001"
    assert (target_dir / "occitanie" / "2026" / "raa-2026-002.pdf").read_bytes() == b"external-raa-002"


def test_discover_prefecture_raa_ignores_non_raa_external_and_social_links():
    region_root = "https://www.prefectures-regions.gouv.fr/occitanie"
    documents_url = f"{region_root}/Documents-publications"
    same_domain_raa_url = f"{documents_url}/Recueil-des-actes-administratifs-2026"
    pdf_url = "https://www.prefectures-regions.gouv.fr/files/occitanie-raa-2026.pdf"
    ignored_urls = [
        "https://www.facebook.com/prefetoccitanie",
        "https://twitter.com/prefetoccitanie",
        "https://www.linkedin.com/company/prefet-occitanie",
        "https://www.service-public.fr/particuliers/actualites",
    ]
    session = _FakeSession(
        {
            region_root: _FakeResponse(f'<html><body><a href="{documents_url}">Documents & publications</a></body></html>'),
            documents_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{same_domain_raa_url}">Recueil des actes administratifs 2026</a>
                  <a href="{ignored_urls[0]}">Facebook</a>
                  <a href="{ignored_urls[1]}">Twitter</a>
                  <a href="{ignored_urls[2]}">LinkedIn</a>
                  <a href="{ignored_urls[3]}">Demarches administratives</a>
                </body></html>
                """
            ),
            same_domain_raa_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{pdf_url}">RAA Occitanie 2026 PDF</a>
                  <a href="{ignored_urls[0]}">Suivez-nous</a>
                </body></html>
                """
            ),
        }
    )

    result = discover_prefecture_raa(region_root, session=session)

    assert result["raa_pages"] == [same_domain_raa_url]
    assert result["pdf_links"] == [pdf_url]
    assert not any(call["url"] in ignored_urls for call in session.calls)


def test_discover_prefecture_raa_handles_accented_tag_and_plural_year_pages():
    region_root = "https://www.prefectures-regions.gouv.fr/bourgogne-franche-comte"
    documents_url = f"{region_root}/Documents-publications"
    accented_tag_url = f"{region_root}/tags/view/Bourgogne-Franche-Comt%C3%A9/Documents+et+publications/Recueil+des+actes+administratifs"
    year_2026_url = f"{documents_url}/Recueils-des-actes-administratifs/Recueils-des-actes-administratifs-Bourgogne-Franche-Comte-2026"
    year_2025_url = f"{documents_url}/Recueils-des-actes-administratifs/Recueils-des-actes-administratifs-Bourgogne-Franche-Comte-2025"
    pdf_2026_url = "https://www.prefectures-regions.gouv.fr/files/raa-059-bourgogne-franche-comte-2026.pdf"
    pdf_2025_url = "https://www.prefectures-regions.gouv.fr/files/raa-bfc-2025.pdf"
    session = _FakeSession(
        {
            region_root: _FakeResponse(f'<html><body><a href="{documents_url}">Documents & publications</a></body></html>'),
            documents_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{accented_tag_url}">Recueil des actes administratifs</a>
                </body></html>
                """
            ),
            accented_tag_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{year_2026_url}">Recueils des actes administratifs Bourgogne Franche-Comté 2026</a>
                  <a href="{year_2025_url}">Recueils des actes administratifs Bourgogne Franche-Comté 2025</a>
                </body></html>
                """
            ),
            year_2026_url: _FakeResponse(f'<html><body><a href="{pdf_2026_url}">RAA 059 Bourgogne-Franche-Comté du 17 avril 2026 PDF</a></body></html>'),
            year_2025_url: _FakeResponse(f'<html><body><a href="{pdf_2025_url}">RAA Bourgogne-Franche-Comté 2025 PDF</a></body></html>'),
        }
    )

    result = discover_prefecture_raa(region_root, session=session)

    assert result["region_results"][0]["raa_entry_urls"] == [accented_tag_url]
    assert result["raa_pages"] == [year_2026_url, year_2025_url]
    assert result["pdf_links"] == [pdf_2026_url, pdf_2025_url]
    assert [document["year"] for document in result["documents"]] == ["2026", "2025"]
    assert result["region_results"][0]["available_years"] == ["2026", "2025"]


def test_discover_prefecture_raa_scans_all_raa_strategies_before_no_pdf_result():
    region_root = "https://www.prefectures-regions.gouv.fr/corse"
    documents_url = f"{region_root}/Documents-publications"
    tag_url = f"{region_root}/tags/view/Corse/Documents+et+publications/Recueil+des+actes+administratifs"
    session = _FakeSession(
        {
            region_root: _FakeResponse(f'<html><body><a href="{documents_url}">Documents & publications</a></body></html>'),
            documents_url: _FakeResponse(f'<html><body><a href="{tag_url}">Recueil des actes administratifs</a></body></html>'),
            tag_url: _FakeResponse('<html><body><p>Aucun recueil publié.</p></body></html>'),
        }
    )

    result = discover_prefecture_raa(region_root, session=session)

    assert result["region_results"][0]["has_raa_pdfs"] is False
    assert result["region_results"][0]["pdf_links"] == []
    assert result["pages_scanned"] == [documents_url, tag_url]
    assert tag_url in [call["url"] for call in session.calls]


def test_discover_prefecture_raa_crawls_corse_style_external_year_pages():
    region_root = "https://www.prefectures-regions.gouv.fr/corse"
    documents_url = f"{region_root}/Documents-publications"
    dossier_url = f"{documents_url}/Dossiers/Recueils-des-Actes-Administratifs"
    external_collection_url = "https://www.corse-du-sud.gouv.fr/Publications/Recueil-des-actes-administratifs/Recueil-des-actes-administratifs-de-la-Region-Corse"
    external_year_url = f"{external_collection_url}/Recueil-des-actes-administratifs-de-la-Region-Corse-pour-l-annee-2026"
    pdf_url = "https://www.corse-du-sud.gouv.fr/contenu/telechargement/123/456/file/raa-corse-2026-001.pdf"
    session = _FakeSession(
        {
            region_root: _FakeResponse(f'<html><body><a href="{documents_url}">Documents & publications</a></body></html>'),
            documents_url: _FakeResponse(f'<html><body><a href="{dossier_url}">Recueils des Actes Administratifs</a></body></html>'),
            dossier_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{external_collection_url}">Les recueils de la region Corse</a>
                </body></html>
                """
            ),
            external_collection_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{external_year_url}">Recueil des actes administratifs de la Region Corse pour l'annee 2026</a>
                </body></html>
                """
            ),
            external_year_url: _FakeResponse(
                f"""
                <html><body>
                  <h2>2026</h2>
                  <a href="{pdf_url}">RAA Corse 2026 numero 001</a>
                </body></html>
                """
            ),
        }
    )

    result = discover_prefecture_raa(region_root, session=session)

    assert result["pdf_links"] == [pdf_url]
    assert result["region_results"][0]["has_raa_pdfs"] is True
    assert result["region_results"][0]["available_years"] == ["2026"]
    assert result["region_results"][0]["elapsed_s"] >= 0


def test_extract_pdf_links_parallel_preserves_order_years_and_duplicates(monkeypatch):
    page_2026 = "https://www.prefectures-regions.gouv.fr/test/Documents-publications/Recueil-des-actes-administratifs-2026"
    page_2025 = "https://www.prefectures-regions.gouv.fr/test/Documents-publications/Recueil-des-actes-administratifs-2025"
    pdf_2026 = "https://www.prefectures-regions.gouv.fr/files/raa-2026-001.pdf"
    pdf_2025 = "https://www.prefectures-regions.gouv.fr/files/raa-2025-001.pdf"

    html_by_url = {
        page_2026: f"""
            <html><body>
              <h2>2026</h2>
              <a href="{pdf_2026}">RAA 2026 001</a>
              <a href="{pdf_2025}">Duplicate from another page</a>
            </body></html>
        """,
        page_2025: f"""
            <html><body>
              <h2>2025</h2>
              <a href="{pdf_2025}">RAA 2025 001</a>
            </body></html>
        """,
    }

    def fake_fetch_text(url, timeout, session, referer=""):
        return html_by_url[url]

    monkeypatch.setattr(raa_module, "_fetch_text", fake_fetch_text)

    result = extract_pdf_links_from_raa_pages(
        [page_2026, page_2025],
        timeout=5,
        session=None,
        region_slug="test",
        max_workers=2,
    )

    assert result["raa_pages_with_pdfs"] == [page_2026, page_2025]
    assert result["pdf_links"] == [pdf_2026, pdf_2025]
    assert [document["year"] for document in result["documents"]] == ["2026", "2026"]
    assert result["duplicate_pdf_links"] == 1
    assert result["errors"] == []


def test_extract_pdf_links_adds_publication_date_metadata_from_context(monkeypatch):
    page = "https://www.prefectures-regions.gouv.fr/test/Documents-publications/Recueil-des-actes-administratifs-2026"
    unknown_page = "https://www.prefectures-regions.gouv.fr/test/Documents-publications/Archives-RAA-2026"
    dated_pdf = "https://www.prefectures-regions.gouv.fr/files/20260401_filename-date.pdf"
    heading_pdf = "https://www.prefectures-regions.gouv.fr/files/no-date-in-name.pdf"
    unknown_pdf = "https://www.prefectures-regions.gouv.fr/files/raa-r76-2026-189.pdf"
    html_by_url = {
        page: f"""
            <html><body>
              <h2>Recueil n°180 du 30 avril 2026</h2>
              <a href="{dated_pdf}">RAA publié le 29 avril 2026</a>
              <h2>RAA spécial du 22 avril 2026</h2>
              <a href="{heading_pdf}">Télécharger</a>
            </body></html>
        """,
        unknown_page: f"""
            <html><body>
              <h2>Archives 2026</h2>
              <a href="{unknown_pdf}">RAA 2026 numéro 189</a>
            </body></html>
        """,
    }

    def fake_fetch_text(url, timeout, session, referer=""):
        return html_by_url[url]

    monkeypatch.setattr(raa_module, "_fetch_text", fake_fetch_text)

    result = extract_pdf_links_from_raa_pages(
        [page, unknown_page],
        timeout=5,
        session=None,
        region_slug="test",
        previous_days=10,
        today=date(2026, 4, 30),
    )

    documents = {document["url"]: document for document in result["documents"]}
    assert documents[dated_pdf]["publication_date"] == "2026-04-29"
    assert documents[dated_pdf]["date_source"] == "link_text"
    assert documents[dated_pdf]["is_within_date_window"] is True
    assert documents[heading_pdf]["publication_date"] == "2026-04-22"
    assert documents[heading_pdf]["date_source"] == "heading_context"
    assert documents[heading_pdf]["is_within_date_window"] is True
    assert documents[unknown_pdf]["publication_date"] == ""
    assert documents[unknown_pdf]["date_source"] == "unknown"
    assert documents[unknown_pdf]["is_within_date_window"] is False


def test_extract_pdf_links_uses_previous_block_and_duplicate_title_dates(monkeypatch):
    page = "https://www.prefectures-regions.gouv.fr/normandie/Documents-publications/Recueil-des-actes-administratifs-regionaux-Avril-2026"
    previous_block_pdf = "https://www.prefectures-regions.gouv.fr/normandie/files/recueil-r28-2026-095.pdf"
    duplicate_title_pdf = "https://www.prefectures-regions.gouv.fr/normandie/files/recueil-r28-2026-094.pdf"
    html_by_url = {
        page: f"""
            <html><body>
              <h1>Recueil des actes administratifs régionaux - Avril 2026</h1>
              <p><b>Recueil spécial n°95 publié le 28 avril 2026</b></p>
              <div><a class="link-download" href="{previous_block_pdf}"><span>recueil-r28-2026-095</span></a></div>
              <p><b>Recueil spécial n°94</b></p>
              <div><a class="link-download" href="{duplicate_title_pdf}"><span>recueil-r28-2026-094</span></a></div>
              <aside>
                <a class="link-download" href="{duplicate_title_pdf}" title="recueil-r28-2026-094 - 27/04/2026">
                  <span>recueil-r28-2026-094</span>
                </a>
              </aside>
            </body></html>
        """,
    }

    def fake_fetch_text(url, timeout, session, referer=""):
        return html_by_url[url]

    monkeypatch.setattr(raa_module, "_fetch_text", fake_fetch_text)

    result = extract_pdf_links_from_raa_pages(
        [page],
        timeout=5,
        session=None,
        region_slug="normandie",
        previous_days=10,
        today=date(2026, 4, 30),
    )

    documents = {document["url"]: document for document in result["documents"]}
    assert documents[previous_block_pdf]["publication_date"] == "2026-04-28"
    assert documents[previous_block_pdf]["date_source"] == "heading_context"
    assert documents[previous_block_pdf]["is_within_date_window"] is True
    assert documents[duplicate_title_pdf]["publication_date"] == "2026-04-27"
    assert documents[duplicate_title_pdf]["date_source"] == "link_text"
    assert documents[duplicate_title_pdf]["is_within_date_window"] is True


def test_extract_pdf_links_uses_source_page_update_date_as_last_resort(monkeypatch):
    page = "https://www.prefectures-regions.gouv.fr/nouvelle-aquitaine/Documents-publications/Recueil-des-Actes-Administratifs/Recueil-des-actes-administratifs-pour-l-annee-2026/Avril-2026"
    pdf_url = "https://www.prefectures-regions.gouv.fr/nouvelle-aquitaine/files/recueil-r75-2026-139.pdf"
    html_by_url = {
        page: f"""
            <html><body>
              <h1>Avril 2026</h1>
              <div class="page-title-maj">Mise à jour : 30 avril 2026</div>
              <div><a class="link-download" href="{pdf_url}"><span>recueil-r75-2026-139</span></a></div>
            </body></html>
        """,
    }

    def fake_fetch_text(url, timeout, session, referer=""):
        return html_by_url[url]

    monkeypatch.setattr(raa_module, "_fetch_text", fake_fetch_text)

    result = extract_pdf_links_from_raa_pages(
        [page],
        timeout=5,
        session=None,
        region_slug="nouvelle-aquitaine",
        previous_days=10,
        today=date(2026, 4, 30),
    )

    document = result["documents"][0]
    assert document["publication_date"] == "2026-04-30"
    assert document["date_source"] == "source_page"
    assert document["is_within_date_window"] is True


def test_find_documents_publications_url_handles_uppercase_variant():
    region_root = "https://www.prefectures-regions.gouv.fr/pays-de-la-loire"
    documents_url = "https://www.prefectures-regions.gouv.fr/pays-de-la-loire/DOCUMENTS-PUBLICATIONS"
    session = _FakeSession(
        {
            region_root: _FakeResponse(
                f"""
                <html><body>
                  <a href="{documents_url}">DOCUMENTS & PUBLICATIONS</a>
                </body></html>
                """
            )
        }
    )

    result = find_documents_publications_url(region_root, session=session)

    assert result == documents_url


def test_find_documents_publications_url_prefers_canonical_documents_page_over_tag_pages():
    region_root = "https://www.prefectures-regions.gouv.fr/nouvelle-aquitaine"
    documents_url = f"{region_root}/Documents-publications"
    noisy_tag_url = "https://www.prefectures-regions.gouv.fr/nouvelle-aquitaine/tags/view/Nouvelle-Aquitaine/Documents+et+publications/Salle+de+Presse"
    session = _FakeSession(
        {
            region_root: _FakeResponse(
                f"""
                <html><body>
                  <a href="{noisy_tag_url}">Documents et publications - Salle de Presse</a>
                  <a href="{documents_url}">Documents & publications</a>
                </body></html>
                """
            )
        }
    )

    result = find_documents_publications_url(region_root, session=session)

    assert result == documents_url


def test_discover_prefecture_raa_does_not_treat_draaf_as_raa_page():
    region_url = "https://www.prefectures-regions.gouv.fr/centre-val-de-loire/Documents-publications"
    raa_url = f"{region_url}/Recueil-des-Actes-Administratifs/Recueil-des-actes-administratifs-pour-l-annee-2026"
    session = _FakeSession(
        {
            region_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="/centre-val-de-loire/Documents-publications/Publications-et-etudes/DRAAF/Atlas-agricole">Atlas DRAAF</a>
                  <a href="{raa_url}">Recueil des actes administratifs 2026</a>
                </body></html>
                """
            ),
            raa_url: _FakeResponse('<html><body><a href="/files/raa-2026.pdf">PDF</a></body></html>'),
        }
    )

    result = discover_prefecture_raa(region_url, session=session)

    assert result["raa_pages"] == [raa_url]
    assert result["pdf_links"] == ["https://www.prefectures-regions.gouv.fr/files/raa-2026.pdf"]


def test_find_raa_entry_urls_prefers_strict_recueil_path_and_ignores_contactish_links():
    documents_url = "https://www.prefectures-regions.gouv.fr/nouvelle-aquitaine/Documents-publications"
    strict_raa_url = "https://www.prefectures-regions.gouv.fr/nouvelle-aquitaine/tags/view/Nouvelle-Aquitaine/Documents+et+publications/Recueil+des+actes+administratifs"
    session = _FakeSession(
        {
            documents_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{strict_raa_url}">Recueil des actes administratifs</a>
                  <a href="raa-regional@nouvelle-aquitaine.pref.gouv.fr">RAA contact</a>
                  <a href="/nouvelle-aquitaine/Documents-publications/Publications-et-etudes/DRAAF/Atlas">Atlas DRAAF</a>
                </body></html>
                """
            ),
        }
    )

    result = find_raa_entry_urls(documents_url, session=session)

    assert result == [strict_raa_url]
    assert not any("@" in call["url"] for call in session.calls)


def test_discover_prefecture_raa_nouvelle_aquitaine_hierarchy_ignores_contact_email():
    region_root = "https://www.prefectures-regions.gouv.fr/nouvelle-aquitaine"
    documents_url = f"{region_root}/Documents-publications"
    raa_tag_url = "https://www.prefectures-regions.gouv.fr/nouvelle-aquitaine/tags/view/Nouvelle-Aquitaine/Documents+et+publications/Recueil+des+actes+administratifs"
    year_url = f"{documents_url}/Recueil-des-Actes-Administratifs/Recueil-des-actes-administratifs-pour-l-annee-2026"
    jan_url = f"{year_url}/Janvier-2026"
    pdf_url = "https://www.prefectures-regions.gouv.fr/files/nouvelle-aquitaine-2026-01.pdf"
    session = _FakeSession(
        {
            region_root: _FakeResponse(f'<html><body><a href="{documents_url}">Documents & publications</a></body></html>'),
            documents_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{raa_tag_url}">Recueil des actes administratifs</a>
                  <a href="/nouvelle-aquitaine/Documents-publications/Publications-et-etudes/Autre">Autre publication</a>
                </body></html>
                """
            ),
            raa_tag_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{year_url}">Recueil des actes administratifs pour l'annee 2026</a>
                </body></html>
                """
            ),
            year_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{jan_url}">Janvier 2026</a>
                  <a href="raa-regional@nouvelle-aquitaine.pref.gouv.fr">contact</a>
                </body></html>
                """
            ),
            jan_url: _FakeResponse(f'<html><body><a href="{pdf_url}">PDF janvier</a></body></html>'),
        }
    )

    result = discover_prefecture_raa(region_root, session=session)

    assert result["region_pages"] == [documents_url]
    assert result["raa_pages"] == [jan_url]
    assert result["pdf_links"] == [pdf_url]
    assert result["region_results"][0]["available_years"] == ["2026"]
    assert not any("@" in call["url"] for call in session.calls)


def test_discover_prefecture_raa_scoped_daily_discovery_prunes_old_year_and_month_pages():
    region_root = "https://www.prefectures-regions.gouv.fr/normandie"
    documents_url = f"{region_root}/Documents-publications"
    raa_url = f"{documents_url}/Recueil-des-Actes-Administratifs"
    may_url = f"{raa_url}/Recueil-des-actes-administratifs-2026/Mai-2026"
    april_url = f"{raa_url}/Recueil-des-actes-administratifs-2026/Avril-2026"
    march_url = f"{raa_url}/Recueil-des-actes-administratifs-2026/Mars-2026"
    old_year_url = f"{raa_url}/Recueil-des-Actes-Administratifs-2025/Decembre-2025"
    may_pdf = "https://www.prefectures-regions.gouv.fr/files/raa-may-2026.pdf"
    april_pdf = "https://www.prefectures-regions.gouv.fr/files/raa-april-2026.pdf"
    session = _FakeSession(
        {
            region_root: _FakeResponse(f'<html><body><a href="{documents_url}">Documents & publications</a></body></html>'),
            documents_url: _FakeResponse(f'<html><body><a href="{raa_url}">Recueil des actes administratifs</a></body></html>'),
            raa_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{may_url}">Mai 2026</a>
                  <a href="{april_url}">Avril 2026</a>
                  <a href="{march_url}">Mars 2026</a>
                  <a href="{old_year_url}">Decembre 2025</a>
                </body></html>
                """
            ),
            may_url: _FakeResponse(f'<html><body><a href="{may_pdf}">RAA du 07 mai 2026</a></body></html>'),
            april_url: _FakeResponse(f'<html><body><a href="{april_pdf}">RAA du 30 avril 2026</a></body></html>'),
        }
    )

    result = discover_prefecture_raa(
        region_root,
        session=session,
        target_years=["2026"],
        target_months=["May", "April"],
    )

    fetched_urls = [call["url"] for call in session.calls]
    assert may_url in fetched_urls
    assert april_url in fetched_urls
    assert march_url not in fetched_urls
    assert old_year_url not in fetched_urls
    assert result["pdf_links"] == [may_pdf, april_pdf]


def test_run_prefecture_raa_workflow_discovers_region_root_before_download():
    portal_url = "https://www.prefectures-regions.gouv.fr/"
    region_root = "https://www.prefectures-regions.gouv.fr/centre-val-de-loire"
    documents_url = "https://www.prefectures-regions.gouv.fr/centre-val-de-loire/Documents-publications"
    raa_url = f"{documents_url}/Recueil-des-Actes-Administratifs-2026"
    target_dir = _local_test_dir("prefecture_raa_region_root")
    session = _FakeSession(
        {
            portal_url: _FakeResponse(f'<html><body><a href="{region_root}">Centre-Val de Loire</a></body></html>'),
            region_root: _FakeResponse(f'<html><body><a href="{documents_url}">Documents & publications</a></body></html>'),
            documents_url: _FakeResponse(f'<html><body><a href="{raa_url}">Recueil des actes administratifs 2026</a></body></html>'),
            raa_url: _FakeResponse('<html><body><a href="/files/root-final.pdf">Final PDF</a></body></html>'),
            "https://www.prefectures-regions.gouv.fr/files/root-final.pdf": _FakeResponse(
                content=b"root-final",
                headers={
                    "Content-Disposition": 'attachment; filename="root-final.pdf"',
                    "Content-Type": "application/pdf",
                },
            ),
        }
    )

    result = run_prefecture_raa_workflow(portal_url, destination=target_dir, session=session)

    assert result["regions_discovered"] == 1
    assert result["raa_pages_found"] == 1
    assert result["pdfs_discovered"] == 1
    assert result["pdfs_downloaded_successfully"] == 1
    assert (target_dir / "centre-val-de-loire" / "2026" / "root-final.pdf").read_bytes() == b"root-final"


def test_download_prefecture_raa_pdfs_tracks_attempted_downloaded_failed_and_duplicates():
    target_dir = _local_test_dir("prefecture_raa_download")
    session = _FakeSession(
        {
            "https://www.prefectures-regions.gouv.fr/files/a.pdf": _FakeResponse(
                content=b"a",
                headers={
                    "Content-Disposition": 'attachment; filename="a.pdf"',
                    "Content-Type": "application/pdf",
                },
            ),
            "https://www.prefectures-regions.gouv.fr/files/b.pdf": _FakeResponse(
                text="<html>blocked</html>",
                headers={"Content-Type": "text/html"},
            ),
        }
    )

    result = download_prefecture_raa_pdfs(
        {
            "page_url": "https://www.prefectures-regions.gouv.fr/",
            "pdf_links": [
                "https://www.prefectures-regions.gouv.fr/files/a.pdf",
                "https://www.prefectures-regions.gouv.fr/files/a.pdf#dup",
                "https://www.prefectures-regions.gouv.fr/files/b.pdf",
            ],
            "documents": [
                {"url": "https://www.prefectures-regions.gouv.fr/files/a.pdf", "region": "centre-val-de-loire", "year": "2026", "file_format": "PDF"},
                {"url": "https://www.prefectures-regions.gouv.fr/files/a.pdf#dup", "region": "centre-val-de-loire", "year": "2026", "file_format": "PDF"},
                {"url": "https://www.prefectures-regions.gouv.fr/files/b.pdf", "region": "centre-val-de-loire", "year": "2026", "file_format": "PDF"},
            ],
        },
        target_dir,
        session=session,
    )

    assert result["attempted"] == 2
    assert result["downloaded"] == 1
    assert result["failed"] == 1
    assert result["skipped_duplicates"] == 1
    assert result["duplicate_pdf_links_skipped"] == 1
    assert result["skipped_by_region_limit"] == 0
    assert (target_dir / "centre-val-de-loire" / "2026" / "a.pdf").read_bytes() == b"a"
    assert result["downloads"][1]["status"].startswith("failed:")


def test_download_prefecture_raa_pdfs_groups_files_by_region_and_year():
    target_dir = _local_test_dir("prefecture_raa_grouping")
    session = _FakeSession(
        {
            "https://www.prefectures-regions.gouv.fr/files/a.pdf": _FakeResponse(
                content=b"a",
                headers={
                    "Content-Disposition": 'attachment; filename="a.pdf"',
                    "Content-Type": "application/pdf",
                },
            ),
            "https://www.prefectures-regions.gouv.fr/files/b.pdf": _FakeResponse(
                content=b"b",
                headers={
                    "Content-Disposition": 'attachment; filename="b.pdf"',
                    "Content-Type": "application/pdf",
                },
            ),
        }
    )

    result = download_prefecture_raa_pdfs(
        {
            "page_url": "https://www.prefectures-regions.gouv.fr/",
            "pdf_links": [
                "https://www.prefectures-regions.gouv.fr/files/a.pdf",
                "https://www.prefectures-regions.gouv.fr/files/b.pdf",
            ],
            "documents": [
                {"url": "https://www.prefectures-regions.gouv.fr/files/a.pdf", "region": "bretagne", "year": "2026", "file_format": "PDF"},
                {"url": "https://www.prefectures-regions.gouv.fr/files/b.pdf", "region": "bretagne", "year": "2025", "file_format": "PDF"},
            ],
        },
        target_dir,
        session=session,
    )

    assert result["downloaded"] == 2
    assert (target_dir / "bretagne" / "2026" / "a.pdf").read_bytes() == b"a"
    assert (target_dir / "bretagne" / "2025" / "b.pdf").read_bytes() == b"b"


def test_download_prefecture_raa_pdfs_filters_to_previous_days_window():
    target_dir = _local_test_dir("prefecture_raa_date_filter")
    in_start = "https://www.prefectures-regions.gouv.fr/files/in-start.pdf"
    in_end = "https://www.prefectures-regions.gouv.fr/files/in-end.pdf"
    old = "https://www.prefectures-regions.gouv.fr/files/old.pdf"
    unknown = "https://www.prefectures-regions.gouv.fr/files/unknown.pdf"
    session = _FakeSession(
        {
            in_start: _FakeResponse(
                content=b"in-start",
                headers={"Content-Disposition": 'attachment; filename="in-start.pdf"', "Content-Type": "application/pdf"},
            ),
            in_end: _FakeResponse(
                content=b"in-end",
                headers={"Content-Disposition": 'attachment; filename="in-end.pdf"', "Content-Type": "application/pdf"},
            ),
        }
    )

    result = download_prefecture_raa_pdfs(
        {
            "page_url": "https://www.prefectures-regions.gouv.fr/",
            "date_filter_days": 10,
            "pdf_links": [in_start, in_end, old, unknown],
            "documents": [
                {
                    "url": in_start,
                    "title": "in-start.pdf",
                    "region": "bretagne",
                    "year": "2026",
                    "file_format": "PDF",
                    "publication_date": "2026-04-21",
                    "date_source": "link_text",
                    "is_within_date_window": True,
                },
                {
                    "url": in_end,
                    "title": "in-end.pdf",
                    "region": "bretagne",
                    "year": "2026",
                    "file_format": "PDF",
                    "publication_date": "2026-04-30",
                    "date_source": "link_text",
                    "is_within_date_window": True,
                },
                {
                    "url": old,
                    "title": "old.pdf",
                    "region": "bretagne",
                    "year": "2026",
                    "file_format": "PDF",
                    "publication_date": "2026-04-20",
                    "date_source": "link_text",
                    "is_within_date_window": False,
                },
                {
                    "url": unknown,
                    "title": "unknown.pdf",
                    "region": "bretagne",
                    "year": "2026",
                    "file_format": "PDF",
                    "publication_date": "",
                    "date_source": "unknown",
                    "is_within_date_window": False,
                },
            ],
        },
        target_dir,
        session=session,
    )

    assert result["attempted"] == 2
    assert result["downloaded"] == 2
    assert result["pdfs_in_date_window"] == 2
    assert result["unknown_date_pdfs"] == 1
    assert result["skipped_by_date_filter"] == 2
    assert result["documents_selected_for_download"] == 2
    assert (target_dir / "bretagne" / "2026" / "in-start.pdf").read_bytes() == b"in-start"
    assert (target_dir / "bretagne" / "2026" / "in-end.pdf").read_bytes() == b"in-end"
    assert not (target_dir / "bretagne" / "2026" / "old.pdf").exists()
    assert not (target_dir / "bretagne" / "2026" / "unknown.pdf").exists()


def test_download_prefecture_raa_pdfs_previous_days_zero_disables_date_filter():
    target_dir = _local_test_dir("prefecture_raa_date_filter_disabled")
    old = "https://www.prefectures-regions.gouv.fr/files/old.pdf"
    unknown = "https://www.prefectures-regions.gouv.fr/files/unknown.pdf"
    session = _FakeSession(
        {
            old: _FakeResponse(
                content=b"old",
                headers={"Content-Disposition": 'attachment; filename="old.pdf"', "Content-Type": "application/pdf"},
            ),
            unknown: _FakeResponse(
                content=b"unknown",
                headers={"Content-Disposition": 'attachment; filename="unknown.pdf"', "Content-Type": "application/pdf"},
            ),
        }
    )

    result = download_prefecture_raa_pdfs(
        {
            "page_url": "https://www.prefectures-regions.gouv.fr/",
            "date_filter_days": 0,
            "pdf_links": [old, unknown],
            "documents": [
                {
                    "url": old,
                    "title": "old.pdf",
                    "region": "bretagne",
                    "year": "2026",
                    "file_format": "PDF",
                    "publication_date": "2026-04-20",
                    "date_source": "link_text",
                    "is_within_date_window": False,
                },
                {
                    "url": unknown,
                    "title": "unknown.pdf",
                    "region": "bretagne",
                    "year": "2026",
                    "file_format": "PDF",
                    "publication_date": "",
                    "date_source": "unknown",
                    "is_within_date_window": False,
                },
            ],
        },
        target_dir,
        session=session,
    )

    assert result["attempted"] == 2
    assert result["downloaded"] == 2
    assert result["skipped_by_date_filter"] == 0
    assert (target_dir / "bretagne" / "2026" / "old.pdf").read_bytes() == b"old"
    assert (target_dir / "bretagne" / "2026" / "unknown.pdf").read_bytes() == b"unknown"


def test_download_prefecture_raa_pdfs_can_cap_downloads_per_region():
    target_dir = _local_test_dir("prefecture_raa_cap")
    session = _FakeSession(
        {
            "https://www.prefectures-regions.gouv.fr/files/a1.pdf": _FakeResponse(
                content=b"a1",
                headers={
                    "Content-Disposition": 'attachment; filename="a1.pdf"',
                    "Content-Type": "application/pdf",
                },
            ),
            "https://www.prefectures-regions.gouv.fr/files/a2.pdf": _FakeResponse(
                content=b"a2",
                headers={
                    "Content-Disposition": 'attachment; filename="a2.pdf"',
                    "Content-Type": "application/pdf",
                },
            ),
            "https://www.prefectures-regions.gouv.fr/files/b1.pdf": _FakeResponse(
                content=b"b1",
                headers={
                    "Content-Disposition": 'attachment; filename="b1.pdf"',
                    "Content-Type": "application/pdf",
                },
            ),
            "https://www.prefectures-regions.gouv.fr/files/b2.pdf": _FakeResponse(
                content=b"b2",
                headers={
                    "Content-Disposition": 'attachment; filename="b2.pdf"',
                    "Content-Type": "application/pdf",
                },
            ),
        }
    )

    result = download_prefecture_raa_pdfs(
        {
            "page_url": "https://www.prefectures-regions.gouv.fr/",
            "pdf_links": [
                "https://www.prefectures-regions.gouv.fr/files/a1.pdf",
                "https://www.prefectures-regions.gouv.fr/files/a2.pdf",
                "https://www.prefectures-regions.gouv.fr/files/b1.pdf",
                "https://www.prefectures-regions.gouv.fr/files/b2.pdf",
            ],
            "documents": [
                {"url": "https://www.prefectures-regions.gouv.fr/files/a1.pdf", "region": "bretagne", "year": "2026", "file_format": "PDF"},
                {"url": "https://www.prefectures-regions.gouv.fr/files/a2.pdf", "region": "bretagne", "year": "2026", "file_format": "PDF"},
                {"url": "https://www.prefectures-regions.gouv.fr/files/b1.pdf", "region": "centre-val-de-loire", "year": "2026", "file_format": "PDF"},
                {"url": "https://www.prefectures-regions.gouv.fr/files/b2.pdf", "region": "centre-val-de-loire", "year": "2026", "file_format": "PDF"},
            ],
        },
        target_dir,
        session=session,
        max_downloads_per_region=1,
    )

    assert result["documents_selected_for_download"] == 2
    assert result["attempted"] == 2
    assert result["downloaded"] == 2
    assert result["skipped_duplicates"] == 2
    assert result["duplicate_pdf_links_skipped"] == 0
    assert result["skipped_by_region_limit"] == 2
    assert (target_dir / "bretagne" / "2026" / "a1.pdf").read_bytes() == b"a1"
    assert not (target_dir / "bretagne" / "2026" / "a2.pdf").exists()
    assert (target_dir / "centre-val-de-loire" / "2026" / "b1.pdf").read_bytes() == b"b1"
    assert not (target_dir / "centre-val-de-loire" / "2026" / "b2.pdf").exists()


def test_run_prefecture_raa_workflow_combines_discovery_and_download():
    region_url = "https://www.prefectures-regions.gouv.fr/centre-val-de-loire/Documents-publications"
    raa_url = f"{region_url}/Recueil-des-Actes-Administratifs-2026"
    target_dir = _local_test_dir("prefecture_raa_workflow")
    session = _FakeSession(
        {
            region_url: _FakeResponse(
                f"""
                <html><body>
                  <a href="{raa_url}">RAA 2026</a>
                </body></html>
                """
            ),
            raa_url: _FakeResponse(
                """
                <html><body>
                  <a href="/files/final.pdf">Final PDF</a>
                </body></html>
                """
            ),
            "https://www.prefectures-regions.gouv.fr/files/final.pdf": _FakeResponse(
                content=b"final",
                headers={
                    "Content-Disposition": 'attachment; filename="final.pdf"',
                    "Content-Type": "application/pdf",
                },
            ),
        }
    )

    result = run_prefecture_raa_workflow(region_url, destination=target_dir, session=session)

    assert result["regions_discovered"] == 1
    assert result["raa_pages_found"] == 1
    assert result["pdfs_discovered"] == 1
    assert result["attempted"] == 1
    assert result["downloaded"] == 1
    assert result["failed"] == 0
    assert result["skipped_duplicates"] == 0
    assert (target_dir / "centre-val-de-loire" / "2026" / "final.pdf").read_bytes() == b"final"


def _daily_discovery(documents):
    return {
        "documents": documents,
        "region_results": [],
        "pdf_links": [document["url"] for document in documents],
        "pdfs_discovered": len(documents),
    }


def _daily_document(url, region="normandie", year="2026", month="May", publication_date="2026-05-07"):
    return {
        "url": url,
        "title": Path(url).name,
        "file_format": "PDF",
        "region": region,
        "year": year,
        "month": month,
        "publication_date": publication_date,
        "date_source": "source_page",
        "source_page": "https://example.test/raa",
    }


def test_archive_baseline_stores_all_non_corse_documents_without_downloads():
    repo = _FakeDailyRepo()
    documents = [
        _daily_document("https://example.test/a.pdf"),
        _daily_document("https://example.test/b.pdf", region="Corse"),
        _daily_document("https://example.test/april.pdf", month="April", publication_date="2026-04-28"),
        _daily_document("https://example.test/old.pdf", month="March", publication_date="2026-03-28"),
    ]

    result = create_archive_baseline(
        _local_test_dir("daily_baseline"),
        repo,
        discovery_func=lambda *args, **kwargs: _daily_discovery(documents),
        write_excel=False,
    )

    assert result["status"] == "baseline_created"
    assert result["months_scanned"] == "archive-all"
    assert result["new_pdfs"] == 0
    assert result["downloaded"] == 0
    assert set(["discovery_seconds", "db_seconds", "excel_seconds", "total_seconds"]).issubset(result)
    assert len(repo.documents) == 3
    assert repo.has_completed_archive_baseline() is True
    assert all(document["region"].lower() != "corse" for document in repo.documents.values())


def test_daily_raa_check_second_run_reports_new_urls_without_downloading():
    repo = _FakeDailyRepo()
    target_dir = _local_test_dir("daily_diff")
    first_documents = [_daily_document("https://example.test/a.pdf")]
    second_documents = [
        _daily_document("https://example.test/a.pdf"),
        _daily_document("https://example.test/new.pdf"),
        _daily_document("https://example.test/old-month.pdf", month="March", publication_date="2026-03-30"),
    ]

    create_archive_baseline(
        target_dir,
        repo,
        discovery_func=lambda *args, **kwargs: _daily_discovery(first_documents),
        write_excel=False,
    )

    result = run_daily_raa_check(
        target_dir,
        repo,
        today=date(2026, 5, 7),
        discovery_func=lambda *args, **kwargs: _daily_discovery(second_documents),
        write_excel=False,
    )

    assert result["status"] == "completed"
    assert result["pdfs_discovered"] == 2
    assert result["already_known"] == 1
    assert result["new_pdfs"] == 1
    assert result["downloaded"] == 0
    assert set(["discovery_seconds", "db_seconds", "excel_seconds", "total_seconds"]).issubset(result)
    assert result["daily_by_region"] == [
        {"region": "normandie", "daily_scoped_pdfs": 2, "already_known": 1, "new_pdfs": 1, "years": "2026", "months": "May"}
    ]
    assert result["new_documents"][0]["url"] == "https://example.test/new.pdf"
    assert result["new_by_region"] == [{"region": "normandie", "new_pdfs": 1, "years": "2026", "months": "May"}]
    assert len(repo.downloads) == 0

    def fake_download(documents, destination, timeout=0):
        return [
            {
                "url": document["url"],
                "file_path": str(Path(destination) / document["region"] / document["year"] / document["title"]),
                "filename": document["title"],
                "status": "downloaded",
            }
            for document in documents
        ]

    download_result = download_daily_raa_diff(
        target_dir,
        repo,
        result["run_id"],
        documents=result["new_documents"],
        download_func=fake_download,
        write_excel=False,
    )

    assert download_result["documents_selected_for_download"] == 1
    assert download_result["downloaded"] == 1
    assert set(["db_seconds", "download_seconds", "excel_seconds", "total_seconds"]).issubset(download_result)
    assert len(repo.downloads) == 1
    assert repo.downloads[0]["status"] == "downloaded"


def test_daily_pipeline_writes_excel_with_timezone_aware_run_timestamps():
    repo = _FakeDailyRepo()
    target_dir = _local_test_dir("daily_excel")
    repo.create_run(
        datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc),
        "baseline_created",
        regions_scanned=12,
        months_scanned="archive-all",
        run_type="archive_baseline",
    )
    repo.finish_run(
        1,
        datetime(2026, 5, 7, 8, 30, tzinfo=timezone.utc),
        "baseline_created",
        1,
        0,
        0,
        0,
    )

    result = run_daily_raa_check(
        target_dir,
        repo,
        today=date(2026, 5, 7),
        discovery_func=lambda *args, **kwargs: _daily_discovery([_daily_document("https://example.test/new-excel.pdf")]),
        write_excel=True,
    )

    workbook_path = Path(result["excel_path"])
    assert workbook_path.exists()
    sheets = pd.read_excel(workbook_path, sheet_name=None)
    assert set(sheets) == {"daily_runs", "new_documents", "downloads", "inventory_snapshot"}
    assert sheets["daily_runs"].shape[0] == 2
    assert sheets["new_documents"].shape[0] == 1
    assert result["downloaded"] == 0


def test_daily_raa_check_current_month_scope_for_may_2026():
    assert current_and_previous_months(date(2026, 5, 7)) == [
        ("May", 2026, "2026-05"),
    ]


def test_daily_scan_passes_scoped_discovery_options_after_archive_baseline():
    repo = _FakeDailyRepo()
    target_dir = _local_test_dir("daily_scoped_args")
    create_archive_baseline(
        target_dir,
        repo,
        discovery_func=lambda *args, **kwargs: _daily_discovery([_daily_document("https://example.test/a.pdf")]),
        write_excel=False,
    )
    captured = {}

    def fake_discovery(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _daily_discovery([_daily_document("https://example.test/a.pdf")])

    result = run_daily_raa_check(
        target_dir,
        repo,
        start_url="https://www.prefectures-regions.gouv.fr/",
        timeout=17,
        today=date(2026, 5, 7),
        discovery_func=fake_discovery,
        write_excel=False,
    )

    assert result["status"] == "completed"
    assert captured["args"][0] == "https://www.prefectures-regions.gouv.fr/"
    assert captured["kwargs"]["timeout"] == 17
    assert captured["kwargs"]["previous_days"] == 0
    assert captured["kwargs"]["exclude_region_slugs"] == {"corse"}
    assert captured["kwargs"]["target_years"] == ["2026"]
    assert captured["kwargs"]["target_months"] == ["May"]


def test_daily_scope_prefers_publication_date_over_year_month_and_excludes_corse_case_insensitive():
    repo = _FakeDailyRepo()
    target_dir = _local_test_dir("daily_scope")
    create_archive_baseline(
        target_dir,
        repo,
        discovery_func=lambda *args, **kwargs: _daily_discovery([_daily_document("https://example.test/a.pdf")]),
        write_excel=False,
    )
    documents = [
        _daily_document("https://example.test/march-date.pdf", month="May", publication_date="2026-03-31"),
        _daily_document("https://example.test/may-no-date.pdf", month="May", publication_date=""),
        _daily_document("https://example.test/march-no-date.pdf", month="March", publication_date=""),
        _daily_document("https://example.test/corse.pdf", region="Corse", month="May", publication_date="2026-05-07"),
    ]

    result = run_daily_raa_check(
        target_dir,
        repo,
        today=date(2026, 5, 7),
        discovery_func=lambda *args, **kwargs: _daily_discovery(documents),
        write_excel=False,
    )

    assert result["pdfs_discovered"] == 1
    assert result["new_pdfs"] == 1
    assert result["new_documents"][0]["url"] == "https://example.test/may-no-date.pdf"


def test_daily_diff_uses_normalized_urls_for_known_detection():
    repo = _FakeDailyRepo()
    target_dir = _local_test_dir("daily_normalized")
    create_archive_baseline(
        target_dir,
        repo,
        discovery_func=lambda *args, **kwargs: _daily_discovery(
            [_daily_document("HTTPS://EXAMPLE.test/path/a.pdf#page=2")]
        ),
        write_excel=False,
    )

    result = run_daily_raa_check(
        target_dir,
        repo,
        today=date(2026, 5, 7),
        discovery_func=lambda *args, **kwargs: _daily_discovery(
            [_daily_document("https://example.test/path/a.pdf")]
        ),
        write_excel=False,
    )

    assert result["already_known"] == 1
    assert result["new_pdfs"] == 0
    assert len(repo.documents) == 1
    stored = next(iter(repo.documents.values()))
    assert stored["first_run_id"] == 1
    assert stored["last_run_id"] == 2

try:
    from .crawler import _looks_product_relevant, _should_follow_url, crawl
    from .scraper import PageResult
except ImportError:
    from crawler import _looks_product_relevant, _should_follow_url, crawl
    from scraper import PageResult


def test_crawler_stays_inside_modality_tree_and_skips_corporate_noise():
    start_url = "https://www.siemens-healthineers.com/magnetic-resonance-imaging"

    assert _should_follow_url(
        "https://www.siemens-healthineers.com/magnetic-resonance-imaging/high-v-mri/magnetom-free-xl",
        start_url,
    )
    assert _should_follow_url(
        "https://www.siemens-healthineers.com/magnetic-resonance-imaging/0-35-to-1-5t-mri-scanners",
        start_url,
    )
    assert not _should_follow_url(
        "https://www.siemens-healthineers.com/cookie",
        start_url,
    )
    assert not _should_follow_url(
        "https://www.siemens-healthineers.com/careers",
        start_url,
    )
    assert not _should_follow_url(
        "https://www.siemens-healthineers.com/investor-relations",
        start_url,
    )


def test_crawler_treats_document_publication_pages_as_relevant_for_file_discovery():
    assert _looks_product_relevant(
        "https://example.com/Documents-publications/Recueil-des-Actes-Administratifs-2026",
    )
    assert _looks_product_relevant(
        "https://example.com/publications/report-page",
    )
    assert not _looks_product_relevant("https://example.com/Region-et-institutions")


class _FakeScraper:
    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    async def fetch(self, url, *args, **kwargs):
        self.fetched.append(url)
        links = self.pages.get(url, [])
        return PageResult(url=url, title=url.rsplit("/", 1)[-1], links=links, success=True)


def test_crawl_honors_explicit_include_patterns_for_off_path_documents():
    start = "https://example.com/documents"
    off_path = "https://example.com/library/reports/report-1"
    scraper = _FakeScraper({start: [off_path], off_path: []})

    import asyncio

    results = asyncio.run(
        crawl(
            start,
            max_depth=1,
            max_pages=5,
            include_patterns=["*/library/reports/*"],
            respect_robots=False,
            scraper=scraper,
            verbose=False,
        )
    )

    assert [result.url for result in results] == [start, off_path]


def test_crawl_include_patterns_with_literal_regex_chars_stay_in_path_family():
    start = "https://example.com/tags/view/Centre-Val+de+Loire/Documents+et+publications"
    page_two = "https://example.com/tags/view/Centre-Val+de+Loire/Documents+et+publications/(offset)/10"
    homepage = "https://example.com/"
    scraper = _FakeScraper({start: [page_two, homepage], page_two: [], homepage: []})

    import asyncio

    results = asyncio.run(
        crawl(
            start,
            max_depth=1,
            max_pages=5,
            include_patterns=[
                "*/tags/view/Centre-Val+de+Loire/Documents+et+publications*",
                "*/tags/view/Centre-Val+de+Loire/Documents+et+publications/*",
            ],
            respect_robots=False,
            scraper=scraper,
            verbose=False,
        )
    )

    assert [result.url for result in results] == [start, page_two]


def test_crawl_enforces_hard_max_pages_with_concurrent_batch():
    start = "https://example.com/products"
    scraper = _FakeScraper(
        {
            start: [
                "https://example.com/products/a",
                "https://example.com/products/b",
                "https://example.com/products/c",
            ],
            "https://example.com/products/a": [],
            "https://example.com/products/b": [],
            "https://example.com/products/c": [],
        }
    )

    import asyncio

    results = asyncio.run(
        crawl(
            start,
            max_depth=1,
            max_pages=2,
            concurrency=3,
            respect_robots=False,
            scraper=scraper,
            verbose=False,
        )
    )

    assert len(results) == 2

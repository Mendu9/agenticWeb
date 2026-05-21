from __future__ import annotations

import asyncio
import fnmatch
import re
from collections import deque
from typing import List, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

try:
    from .scraper import FireScraper, PageResult
except ImportError:
    from scraper import FireScraper, PageResult


def _normalize_url(url: str) -> str:
    """Normalize URL: strip fragment, lowercase scheme+host."""
    parsed = urlparse(url)
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        fragment="",
    )
    path = normalized.path.rstrip("/") or "/"
    return normalized._replace(path=path).geturl()


def _same_domain(url: str, base: str) -> bool:
    """Return True if url has the same domain as base."""
    return urlparse(url).netloc.lower() == urlparse(base).netloc.lower()


_REGEX_CHARS = re.compile(r'[()\[\]^$+?{|\\]')

_DEFAULT_EXCLUDE_TERMS = (
    "cookie",
    "privacy",
    "legal",
    "terms",
    "career",
    "jobs",
    "investor",
    "press",
    "news",
    "media",
    "support",
    "documentation",
    "contact",
    "login",
    "auth",
    "register",
    "shop",
)

_PRODUCT_PATH_HINTS = (
    "products",
    "product",
    "document",
    "publication",
    "download",
    "file",
    "recueil",
    "arrete",
    "magnetic-resonance-imaging",
    "computed-tomography",
    "ultrasound",
    "mammography",
    "x-ray",
    "radiography",
    "laboratory-diagnostics",
    "medical-imaging",
)

def _matches_any(url: str, patterns: Optional[List[str]]) -> bool:
    """Return True if url matches any fnmatch-style or regex pattern.

    Patterns containing regex metacharacters are treated as regex;
    all others are treated as fnmatch glob patterns (e.g. '*/products*').
    This avoids passing bare globs like '*/products*' to re.search(),
    which would raise 'nothing to repeat' on the leading '*'.
    """
    if not patterns:
        return False
    for pattern in patterns:
        if fnmatch.fnmatch(url, pattern):
            return True
        if _REGEX_CHARS.search(pattern):
            try:
                if re.search(pattern, url):
                    return True
            except re.error:
                pass
    return False


def _is_noise_url(url: str) -> bool:
    lowered = url.lower()
    return any(term in lowered for term in _DEFAULT_EXCLUDE_TERMS)


def _path_prefix(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path or "/"


def _same_path_family(url: str, start_url: str) -> bool:
    start_path = _path_prefix(start_url)
    candidate_path = _path_prefix(url)
    if start_path == "/":
        return True
    return candidate_path == start_path or candidate_path.startswith(start_path + "/")


def _looks_product_relevant(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in _PRODUCT_PATH_HINTS)


def _should_follow_url(url: str, start_url: str) -> bool:
    if _is_noise_url(url):
        return False
    if _same_path_family(url, start_url):
        return True
    if _path_prefix(start_url) == "/":
        return _looks_product_relevant(url)
    return False


def _link_priority(url: str, start_url: str) -> tuple[int, int]:
    same_family = 1 if _same_path_family(url, start_url) else 0
    product_relevant = 1 if _looks_product_relevant(url) else 0
    return (-same_family, -product_relevant)


class RobotsCache:
    """Caches robots.txt parsers per domain."""

    def __init__(self):
        self._cache: dict[str, Optional[RobotFileParser]] = {}

    def _get_parser(self, url: str) -> Optional[RobotFileParser]:
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        if domain in self._cache:
            return self._cache[domain]
        rp = RobotFileParser()
        rp.set_url(f"{domain}/robots.txt")
        try:
            rp.read()
            self._cache[domain] = rp
        except Exception:
            self._cache[domain] = None
        return self._cache[domain]

    def allowed(self, url: str, user_agent: str = "*") -> bool:
        rp = self._get_parser(url)
        if rp is None:
            return True
        return rp.can_fetch(user_agent, url)


async def crawl(
    start_url: str,
    max_depth: int = 2,
    max_pages: int = 50,
    include_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    concurrency: int = 3,
    respect_robots: bool = True,
    scraper: Optional[FireScraper] = None,
    verbose: bool = True,
) -> List[PageResult]:
    """
    Recursively crawl a site starting from start_url.

    Args:
        start_url: Entry point URL
        max_depth: How many link-hops from start_url to follow (default 2)
        max_pages: Hard cap on total pages fetched (default 50)
        include_patterns: Only follow URLs matching these glob/regex patterns
        exclude_patterns: Skip URLs matching these patterns
        concurrency: Parallel Playwright pages (default 3)
        respect_robots: Honour robots.txt (default True)
        scraper: Existing FireScraper instance; if None, one is created
        verbose: Print progress

    Returns:
        List of PageResult, one per successfully fetched page
    """
    start_url = _normalize_url(start_url)
    robots = RobotsCache() if respect_robots else None

    visited: set[str] = set()
    results: List[PageResult] = []

    # Queue items: (url, depth)
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])

    async def _process_batch(batch: list[tuple[str, int]], s: FireScraper):
        nonlocal results
        semaphore = asyncio.Semaphore(concurrency)

        async def _fetch_one(url: str, depth: int):
            async with semaphore:
                if verbose:
                    print(f"  [{depth}] {url}")
                result = await s.fetch(url)
                return result, depth

        tasks = [_fetch_one(url, depth) for url, depth in batch]
        fetched = await asyncio.gather(*tasks)

        for result, depth in fetched:
            if len(results) >= max_pages:
                break
            if not result.success:
                continue
            results.append(result)

            if depth < max_depth and len(results) < max_pages:
                candidate_links = sorted(result.links, key=lambda link: _link_priority(link, start_url))
                for link in candidate_links:
                    norm = _normalize_url(link)
                    if norm in visited:
                        continue
                    if not _same_domain(norm, start_url):
                        continue
                    if robots and not robots.allowed(norm):
                        continue
                    if include_patterns and not _matches_any(norm, include_patterns):
                        continue
                    if exclude_patterns and _matches_any(norm, exclude_patterns):
                        continue
                    if not include_patterns and not _should_follow_url(norm, start_url):
                        continue
                    visited.add(norm)
                    queue.append((norm, depth + 1))

    own_scraper = scraper is None
    _scraper = scraper or FireScraper()

    try:
        if own_scraper:
            await _scraper._start_browser()

        visited.add(start_url)
        if verbose:
            print(f"Crawling {start_url} (max_depth={max_depth}, max_pages={max_pages})")

        while queue and len(results) < max_pages:
            # Drain up to concurrency*2 items per batch to keep the queue moving
            batch_size = min(concurrency * 2, max_pages - len(results))
            batch: list[tuple[str, int]] = []
            while queue and len(batch) < batch_size:
                item = queue.popleft()
                batch.append(item)

            if batch:
                await _process_batch(batch, _scraper)

    finally:
        if own_scraper:
            await _scraper._stop_browser()

    if verbose:
        print(f"Crawl complete: {len(results)} pages fetched.")

    return results


async def map_site(
    start_url: str,
    max_pages: int = 200,
    respect_robots: bool = True,
) -> List[str]:
    """
    Return all URLs found on a domain without downloading full content.
    Like Firecrawl's /map endpoint — fast URL discovery.

    Uses the links extracted from each page rather than downloading full content.
    """
    start_url = _normalize_url(start_url)
    robots = RobotsCache() if respect_robots else None
    visited: set[str] = set([start_url])
    queue: deque[str] = deque([start_url])

    async with FireScraper() as scraper:
        while queue and len(visited) < max_pages:
            url = queue.popleft()
            result = await scraper.fetch(url, wait_for="domcontentloaded", scroll=False)
            if not result.success:
                continue
            for link in result.links:
                norm = _normalize_url(link)
                if norm in visited:
                    continue
                if not _same_domain(norm, start_url):
                    continue
                if robots and not robots.allowed(norm):
                    continue
                visited.add(norm)
                queue.append(norm)

    return sorted(visited)

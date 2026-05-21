from __future__ import annotations

import asyncio
import re
from io import BytesIO
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from readability import Document
    READABILITY_AVAILABLE = True
except ImportError:
    READABILITY_AVAILABLE = False

try:
    from markdownify import markdownify as _markdownify
    MARKDOWNIFY_AVAILABLE = True
except ImportError:
    MARKDOWNIFY_AVAILABLE = False


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class PageResult(BaseModel):
    """Structured result from a single page fetch."""
    url: str
    title: str = ""
    markdown: str = ""
    html: str = ""
    links: List[str] = Field(default_factory=list)
    images: List[Dict] = Field(default_factory=list)
    metadata: Dict = Field(default_factory=dict)
    success: bool = True
    error: Optional[str] = None

    @property
    def text(self) -> str:
        """Plain text version (strips markdown syntax)."""
        return re.sub(r'[#*`\[\]()]', '', self.markdown)


# ---------------------------------------------------------------------------
# Noise selectors stripped before markdown conversion
# ---------------------------------------------------------------------------
_NOISE_SELECTORS = [
    "nav", "header", "footer", "aside",
    "[role='navigation']", "[role='banner']", "[role='contentinfo']",
    ".nav", ".navbar", ".header", ".footer", ".sidebar", ".cookie-banner",
    ".ad", ".ads", ".advertisement", "#cookie-notice", ".popup",
    "script", "style", "noscript", "iframe",
    "[id*='cookie' i]", "[class*='cookie' i]",
    "[id*='consent' i]", "[class*='consent' i]",
    "[id*='privacy' i]", "[class*='privacy' i]",
    "[id*='newsletter' i]", "[class*='newsletter' i]",
    "[id*='login' i]", "[class*='login' i]",
    "[id*='auth' i]", "[class*='auth' i]",
    "form", "dialog",
]

_COOKIE_HEAVY_MARKERS = (
    "cookieconsent",
    "http cookie",
    "html local storage",
    "indexeddb",
    "privacy policy",
    "used to store",
    "session state",
    "auth0",
    "cookiebot",
)

_CONTENT_HINTS = (
    "mri", "magnetic resonance", "ct", "computed tomography", "ultrasound",
    "mammography", "radiography", "x-ray", "laboratory diagnostics",
    "scanner", "system", "platform", "workflow", "product", "solution",
)

_BLOCK_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "li", "dt", "dd", "td", "th", "a", "span", "div",
}

_NOISE_LINK_TERMS = (
    "cookie", "privacy", "legal", "terms", "auth", "login", "register",
    "consent", "cookiebot", "auth0", "walkme", "privacystatement",
)

_LOCALE_CANDIDATE_SEGMENTS = ("en-us", "us", "en", "global")

_US_DOMAIN_HINTS = (
    "siemens-healthineers.com",
    "gehealthcare.com",
    "usa.philips.com",
    "diagnostics.roche.com",
)


def _cookie_marker_count(text: str) -> int:
    lowered = text.lower()
    return sum(lowered.count(marker) for marker in _COOKIE_HEAVY_MARKERS)


def _content_hint_count(text: str) -> int:
    lowered = text.lower()
    return sum(lowered.count(marker) for marker in _CONTENT_HINTS)


def _is_low_quality_markdown(md: str) -> bool:
    if not md:
        return True
    cookie_hits = _cookie_marker_count(md)
    content_hits = _content_hint_count(md)
    table_lines = sum(1 for line in md.splitlines() if line.strip().startswith("|"))
    return (
        cookie_hits >= 3
        or (table_lines >= 4 and content_hits == 0)
        or (len(md) < 300 and cookie_hits > content_hits)
    )


def _extract_semantic_markdown_from_html(html: str) -> str:
    soup = _make_soup(html)
    for selector in _NOISE_SELECTORS:
        for el in soup.select(selector):
            el.decompose()

    root = soup.find("main") or soup.find("article") or soup.find("body") or soup
    parts: List[str] = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if _cookie_marker_count(text) >= 2:
            continue
        if len(text) < 20 and _content_hint_count(text) == 0:
            continue
        parts.append(text)

    deduped: List[str] = []
    seen = set()
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(part)

    return "\n\n".join(deduped).strip()


def _extract_rich_text_blocks_from_html(html: str) -> str:
    soup = _make_soup(html)
    for selector in _NOISE_SELECTORS:
        for el in soup.select(selector):
            el.decompose()

    root = soup.find("main") or soup.find("article") or soup.find("body") or soup
    parts: List[str] = []

    for el in root.find_all(list(_BLOCK_TAGS)):
        if el.name not in _BLOCK_TAGS:
            continue

        # Skip parent containers that simply wrap other rich text blocks.
        if any(child.name in _BLOCK_TAGS for child in el.find_all(recursive=False)):
            continue

        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if len(text) < 3:
            continue
        if _cookie_marker_count(text) >= 2:
            continue
        if el.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            parts.append(text)
            continue
        if el.name == "a" or el.find_parent("a"):
            parts.append(text)
            continue
        if len(text) < 20 and _content_hint_count(text) == 0:
            continue
        parts.append(text)

    return _dedupe_text_parts(parts)


def _dedupe_text_parts(parts: List[str]) -> str:
    deduped: List[str] = []
    seen = set()
    for part in parts:
        normalized = re.sub(r"\s+", " ", part).strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return "\n\n".join(deduped).strip()


def _merge_markdown_candidates(candidates: List[str]) -> str:
    parts: List[str] = []
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        for block in re.split(r"\n{2,}", candidate):
            normalized = re.sub(r"\s+", " ", block).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            parts.append(block.strip())
    return "\n\n".join(parts).strip()


def _link_noise_ratio(links: List[str]) -> float:
    if not links:
        return 0.0
    noisy = 0
    for link in links:
        lowered = link.lower()
        if any(term in lowered for term in _NOISE_LINK_TERMS):
            noisy += 1
    return noisy / max(len(links), 1)


def _assess_source_quality(
    url: str,
    title: str,
    markdown: str,
    links: List[str],
    block_signals: List[str],
) -> Dict:
    reasons: List[str] = []
    if "geo_redirect" in block_signals:
        reasons.append("geo_redirect")
    if "geo_picker" in block_signals:
        reasons.append("geo_picker")
    if "login_wall" in block_signals:
        reasons.append("login_wall")
    if _is_low_quality_markdown(markdown):
        reasons.append("low_quality_markdown")
    noise_ratio = _link_noise_ratio(links[:20])
    if len(links) >= 5 and noise_ratio >= 0.45:
        reasons.append("mostly_noise_links")
    if _cookie_marker_count(title + "\n" + markdown[:1500]) >= 3:
        reasons.append("cookie_heavy_content")
    if _content_hint_count(title + "\n" + markdown) == 0:
        reasons.append("no_domain_content_hints")

    status = "good"
    if reasons:
        if any(reason in reasons for reason in ("geo_redirect", "geo_picker", "login_wall")):
            status = "bad_source_page"
        elif len(reasons) >= 2:
            status = "bad_source_page"
        else:
            status = "low_confidence"

    return {
        "source_quality": status,
        "bad_source_reasons": reasons,
        "noise_link_ratio": round(noise_ratio, 2),
    }


def _replace_first_locale_segment(path: str, replacement: str) -> str:
    parts = [part for part in path.split("/") if part]
    if parts and (len(parts[0]) == 2 or parts[0].lower() in _LOCALE_CANDIDATE_SEGMENTS):
        parts[0] = replacement
        return "/" + "/".join(parts)
    return path


def _canonicalize_to_us_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or "/"

    if "siemens-healthineers.com" in host:
        if host.startswith("www."):
            host = "www.siemens-healthineers.com"
        if path == "/" or path in {"/de", "/de/"}:
            path = "/en-us"
        elif path.startswith("/de/"):
            path = path.replace("/de/", "/en-us/", 1)
        elif path == "/en":
            path = "/en-us"
        elif path.startswith("/en/"):
            path = path.replace("/en/", "/en-us/", 1)
    elif "philips.com" in host and "usa.philips.com" not in host:
        host = "www.usa.philips.com"
    elif "gehealthcare.com" in host:
        if path == "/":
            path = "/"
    elif "diagnostics.roche.com" in host:
        if path in {"/", ""}:
            path = "/us/en.html"
        elif "/global/" in path:
            path = path.replace("/global/", "/us/en/", 1)

    return parsed._replace(netloc=host, path=path).geturl()


def _build_retry_candidates(current_url: str, links: List[str]) -> List[str]:
    current_url = _canonicalize_to_us_url(current_url)
    parsed = urlparse(current_url)
    candidates: List[str] = []

    for replacement in _LOCALE_CANDIDATE_SEGMENTS:
        replaced = _replace_first_locale_segment(parsed.path, replacement)
        if replaced != parsed.path:
            candidates.append(parsed._replace(path=replaced).geturl())

    if parsed.path.startswith("/de/") or parsed.path == "/de":
        candidates.append(parsed._replace(path=parsed.path.replace("/de", "/en-us", 1)).geturl())
        candidates.append(parsed._replace(path=parsed.path.replace("/de", "", 1) or "/").geturl())

    if parsed.path and parsed.path != "/":
        candidates.append(parsed._replace(path="/").geturl())

    same_domain_links = []
    for link in links:
        lp = urlparse(link)
        if lp.netloc.lower() != parsed.netloc.lower():
            continue
        lowered = link.lower()
        if any(term in lowered for term in _NOISE_LINK_TERMS):
            continue
        candidate = _canonicalize_to_us_url(link)
        lowered = candidate.lower()
        if any(segment in lowered for segment in _LOCALE_CANDIDATE_SEGMENTS) or any(
            hint in lowered for hint in _CONTENT_HINTS
        ):
            same_domain_links.append(candidate)

    candidates.extend(same_domain_links[:8])

    deduped: List[str] = []
    seen = {current_url}
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def html_to_markdown(html: str, base_url: str = "") -> str:
    """
    Convert raw HTML to clean LLM-friendly Markdown.

    Pipeline:
      1. readability-lxml — extract main content (drops nav/ads/footer)
      2. BeautifulSoup — strip any remaining noise selectors
      3. markdownify — convert to Markdown
      4. Normalise whitespace

    Falls back to raw BS4 text extraction if readability/markdownify not installed.
    """
    if not html:
        return ""

    # Step 1: readability extraction
    if READABILITY_AVAILABLE:
        try:
            doc = Document(html)
            content_html = doc.summary(html_partial=False)
        except Exception:
            content_html = html
    else:
        content_html = html

    # Step 2: strip residual noise
    soup = _make_soup(content_html)
    for selector in _NOISE_SELECTORS:
        for el in soup.select(selector):
            el.decompose()

    clean_html = str(soup)

    # Step 3: convert to markdown
    # Note: markdownify does not allow both `strip` and `convert` simultaneously.
    # Using `convert` as an allowlist already excludes unlisted tags (script, style, img).
    if MARKDOWNIFY_AVAILABLE:
        md = _markdownify(
            clean_html,
            heading_style="ATX",
            convert=["p", "h1", "h2", "h3", "h4", "h5", "h6",
                     "ul", "ol", "li", "a", "strong", "em",
                     "blockquote", "code", "pre", "table", "tr", "td", "th"],
        )
    else:
        # Plain text fallback
        md = soup.get_text(separator="\n", strip=True)

    alt_md = _extract_semantic_markdown_from_html(html)
    rich_md = _extract_rich_text_blocks_from_html(html)

    if _is_low_quality_markdown(md):
        if alt_md and (
            not _is_low_quality_markdown(alt_md)
            or _content_hint_count(alt_md) > _content_hint_count(md)
        ):
            md = _merge_markdown_candidates([alt_md, rich_md])
        elif rich_md and _content_hint_count(rich_md) >= _content_hint_count(md):
            md = rich_md
    else:
        md = _merge_markdown_candidates([md, alt_md, rich_md])

    # Step 4: normalise whitespace
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return md.strip()


def _make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# Auth helpers (ported from scraping_tools.py)
# ---------------------------------------------------------------------------

class _AuthConfig:
    """Holds authentication state for a scraping session."""

    def __init__(self):
        self.headers: Dict[str, str] = {}
        self.cookies: Dict[str, str] = {}

    def set_token(self, token: str, token_type: str = "Bearer"):
        self.headers["Authorization"] = f"{token_type} {token}"

    def set_cookies(self, cookies: Dict[str, str]):
        self.cookies.update(cookies)


# ---------------------------------------------------------------------------
# Block-signal detection patterns
# ---------------------------------------------------------------------------
_BLOCK_SIGNALS = {
    "access_denied": ["access denied", "403 forbidden", "you don't have permission",
                      "error reference #", "edgesuite.net"],
    "captcha":       ["captcha", "recaptcha", "i'm not a robot", "please verify",
                      "bot detection", "cloudflare ray id"],
    "login_wall":    ["sign in to continue", "log in to view", "create an account to",
                      "for healthcare professionals only", "hcp only"],
    "geo_redirect":  ["startseite", "diagnostik gemeinsam", "sie verlassen",
                      "befinden sich", "zum inhalt"],
    "geo_picker":    ["take me to our location", "select your country",
                      "select a country", "choose your location",
                      "international homepage", "where are you located"],
}

def detect_block_signals(html: str, title: str = "") -> list[str]:
    """Return list of detected block signal types from page content."""
    combined = (html[:3000] + title).lower()
    return [sig for sig, patterns in _BLOCK_SIGNALS.items()
            if any(p in combined for p in patterns)]


# ---------------------------------------------------------------------------
# Realistic browser headers (helps bypass Akamai / basic bot detection)
# ---------------------------------------------------------------------------
_STEALTH_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "cache-control": "max-age=0",
}

# Country-selector bypass: selectors to try in order (most specific first)
_GEO_BYPASS_SELECTORS = [
    'a[href*="/en-us"]',
    'a[href*="/us/"]',
    'a[href*="united-states"]',
    'a[href*="/en/"]',
    'a[href*="/global/"]',
    'a:has-text("United States (Homepage)")',
    'a:has-text("United States")',
    'a:has-text("English")',
    'a:has-text("Global")',
]


class FireScraper:
    """
    Playwright-first async scraping engine.

    Use as an async context manager to manage the browser lifecycle:

        async with FireScraper() as scraper:
            result = await scraper.fetch("https://example.com")

    For one-shot usage without the context manager, call FireScraper.one_shot():

        result = await FireScraper.one_shot("https://example.com")
    """

    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    # First attempt timeout for networkidle before falling back
    _NETWORKIDLE_FIRST_TIMEOUT_MS = 15_000

    def __init__(
        self,
        timeout: int = 30,
        headless: bool = True,
        auth: Optional[_AuthConfig] = None,
    ):
        self.timeout_ms = timeout * 1000
        self.headless = headless
        self.auth = auth or _AuthConfig()
        self._playwright = None
        self._browser = None
        # Requests session for static fallback
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self.DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9",
        })

    # ---- context manager --------------------------------------------------

    async def __aenter__(self) -> "FireScraper":
        await self._start_browser()
        return self

    async def __aexit__(self, *args):
        await self._stop_browser()

    async def _start_browser(self):
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless
            )
        except ImportError:
            print("Playwright not installed. Run: pip install playwright && playwright install chromium")
            print("Falling back to requests-only mode (no JS rendering).")

    async def _stop_browser(self):
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    # ---- public API -------------------------------------------------------

    async def fetch(
        self,
        url: str,
        wait_for: str = "networkidle",
        scroll: bool = True,
        allow_retry: bool = True,
    ) -> PageResult:
        """
        Fetch a URL and return a PageResult with clean markdown.

        Args:
            url: Target URL
            wait_for: Playwright load state — 'networkidle' (default), 'load', 'domcontentloaded'
            scroll: Scroll to trigger lazy-loaded content
        """
        url = _canonicalize_to_us_url(url)
        if self._browser:
            return await self._fetch_playwright(url, wait_for, scroll, allow_retry=allow_retry)
        return self._fetch_requests(url)

    async def fetch_many(self, urls: List[str], concurrency: int = 3) -> List[PageResult]:
        """Fetch multiple URLs with controlled concurrency."""
        semaphore = asyncio.Semaphore(concurrency)

        async def _bounded(url: str) -> PageResult:
            async with semaphore:
                return await self.fetch(url)

        return await asyncio.gather(*[_bounded(u) for u in urls])

    async def spa_login(
        self,
        login_url: str,
        credentials: Dict[str, str],
        target_url: Optional[str] = None,
    ) -> Optional[PageResult]:
        """
        Interactive Playwright login for JavaScript SPAs.

        Ported and cleaned up from scraping_tools.py:_fetch_with_crawl4ai_auth()

        Args:
            login_url: Login page URL
            credentials: {'username': ..., 'password': ...}
            target_url: URL to navigate to after login (defaults to login_url)

        Returns:
            PageResult of target_url after login, or None on failure
        """
        if not self._browser:
            print("Browser not started. Use FireScraper as async context manager.")
            return None

        context = await self._make_context(login_url)
        page = await context.new_page()

        try:
            print(f"Navigating to login page: {login_url}")
            await page.goto(login_url, wait_until="domcontentloaded", timeout=self.timeout_ms)

            # Wait for password field to appear (SPA may render it async)
            try:
                await page.wait_for_selector('input[type="password"]', state="visible", timeout=20000)
            except Exception:
                print("Password field not immediately visible, waiting extra 5s...")
                await asyncio.sleep(5)

            # Find username field
            username_field = await self._find_element(page, [
                'input[name="username"]', 'input[type="email"]', 'input[name="email"]',
                'input[id="inputEmail"]', 'input[id="email"]', 'input[id="username"]',
                'input[placeholder*="email" i]', 'input[placeholder*="username" i]',
            ])
            if not username_field:
                print("Could not find username field.")
                return None

            # Find password field
            password_field = await self._find_element(page, [
                'input[type="password"]', 'input[name="password"]',
                'input[id="password"]', 'input[id="inputPassword"]',
            ])
            if not password_field:
                print("Could not find password field.")
                return None

            # Fill credentials
            await self._fill_field(page, username_field, credentials.get("username", ""))
            await self._fill_field(page, password_field, credentials.get("password", ""))

            # Submit
            submit = await self._find_element(page, [
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Login")', 'button:has-text("Sign in")',
                'button:has-text("Log in")', '.login-button',
            ])
            if submit:
                await submit.click()
            else:
                await password_field.press("Enter")

            # Wait for post-login navigation
            await asyncio.sleep(8)

            nav_url = target_url or login_url
            if target_url and target_url != login_url:
                try:
                    await page.goto(target_url, wait_until="networkidle", timeout=self._NETWORKIDLE_FIRST_TIMEOUT_MS)
                except Exception:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    await asyncio.sleep(3)

            # Scroll for lazy content
            if True:
                for _ in range(4):
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                    await asyncio.sleep(0.8)
                await page.evaluate("window.scrollTo(0, 0)")

            html = await page.content()
            title = await page.title()
            links = await self._extract_links_playwright(page)

            return PageResult(
                url=nav_url,
                title=title,
                markdown=html_to_markdown(html, nav_url),
                html=html,
                links=links,
                metadata={"fetched_via": "playwright_spa_login"},
                success=True,
            )

        except Exception as e:
            print(f"SPA login error: {e}")
            return None
        finally:
            await page.close()
            await context.close()

    # ---- class-level convenience ------------------------------------------

    @classmethod
    async def one_shot(cls, url: str, **kwargs) -> PageResult:
        """Fetch a single URL without keeping the browser alive."""
        async with cls(**kwargs) as scraper:
            return await scraper.fetch(url)

    # ---- private helpers --------------------------------------------------

    async def _make_context(self, url: str):
        """Create a browser context with stealth headers, en-US locale, and auth."""
        extra_headers = {**_STEALTH_HEADERS, **self.auth.headers}
        context = await self._browser.new_context(
            user_agent=self.DEFAULT_UA,
            locale="en-US",                    # Fix: locale redirect (Medtronic/Roche)
            timezone_id="America/New_York",    # Fix: geo detection
            extra_http_headers=extra_headers,  # Fix: stealth + Accept-Language
            viewport={"width": 1280, "height": 800},
        )
        if self.auth.cookies:
            parsed = urlparse(url)
            await context.add_cookies([
                {"name": k, "value": v, "domain": parsed.netloc, "path": "/"}
                for k, v in self.auth.cookies.items()
            ])
        return context

    async def _fetch_playwright(
        self, url: str, wait_for: str, scroll: bool, allow_retry: bool = True
    ) -> PageResult:
        context = await self._make_context(url)
        page = await context.new_page()
        fetch_method = "playwright"

        try:
            # Fix: tiered wait — networkidle with short timeout, fallback to load+sleep
            # Heavy SPAs (Abbott, Philips, J&J, Zimmer) never fire networkidle.
            if wait_for == "networkidle":
                try:
                    await page.goto(
                        url,
                        wait_until="networkidle",
                        timeout=self._NETWORKIDLE_FIRST_TIMEOUT_MS,
                    )
                    fetch_method = "playwright/networkidle"
                except Exception:
                    # Fallback: domcontentloaded + extra sleep for JS to settle
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )
                    await asyncio.sleep(3)
                    fetch_method = "playwright/domcontentloaded+sleep"
            else:
                await page.goto(url, wait_until=wait_for, timeout=self.timeout_ms)

            # Fix: geo-picker bypass (Stryker country selector)
            redirected_to = await self._bypass_geo_picker(page)

            if scroll:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1)
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.5)

            html = await page.content()
            title = await page.title()
            links = await self._extract_links_playwright(page)
            images = await page.eval_on_selector_all(
                "img[src]",
                "els => els.map(e => ({src: e.src, alt: e.alt || ''}))",
            )

            block_signals = detect_block_signals(html, title)
            final_url = redirected_to or url
            markdown = html_to_markdown(html, final_url)
            quality = _assess_source_quality(final_url, title, markdown, links, block_signals)

            retry_candidates = _build_retry_candidates(final_url, links)
            if allow_retry and quality["source_quality"] == "bad_source_page":
                for candidate in retry_candidates[:3]:
                    retried = await self._fetch_playwright(
                        candidate,
                        wait_for="domcontentloaded",
                        scroll=scroll,
                        allow_retry=False,
                    )
                    if retried.success and retried.metadata.get("source_quality") == "good":
                        retried.metadata["retried_from"] = final_url
                        retried.metadata["initial_bad_source_reasons"] = quality["bad_source_reasons"]
                        return retried

            return PageResult(
                url=final_url,
                title=title,
                markdown=markdown,
                html=html,
                links=links,
                images=images,
                metadata={
                    "fetched_via": fetch_method,
                    "block_signals": block_signals,
                    "original_url": url,
                    "source_quality": quality["source_quality"],
                    "bad_source_reasons": quality["bad_source_reasons"],
                    "noise_link_ratio": quality["noise_link_ratio"],
                    "retry_candidates": retry_candidates[:8],
                },
                success=True,
            )
        except Exception as e:
            return PageResult(url=url, success=False, error=str(e))
        finally:
            await page.close()
            await context.close()

    async def _bypass_geo_picker(self, page) -> Optional[str]:
        """
        Detect geo/country selector pages and navigate to the US/English version.

        Fix for: Stryker (country selector), and similar "Take me to our location" pages.
        Returns the new URL navigated to, or None if no bypass was needed.
        """
        try:
            html_preview = await page.evaluate(
                "document.body ? document.body.innerText.slice(0, 1500).toLowerCase() : ''"
            )
            title = await page.title()

            signals = detect_block_signals(html_preview, title)
            if "geo_picker" not in signals:
                return None

            # Try each bypass selector
            for sel in _GEO_BYPASS_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if not el:
                        continue
                    href = await el.get_attribute("href")
                    if not href:
                        continue
                    await page.goto(href, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    await asyncio.sleep(2)
                    return href
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _fetch_requests(self, url: str) -> PageResult:
        """Static fallback using requests + BS4."""
        try:
            self._session.headers.update({
                **{k: v for k, v in _STEALTH_HEADERS.items() if k != "sec-fetch-user"},
                **self.auth.headers,
            })
            if self.auth.cookies:
                self._session.cookies.update(self.auth.cookies)

            resp = self._session.get(url, timeout=self.timeout_ms // 1000)
            resp.raise_for_status()
            html = resp.text

            soup = _make_soup(html)
            title = soup.title.string.strip() if soup.title else ""
            links = [
                urljoin(url, a["href"])
                for a in soup.find_all("a", href=True)
            ]
            images = [
                {"src": urljoin(url, img["src"]), "alt": img.get("alt", "")}
                for img in soup.find_all("img", src=True)
            ]

            block_signals = detect_block_signals(html, title)
            markdown = html_to_markdown(html, url)
            quality = _assess_source_quality(url, title, markdown, links, block_signals)

            return PageResult(
                url=url,
                title=title,
                markdown=markdown,
                html=html,
                links=list(dict.fromkeys(links)),
                images=images,
                metadata={
                    "fetched_via": "requests",
                    "block_signals": block_signals,
                    "source_quality": quality["source_quality"],
                    "bad_source_reasons": quality["bad_source_reasons"],
                    "noise_link_ratio": quality["noise_link_ratio"],
                    "retry_candidates": _build_retry_candidates(url, links)[:8],
                },
                success=True,
            )
        except Exception as e:
            return PageResult(url=url, success=False, error=str(e))

    async def _extract_links_playwright(self, page) -> List[str]:
        try:
            hrefs = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)"
            )
            return list(dict.fromkeys(href for href in hrefs if href.startswith("http")))
        except Exception:
            return []

    @staticmethod
    async def _find_element(page, selectors: List[str]):
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    return el
            except Exception:
                continue
        return None

    @staticmethod
    async def _fill_field(page, element, value: str):
        try:
            await element.fill(value, timeout=5000)
        except Exception:
            # JS fallback
            await page.evaluate(
                "(el, v) => { el.value = v; "
                "el.dispatchEvent(new Event('input', {bubbles:true})); "
                "el.dispatchEvent(new Event('change', {bubbles:true})); }",
                element,
                value,
            )

    # ---- image download (ported from scraping_tools.py) -------------------

    def download_image(
        self, image_url: str, base_url: str = "", max_size_mb: int = 5
    ) -> Optional[Dict]:
        """Download image and extract metadata (width/height via Pillow)."""
        try:
            absolute_url = urljoin(base_url, image_url) if base_url else image_url
            resp = self._session.get(absolute_url, timeout=15, stream=True)
            resp.raise_for_status()

            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > max_size_mb * 1024 * 1024:
                return None

            data = resp.content
            content_type = resp.headers.get("content-type", "image/unknown")
            width = height = None

            if PIL_AVAILABLE:
                try:
                    img = Image.open(BytesIO(data))
                    width, height = img.size
                except Exception:
                    pass

            return {
                "url": absolute_url,
                "data": data,
                "content_type": content_type,
                "width": width,
                "height": height,
            }
        except Exception as e:
            print(f"Image download failed ({image_url}): {e}")
            return None

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence
from urllib.parse import urlparse

try:
    from .crawler import crawl
except ImportError:
    from crawler import crawl


_NOISE_TERMS = (
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
    "event",
    "blog",
    "sitemap",
    "about",
)

_PATH_NOISE_TERMS = (
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
    "login",
    "auth",
    "register",
    "shop",
)

_PRODUCT_TERMS = (
    "product",
    "products",
    "system",
    "systems",
    "scanner",
    "scanners",
    "platform",
    "family",
    "solutions",
    "solution",
    "modality",
    "mri",
    "ct",
    "ultrasound",
    "mammography",
    "radiography",
    "x-ray",
    "xray",
    "pet",
    "molecular",
    "lab",
    "diagnostics",
)

_MODALITY_HINTS = {
    "mri": ("mri", "magnetic-resonance", "magnetic-resonance-imaging", "magnetom"),
    "ct": ("ct", "computed-tomography", "somatom", "revolution"),
    "ultrasound": ("ultrasound", "ultra-sound", "sono"),
    "mammography": ("mammography", "mammo"),
    "radiography": ("radiography", "x-ray", "xray", "digital-radiography"),
    "pet": ("pet", "molecular-imaging", "biograph"),
    "lab": ("lab", "laboratory", "diagnostics", "cobas", "atellica"),
}

_MODALITY_INCLUDE_PATTERNS = {
    modality: tuple(f"*{hint}*" for hint in hints)
    for modality, hints in _MODALITY_HINTS.items()
}

_RANKING_WEIGHTS = {
    "mri": {
        "magnetom": 9,
        "magnetic-resonance-imaging": 8,
        "magnetic-resonance": 7,
        "mri": 3,
    },
    "ct": {
        "somatom": 9,
        "computed-tomography": 8,
        "ct": 3,
    },
    "ultrasound": {
        "ultrasound": 8,
        "sono": 6,
    },
    "mammography": {
        "mammography": 8,
        "mammo": 6,
    },
    "radiography": {
        "radiography": 8,
        "x-ray": 7,
        "xray": 7,
    },
    "pet": {
        "biograph": 9,
        "molecular-imaging": 8,
        "pet": 4,
    },
    "lab": {
        "cobas": 9,
        "atellica": 8,
        "diagnostics": 4,
        "lab": 3,
    },
}


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        fragment="",
    )
    path = normalized.path.rstrip("/") or "/"
    return normalized._replace(path=path).geturl()


def _path_prefix(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path or "/"


def _same_domain(url: str, base: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(base).netloc.lower()


def _same_path_family(url: str, start_url: str) -> bool:
    start_path = _path_prefix(start_url)
    candidate_path = _path_prefix(url)
    if start_path == "/":
        return True
    return candidate_path == start_path or candidate_path.startswith(start_path + "/")


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    deduped: List[str] = []
    for item in items:
        if not item:
            continue
        normalized = item.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _infer_modality_from_text(text: str) -> Optional[str]:
    lowered = text.lower()
    for modality, hints in _MODALITY_HINTS.items():
        if any(hint in lowered for hint in hints):
            return modality
    return None


def infer_modality_from_url(url: str) -> Optional[str]:
    return _infer_modality_from_text(url)


@dataclass(frozen=True)
class FocusedCrawlPlan:
    start_url: str
    modality: Optional[str]
    include_patterns: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    focus_terms: tuple[str, ...]
    path_prefix: str


def build_focused_crawl_plan(
    start_url: str,
    modality: Optional[str] = None,
    include_patterns: Optional[Sequence[str]] = None,
    exclude_patterns: Optional[Sequence[str]] = None,
) -> FocusedCrawlPlan:
    normalized = _normalize_url(start_url)
    resolved_modality = modality or infer_modality_from_url(normalized)
    path_prefix = _path_prefix(normalized)

    include: List[str] = []
    if path_prefix != "/":
        include.extend(
            [
                f"*{path_prefix}*",
                f"*{path_prefix}/*",
            ]
        )

    if resolved_modality and resolved_modality in _MODALITY_INCLUDE_PATTERNS:
        include.extend(_MODALITY_INCLUDE_PATTERNS[resolved_modality])

    include.append("*products*")

    if include_patterns:
        include.extend(include_patterns)

    if not include:
        include.extend(["*product*", "*products*", "*system*", "*scanner*"])

    exclude: List[str] = [f"*{term}*" for term in _NOISE_TERMS]
    if exclude_patterns:
        exclude.extend(exclude_patterns)

    focus_terms = list(_PRODUCT_TERMS)
    if resolved_modality and resolved_modality in _MODALITY_HINTS:
        focus_terms.extend(_MODALITY_HINTS[resolved_modality])

    return FocusedCrawlPlan(
        start_url=normalized,
        modality=resolved_modality,
        include_patterns=tuple(_dedupe(include)),
        exclude_patterns=tuple(_dedupe(exclude)),
        focus_terms=tuple(_dedupe(focus_terms)),
        path_prefix=path_prefix,
    )


def rank_focused_links(
    links: Sequence[str],
    start_url: str,
    modality: Optional[str] = None,
    extra_terms: Optional[Sequence[str]] = None,
) -> List[str]:
    normalized_start = _normalize_url(start_url)
    resolved_modality = modality or infer_modality_from_url(normalized_start)
    focus_terms = list(_PRODUCT_TERMS)
    if resolved_modality and resolved_modality in _MODALITY_HINTS:
        focus_terms.extend(_MODALITY_HINTS[resolved_modality])
    if extra_terms:
        focus_terms.extend(extra_terms)

    def _score(url: str) -> tuple[int, int, int, str]:
        normalized = _normalize_url(url)
        lowered = normalized.lower()
        score = 0
        if _same_domain(normalized, normalized_start):
            score += 20
        if _same_path_family(normalized, normalized_start):
            score += 60
        if any(term in lowered for term in _PATH_NOISE_TERMS):
            score -= 100
        for term in focus_terms:
            if term in lowered:
                score += 25
        if resolved_modality and resolved_modality in _RANKING_WEIGHTS:
            for term, weight in _RANKING_WEIGHTS[resolved_modality].items():
                if term in lowered:
                    score += weight
        if any(term in lowered for term in ("system", "scanner", "platform", "family", "device")):
            score += 8
        depth = len([segment for segment in _path_prefix(normalized).split("/") if segment])
        # Shallower URLs in the same subtree usually represent parent category pages.
        return (-score, depth, len(normalized), normalized)

    deduped = _dedupe(_normalize_url(link) for link in links)
    return sorted(deduped, key=_score)


def filter_focused_links(
    links: Sequence[str],
    start_url: str,
    modality: Optional[str] = None,
    extra_terms: Optional[Sequence[str]] = None,
) -> List[str]:
    ranked = rank_focused_links(links, start_url, modality=modality, extra_terms=extra_terms)
    normalized_start = _normalize_url(start_url)
    resolved_modality = modality or infer_modality_from_url(normalized_start)
    allow_terms = list(_PRODUCT_TERMS)
    if resolved_modality and resolved_modality in _MODALITY_HINTS:
        allow_terms.extend(_MODALITY_HINTS[resolved_modality])
    if extra_terms:
        allow_terms.extend(extra_terms)

    filtered: List[str] = []
    for link in ranked:
        lowered = link.lower()
        if any(term in lowered for term in _NOISE_TERMS):
            continue
        if _same_path_family(link, normalized_start):
            filtered.append(link)
            continue
        if any(term in lowered for term in allow_terms):
            filtered.append(link)
    return filtered


async def crawl_focused_tree(
    start_url: str,
    max_depth: int = 2,
    max_pages: int = 50,
    modality: Optional[str] = None,
    include_patterns: Optional[Sequence[str]] = None,
    exclude_patterns: Optional[Sequence[str]] = None,
    concurrency: int = 3,
    respect_robots: bool = True,
    scraper=None,
    verbose: bool = True,
):
    plan = build_focused_crawl_plan(
        start_url,
        modality=modality,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
    )
    return await crawl(
        plan.start_url,
        max_depth=max_depth,
        max_pages=max_pages,
        include_patterns=list(plan.include_patterns),
        exclude_patterns=list(plan.exclude_patterns),
        concurrency=concurrency,
        respect_robots=respect_robots,
        scraper=scraper,
        verbose=verbose,
    )

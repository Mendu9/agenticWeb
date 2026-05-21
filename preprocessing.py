from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Sequence

from pydantic import BaseModel, Field

try:
    from .scraper import PageResult
except ImportError:
    from scraper import PageResult


URL_NOISE_TERMS = (
    "cookie",
    "privacy",
    "legal",
    "terms",
    "consent",
    "login",
    "auth",
    "register",
    "event",
    "shop",
    "support",
    "career",
    "news",
    "investor",
)

MEDICAL_SIGNAL_TERMS = (
    "mri",
    "magnetic resonance",
    "ct",
    "computed tomography",
    "ultrasound",
    "mammography",
    "x-ray",
    "radiography",
    "molecular imaging",
    "laboratory diagnostics",
    "core lab",
    "point of care",
    "pathology",
    "product",
    "system",
    "scanner",
    "platform",
    "solution",
    "specification",
    "brochure",
    "request quote",
    "contact sales",
    "workflow",
    "detector",
    "throughput",
    "tesla",
    "slice",
    "bore",
    "channel",
)

PRODUCT_FAMILY_TERMS = (
    "somatom",
    "magnetom",
    "acuson",
    "atellica",
    "mammomat",
    "epiq",
    "affiniti",
    "ingenia",
    "incisive",
    "azurion",
    "lumify",
    "signa",
    "revolution",
    "venue",
    "voluson",
    "discovery",
    "definium",
    "cobas",
    "elecsys",
    "lightcycler",
    "navify",
)

NOISE_PATTERNS = (
    r"\bcookieconsent\b",
    r"\bprivacy policy\b",
    r"\bhttp cookie\b",
    r"\bhtml local storage\b",
    r"\bindexeddb\b",
    r"\bsession\b",
    r"\bpersistent\b",
    r"\bphpsessid\b",
    r"\barrAffinity\b",
    r"\b__cf_bm\b",
    r"\bauth0\b",
    r"\bcookiebot\b",
    r"\bwalkme\b",
    r"\bused to store\b",
    r"\bused to distribute traffic\b",
    r"\bdistinguish between humans and bots\b",
    r"\buser session state\b",
    r"\bboolean cookie\b",
    r"\bregistrationformid\b",
    r"\blocalization of buttons and labels\b",
    r"\bselect your country\b",
    r"\binternational homepage\b",
    r"\baccept all cookies\b",
    r"\bmanage preferences\b",
    r"\bsign up\b",
    r"\blog in\b",
    r"\bselect another country or region\b",
    r"\bcontent specific to your location\b",
)

SPEC_PATTERNS = (
    r"\b\d+(?:\.\d+)?\s*tesla\b",
    r"\b\d+\s*slice\b",
    r"\b\d+(?:\.\d+)?\s*(?:cm|mm)\s*bore\b",
    r"\b\d+(?:,\d{3})?\s*(?:tests?|samples?)\s*/\s*(?:hour|hr|h)\b",
    r"\b\d+\s*channel\b",
    r"\b\d+(?:\.\d+)?\s*(?:cm|mm)\b",
)

GERMAN_HINTS = (
    "nachhaltige",
    "gesundheitswesen",
    "aktuelle",
    "kundengeschichten",
    "wirtschaftlichkeit",
    "beschließt",
    "hochhaus",
    "universitätsklinikum",
    "sichere",
    "hygiene",
    "im klinikalltag",
    "zukunftsfähige",
    "hochmoderne",
    "setzt neue standards",
)

GERMAN_HINTS = (
    "nachhaltige",
    "gesundheitswesen",
    "aktuelle",
    "kundengeschichten",
    "wirtschaftlichkeit",
    "beschließt",
    "hochhaus",
    "universitätsklinikum",
    "sichere",
    "hygiene",
    "im klinikalltag",
    "zukunftsfähige",
    "hochmoderne",
    "setzt neue standards",
)

FALLBACK_PATTERNS = (
    r"\bpage (?:was )?not found\b",
    r"\b404\b",
    r"\bwe moved the page\b",
    r"\bdeleted it\b",
    r"\bcheck if (?:the )?url\b",
    r"\balternate recommendations\b",
)

NAVIGATION_TERMS = (
    "home",
    "search",
    "contact us",
    "products & services",
    "products and services",
    "help us to improve",
)


class CleanBlock(BaseModel):
    text: str
    kept: bool
    score: int
    reasons: List[str] = Field(default_factory=list)


class ETLResult(BaseModel):
    url: str
    title: str = ""
    page_type: str = "unknown"
    relevance_score: int = 0
    useful_block_count: int = 0
    dropped_block_count: int = 0
    clean_text: str = ""
    dropped_reasons: List[str] = Field(default_factory=list)
    kept_blocks: List[CleanBlock] = Field(default_factory=list)
    dropped_blocks: List[CleanBlock] = Field(default_factory=list)
    signals: Dict[str, List[str]] = Field(default_factory=dict)
    source_success: bool = True
    source_error: str | None = None
    source_metadata: Dict[str, Any] = Field(default_factory=dict)


def preprocess_page_result(page: PageResult) -> ETLResult:
    blocks = split_markdown_into_blocks(page.markdown)
    cleaned_blocks: List[CleanBlock] = []
    seen_block_keys = set()

    for block in blocks:
        scored_block = score_block(block, page.url, page.title)
        block_key = normalize_block_key(scored_block.text)
        if scored_block.kept and block_key in seen_block_keys:
            cleaned_blocks.append(
                CleanBlock(
                    text=scored_block.text,
                    kept=False,
                    score=0,
                    reasons=["duplicate_block"],
                )
            )
            continue
        if scored_block.kept:
            seen_block_keys.add(block_key)
        cleaned_blocks.append(scored_block)

    kept_blocks = [block for block in cleaned_blocks if block.kept]
    dropped_blocks = [block for block in cleaned_blocks if not block.kept]
    clean_text = "\n\n".join(block.text for block in kept_blocks).strip()
    raw_has_fallback = any(re.search(pattern, page.markdown, re.IGNORECASE) for pattern in FALLBACK_PATTERNS)
    forced_page_type = None
    if raw_has_fallback and (len(clean_text) < 500 or looks_like_navigation_block(clean_text)):
        dropped_blocks.extend(
            CleanBlock(text=block.text, kept=False, score=0, reasons=["fallback_page"])
            for block in kept_blocks
        )
        kept_blocks = []
        clean_text = ""
        forced_page_type = "fallback_noise"

    signals = extract_signals(clean_text)
    relevance_score = sum(block.score for block in kept_blocks)

    return ETLResult(
        url=page.url,
        title=page.title,
        page_type=forced_page_type or classify_page(page.url, page.title, clean_text, dropped_blocks),
        relevance_score=relevance_score,
        useful_block_count=len(kept_blocks),
        dropped_block_count=len(dropped_blocks),
        clean_text=clean_text,
        dropped_reasons=sorted({reason for block in dropped_blocks for reason in block.reasons}),
        kept_blocks=kept_blocks,
        dropped_blocks=dropped_blocks,
        signals=signals,
        source_success=page.success,
        source_error=page.error,
        source_metadata=dict(page.metadata),
    )


def split_markdown_into_blocks(markdown: str) -> List[str]:
    if not markdown:
        return []

    lines = [normalize_line(line) for line in markdown.splitlines()]
    blocks: List[str] = []
    current: List[str] = []

    def flush() -> None:
        if current:
            text = "\n".join(current).strip()
            if text:
                blocks.append(text)
            current.clear()

    for line in lines:
        if not line:
            flush()
            continue
        if is_table_separator(line):
            continue
        current.append(line)
    flush()

    return [block for block in blocks if block]


def normalize_line(line: str) -> str:
    line = unicodedata.normalize("NFKC", line)
    line = line.replace("\u00a0", " ").replace("\u200b", "")
    line = re.sub(r"\s+", " ", line.strip())
    line = line.replace("\\|", "|")
    return line


def is_table_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\|?(?:\s*:?-{3,}:?\s*\|)+\s*", line))


def score_block(block: str, url: str, title: str) -> CleanBlock:
    original_block = block
    block = sanitize_block(block)
    if not block:
        return CleanBlock(text="", kept=False, score=-5, reasons=["fully_sanitized"])

    score = 0
    reasons: List[str] = []
    block_l = block.lower()
    title_l = title.lower()

    if any(term in url.lower() for term in URL_NOISE_TERMS):
        score -= 2
        reasons.append("noise_url")

    table_row_count = sum(1 for line in block.splitlines() if line.strip().startswith("|"))
    if table_row_count >= 3:
        score -= 2
        reasons.append("table_like")

    noise_hits = sum(1 for pattern in NOISE_PATTERNS if re.search(pattern, block, re.IGNORECASE))
    if noise_hits:
        score -= noise_hits * 2
        reasons.append("noise_terms")

    fallback_hits = sum(1 for pattern in FALLBACK_PATTERNS if re.search(pattern, block, re.IGNORECASE))
    if fallback_hits:
        score -= fallback_hits * 4
        reasons.append("fallback_page")

    useful_term_hits = sum(1 for term in MEDICAL_SIGNAL_TERMS if term in block_l)
    if useful_term_hits:
        score += min(useful_term_hits, 4)
        reasons.append("medical_terms")

    family_hits = sum(1 for term in PRODUCT_FAMILY_TERMS if term in block_l)
    if family_hits:
        score += family_hits * 2
        reasons.append("product_family")

    title_hits = sum(1 for term in MEDICAL_SIGNAL_TERMS if term in title_l)
    if title_hits:
        score += min(title_hits, 2)
        reasons.append("title_context")

    title_family_hits = sum(1 for term in PRODUCT_FAMILY_TERMS if term in title_l)
    if title_family_hits:
        score += title_family_hits * 2
        reasons.append("title_family")

    spec_hits = sum(1 for pattern in SPEC_PATTERNS if re.search(pattern, block, re.IGNORECASE))
    if spec_hits:
        score += spec_hits * 2
        reasons.append("spec_pattern")

    if len(block) < 40:
        score -= 1
        reasons.append("thin_block")

    if block_l.count("cookie") >= 2 or block_l.count("session") >= 2:
        score -= 3
        reasons.append("cookie_heavy")

    if looks_like_junk(block):
        score -= 3
        reasons.append("junk_structure")

    if looks_like_navigation_block(block):
        score -= 4
        reasons.append("navigation_block")

    if len(block) < len(original_block):
        reasons.append("sanitized")

    kept = score > 0
    return CleanBlock(text=block, kept=kept, score=score, reasons=reasons)


def sanitize_block(block: str) -> str:
    cleaned_lines: List[str] = []
    for line in block.splitlines():
        normalized = normalize_line(line)
        if not normalized:
            continue
        if should_drop_line(normalized):
            continue
        cleaned_lines.append(normalized)

    return "\n".join(cleaned_lines).strip()


def should_drop_line(line: str) -> bool:
    line_l = line.lower()
    if is_cookieish_line(line):
        return True
    if line.strip().startswith("|") and count_noise_terms(line_l) >= 1:
        return True
    if count_noise_terms(line_l) >= 2 and count_useful_terms(line_l) == 0:
        return True
    return False


def count_noise_terms(text: str) -> int:
    return sum(1 for pattern in NOISE_PATTERNS if re.search(pattern, text, re.IGNORECASE))


def count_useful_terms(text: str) -> int:
    useful_hits = sum(1 for term in MEDICAL_SIGNAL_TERMS if term in text)
    family_hits = sum(1 for term in PRODUCT_FAMILY_TERMS if term in text)
    spec_hits = sum(1 for pattern in SPEC_PATTERNS if re.search(pattern, text, re.IGNORECASE))
    return useful_hits + family_hits + spec_hits


def looks_non_english(text: str) -> bool:
    german_hits = sum(1 for hint in GERMAN_HINTS if hint in text)
    if german_hits >= 1:
        return True
    umlaut_hits = sum(text.count(ch) for ch in ("ä", "ö", "ü", "ß"))
    return umlaut_hits >= 1 and count_useful_terms(text) == 0


def looks_like_junk(block: str) -> bool:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if not lines:
        return True
    if len(lines) >= 4 and all(is_cookieish_line(line) for line in lines):
        return True
    if sum(char.isdigit() for char in block) > len(block) * 0.2 and "product" not in block.lower():
        return True
    return False


def looks_like_navigation_block(block: str) -> bool:
    lines = [line.strip().lower() for line in block.splitlines() if line.strip()]
    if not lines:
        return True
    short_lines = sum(1 for line in lines if len(line) <= 35)
    nav_hits = sum(1 for line in lines if any(term in line for term in NAVIGATION_TERMS))
    useful_hits = count_useful_terms(block.lower())
    return len(lines) >= 3 and short_lines / len(lines) >= 0.8 and nav_hits >= 2 and useful_hits <= nav_hits


def is_cookieish_line(line: str) -> bool:
    line_l = line.lower()
    cookie_terms = ("cookie", "session", "local storage", "privacy", "auth0", "persistent")
    return any(term in line_l for term in cookie_terms)


def classify_page(url: str, title: str, clean_text: str, dropped_blocks: Sequence[CleanBlock]) -> str:
    haystack = f"{url}\n{title}\n{clean_text}".lower()
    if any(term in url.lower() for term in ("privacy", "cookie", "legal", "terms")):
        return "legal_noise"
    source_label = f"{url}\n{title}".lower()
    if "support" in source_label or "documentation" in source_label:
        return "support_noise"
    if any(re.search(pattern, haystack, re.IGNORECASE) for pattern in FALLBACK_PATTERNS):
        return "fallback_noise"
    if "product" in haystack or "system" in haystack or "scanner" in haystack:
        if any(term in haystack for term in PRODUCT_FAMILY_TERMS):
            return "product_detail"
        return "product_category"
    if clean_text and len(clean_text) < 160 and len(dropped_blocks) > 0:
        return "mixed_low_quality"
    return "unknown"


def extract_signals(clean_text: str) -> Dict[str, List[str]]:
    text_l = clean_text.lower()
    modality_hits = sorted({term for term in MEDICAL_SIGNAL_TERMS if term in text_l})
    family_hits = sorted({term for term in PRODUCT_FAMILY_TERMS if term in text_l})
    spec_hits: List[str] = []
    for pattern in SPEC_PATTERNS:
        spec_hits.extend(re.findall(pattern, clean_text, re.IGNORECASE))

    return {
        "medical_terms": modality_hits[:12],
        "product_families": family_hits[:10],
        "spec_values": dedupe_strings(spec_hits)[:12],
    }


def dedupe_strings(values: Sequence[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        item = re.sub(r"\s+", " ", str(value).strip())
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def normalize_block_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()

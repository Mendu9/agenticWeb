from __future__ import annotations

import re
from typing import Iterable, List, Optional

from bs4 import BeautifulSoup

try:
    from .extraction_models import ExtractedTable, PageBlock, StructuredPage
except ImportError:
    from extraction_models import ExtractedTable, PageBlock, StructuredPage


NOISE_SELECTORS = [
    "script",
    "style",
    "noscript",
    "iframe",
    "header",
    "footer",
    "nav",
    "aside",
    "dialog",
    "[role='navigation']",
    "[role='banner']",
    "[role='contentinfo']",
    "[id*='cookie' i]",
    "[class*='cookie' i]",
    "[id*='consent' i]",
    "[class*='consent' i]",
    "[id*='privacy' i]",
    "[class*='privacy' i]",
]

TEXT_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "dt", "dd", "figcaption"]


def parse_structured_page(html: str, url: str, title: str = "") -> StructuredPage:
    soup = _make_soup(html)
    for selector in NOISE_SELECTORS:
        for element in soup.select(selector):
            element.decompose()

    root = soup.find("main") or soup.find("article") or soup.find("body") or soup
    blocks: List[PageBlock] = []
    tables: List[ExtractedTable] = []
    block_index = 0

    for table in root.find_all("table"):
        parsed = _parse_table(table)
        if parsed.rows:
            tables.append(parsed)
            blocks.append(
                PageBlock(
                    block_id=f"table_{block_index}",
                    block_type="table",
                    text=_table_to_text(parsed),
                    heading=parsed.title,
                    url=url,
                    provenance=_element_provenance(table),
                    table=parsed,
                )
            )
            block_index += 1

    for element in root.find_all(["section", "article", "div"], recursive=True):
        block = _parse_card_or_section(element, url, block_index)
        if block:
            blocks.append(block)
            block_index += 1

    for element in root.find_all(TEXT_TAGS, recursive=True):
        if _is_nested_inside_card_like(element):
            continue
        text = _normalize_text(element.get_text(" ", strip=True))
        if not text:
            continue
        blocks.append(
            PageBlock(
                block_id=f"text_{block_index}",
                block_type=_heading_or_text_type(element.name),
                text=text,
                heading=text if element.name.startswith("h") else "",
                url=url,
                provenance=_element_provenance(element),
            )
        )
        block_index += 1

    deduped_blocks = _dedupe_blocks(blocks)
    combined_text = "\n\n".join(block.text for block in deduped_blocks if block.text).strip()
    return StructuredPage(
        url=url,
        title=title,
        blocks=deduped_blocks,
        tables=tables,
        raw_text=combined_text,
        clean_text=combined_text,
        metadata={"block_count": str(len(deduped_blocks)), "table_count": str(len(tables))},
    )


def _parse_card_or_section(element, url: str, block_index: int) -> Optional[PageBlock]:
    if not _is_content_container(element):
        return None

    links = [a.get("href", "").strip() for a in element.find_all("a", href=True)]
    text_parts, heading = _collect_text_parts(element)
    combined = "\n".join(text_parts)

    block_type = "card" if len(links) >= 1 and (heading or len(text_parts) >= 2) else "section"
    return PageBlock(
        block_id=f"{block_type}_{block_index}",
        block_type=block_type,
        text=combined,
        heading=heading,
        url=url,
        links=_clean_links(links),
        provenance=_element_provenance(element),
        metadata={"text_part_count": str(len(text_parts))},
    )


def _collect_text_parts(element) -> tuple[List[str], str]:
    text_parts = []
    heading = ""

    for tag in element.find_all(TEXT_TAGS, recursive=True):
        text = _normalize_text(tag.get_text(" ", strip=True))
        if not text:
            continue
        if tag.name.startswith("h") and not heading:
            heading = text
        text_parts.append(text)

    return _dedupe_strings(text_parts), heading


def _is_content_container(element) -> bool:
    if element.name not in {"section", "article", "div"}:
        return False

    text_parts, heading = _collect_text_parts(element)
    if not text_parts:
        return False

    links = element.find_all("a", href=True)
    combined = "\n".join(text_parts)
    return bool(heading or len(text_parts) >= 2 or (links and len(combined) >= 40))


def _parse_table(table) -> ExtractedTable:
    headers = [
        _normalize_text(cell.get_text(" ", strip=True))
        for cell in table.find_all(["th"])
        if _normalize_text(cell.get_text(" ", strip=True))
    ]
    rows: List[List[str]] = []
    for tr in table.find_all("tr"):
        cells = [
            _normalize_text(cell.get_text(" ", strip=True))
            for cell in tr.find_all(["td", "th"])
        ]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(cells)

    title = ""
    caption = table.find("caption")
    if caption:
        title = _normalize_text(caption.get_text(" ", strip=True))

    return ExtractedTable(
        title=title,
        headers=headers,
        rows=rows,
        table_type=_classify_table(headers, rows),
    )


def _classify_table(headers: List[str], rows: List[List[str]]) -> str:
    joined = " ".join(headers + [cell for row in rows for cell in row]).lower()
    if any(term in joined for term in ("cookie", "privacy", "session", "local storage", "consent")):
        return "cookie_legal"
    if any(term in joined for term in ("tesla", "bore", "slice", "throughput", "detector", "parameter", "spec")):
        return "spec_table"
    return "unknown"


def _table_to_text(table: ExtractedTable) -> str:
    parts: List[str] = []
    if table.title:
        parts.append(table.title)
    if table.headers:
        parts.append(" | ".join(table.headers))
    for row in table.rows:
        parts.append(" | ".join(row))
    return "\n".join(parts).strip()


def _clean_links(links: Iterable[str]) -> List[str]:
    cleaned: List[str] = []
    seen = set()
    for link in links:
        value = link.strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return cleaned


def _heading_or_text_type(tag_name: str) -> str:
    return "heading" if tag_name.startswith("h") else "text"


def _is_nested_inside_card_like(element) -> bool:
    parent = element.parent
    while parent is not None:
        if _is_content_container(parent):
            return True
        parent = parent.parent
    return False


def _element_provenance(element) -> dict[str, str]:
    path_parts: List[str] = []
    current = element
    while current is not None and getattr(current, "name", None):
        index = 1
        sibling = current.previous_sibling
        while sibling is not None:
            if getattr(sibling, "name", None) == current.name:
                index += 1
            sibling = sibling.previous_sibling

        part = current.name
        element_id = current.get("id")
        if element_id:
            part += f"#{element_id}"
        classes = current.get("class") or []
        if classes:
            part += "." + ".".join(classes[:2])
        part += f"[{index}]"
        path_parts.append(part)

        current = current.parent
        if current is not None and getattr(current, "name", None) in {"html", "[document]"}:
            break

    return {
        "source_tag": getattr(element, "name", ""),
        "source_path": ">".join(reversed(path_parts)),
    }


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_strings(values: List[str]) -> List[str]:
    deduped: List[str] = []
    seen = set()
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def _dedupe_blocks(blocks: List[PageBlock]) -> List[PageBlock]:
    deduped: List[PageBlock] = []
    seen = set()
    for block in blocks:
        key = (
            block.block_type,
            block.heading.lower(),
            block.text.lower(),
            block.provenance.get("source_path", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(block)
    return deduped


def _make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")

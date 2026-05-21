from __future__ import annotations

import re
from typing import Dict, List

try:
    from .extraction_models import ClinicalParameter, ProductRecord, StructuredPage
except ImportError:
    from extraction_models import ClinicalParameter, ProductRecord, StructuredPage


PARAMETER_PATTERNS: Dict[str, re.Pattern[str]] = {
    "field_strength": re.compile(r"\b\d+(?:\.\d+)?\s*T(?:esla)?\b", re.IGNORECASE),
    "injected_dose": re.compile(r"Injected dose:\s*([^\n]+)", re.IGNORECASE),
    "uptake_time": re.compile(r"Uptake time:\s*([^\n]+)", re.IGNORECASE),
    "mr_ta": re.compile(r"MR TA:\s*([^\n]+)", re.IGNORECASE),
    "pet_ta": re.compile(r"PET TA:\s*([^\n]+)", re.IGNORECASE),
    "scan_range": re.compile(r"Total scan range:\s*([^\n]+)", re.IGNORECASE),
    "sequences": re.compile(r"Sequences:\s*([^\n]+)", re.IGNORECASE),
    "weight": re.compile(r"\b\d+\s*kg\b", re.IGNORECASE),
}

MODALITY_HINTS = {
    "mri": ("mri", "magnetic resonance"),
    "ct": ("ct", "computed tomography"),
    "pet_mr": ("pet/mr", "pet mr"),
    "ultrasound": ("ultrasound",),
    "radiography": ("radiography", "x-ray"),
}

PRODUCT_FAMILY_HINTS = (
    "magnetom",
    "somatom",
    "biograph",
    "epiq",
    "ingenia",
    "revolution",
    "signa",
    "cobas",
)


def extract_product_records(page: StructuredPage) -> List[ProductRecord]:
    candidates = [block for block in page.blocks if _is_product_candidate(block)]
    records: List[ProductRecord] = []

    if candidates:
        for block in candidates:
            records.append(
                _record_from_block(
                    page,
                    _product_name_for_block(page, block),
                    block.text,
                    block.block_id,
                )
            )
    else:
        title = page.title or _first_heading(page)
        if title:
            records.append(_record_from_block(page, title, page.clean_text or page.raw_text, "page_root"))

    return _merge_records(records)


def _record_from_block(page: StructuredPage, product_name: str, text: str, block_id: str) -> ProductRecord:
    modality = _detect_modality(page.title + "\n" + text)
    family = _detect_family(product_name + "\n" + text)
    technical_specs: Dict[str, List[str]] = {}
    parameters: List[ClinicalParameter] = []

    for name, pattern in PARAMETER_PATTERNS.items():
        matches = pattern.findall(text)
        values = _normalize_values(matches if isinstance(matches, list) else [matches])
        if values:
            technical_specs[name] = values
            for value in values:
                parameters.append(
                    ClinicalParameter(
                        name=name,
                        value=value,
                        evidence_text=value,
                        source_block_id=block_id,
                    )
                )

    return ProductRecord(
        product_name=product_name,
        modality=modality,
        product_family=family,
        summary=_summarize_text(text),
        technical_specs=technical_specs,
        clinical_parameters=parameters,
        evidence_block_ids=[block_id],
        source_url=page.url,
    )


def _is_product_candidate(block) -> bool:
    if block.block_type == "card":
        return True
    if block.block_type not in {"section", "text", "heading"}:
        return False

    text = f"{block.heading}\n{block.text}"
    if any(pattern.search(text) for pattern in PARAMETER_PATTERNS.values()):
        return True
    if block.block_type in {"section", "card"} and (block.links or block.heading):
        return True
    if _detect_family(text) or _detect_modality(text) != "unknown":
        return True
    return len(block.text) >= 80 and block.block_type == "section"


def _product_name_for_block(page: StructuredPage, block) -> str:
    if block.heading:
        return block.heading
    if block.block_type in {"card", "section"}:
        first_line = _first_line(block.text)
        if first_line:
            return first_line
    title = _first_heading(page) or page.title
    if title:
        return title
    return _first_line(block.text)


def _detect_modality(text: str) -> str:
    lowered = text.lower()
    for modality, hints in MODALITY_HINTS.items():
        if any(hint in lowered for hint in hints):
            return modality
    return "unknown"


def _detect_family(text: str) -> str:
    lowered = text.lower()
    for family in PRODUCT_FAMILY_HINTS:
        if family in lowered:
            return family
    return ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        normalized = line.strip()
        if normalized:
            return normalized
    return ""


def _first_heading(page: StructuredPage) -> str:
    for block in page.blocks:
        if block.heading:
            return block.heading
        if block.block_type == "heading" and block.text:
            return block.text
    return ""


def _summarize_text(text: str) -> str:
    parts = [part.strip() for part in re.split(r"\.\s+", text) if part.strip()]
    return ". ".join(parts[:3]).strip()


def _normalize_values(values: List[str]) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value).strip())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned)
    return normalized


def _merge_records(records: List[ProductRecord]) -> List[ProductRecord]:
    merged: Dict[str, ProductRecord] = {}
    for record in records:
        key = record.product_name.lower()
        if key not in merged:
            merged[key] = record
            continue
        current = merged[key]
        for spec_name, values in record.technical_specs.items():
            existing = current.technical_specs.setdefault(spec_name, [])
            for value in values:
                if value not in existing:
                    existing.append(value)
        current.clinical_parameters.extend(
            parameter for parameter in record.clinical_parameters
            if parameter not in current.clinical_parameters
        )
        current.evidence_block_ids.extend(
            block_id for block_id in record.evidence_block_ids
            if block_id not in current.evidence_block_ids
        )
    return list(merged.values())

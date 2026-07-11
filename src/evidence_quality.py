"""Evidence filtering and deduplication helpers.

These rules keep scoring focused on official, accessible, non-boilerplate
evidence instead of raw evidence volume.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from .models import EvidenceRecord


SCHEMA_TYPE_BOILERPLATE = {
    "breadcrumblist",
    "collectionpage",
    "faqpage",
    "itemlist",
    "listitem",
    "organization",
    "product",
    "videoobject",
    "webpage",
    "website",
}

GENERIC_LINK_LABELS = {
    "learn more",
    "read more",
    "view product",
    "shop now",
    "skip to content",
    "quick links",
    "en",
    "cn",
    "jp",
    "ko",
}

INVALID_TEXT_MARKERS = (
    "access denied",
    "captcha challenge",
    "checking your browser",
    "enable cookies",
    "enter using password",
    "opening soon",
    "password protected",
    "store owner? log in",
    "subscribe to continue",
    "verify you are human",
)

BOILERPLATE_TEXT_MARKERS = (
    "accept cookies",
    "all rights reserved",
    "cookie policy",
    "continue shopping",
    "powered by shopify",
    "privacy policy",
    "proceed to cart",
    "terms & conditions",
    "terms and conditions",
    "your cart is empty",
)

USER_GENERATED_MARKERS = (
    "customer review",
    "customer reviews",
    "reviewed by",
    "testimonial",
    "verified buyer",
    "verified purchase",
    "write a review",
)

GATED_PATH_MARKERS = (
    "/password",
    "/account/login",
    "/login",
)


def is_gated_or_restricted_url(url: str) -> bool:
    """Return True when the final URL is not valid qualification evidence."""

    path = urlsplit(url or "").path.casefold().rstrip("/")
    if not path:
        return False
    return any(path == marker or path.endswith(marker) for marker in GATED_PATH_MARKERS)


def is_invalid_evidence_record(record: EvidenceRecord) -> bool:
    """Return True when an evidence row should not support scoring."""

    if is_gated_or_restricted_url(record.source_url):
        return True
    value = _normalize_text(record.fact_value)
    snippet = _normalize_text(record.evidence_snippet)
    combined = f"{value} {snippet}".strip()
    if not combined:
        return True
    if record.fact_type == "industry_or_category" and value in SCHEMA_TYPE_BOILERPLATE:
        return True
    if record.extraction_method == "visible_public_link" and value in GENERIC_LINK_LABELS:
        return True
    if value in {"", "#", "2"}:
        return True
    if _contains_any(combined, INVALID_TEXT_MARKERS):
        return True
    if _contains_any(combined, USER_GENERATED_MARKERS):
        return True
    if _is_boilerplate_only(combined):
        return True
    if record.fact_type == "marketing_or_content_activity_signal" and value == "newsletter":
        return True
    if record.fact_type == "ecommerce_signal" and value == "cart" and "your cart is empty" in combined:
        return True
    return False


def filter_and_dedupe_evidence(records: list[EvidenceRecord]) -> list[EvidenceRecord]:
    """Filter invalid evidence and retain one best record per fact type per page."""

    best_by_key: dict[tuple[str, str, str, str], EvidenceRecord] = {}
    best_per_page_fact: dict[tuple[str, str, str], EvidenceRecord] = {}

    for record in records:
        if is_invalid_evidence_record(record):
            continue
        canonical_url = canonicalize_source_url(record.source_url)
        snippet_key = normalized_evidence_snippet(record.evidence_snippet)
        stable_key = (
            record.seed_id,
            canonical_url,
            record.fact_type,
            snippet_key,
        )
        current = best_by_key.get(stable_key)
        if current is None or evidence_rank(record) > evidence_rank(current):
            best_by_key[stable_key] = record

    for record in best_by_key.values():
        page_fact_key = (record.seed_id, canonicalize_source_url(record.source_url), record.fact_type)
        current = best_per_page_fact.get(page_fact_key)
        if current is None or evidence_rank(record) > evidence_rank(current):
            best_per_page_fact[page_fact_key] = record

    return sorted(
        best_per_page_fact.values(),
        key=lambda item: (item.seed_id, canonicalize_source_url(item.source_url), item.fact_type),
    )


def normalized_evidence_snippet(snippet: str) -> str:
    """Normalize snippets for stable deduplication."""

    normalized = _normalize_text(snippet)
    normalized = re.sub(r"\d+", "#", normalized)
    normalized = re.sub(r"https?://\S+", "url", normalized)
    return normalized[:240]


def canonicalize_source_url(url: str) -> str:
    split = urlsplit(url or "")
    scheme = split.scheme.casefold()
    host = (split.hostname or "").casefold().strip(".")
    if host.startswith("www."):
        host = host[4:]
    path = split.path.rstrip("/") or "/"
    return urlunsplit((scheme, host, path, "", ""))


def evidence_rank(record: EvidenceRecord) -> tuple[int, int, int]:
    confidence_rank = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
    method_rank = {
        "json_ld": 5,
        "meta_description": 4,
        "metadata": 4,
        "open_graph": 4,
        "page_type": 3,
        "visible_public_link": 2,
        "visible_public_text": 1,
    }
    return (
        confidence_rank.get(record.confidence, 0),
        method_rank.get(record.extraction_method, 0),
        len(record.evidence_snippet or ""),
    )


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _is_boilerplate_only(text: str) -> bool:
    if _contains_any(text, BOILERPLATE_TEXT_MARKERS):
        substantive_markers = (
            "ayurveda",
            "beauty",
            "fitness",
            "health",
            "nutrition",
            "product",
            "skincare",
            "supplement",
            "wellness",
        )
        return not _contains_any(text, substantive_markers)
    return False


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()

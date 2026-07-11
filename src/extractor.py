"""Evidence-backed field extraction from collected public pages.

The extractor is deliberately conservative. It emits facts only when a public
source string can be attached as an evidence snippet, and it prefers JSON-LD and
metadata before visible text cues.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from .evidence_quality import filter_and_dedupe_evidence, is_gated_or_restricted_url
from .html_analysis import HtmlAnalysis, analyze_html
from .models import CollectedPage, EvidenceRecord, NormalizedSeed


FACT_TYPES = (
    "industry_or_category",
    "product_or_service_type",
    "business_model",
    "geography_or_market_signal",
    "target_audience_signal",
    "core_product_positioning",
    "key_claims_or_messaging",
    "ecommerce_signal",
    "marketing_or_content_activity_signal",
    "public_contact_route",
)

STRUCTURED_DATA_PREFERENCES = (
    "json_ld",
    "meta_description",
    "open_graph",
    "visible_public_text",
)

GENERIC_EMAIL_PREFIXES = {
    "care",
    "contact",
    "customercare",
    "hello",
    "help",
    "info",
    "sales",
    "support",
}

INDUSTRY_KEYWORDS = {
    "wellness": "wellness",
    "nutrition": "nutrition",
    "supplement": "nutrition supplements",
    "fitness": "fitness",
    "health": "health",
    "skincare": "skincare",
    "skin care": "skincare",
    "cosmetic": "cosmetics",
    "beauty": "beauty",
    "lifestyle": "lifestyle",
}

TARGET_AUDIENCE_KEYWORDS = {
    "men": "men",
    "women": "women",
    "kids": "kids",
    "family": "family",
    "athletes": "athletes",
    "parents": "parents",
    "mothers": "mothers",
}

ECOMMERCE_KEYWORDS = (
    "add to cart",
    "cart",
    "checkout",
    "shop now",
    "buy now",
    "sale price",
)

MARKETING_KEYWORDS = (
    "blog",
    "journal",
    "campaign",
    "newsletter",
    "instagram",
    "facebook",
    "youtube",
)


def extract_evidence(
    normalized_seed: NormalizedSeed,
    collected_pages: list[CollectedPage],
) -> list[EvidenceRecord]:
    """Extract evidence-backed facts from fetched public pages."""

    evidence: list[EvidenceRecord] = []
    seen: set[tuple[str, str, str, str]] = set()

    for page in collected_pages:
        if page.status != "fetched":
            continue

        source_url = page.final_url or page.source_url
        if is_gated_or_restricted_url(source_url):
            continue

        def add(
            fact_type: str,
            fact_value: str,
            snippet: str,
            confidence: str,
            method: str,
        ) -> None:
            cleaned_value = _redact_disallowed_contact_info(_clean_text(fact_value))
            cleaned_snippet = _truncate_snippet(
                _redact_disallowed_contact_info(snippet)
            )
            if not cleaned_value or not cleaned_snippet:
                return
            key = (fact_type, cleaned_value.casefold(), source_url, method)
            if key in seen:
                return
            seen.add(key)
            evidence.append(
                EvidenceRecord(
                    seed_id=normalized_seed.seed_id,
                    company_name=normalized_seed.clean_company_name,
                    normalized_domain=normalized_seed.normalized_domain,
                    fact_type=fact_type,
                    fact_value=cleaned_value,
                    source_url=source_url,
                    evidence_snippet=cleaned_snippet,
                    collected_at=page.collected_at,
                    confidence=confidence,
                    extraction_method=method,
                )
            )

        _extract_json_ld(page, add)

        if not page.html:
            continue

        html_summary = analyze_html(page.html, base_url=source_url)
        _extract_from_metadata(html_summary, add)
        _extract_from_links(html_summary, page, source_url, add)
        _extract_from_visible_text(html_summary, page, add)

    return filter_and_dedupe_evidence(evidence)


def _extract_json_ld(page: CollectedPage, add: Any) -> None:
    for block in page.json_ld_blocks:
        for item in _json_ld_items(block):
            item_type = _json_value_as_text(item.get("@type"))
            name = _json_value_as_text(item.get("name"))
            description = _json_value_as_text(item.get("description"))
            category = _json_value_as_text(item.get("category"))

            if item_type:
                add(
                    "industry_or_category",
                    item_type,
                    _json_snippet("@type", item_type),
                    "high",
                    "json_ld",
                )
            if category:
                add(
                    "industry_or_category",
                    category,
                    _json_snippet("category", category),
                    "high",
                    "json_ld",
                )
            if name and item_type and "product" in item_type.casefold():
                add(
                    "product_or_service_type",
                    name,
                    _json_snippet("name", name),
                    "high",
                    "json_ld",
                )
            if description:
                add(
                    "core_product_positioning",
                    description,
                    _json_snippet("description", description),
                    "high",
                    "json_ld",
                )
            if _has_json_key(item, "offers"):
                add(
                    "ecommerce_signal",
                    "JSON-LD offers data",
                    "JSON-LD contains offers",
                    "high",
                    "json_ld",
                )

            address = item.get("address")
            if isinstance(address, dict):
                country = _json_value_as_text(address.get("addressCountry"))
                if country:
                    add(
                        "geography_or_market_signal",
                        country,
                        _json_snippet("addressCountry", country),
                        "high",
                        "json_ld",
                    )

            contact_points = item.get("contactPoint")
            for contact in _as_list(contact_points):
                if isinstance(contact, dict):
                    contact_type = _json_value_as_text(contact.get("contactType"))
                    email = _json_value_as_text(contact.get("email"))
                    if email and _is_generic_email(email):
                        add(
                            "public_contact_route",
                            email,
                            _json_snippet("email", email),
                            "medium",
                            "json_ld",
                        )
                    elif contact_type:
                        add(
                            "public_contact_route",
                            contact_type,
                            _json_snippet("contactType", contact_type),
                            "medium",
                            "json_ld",
                        )


def _extract_from_metadata(html: HtmlAnalysis, add: Any) -> None:
    description = html.meta_description or html.open_graph.get("og:description")
    title = html.open_graph.get("og:title") or html.page_title

    if description:
        add(
            "core_product_positioning",
            description,
            description,
            "medium",
            "meta_description",
        )
        _extract_keyword_fact(
            text=description,
            keywords=INDUSTRY_KEYWORDS,
            fact_type="industry_or_category",
            confidence="medium",
            method="meta_description",
            add=add,
        )
    if title:
        add(
            "key_claims_or_messaging",
            title,
            title,
            "medium",
            "metadata",
        )


def _extract_from_links(
    html: HtmlAnalysis,
    page: CollectedPage,
    source_url: str,
    add: Any,
) -> None:
    for link in html.links:
        href = link.href
        label = link.text
        absolute_href = urljoin(source_url, href)
        path = urlparse(absolute_href).path.casefold()
        label_or_href = label or href
        snippet = f"{label_or_href} -> {absolute_href}"

        if href.startswith("mailto:"):
            email = href.removeprefix("mailto:").split("?", 1)[0].strip()
            if _is_generic_email(email):
                add(
                    "public_contact_route",
                    email,
                    snippet,
                    "medium",
                    "visible_public_link",
                )
        elif "contact" in path:
            add(
                "public_contact_route",
                absolute_href,
                snippet,
                "medium",
                "visible_public_link",
            )
        elif any(token in path for token in ("product", "collections", "shop", "store")):
            add(
                "product_or_service_type",
                label_or_href,
                snippet,
                "low",
                "visible_public_link",
            )
        elif any(token in path for token in ("blog", "journal", "article")):
            add(
                "marketing_or_content_activity_signal",
                label_or_href,
                snippet,
                "low",
                "visible_public_link",
            )

    if page.page_type == "product":
        add(
            "product_or_service_type",
            "Public product or collection page",
            page.final_url or page.source_url,
            "medium",
            "page_type",
        )
    elif page.page_type == "blog":
        add(
            "marketing_or_content_activity_signal",
            "Public blog or article page",
            page.final_url or page.source_url,
            "medium",
            "page_type",
        )
    elif page.page_type == "contact":
        add(
            "public_contact_route",
            page.final_url or page.source_url,
            page.final_url or page.source_url,
            "medium",
            "page_type",
        )


def _extract_from_visible_text(html: HtmlAnalysis, page: CollectedPage, add: Any) -> None:
    text = html.visible_text
    if not text:
        return

    _extract_keyword_fact(
        text=text,
        keywords=INDUSTRY_KEYWORDS,
        fact_type="industry_or_category",
        confidence="low",
        method="visible_public_text",
        add=add,
    )
    _extract_keyword_fact(
        text=text,
        keywords=TARGET_AUDIENCE_KEYWORDS,
        fact_type="target_audience_signal",
        confidence="low",
        method="visible_public_text",
        add=add,
    )

    lower_text = text.casefold()
    for keyword in ECOMMERCE_KEYWORDS:
        if keyword in lower_text:
            add(
                "ecommerce_signal",
                keyword,
                _snippet_around(text, keyword),
                "low",
                "visible_public_text",
            )
            break

    for keyword in MARKETING_KEYWORDS:
        if keyword in lower_text:
            add(
                "marketing_or_content_activity_signal",
                keyword,
                _snippet_around(text, keyword),
                "low",
                "visible_public_text",
            )
            break

    if any(token in lower_text for token in ("online store", "shop online", "e-commerce")):
        add(
            "business_model",
            "consumer e-commerce",
            _snippet_around(text, "online"),
            "low",
            "visible_public_text",
        )

    if page.normalized_domain.endswith(".in") or " india " in f" {lower_text} ":
        snippet = _snippet_around(text, "India") if "india" in lower_text else page.source_url
        add(
            "geography_or_market_signal",
            "India",
            snippet,
            "low",
            "visible_public_text",
        )


def _extract_keyword_fact(
    text: str,
    keywords: dict[str, str],
    fact_type: str,
    confidence: str,
    method: str,
    add: Any,
) -> None:
    lower_text = text.casefold()
    for keyword, value in keywords.items():
        if keyword in lower_text:
            add(fact_type, value, _snippet_around(text, keyword), confidence, method)
            return

def _json_ld_items(block: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(block)
    except json.JSONDecodeError:
        return []
    return [item for item in _walk_json_objects(parsed) if isinstance(item, dict)]


def _walk_json_objects(value: Any) -> list[Any]:
    if isinstance(value, dict):
        items: list[Any] = [value]
        if "@graph" in value:
            items.extend(_walk_json_objects(value["@graph"]))
        return items
    if isinstance(value, list):
        items = []
        for item in value:
            items.extend(_walk_json_objects(item))
        return items
    return []


def _has_json_key(value: Any, wanted_key: str) -> bool:
    if isinstance(value, dict):
        return wanted_key in value or any(_has_json_key(item, wanted_key) for item in value.values())
    if isinstance(value, list):
        return any(_has_json_key(item, wanted_key) for item in value)
    return False


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _json_value_as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return _clean_text(", ".join(_json_value_as_text(item) for item in value))
    if isinstance(value, dict):
        return ""
    return _clean_text(str(value))


def _json_snippet(key: str, value: str) -> str:
    return f'"{key}": "{_truncate_snippet(value, limit=180)}"'


def _is_generic_email(email: str) -> bool:
    if not email or "@" not in email:
        return False
    prefix = email.split("@", 1)[0].casefold()
    if "." in prefix:
        return False
    return prefix in GENERIC_EMAIL_PREFIXES


def _redact_disallowed_contact_info(text: str) -> str:
    text = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        _email_replacement,
        text or "",
    )
    return re.sub(
        r"(?<!\w)(?:\+?\d[\d\s().-]{8,}\d)(?!\w)",
        "[redacted_phone]",
        text,
    )


def _email_replacement(match: re.Match[str]) -> str:
    email = match.group(0)
    return email if _is_generic_email(email) else "[redacted_email]"


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _truncate_snippet(text: str, limit: int = 240) -> str:
    cleaned = _clean_text(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _snippet_around(text: str, keyword: str, window: int = 90) -> str:
    lower_text = text.casefold()
    keyword_index = lower_text.find(keyword.casefold())
    if keyword_index == -1:
        return _truncate_snippet(text)
    start = max(0, keyword_index - window)
    end = min(len(text), keyword_index + len(keyword) + window)
    return _truncate_snippet(text[start:end])

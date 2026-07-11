"""Transparent 10-point lead qualification scoring and lead assembly."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from .evidence_quality import (
    canonicalize_source_url,
    evidence_rank,
    filter_and_dedupe_evidence,
    is_gated_or_restricted_url,
)
from .models import (
    EvidenceRecord,
    LeadRecord,
    NormalizedSeed,
    ReviewQueueRecord,
    ScoreResult,
)


SCORING_MODEL = {
    "indian_consumer_d2c_and_target_category_fit": 3,
    "active_accessible_website_or_ecommerce": 2,
    "clear_product_or_service_offering": 2,
    "substantive_marketing_or_growth_signal": 2,
    "supporting_evidence_and_source_quality": 1,
}

LEGACY_SIGNAL_ALIASES = {
    "relevant_industry_or_category": "indian_consumer_d2c_and_target_category_fit",
    "active_website_or_ecommerce_presence": "active_accessible_website_or_ecommerce",
    "clear_product_or_service_offering": "clear_product_or_service_offering",
    "marketing_or_growth_signal": "substantive_marketing_or_growth_signal",
    "supporting_evidence_and_source_quality": "supporting_evidence_and_source_quality",
}

QUALIFIED_MIN_SCORE = 7
PENDING_REVIEW_MIN_SCORE = 4
MEDIUM_HIGH_CONFIDENCE = {"medium", "high"}

TARGET_CATEGORY_KEYWORDS = (
    "ayurveda",
    "beauty",
    "body care",
    "cosmetic",
    "fitness",
    "food",
    "hair care",
    "health",
    "lifestyle",
    "makeup",
    "nutrition",
    "personal care",
    "protein",
    "skincare",
    "skin care",
    "supplement",
    "wellness",
)

B2B_MARKERS = (
    "b2b",
    "business-to-business",
    "ingredient business",
    "ingredient portfolio",
    "ingredients to nutrition brands",
    "natural ingredients to nutrition brands",
    "our customers",
    "serving customers",
    "speciality actives",
    "standard actives",
    "to nutrition brands",
)

PARENT_COMPANY_MARKERS = (
    "brings you india's loved brands",
    "consumer ltd",
    "house of brands",
    "our brands",
    "parent",
    "portfolio of brands",
)

MARKETPLACE_MARKERS = (
    "marketplace",
    "bodybuilding supplements online",
    "buy 100% genuine",
)

TRANSACTION_MARKERS = (
    "add to cart",
    "buy now",
    "checkout",
    "json-ld offers data",
    "price",
    "sale price",
    "shop now",
    "subscribe and save",
)

SUBSTANTIVE_MARKETING_MARKERS = (
    "blog",
    "campaign",
    "editorial",
    "journal",
    "launch",
    "new flavor",
    "new flavors",
    "new launch",
    "news",
    "offer",
    "press",
    "sale",
    "story",
)

FOREIGN_COUNTRY_VALUES = {
    "gb",
    "uk",
    "united kingdom",
    "united states",
    "usa",
    "us",
}

FIELD_FACT_TYPES = {
    "industry_or_category": "industry_or_category",
    "product_or_service_type": "product_or_service_type",
    "business_model": "business_model",
    "geography_or_market_signal": "geography_or_market_signal",
    "target_audience_signal": "target_audience_signal",
    "core_product_positioning": "core_product_positioning",
    "key_claims_or_messaging": "key_claims_or_messaging",
    "ecommerce_signal": "ecommerce_signal",
    "marketing_or_content_activity_signal": "marketing_or_content_activity_signal",
    "public_contact_route": "public_contact_route",
}


@dataclass(frozen=True)
class LeadScoringInput:
    """Evidence summary used to score one lead."""

    official_evidence_count: int
    medium_high_official_evidence_count: int
    signals: dict[str, int]


@dataclass(frozen=True)
class LeadOutput:
    """Lead master and optional review queue row for one seed."""

    lead_record: LeadRecord
    review_record: ReviewQueueRecord | None


@dataclass(frozen=True)
class ComponentScore:
    name: str
    points: int
    reason: str
    evidence: EvidenceRecord | None = None


@dataclass(frozen=True)
class ScoringAnalysis:
    components: tuple[ComponentScore, ...]
    score: int
    status: str
    confidence: str
    rationale: str
    review_next_step: str
    review_eligible: bool


def score_lead_signals(
    signals: Mapping[str, bool | int | float],
    evidence_count: int,
    official_evidence_count: int | None = None,
    medium_high_official_evidence_count: int | None = None,
) -> ScoreResult:
    """Score explicit component values.

    This function is kept for direct unit tests. Full lead qualification uses
    evidence-aware hard gates in :func:`build_lead_outputs`.
    """

    if evidence_count <= 0 and (official_evidence_count is None or official_evidence_count <= 0):
        return ScoreResult(
            score=0,
            status="insufficient_information",
            rationale="No official public evidence was available for qualification.",
            confidence="unknown",
        )

    raw_score = 0.0
    matched_signals: list[str] = []
    normalized_signals = {
        LEGACY_SIGNAL_ALIASES.get(signal_name, signal_name): signal_value
        for signal_name, signal_value in signals.items()
    }
    for signal_name, weight in SCORING_MODEL.items():
        signal_value = normalized_signals.get(signal_name, False)
        if signal_value is True:
            raw_score += weight
            matched_signals.append(signal_name)
        elif isinstance(signal_value, (int, float)) and not isinstance(signal_value, bool):
            if signal_name in SCORING_MODEL:
                bounded_points = min(max(float(signal_value), 0.0), float(weight))
                raw_score += bounded_points
                if bounded_points > 0:
                    matched_signals.append(signal_name)

    score = min(10, round(raw_score))
    official_count = evidence_count if official_evidence_count is None else official_evidence_count
    medium_high_count = (
        official_count
        if medium_high_official_evidence_count is None
        else medium_high_official_evidence_count
    )
    confidence = _confidence_from_components(
        score=score,
        evidence_records_count=official_count,
        medium_high_count=medium_high_count,
        evidence_quality_points=1 if normalized_signals.get("supporting_evidence_and_source_quality") else 0,
    )
    status = (
        "qualified"
        if score >= QUALIFIED_MIN_SCORE and confidence in MEDIUM_HIGH_CONFIDENCE
        else "pending_human_review"
        if score >= PENDING_REVIEW_MIN_SCORE
        else "not_a_fit"
    )
    rationale = (
        f"Score {score}/10 from component signals: "
        f"{', '.join(matched_signals) if matched_signals else 'none'}. "
        f"Official evidence records: {official_count}; medium/high records: {medium_high_count}."
    )
    return ScoreResult(score=score, status=status, rationale=rationale, confidence=confidence)


def build_lead_outputs(
    normalized_seeds: list[NormalizedSeed],
    evidence_records: list[EvidenceRecord],
    official_domains_by_seed: Mapping[str, str] | None = None,
) -> list[LeadOutput]:
    """Build lead master rows and review queue rows from evidence."""

    official_domains_by_seed = official_domains_by_seed or {}
    evidence_by_seed: dict[str, list[EvidenceRecord]] = {}
    for record in filter_and_dedupe_evidence(evidence_records):
        evidence_by_seed.setdefault(record.seed_id, []).append(record)

    outputs: list[LeadOutput] = []
    for seed in normalized_seeds:
        official_domain = official_domains_by_seed.get(seed.seed_id, seed.normalized_domain)
        official_evidence = [
            record
            for record in evidence_by_seed.get(seed.seed_id, [])
            if is_official_public_evidence(record, official_domain)
            and not is_gated_or_restricted_url(record.source_url)
        ]
        selected_facts = _select_best_facts(official_evidence)
        analysis = _score_from_evidence(seed, official_evidence)
        primary_evidence = _primary_evidence(official_evidence)
        lead_record = _build_lead_record(
            seed=seed,
            selected_facts=selected_facts,
            primary_evidence=primary_evidence,
            score_result=ScoreResult(
                score=analysis.score,
                status=analysis.status,
                rationale=analysis.rationale,
                confidence=analysis.confidence,
            ),
        )
        review_record = _build_review_record(
            lead_record=lead_record,
            primary_evidence=primary_evidence,
            analysis=analysis,
        )
        outputs.append(LeadOutput(lead_record=lead_record, review_record=review_record))

    return outputs


def is_official_public_evidence(record: EvidenceRecord, normalized_domain: str) -> bool:
    """Return True when evidence comes from the seed company's official domain."""

    source_domain = _domain_from_url(record.source_url)
    if not source_domain:
        return False
    return source_domain == normalized_domain or source_domain.endswith(f".{normalized_domain}")


def _score_from_evidence(
    seed: NormalizedSeed,
    evidence_records: list[EvidenceRecord],
) -> ScoringAnalysis:
    if not evidence_records:
        return ScoringAnalysis(
            components=(),
            score=0,
            status="insufficient_information",
            confidence="unknown",
            rationale=(
                "Score 0/10. No valid accessible official evidence remained after "
                "filtering gated pages, boilerplate and duplicates."
            ),
            review_next_step="",
            review_eligible=False,
        )

    combined_text = _combined_text(evidence_records)
    b2b_supplier = _contains_any(combined_text, B2B_MARKERS)
    parent_company = _contains_any(combined_text, PARENT_COMPANY_MARKERS)
    marketplace = _has_marketplace_signal(evidence_records)
    foreign_mismatch = _has_foreign_company_signal(evidence_records) and not _has_strong_india_company_signal(
        seed,
        evidence_records,
    )
    target_category = _has_target_category(evidence_records)
    india_signal = _has_india_signal(seed, evidence_records)
    transaction_signal = _has_transaction_signal(evidence_records)
    consumer_offering = _has_consumer_offering(evidence_records)
    marketing_signal = _has_substantive_marketing_signal(evidence_records)
    source_quality = _has_supporting_evidence_quality(evidence_records)

    components = (
        _score_icp_fit(
            target_category=target_category,
            india_signal=india_signal,
            transaction_signal=transaction_signal,
            consumer_offering=consumer_offering,
            b2b_supplier=b2b_supplier,
            parent_company=parent_company,
            marketplace=marketplace,
            foreign_mismatch=foreign_mismatch,
            evidence_records=evidence_records,
        ),
        _score_active_accessible_site(
            transaction_signal=transaction_signal,
            b2b_supplier=b2b_supplier,
            parent_company=parent_company,
            evidence_records=evidence_records,
        ),
        _score_offering(
            consumer_offering=consumer_offering,
            b2b_supplier=b2b_supplier,
            parent_company=parent_company,
            marketplace=marketplace,
            evidence_records=evidence_records,
        ),
        _score_marketing(marketing_signal=marketing_signal, evidence_records=evidence_records),
        _score_quality(source_quality=source_quality, evidence_records=evidence_records),
    )
    score = sum(component.points for component in components)
    medium_high_count = sum(
        1 for record in evidence_records if record.confidence in MEDIUM_HIGH_CONFIDENCE
    )
    confidence = _confidence_from_components(
        score=score,
        evidence_records_count=len(evidence_records),
        medium_high_count=medium_high_count,
        evidence_quality_points=components[-1].points,
    )

    icp_points = components[0].points
    ambiguity_reasons: list[str] = []
    if parent_company:
        ambiguity_reasons.append("parent-company versus brand-level outreach requires a decision")
    if marketplace:
        ambiguity_reasons.append("marketplace/platform fit requires ICP confirmation")

    if foreign_mismatch:
        status = "not_a_fit"
        status_reason = "official evidence indicates a non-Indian company under the documented ICP"
        review_next_step = ""
        review_eligible = False
    elif b2b_supplier:
        status = "not_a_fit"
        status_reason = "official evidence indicates a B2B ingredient/supplier model rather than consumer D2C"
        review_next_step = ""
        review_eligible = False
    elif score >= QUALIFIED_MIN_SCORE and icp_points == 3 and confidence in MEDIUM_HIGH_CONFIDENCE and not ambiguity_reasons:
        status = "qualified"
        status_reason = "qualified because score, ICP fit, confidence and official evidence gates were met"
        review_next_step = "Business review."
        review_eligible = True
    elif ambiguity_reasons and score >= PENDING_REVIEW_MIN_SCORE and confidence in MEDIUM_HIGH_CONFIDENCE:
        status = "pending_human_review"
        status_reason = "; ".join(ambiguity_reasons)
        review_next_step = _review_next_step_for_ambiguity(parent_company, marketplace)
        review_eligible = True
    elif score >= PENDING_REVIEW_MIN_SCORE:
        status = "pending_human_review"
        status_reason = "score is non-trivial but a mandatory qualification question remains unresolved"
        review_next_step = "Review unresolved ICP evidence before outreach."
        review_eligible = confidence in MEDIUM_HIGH_CONFIDENCE
    else:
        status = "insufficient_information"
        status_reason = "accessible public evidence is too limited for qualification"
        review_next_step = ""
        review_eligible = False

    rationale = _build_rationale(
        score=score,
        components=components,
        confidence=confidence,
        status_reason=status_reason,
        evidence_records=evidence_records,
    )
    return ScoringAnalysis(
        components=components,
        score=score,
        status=status,
        confidence=confidence,
        rationale=rationale,
        review_next_step=review_next_step,
        review_eligible=review_eligible,
    )


def _score_icp_fit(
    target_category: bool,
    india_signal: bool,
    transaction_signal: bool,
    consumer_offering: bool,
    b2b_supplier: bool,
    parent_company: bool,
    marketplace: bool,
    foreign_mismatch: bool,
    evidence_records: list[EvidenceRecord],
) -> ComponentScore:
    if foreign_mismatch:
        return ComponentScore("ICP fit", 0, "non-Indian company signal", _evidence_for("geography_or_market_signal", evidence_records))
    if b2b_supplier:
        return ComponentScore("ICP fit", 1, "category-related but B2B supplier", _primary_evidence(evidence_records))
    if parent_company or marketplace:
        return ComponentScore("ICP fit", 2, "relevant parent/platform rather than independent D2C brand", _primary_evidence(evidence_records))
    if india_signal and target_category and (transaction_signal or consumer_offering):
        return ComponentScore("ICP fit", 3, "India-based consumer target-category brand", _primary_evidence(evidence_records))
    if target_category:
        return ComponentScore("ICP fit", 1, "category-related but incomplete India/D2C proof", _primary_evidence(evidence_records))
    return ComponentScore("ICP fit", 0, "insufficient ICP evidence")


def _score_active_accessible_site(
    transaction_signal: bool,
    b2b_supplier: bool,
    parent_company: bool,
    evidence_records: list[EvidenceRecord],
) -> ComponentScore:
    if not evidence_records:
        return ComponentScore("Active website/e-commerce", 0, "no accessible official evidence")
    if transaction_signal and not b2b_supplier and not parent_company:
        return ComponentScore("Active website/e-commerce", 2, "accessible catalogue/store or transaction signal", _evidence_for("ecommerce_signal", evidence_records) or _primary_evidence(evidence_records))
    return ComponentScore("Active website/e-commerce", 1, "active official website without direct consumer transaction proof", _primary_evidence(evidence_records))


def _score_offering(
    consumer_offering: bool,
    b2b_supplier: bool,
    parent_company: bool,
    marketplace: bool,
    evidence_records: list[EvidenceRecord],
) -> ComponentScore:
    if not consumer_offering:
        return ComponentScore("Offering", 0, "no accessible offering evidence")
    if b2b_supplier or parent_company or marketplace:
        return ComponentScore("Offering", 1, "indirect portfolio, B2B, marketplace or parent-company offering", _evidence_for("product_or_service_type", evidence_records) or _primary_evidence(evidence_records))
    return ComponentScore("Offering", 2, "clear consumer product/service catalogue", _evidence_for("product_or_service_type", evidence_records) or _primary_evidence(evidence_records))


def _score_marketing(
    marketing_signal: bool,
    evidence_records: list[EvidenceRecord],
) -> ComponentScore:
    if marketing_signal:
        return ComponentScore("Marketing/growth", 2, "substantive campaign, content, offer or positioning signal", _evidence_for("marketing_or_content_activity_signal", evidence_records) or _evidence_for("key_claims_or_messaging", evidence_records) or _primary_evidence(evidence_records))
    return ComponentScore("Marketing/growth", 0, "no reliable marketing activity beyond boilerplate")


def _score_quality(
    source_quality: bool,
    evidence_records: list[EvidenceRecord],
) -> ComponentScore:
    if source_quality:
        return ComponentScore("Evidence quality", 1, "distinct relevant official sources directly support the signals", _primary_evidence(evidence_records))
    return ComponentScore("Evidence quality", 0, "evidence is too sparse, duplicated or indirect")


def _build_lead_record(
    seed: NormalizedSeed,
    selected_facts: dict[str, EvidenceRecord],
    primary_evidence: EvidenceRecord | None,
    score_result: ScoreResult,
) -> LeadRecord:
    field_values = {
        field_name: selected_facts.get(fact_type).fact_value
        if selected_facts.get(fact_type)
        else _missing_value_for_field(field_name)
        for field_name, fact_type in FIELD_FACT_TYPES.items()
    }

    source_url = primary_evidence.source_url if primary_evidence else "not_available"
    evidence_snippet = primary_evidence.evidence_snippet if primary_evidence else "not_available"
    collection_date = primary_evidence.collected_at if primary_evidence else "not_available"

    return LeadRecord(
        lead_id=_lead_id_from_seed(seed),
        company_name=seed.clean_company_name,
        website_url=seed.normalized_url,
        normalized_domain=seed.normalized_domain,
        source_url=source_url,
        evidence_snippet=evidence_snippet,
        collection_date=collection_date,
        data_confidence=score_result.confidence,
        lead_qualification_score=score_result.score,
        qualification_rationale=score_result.rationale,
        lead_status=score_result.status,
        **field_values,
    )


def _build_review_record(
    lead_record: LeadRecord,
    primary_evidence: EvidenceRecord | None,
    analysis: ScoringAnalysis,
) -> ReviewQueueRecord | None:
    if not analysis.review_eligible:
        return None
    if lead_record.lead_status not in {"qualified", "pending_human_review"}:
        return None
    if lead_record.data_confidence not in MEDIUM_HIGH_CONFIDENCE:
        return None
    if primary_evidence is None:
        return None

    return ReviewQueueRecord(
        company_name=lead_record.company_name,
        website=lead_record.website_url,
        qualification_score=lead_record.lead_qualification_score,
        why_it_is_relevant=lead_record.qualification_rationale,
        evidence_source=primary_evidence.source_url,
        confidence=lead_record.data_confidence,
        recommended_next_step=analysis.review_next_step,
        review_status="pending_review",
    )


def _select_best_facts(
    evidence_records: list[EvidenceRecord],
) -> dict[str, EvidenceRecord]:
    selected: dict[str, EvidenceRecord] = {}
    for record in evidence_records:
        if record.fact_type not in FIELD_FACT_TYPES.values():
            continue
        current = selected.get(record.fact_type)
        if current is None or evidence_rank(record) > evidence_rank(current):
            selected[record.fact_type] = record
    return selected


def _primary_evidence(evidence_records: list[EvidenceRecord]) -> EvidenceRecord | None:
    if not evidence_records:
        return None
    return sorted(evidence_records, key=evidence_rank, reverse=True)[0]


def _evidence_for(
    fact_type: str,
    evidence_records: list[EvidenceRecord],
) -> EvidenceRecord | None:
    matches = [record for record in evidence_records if record.fact_type == fact_type]
    return _primary_evidence(matches)


def _confidence_from_components(
    score: int,
    evidence_records_count: int,
    medium_high_count: int,
    evidence_quality_points: int,
) -> str:
    if (
        score >= QUALIFIED_MIN_SCORE
        and evidence_quality_points == 1
        and medium_high_count >= 3
        and evidence_records_count >= 4
    ):
        return "high"
    if evidence_records_count >= 2 and medium_high_count >= 1:
        return "medium"
    if evidence_records_count > 0:
        return "low"
    return "unknown"


def _has_target_category(evidence_records: list[EvidenceRecord]) -> bool:
    return _contains_any(_combined_text(evidence_records), TARGET_CATEGORY_KEYWORDS)


def _has_india_signal(seed: NormalizedSeed, evidence_records: list[EvidenceRecord]) -> bool:
    if seed.normalized_domain.endswith(".in"):
        return True
    if _contains_any(_combined_text(evidence_records), ("10l+", "lakh", "lakhs", "lac", "crore", "₹", " inr ")):
        return True
    return _has_strong_india_company_signal(seed, evidence_records)


def _has_strong_india_company_signal(
    seed: NormalizedSeed,
    evidence_records: list[EvidenceRecord],
) -> bool:
    del seed
    for record in evidence_records:
        text = _record_text(record)
        if record.fact_type == "geography_or_market_signal" and record.fact_value.casefold() in {"in", "india"}:
            return True
        if re.search(r"\bindia(?:'s|n)?\b", text) and not _looks_like_country_selector(text):
            return True
    return False


def _has_foreign_company_signal(evidence_records: list[EvidenceRecord]) -> bool:
    for record in evidence_records:
        value = record.fact_value.casefold().strip()
        text = _record_text(record)
        if record.fact_type == "geography_or_market_signal" and value in FOREIGN_COUNTRY_VALUES:
            return True
        if "huel ltd" in text or "united kingdom" in text or "addresscountry\": \"gb" in text:
            return True
    return False


def _has_marketplace_signal(evidence_records: list[EvidenceRecord]) -> bool:
    if _contains_any(_combined_text(evidence_records), MARKETPLACE_MARKERS):
        return True
    for record in evidence_records:
        path = urlsplit(record.source_url).path.casefold()
        if "top-brands-list" in path or "stores/all-cities" in path:
            return True
    return False


def _has_transaction_signal(evidence_records: list[EvidenceRecord]) -> bool:
    for record in evidence_records:
        text = _record_text(record)
        source_path = urlsplit(record.source_url).path.casefold()
        if record.fact_type == "ecommerce_signal" and not _is_cart_boilerplate(text):
            return True
        if any(token in source_path for token in ("/product", "/products", "/collection", "/collections", "/shop")):
            return True
        if _contains_any(text, TRANSACTION_MARKERS) and not _is_cart_boilerplate(text):
            return True
    return False


def _has_consumer_offering(evidence_records: list[EvidenceRecord]) -> bool:
    for record in evidence_records:
        text = _record_text(record)
        if record.fact_type in {"product_or_service_type", "core_product_positioning"} and _contains_any(
            text,
            TARGET_CATEGORY_KEYWORDS + ("catalogue", "catalog", "products", "service"),
        ):
            return True
    return False


def _has_substantive_marketing_signal(evidence_records: list[EvidenceRecord]) -> bool:
    for record in evidence_records:
        text = _record_text(record)
        if record.fact_type == "marketing_or_content_activity_signal" and _contains_any(text, SUBSTANTIVE_MARKETING_MARKERS):
            return True
        if record.fact_type in {"key_claims_or_messaging", "core_product_positioning"} and _contains_any(
            text,
            SUBSTANTIVE_MARKETING_MARKERS
            + (
                "clean",
                "clinically",
                "cruelty-free",
                "environmental impact",
                "low-carbon",
                "made safe",
                "natural",
                "nutritionally complete",
                "science-backed",
                "vegan",
            ),
        ):
            return True
    return False


def _has_supporting_evidence_quality(evidence_records: list[EvidenceRecord]) -> bool:
    direct_fact_types = {
        record.fact_type
        for record in evidence_records
        if record.fact_type
        in {
            "industry_or_category",
            "product_or_service_type",
            "core_product_positioning",
            "ecommerce_signal",
            "marketing_or_content_activity_signal",
            "geography_or_market_signal",
        }
    }
    source_types = {_source_type(record.source_url) for record in evidence_records}
    useful_source_types = {source_type for source_type in source_types if source_type != "other"}
    medium_high_count = sum(1 for record in evidence_records if record.confidence in MEDIUM_HIGH_CONFIDENCE)
    return (
        len(direct_fact_types) >= 3
        and medium_high_count >= 2
        and (len(useful_source_types) >= 2 or len({canonicalize_source_url(record.source_url) for record in evidence_records}) >= 3)
    )


def _source_type(url: str) -> str:
    path = urlsplit(url or "").path.casefold()
    if any(token in path for token in ("about", "story", "brand", "contact", "legal")):
        return "identity"
    if any(token in path for token in ("product", "collection", "shop", "store")):
        return "offering"
    if any(token in path for token in ("blog", "journal", "article", "news", "press")):
        return "marketing"
    if path in {"", "/"}:
        return "homepage"
    return "other"


def _build_rationale(
    score: int,
    components: tuple[ComponentScore, ...],
    confidence: str,
    status_reason: str,
    evidence_records: list[EvidenceRecord],
) -> str:
    component_text = "; ".join(
        f"{component.name} {component.points}: {component.reason}" for component in components
    )
    distinct_sources = len({canonicalize_source_url(record.source_url) for record in evidence_records})
    return (
        f"Score {score}/10. {component_text}. "
        f"Confidence {confidence} from {distinct_sources} distinct valid official source URL(s) "
        f"after filtering duplicates, gated pages and boilerplate. Status reason: {status_reason}."
    )


def _review_next_step_for_ambiguity(parent_company: bool, marketplace: bool) -> str:
    if parent_company:
        return "Decide whether outreach should target the parent company or brand team."
    if marketplace:
        return "Confirm whether marketplaces/platforms are accepted by the ICP."
    return "Review unresolved ICP evidence before outreach."


def _combined_text(evidence_records: list[EvidenceRecord]) -> str:
    return " ".join(_record_text(record) for record in evidence_records)


def _record_text(record: EvidenceRecord) -> str:
    return f"{record.fact_value} {record.evidence_snippet}".casefold()


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _looks_like_country_selector(text: str) -> bool:
    return "americas" in text and "asia" in text and "india" in text


def _is_cart_boilerplate(text: str) -> bool:
    return "your cart is empty" in text or "proceed to cart" in text


def _domain_from_url(url: str) -> str:
    split = urlsplit(url)
    if split.scheme not in {"http", "https"}:
        return ""
    host = (split.hostname or "").casefold().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _missing_value_for_field(field_name: str) -> str:
    if field_name == "public_contact_route":
        return "not_available"
    return "unknown"


def _lead_id_from_seed(seed: NormalizedSeed) -> str:
    suffix = seed.seed_id.removeprefix("SEED-")
    return f"LEAD-{suffix}"

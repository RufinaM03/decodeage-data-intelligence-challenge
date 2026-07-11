"""Internal schemas used by the data intelligence pipeline.

The project intentionally uses standard-library dataclasses instead of a
larger validation framework so the rules are easy to explain in discussion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


LEAD_MASTER_COLUMNS = [
    "Lead ID",
    "Company Name",
    "Website URL",
    "Normalized Domain",
    "Industry or Category",
    "Product or Service Type",
    "Business Model",
    "Geography or Market Signal",
    "Target Audience Signal",
    "Core Product Positioning",
    "Key Claims or Messaging",
    "E-commerce Signal",
    "Marketing or Content Activity Signal",
    "Public Contact Route",
    "Source URL",
    "Evidence Snippet",
    "Collection Date",
    "Data Confidence",
    "Lead Qualification Score",
    "Qualification Rationale",
    "Lead Status",
]

REVIEW_QUEUE_COLUMNS = [
    "Company Name",
    "Website",
    "Qualification Score",
    "Why It Is Relevant",
    "Evidence Source",
    "Confidence",
    "Recommended Next Step",
    "Review Status",
]

COLLECTION_LOG_COLUMNS = [
    "seed_id",
    "url",
    "status",
    "response_code",
    "redirect_url",
    "collected_at",
    "error",
    "collection_method",
    "fallback_reason",
    "visible_text_length",
    "application_shell_detected",
]

EVIDENCE_COLUMNS = [
    "seed_id",
    "company_name",
    "normalized_domain",
    "fact_type",
    "fact_value",
    "source_url",
    "evidence_snippet",
    "collected_at",
    "confidence",
    "extraction_method",
]

LEAD_STATUSES = {
    "qualified",
    "pending_human_review",
    "insufficient_information",
    "not_a_fit",
}

REVIEW_STATUSES = {
    "pending_review",
    "approved_for_outreach",
    "rejected",
    "needs_more_research",
}

CONFIDENCE_LEVELS = {"unknown", "low", "medium", "high"}


def utc_now_iso() -> str:
    """Return a timezone-aware UTC timestamp for provenance fields."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")


@dataclass(frozen=True)
class SeedRecord:
    """One row from seed_companies.csv."""

    seed_id: str
    company_name: str
    website_url: str

    def __post_init__(self) -> None:
        _require_text(self.seed_id, "seed_id")
        _require_text(self.company_name, "company_name")
        _require_text(self.website_url, "website_url")


@dataclass(frozen=True)
class NormalizedSeed:
    """Seed row after name and URL normalization.

    Original values are kept so any correction can be audited.
    """

    seed: SeedRecord
    clean_company_name: str
    original_url: str
    normalized_url: str
    normalized_domain: str
    correction_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.clean_company_name, "clean_company_name")
        _require_text(self.original_url, "original_url")
        _require_text(self.normalized_url, "normalized_url")
        _require_text(self.normalized_domain, "normalized_domain")

    @property
    def seed_id(self) -> str:
        return self.seed.seed_id


@dataclass(frozen=True)
class CollectedPage:
    """Structured record for a page collection attempt."""

    seed_id: str
    normalized_domain: str
    source_url: str
    final_url: str = ""
    status: str = "not_collected"
    response_code: int | None = None
    redirect_url: str = ""
    collected_at: str = field(default_factory=utc_now_iso)
    content_type: str = ""
    html: str = ""
    error: str = ""
    allowed_by_robots: bool | None = None
    page_type: str = "unknown"
    discovery_method: str = "unknown"
    json_ld_blocks: tuple[str, ...] = ()
    collection_method: str = "urllib_beautifulsoup"
    fallback_reason: str = "not_required"
    visible_text_length: int = 0
    application_shell_detected: bool = False

    def __post_init__(self) -> None:
        _require_text(self.seed_id, "seed_id")
        _require_text(self.normalized_domain, "normalized_domain")
        _require_text(self.source_url, "source_url")

    def to_json_record(self) -> dict[str, object]:
        """Return a JSON-serializable raw collection record."""

        return asdict(self)

    def to_log_row(self) -> dict[str, str | int]:
        """Return the lightweight row for collection_log.csv."""

        return {
            "seed_id": self.seed_id,
            "url": self.source_url,
            "status": self.status,
            "response_code": self.response_code if self.response_code is not None else "",
            "redirect_url": self.redirect_url,
            "collected_at": self.collected_at,
            "error": self.error,
            "collection_method": self.collection_method,
            "fallback_reason": self.fallback_reason,
            "visible_text_length": self.visible_text_length,
            "application_shell_detected": str(self.application_shell_detected).lower(),
        }


@dataclass(frozen=True)
class EvidenceRecord:
    """One extracted fact tied to its public source and snippet."""

    seed_id: str
    company_name: str
    normalized_domain: str
    fact_type: str
    fact_value: str
    source_url: str
    evidence_snippet: str
    collected_at: str = field(default_factory=utc_now_iso)
    confidence: str = "unknown"
    extraction_method: str = "not_run"

    def __post_init__(self) -> None:
        for field_name in (
            "seed_id",
            "company_name",
            "normalized_domain",
            "fact_type",
            "fact_value",
            "source_url",
            "evidence_snippet",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"Invalid confidence: {self.confidence}")

    def to_csv_row(self) -> dict[str, str]:
        return {
            "seed_id": self.seed_id,
            "company_name": self.company_name,
            "normalized_domain": self.normalized_domain,
            "fact_type": self.fact_type,
            "fact_value": self.fact_value,
            "source_url": self.source_url,
            "evidence_snippet": self.evidence_snippet,
            "collected_at": self.collected_at,
            "confidence": self.confidence,
            "extraction_method": self.extraction_method,
        }


@dataclass(frozen=True)
class ScoreResult:
    """Transparent score and status assigned from evidence-backed signals."""

    score: int
    status: str
    rationale: str
    confidence: str

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 10:
            raise ValueError("score must be between 0 and 10")
        if self.status not in LEAD_STATUSES:
            raise ValueError(f"Invalid lead status: {self.status}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"Invalid confidence: {self.confidence}")
        _require_text(self.rationale, "rationale")


@dataclass(frozen=True)
class LeadRecord:
    """Output row for lead_master.csv."""

    lead_id: str
    company_name: str
    website_url: str
    normalized_domain: str
    industry_or_category: str = "unknown"
    product_or_service_type: str = "unknown"
    business_model: str = "unknown"
    geography_or_market_signal: str = "unknown"
    target_audience_signal: str = "unknown"
    core_product_positioning: str = "unknown"
    key_claims_or_messaging: str = "unknown"
    ecommerce_signal: str = "unknown"
    marketing_or_content_activity_signal: str = "unknown"
    public_contact_route: str = "not_available"
    source_url: str = "not_available"
    evidence_snippet: str = "not_available"
    collection_date: str = "not_available"
    data_confidence: str = "unknown"
    lead_qualification_score: int = 0
    qualification_rationale: str = "No evidence-backed qualification yet."
    lead_status: str = "insufficient_information"

    def __post_init__(self) -> None:
        for field_name in ("lead_id", "company_name", "website_url", "normalized_domain"):
            _require_text(getattr(self, field_name), field_name)
        if self.lead_status not in LEAD_STATUSES:
            raise ValueError(f"Invalid lead status: {self.lead_status}")
        if self.data_confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"Invalid data confidence: {self.data_confidence}")

    def to_master_row(self) -> dict[str, str | int]:
        return {
            "Lead ID": self.lead_id,
            "Company Name": self.company_name,
            "Website URL": self.website_url,
            "Normalized Domain": self.normalized_domain,
            "Industry or Category": self.industry_or_category,
            "Product or Service Type": self.product_or_service_type,
            "Business Model": self.business_model,
            "Geography or Market Signal": self.geography_or_market_signal,
            "Target Audience Signal": self.target_audience_signal,
            "Core Product Positioning": self.core_product_positioning,
            "Key Claims or Messaging": self.key_claims_or_messaging,
            "E-commerce Signal": self.ecommerce_signal,
            "Marketing or Content Activity Signal": self.marketing_or_content_activity_signal,
            "Public Contact Route": self.public_contact_route,
            "Source URL": self.source_url,
            "Evidence Snippet": self.evidence_snippet,
            "Collection Date": self.collection_date,
            "Data Confidence": self.data_confidence,
            "Lead Qualification Score": self.lead_qualification_score,
            "Qualification Rationale": self.qualification_rationale,
            "Lead Status": self.lead_status,
        }


@dataclass(frozen=True)
class ReviewQueueRecord:
    """Output row for lead_review_queue.csv."""

    company_name: str
    website: str
    qualification_score: int
    why_it_is_relevant: str
    evidence_source: str
    confidence: str
    recommended_next_step: str = "human_review"
    review_status: str = "pending_review"

    def __post_init__(self) -> None:
        for field_name in (
            "company_name",
            "website",
            "why_it_is_relevant",
            "evidence_source",
            "recommended_next_step",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"Invalid confidence: {self.confidence}")
        if self.review_status not in REVIEW_STATUSES:
            raise ValueError(f"Invalid review status: {self.review_status}")

    def to_review_row(self) -> dict[str, str | int]:
        return {
            "Company Name": self.company_name,
            "Website": self.website,
            "Qualification Score": self.qualification_score,
            "Why It Is Relevant": self.why_it_is_relevant,
            "Evidence Source": self.evidence_source,
            "Confidence": self.confidence,
            "Recommended Next Step": self.recommended_next_step,
            "Review Status": self.review_status,
        }

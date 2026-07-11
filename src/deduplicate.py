"""Duplicate-domain and fuzzy-name candidate detection."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from .models import CollectedPage, NormalizedSeed
from .normalize import normalize_company_name


@dataclass(frozen=True)
class DuplicateDomainGroup:
    """Seeds sharing the same effective normalized domain."""

    normalized_domain: str
    seed_ids: tuple[str, ...]
    company_names: tuple[str, ...]
    normalized_urls: tuple[str, ...]
    duplicate_basis: str = "seed_domain"
    source_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class SimilarNameCandidate:
    """Pair of companies that may need human review.

    These candidates are never automatically merged. This is important because
    a parent company and a consumer brand can look related but still represent
    different business records.
    """

    left_seed_id: str
    right_seed_id: str
    left_company_name: str
    right_company_name: str
    similarity: float
    review_note: str = "candidate_only_do_not_auto_merge"


def find_duplicate_domains(
    seeds: list[NormalizedSeed],
    collected_pages: list[CollectedPage] | None = None,
) -> list[DuplicateDomainGroup]:
    """Group records that share one effective domain.

    Redirect/final collection domains take precedence over seed domains because
    two messy seed URLs can resolve to the same real website. This function only
    groups exact domain evidence; it never uses fuzzy names for merging.
    """

    resolved_domains = resolve_final_domains(seeds, collected_pages or [])
    groups: dict[str, list[NormalizedSeed]] = {}
    for seed in seeds:
        groups.setdefault(resolved_domains[seed.seed_id].domain, []).append(seed)

    duplicate_groups: list[DuplicateDomainGroup] = []
    for domain, domain_seeds in sorted(groups.items()):
        if len(domain_seeds) < 2:
            continue
        basis = "redirect_or_final_domain" if any(
            resolved_domains[seed.seed_id].basis != "seed_domain" for seed in domain_seeds
        ) else "seed_domain"
        duplicate_groups.append(
            DuplicateDomainGroup(
                normalized_domain=domain,
                seed_ids=tuple(seed.seed_id for seed in domain_seeds),
                company_names=tuple(seed.clean_company_name for seed in domain_seeds),
                normalized_urls=tuple(seed.normalized_url for seed in domain_seeds),
                duplicate_basis=basis,
                source_urls=tuple(resolved_domains[seed.seed_id].source_url for seed in domain_seeds),
            )
        )
    return duplicate_groups


@dataclass(frozen=True)
class FinalDomainResolution:
    """The domain used for exact deduplication and why it was chosen."""

    seed_id: str
    domain: str
    basis: str
    source_url: str


def resolve_final_domains(
    seeds: list[NormalizedSeed],
    collected_pages: list[CollectedPage],
) -> dict[str, FinalDomainResolution]:
    """Resolve final domains, preferring redirects and final URLs over seeds."""

    pages_by_seed: dict[str, list[CollectedPage]] = {}
    for page in collected_pages:
        pages_by_seed.setdefault(page.seed_id, []).append(page)

    resolutions: dict[str, FinalDomainResolution] = {}
    for seed in seeds:
        redirect_domain = _first_page_domain(
            pages_by_seed.get(seed.seed_id, []),
            url_field="redirect_url",
        )
        if redirect_domain:
            resolutions[seed.seed_id] = redirect_domain
            continue

        final_domain = _first_page_domain(
            pages_by_seed.get(seed.seed_id, []),
            url_field="final_url",
        )
        if final_domain:
            resolutions[seed.seed_id] = final_domain
            continue

        resolutions[seed.seed_id] = FinalDomainResolution(
            seed_id=seed.seed_id,
            domain=seed.normalized_domain,
            basis="seed_domain",
            source_url=seed.normalized_url,
        )
    return resolutions


def flag_similar_company_names(
    seeds: list[NormalizedSeed],
    threshold: float = 0.86,
) -> list[SimilarNameCandidate]:
    """Flag similar company names for review without merging them."""

    candidates: list[SimilarNameCandidate] = []
    seen_pairs: set[tuple[str, str]] = set()
    for left_index, left in enumerate(seeds):
        left_name = normalize_company_name(left.clean_company_name).casefold()
        left_tokens = _company_tokens(left_name)
        for right in seeds[left_index + 1 :]:
            right_name = normalize_company_name(right.clean_company_name).casefold()
            right_tokens = _company_tokens(right_name)
            similarity = SequenceMatcher(None, left_name, right_name).ratio()
            parent_brand_ambiguous = _is_parent_brand_ambiguous(left_tokens, right_tokens)
            if similarity >= threshold or parent_brand_ambiguous:
                pair_key = (left.seed_id, right.seed_id)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                review_note = (
                    "candidate_only_parent_brand_or_alias_do_not_auto_merge"
                    if parent_brand_ambiguous
                    else "candidate_only_do_not_auto_merge"
                )
                candidates.append(
                    SimilarNameCandidate(
                        left_seed_id=left.seed_id,
                        right_seed_id=right.seed_id,
                        left_company_name=left.clean_company_name,
                        right_company_name=right.clean_company_name,
                        similarity=round(similarity, 3),
                        review_note=review_note,
                    )
                )
    return candidates


def _first_page_domain(
    pages: list[CollectedPage],
    url_field: str,
) -> FinalDomainResolution | None:
    for page in _pages_in_dedupe_order(pages):
        url = getattr(page, url_field)
        domain = _normalized_domain_from_url(url)
        if domain:
            return FinalDomainResolution(
                seed_id=page.seed_id,
                domain=domain,
                basis=url_field,
                source_url=url,
            )
    return None


def _pages_in_dedupe_order(pages: list[CollectedPage]) -> list[CollectedPage]:
    return sorted(
        pages,
        key=lambda page: (
            0 if page.page_type == "homepage" else 1,
            0 if page.redirect_url else 1,
            page.source_url,
        ),
    )


def _normalized_domain_from_url(url: str) -> str:
    if not url:
        return ""
    split = urlsplit(url.strip())
    if split.scheme not in {"http", "https"}:
        return ""
    host = (split.hostname or "").casefold().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _company_tokens(company_name: str) -> set[str]:
    ignored = {"and", "by", "consumer", "company", "group", "india", "private", "limited"}
    return {
        token
        for token in company_name.replace("&", " and ").split()
        if token and token not in ignored
    }


def _is_parent_brand_ambiguous(left_tokens: set[str], right_tokens: set[str]) -> bool:
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True
    smaller, larger = sorted((left_tokens, right_tokens), key=len)
    return len(smaller) < len(larger) and smaller.issubset(larger)

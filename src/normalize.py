"""Normalization helpers for company names, URLs, and domains."""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit, urlunsplit

from .models import NormalizedSeed, SeedRecord


SUPPORTED_SCHEMES = {"http", "https"}
OBVIOUS_SCHEME_FIXES = {
    "htps": "https",
    "httpss": "https",
    "ttp": "http",
}

DOMAIN_LIKE_PATTERN = re.compile(r"^[A-Za-z0-9.-]+(?::\d+)?(?:/.*)?$")


@dataclass(frozen=True)
class UrlNormalizationResult:
    """Normalized URL plus audit notes for any changes made."""

    original_url: str
    normalized_url: str
    normalized_domain: str
    correction_notes: tuple[str, ...]


def normalize_company_name(company_name: str) -> str:
    """Collapse whitespace while preserving the visible company name."""

    return " ".join(company_name.strip().split())


def normalize_url(raw_url: str) -> UrlNormalizationResult:
    """Normalize a public company URL without inventing missing facts.

    The function repairs only obvious scheme mistakes, drops query strings and
    fragments, removes leading "www.", lowercases the host, and strips a
    trailing slash. The original URL and correction notes are kept for audit.
    """

    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("raw_url is required")

    original_url = raw_url
    working_url = raw_url.strip()
    notes: list[str] = []

    split = urlsplit(working_url)
    scheme = split.scheme.lower()

    if scheme in OBVIOUS_SCHEME_FIXES:
        fixed_scheme = OBVIOUS_SCHEME_FIXES[scheme]
        working_url = f"{fixed_scheme}:{working_url.split(':', 1)[1]}"
        notes.append(f"corrected_scheme:{scheme}->{fixed_scheme}")
        split = urlsplit(working_url)
        scheme = split.scheme.lower()
    elif not scheme and DOMAIN_LIKE_PATTERN.match(working_url):
        working_url = f"https://{working_url.lstrip('/')}"
        notes.append("added_missing_scheme:https")
        split = urlsplit(working_url)
        scheme = split.scheme.lower()

    if scheme not in SUPPORTED_SCHEMES:
        raise ValueError(f"Unsupported URL scheme: {split.scheme or 'missing'}")

    host = split.hostname or ""
    if not host:
        raise ValueError(f"Could not determine host for URL: {original_url}")

    normalized_host = host.lower().strip(".")
    if host != normalized_host:
        notes.append("normalized_host_case")

    if normalized_host.startswith("www."):
        normalized_host = normalized_host[4:]
        notes.append("removed_www")

    port = ""
    try:
        if split.port and not _is_default_port(scheme, split.port):
            port = f":{split.port}"
    except ValueError as exc:
        raise ValueError(f"Invalid URL port in {original_url}") from exc

    path = split.path or ""
    if path == "/":
        path = ""
        notes.append("removed_trailing_slash")
    elif path.endswith("/"):
        path = path.rstrip("/")
        notes.append("removed_trailing_slash")

    if split.query:
        notes.append("removed_query")
    if split.fragment:
        notes.append("removed_fragment")

    network_location = f"{normalized_host}{port}"
    normalized_url = urlunsplit((scheme, network_location, path, "", ""))

    return UrlNormalizationResult(
        original_url=original_url,
        normalized_url=normalized_url,
        normalized_domain=normalized_host,
        correction_notes=tuple(notes),
    )


def normalize_seed(seed: SeedRecord) -> NormalizedSeed:
    """Normalize one seed record and preserve audit information."""

    url_result = normalize_url(seed.website_url)
    return NormalizedSeed(
        seed=seed,
        clean_company_name=normalize_company_name(seed.company_name),
        original_url=url_result.original_url,
        normalized_url=url_result.normalized_url,
        normalized_domain=url_result.normalized_domain,
        correction_notes=url_result.correction_notes,
    )


def _is_default_port(scheme: str, port: int) -> bool:
    return (scheme == "https" and port == 443) or (scheme == "http" and port == 80)

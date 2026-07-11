"""Minimal CLI orchestration for the data intelligence pipeline.

The command loads and normalizes seeds, optionally performs responsible public
collection, and writes raw collection records plus header-safe output files.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .collector import (
    CollectionConfig,
    PlaywrightBrowserRenderer,
    ProgressLogger,
    collect_public_pages,
)
from .deduplicate import (
    find_duplicate_domains,
    flag_similar_company_names,
    resolve_final_domains,
)
from .extractor import extract_evidence
from .models import (
    CollectedPage,
    COLLECTION_LOG_COLUMNS,
    EVIDENCE_COLUMNS,
    LEAD_MASTER_COLUMNS,
    REVIEW_QUEUE_COLUMNS,
    LeadRecord,
    SeedRecord,
)
from .normalize import normalize_seed
from .scoring import build_lead_outputs


DEFAULT_INPUT = Path("data/input/seed_companies.csv")


@dataclass(frozen=True)
class PipelineSummary:
    seeds_loaded: int
    duplicate_domain_groups: int
    similar_name_candidates: int
    collected_pages: int
    evidence_records: int
    lead_rows: int
    review_rows: int
    live_collection_enabled: bool
    playwright_fallback_enabled: bool
    output_dir: str
    collection_attempts: int
    collection_fetched: int
    collection_blocked_by_robots: int
    collection_access_restricted: int
    collection_rate_limited: int
    collection_failed: int
    playwright_eligible_pages: int
    playwright_attempts: int
    playwright_successes: int
    playwright_failures: int
    playwright_skipped_disabled: int
    playwright_ineligible_restricted: int
    qualified_count: int
    pending_human_review_count: int
    not_a_fit_count: int
    insufficient_information_count: int


def load_seed_records(input_path: Path) -> list[SeedRecord]:
    """Load seeds from a CSV file."""

    with input_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        required_columns = {"seed_id", "company_name", "website_url"}
        missing_columns = required_columns.difference(reader.fieldnames or [])
        if missing_columns:
            missing = ", ".join(sorted(missing_columns))
            raise ValueError(f"Input CSV is missing required columns: {missing}")

        return [
            SeedRecord(
                seed_id=row["seed_id"],
                company_name=row["company_name"],
                website_url=row["website_url"],
            )
            for row in reader
        ]


def run_pipeline(
    input_path: Path = DEFAULT_INPUT,
    output_dir: Path = Path("."),
    collection_config: CollectionConfig | None = None,
    progress_logger: ProgressLogger | None = None,
) -> PipelineSummary:
    """Run the pipeline and optionally collect public pages."""

    output_dir = output_dir.resolve()
    seeds = load_seed_records(input_path)
    normalized_seeds = [normalize_seed(seed) for seed in seeds]
    _log_progress(
        progress_logger,
        f"Loaded {len(seeds)} seed records from {input_path}",
    )

    config = collection_config or CollectionConfig(live_collection_enabled=False)
    collected_pages: list[CollectedPage] = []
    evidence_records = []
    browser_renderer = (
        PlaywrightBrowserRenderer()
        if config.live_collection_enabled and config.playwright_fallback_enabled
        else None
    )
    try:
        for index, normalized_seed in enumerate(normalized_seeds, start=1):
            _log_progress(
                progress_logger,
                f"Processing {index}/{len(normalized_seeds)} "
                f"{normalized_seed.seed_id} {normalized_seed.clean_company_name} "
                f"({normalized_seed.normalized_domain})",
            )
            seed_pages = collect_public_pages(
                normalized_seed,
                config,
                browser_renderer=browser_renderer,
                progress_logger=progress_logger,
            )
            collected_pages.extend(seed_pages)
            seed_evidence = extract_evidence(normalized_seed, seed_pages)
            evidence_records.extend(seed_evidence)
            _log_progress(
                progress_logger,
                f"{normalized_seed.seed_id}: extracted {len(seed_evidence)} "
                "evidence records",
            )
    finally:
        if browser_renderer is not None:
            browser_renderer.close()

    final_domain_resolutions = resolve_final_domains(normalized_seeds, collected_pages)
    official_domains_by_seed = {
        seed_id: resolution.domain
        for seed_id, resolution in final_domain_resolutions.items()
    }
    duplicate_domains = find_duplicate_domains(normalized_seeds, collected_pages)
    similar_names = flag_similar_company_names(normalized_seeds)
    lead_outputs = build_lead_outputs(
        normalized_seeds,
        evidence_records,
        official_domains_by_seed=official_domains_by_seed,
    )
    lead_records = [output.lead_record for output in lead_outputs]
    review_records = [
        output.review_record
        for output in lead_outputs
        if output.review_record is not None
    ]

    _prepare_output_files(output_dir)
    _write_csv(
        output_dir / "lead_master.csv",
        LEAD_MASTER_COLUMNS,
        [record.to_master_row() for record in lead_records],
    )
    _write_csv(
        output_dir / "lead_review_queue.csv",
        REVIEW_QUEUE_COLUMNS,
        [record.to_review_row() for record in review_records],
    )
    _write_csv(
        output_dir / "data" / "raw" / "collection_log.csv",
        COLLECTION_LOG_COLUMNS,
        [page.to_log_row() for page in collected_pages],
    )
    _write_csv(
        output_dir / "data" / "evidence" / "evidence_records.csv",
        EVIDENCE_COLUMNS,
        [record.to_csv_row() for record in evidence_records],
    )
    _write_jsonl(
        output_dir / "data" / "raw" / "collection_records.jsonl",
        [page.to_json_record() for page in collected_pages],
    )
    _log_progress(progress_logger, f"Wrote outputs under {output_dir}")

    collection_stats = _collection_stats(collected_pages)
    qualification_stats = _qualification_stats(lead_records)
    return PipelineSummary(
        seeds_loaded=len(seeds),
        duplicate_domain_groups=len(duplicate_domains),
        similar_name_candidates=len(similar_names),
        collected_pages=len(collected_pages),
        evidence_records=len(evidence_records),
        lead_rows=len(lead_records),
        review_rows=len(review_records),
        live_collection_enabled=config.live_collection_enabled,
        playwright_fallback_enabled=config.playwright_fallback_enabled,
        output_dir=str(output_dir),
        collection_attempts=collection_stats["attempted"],
        collection_fetched=collection_stats["fetched"],
        collection_blocked_by_robots=collection_stats["blocked_by_robots"],
        collection_access_restricted=collection_stats["access_restricted"],
        collection_rate_limited=collection_stats["rate_limited"],
        collection_failed=collection_stats["failed"],
        playwright_eligible_pages=collection_stats["browser_eligible"],
        playwright_attempts=collection_stats["browser_attempts"],
        playwright_successes=collection_stats["browser_successes"],
        playwright_failures=collection_stats["browser_failures"],
        playwright_skipped_disabled=collection_stats["browser_skipped_disabled"],
        playwright_ineligible_restricted=collection_stats["browser_ineligible_restricted"],
        qualified_count=qualification_stats["qualified"],
        pending_human_review_count=qualification_stats["pending_human_review"],
        not_a_fit_count=qualification_stats["not_a_fit"],
        insufficient_information_count=qualification_stats["insufficient_information"],
    )


def format_pipeline_summary(summary: PipelineSummary, verbose: bool = False) -> str:
    """Return the human-readable terminal summary."""

    mode = "live public collection" if summary.live_collection_enabled else "offline processing"
    lines = [
        "=== Pipeline Summary ===",
        f"Mode: {mode}",
        f"Input: {summary.seeds_loaded} seeds",
        "",
        (
            "Deduplication: "
            f"{summary.duplicate_domain_groups} exact-domain groups | "
            f"{summary.similar_name_candidates} similar-name candidate"
            f"{'' if summary.similar_name_candidates == 1 else 's'}"
        ),
        (
            "Collection: "
            f"{summary.collection_attempts} attempted | "
            f"{summary.collection_fetched} fetched | "
            f"{summary.collection_blocked_by_robots} blocked by robots.txt | "
            f"{summary.collection_access_restricted} restricted/gated | "
            f"{summary.collection_rate_limited} rate-limited | "
            f"{summary.collection_failed} failed"
        ),
        f"Extraction: {summary.evidence_records} deduplicated evidence records",
        (
            "Qualification: "
            f"{summary.qualified_count} qualified | "
            f"{summary.pending_human_review_count} pending review | "
            f"{summary.not_a_fit_count} not a fit | "
            f"{summary.insufficient_information_count} insufficient information"
        ),
        (
            "Outputs: "
            f"{summary.lead_rows} lead-master rows | "
            f"{summary.review_rows} review-queue rows"
        ),
        "",
        "Runtime: urllib + BeautifulSoup (html.parser)",
        _browser_fallback_summary_line(summary),
        "",
        f"Saved to: {summary.output_dir}",
    ]

    if verbose:
        lines.extend(["", *_browser_diagnostic_lines(summary)])

    return "\n".join(lines)


def _browser_fallback_summary_line(summary: PipelineSummary) -> str:
    if not summary.playwright_fallback_enabled:
        return "Browser rendering fallback: disabled"
    if summary.playwright_eligible_pages == 0 and summary.playwright_attempts == 0:
        return "Browser rendering fallback: enabled, not needed"
    return (
        "Browser rendering fallback: "
        f"{summary.playwright_attempts} attempted | "
        f"{summary.playwright_successes} successful | "
        f"{summary.playwright_failures} failed"
    )


def _browser_diagnostic_lines(summary: PipelineSummary) -> list[str]:
    return [
        f"[diagnostics] Browser fallback eligible: {summary.playwright_eligible_pages}",
        f"[diagnostics] Browser fallback attempted: {summary.playwright_attempts}",
        f"[diagnostics] Browser fallback succeeded: {summary.playwright_successes}",
        f"[diagnostics] Browser fallback failed: {summary.playwright_failures}",
        (
            "[diagnostics] Browser fallback skipped because disabled: "
            f"{summary.playwright_skipped_disabled}"
        ),
        (
            "[diagnostics] Browser fallback ineligible because restricted: "
            f"{summary.playwright_ineligible_restricted}"
        ),
    ]


def _collection_stats(collected_pages: list[CollectedPage]) -> dict[str, int]:
    status_counts = Counter(page.status for page in collected_pages)
    eligible_reasons = {"empty_spa_shell", "sparse_js_rendered_page"}
    browser_attempts = sum(
        1
        for page in collected_pages
        if page.collection_method == "playwright_fallback"
        or page.fallback_reason == "playwright_failed"
    )
    return {
        "attempted": len(collected_pages),
        "fetched": status_counts["fetched"],
        "blocked_by_robots": status_counts["blocked_by_robots"],
        "access_restricted": status_counts["access_restricted"],
        "rate_limited": status_counts["rate_limited"],
        "failed": status_counts["failed"],
        "browser_eligible": sum(
            1
            for page in collected_pages
            if page.collection_method == "playwright_fallback"
            or page.fallback_reason in eligible_reasons
            or page.fallback_reason == "playwright_failed"
            or page.fallback_reason == "disabled"
        ),
        "browser_attempts": browser_attempts,
        "browser_successes": sum(
            1 for page in collected_pages if page.collection_method == "playwright_fallback"
        ),
        "browser_failures": sum(
            1 for page in collected_pages if page.fallback_reason == "playwright_failed"
        ),
        "browser_skipped_disabled": sum(
            1 for page in collected_pages if page.fallback_reason == "disabled"
        ),
        "browser_ineligible_restricted": sum(
            1
            for page in collected_pages
            if page.status == "access_restricted"
            and page.fallback_reason == "ineligible_access_restriction"
        ),
    }


def _qualification_stats(lead_records: list[LeadRecord]) -> dict[str, int]:
    status_counts = Counter(record.lead_status for record in lead_records)
    return {
        "qualified": status_counts["qualified"],
        "pending_human_review": status_counts["pending_human_review"],
        "not_a_fit": status_counts["not_a_fit"],
        "insufficient_information": status_counts["insufficient_information"],
    }


def _prepare_output_files(output_dir: Path) -> None:
    for relative_path in (
        Path("data/input"),
        Path("data/raw"),
        Path("data/evidence"),
    ):
        (output_dir / relative_path).mkdir(parents=True, exist_ok=True)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as jsonl_file:
        for row in rows:
            jsonl_file.write(json.dumps(row, ensure_ascii=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the data intelligence pipeline.")
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="Path to seed_companies.csv.",
    )
    parser.add_argument(
        "--collect-live",
        action="store_true",
        help="Enable responsible live public-web collection. Disabled by default.",
    )
    parser.add_argument(
        "--max-pages-per-domain",
        type=int,
        default=8,
        help="Strict page limit per seed domain when --collect-live is used.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=10,
        help="HTTP timeout for each request when --collect-live is used.",
    )
    parser.add_argument(
        "--rate-limit-seconds",
        type=float,
        default=1.0,
        help="Minimum delay between requests to the same domain.",
    )
    parser.add_argument(
        "--playwright-fallback",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable optional browser rendering for eligible sparse JS app shells.",
    )
    parser.add_argument(
        "--browser-timeout-seconds",
        type=int,
        default=15,
        help="Strict browser navigation timeout when fallback is enabled.",
    )
    parser.add_argument(
        "--browser-render-wait-ms",
        type=int,
        default=1000,
        help="Bounded render wait after domcontentloaded when fallback is enabled.",
    )
    parser.add_argument(
        "--minimum-visible-text-chars",
        type=int,
        default=250,
        help="Visible-text threshold below which app shells may use browser fallback.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print progress logs and browser-rendering diagnostics.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = CollectionConfig(
        live_collection_enabled=args.collect_live,
        max_pages_per_domain=args.max_pages_per_domain,
        timeout_seconds=args.timeout_seconds,
        rate_limit_seconds=args.rate_limit_seconds,
        playwright_fallback_enabled=args.playwright_fallback,
        browser_timeout_seconds=args.browser_timeout_seconds,
        browser_render_wait_ms=args.browser_render_wait_ms,
        minimum_visible_text_chars=args.minimum_visible_text_chars,
    )
    progress_logger = _print_progress if args.verbose else None
    summary = run_pipeline(
        input_path=args.input,
        collection_config=config,
        progress_logger=progress_logger,
    )
    print(format_pipeline_summary(summary, verbose=args.verbose))


def _log_progress(progress_logger: ProgressLogger | None, message: str) -> None:
    if progress_logger is not None:
        progress_logger(f"[pipeline] {message}")


def _print_progress(message: str) -> None:
    print(message, flush=True)


if __name__ == "__main__":
    main()


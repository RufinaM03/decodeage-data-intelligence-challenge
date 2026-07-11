import json
import tempfile
from pathlib import Path
import unittest

from src.models import CollectedPage, LeadRecord
from src.pipeline import (
    PipelineSummary,
    _collection_stats,
    _qualification_stats,
    _write_jsonl,
    format_pipeline_summary,
)


class PipelineSerializationTests(unittest.TestCase):
    def test_jsonl_escapes_unicode_line_separators(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "records.jsonl"
            _write_jsonl(path, [{"html": "one\u2028two", "id": "A"}])

            lines = path.read_text(encoding="utf-8").splitlines()

            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["html"], "one\u2028two")


class PipelineSummaryFormattingTests(unittest.TestCase):
    def test_default_summary_names_urllib_and_beautifulsoup_once(self):
        text = format_pipeline_summary(_summary())

        self.assertEqual(text.count("urllib"), 1)
        self.assertEqual(text.count("BeautifulSoup"), 1)
        self.assertIn("Runtime: urllib + BeautifulSoup (html.parser)", text)

    def test_default_summary_has_only_one_browser_fallback_line(self):
        text = format_pipeline_summary(_summary(playwright_fallback_enabled=True))
        browser_lines = [
            line for line in text.splitlines() if line.startswith("Browser rendering fallback:")
        ]

        self.assertEqual(len(browser_lines), 1)

    def test_zero_browser_diagnostics_are_hidden_without_verbose(self):
        text = format_pipeline_summary(
            _summary(
                playwright_fallback_enabled=True,
                playwright_eligible_pages=0,
                playwright_attempts=0,
                playwright_successes=0,
                playwright_failures=0,
            )
        )

        self.assertIn("Browser rendering fallback: enabled, not needed", text)
        self.assertNotIn("[diagnostics]", text)
        self.assertNotIn("0 attempted | 0 successful | 0 failed", text)

    def test_verbose_summary_exposes_browser_diagnostics(self):
        text = format_pipeline_summary(
            _summary(
                playwright_fallback_enabled=True,
                playwright_eligible_pages=3,
                playwright_attempts=2,
                playwright_successes=1,
                playwright_failures=1,
                playwright_skipped_disabled=4,
                playwright_ineligible_restricted=5,
            ),
            verbose=True,
        )

        self.assertIn("[diagnostics] Browser fallback eligible: 3", text)
        self.assertIn("[diagnostics] Browser fallback attempted: 2", text)
        self.assertIn("[diagnostics] Browser fallback succeeded: 1", text)
        self.assertIn("[diagnostics] Browser fallback failed: 1", text)
        self.assertIn("[diagnostics] Browser fallback skipped because disabled: 4", text)
        self.assertIn("[diagnostics] Browser fallback ineligible because restricted: 5", text)

    def test_lead_status_counts_sum_to_lead_master_rows(self):
        summary = _summary(
            lead_rows=18,
            qualified_count=12,
            pending_human_review_count=2,
            not_a_fit_count=2,
            insufficient_information_count=2,
        )

        total_statuses = (
            summary.qualified_count
            + summary.pending_human_review_count
            + summary.not_a_fit_count
            + summary.insufficient_information_count
        )

        self.assertEqual(total_statuses, summary.lead_rows)
        self.assertIn(
            "Qualification: 12 qualified | 2 pending review | 2 not a fit | 2 insufficient information",
            format_pipeline_summary(summary),
        )

    def test_review_queue_count_is_rendered_from_summary(self):
        text = format_pipeline_summary(_summary(lead_rows=21, review_rows=8))

        self.assertIn("Outputs: 21 lead-master rows | 8 review-queue rows", text)

    def test_collection_status_counts_are_calculated_from_actual_statuses(self):
        pages = [
            CollectedPage(seed_id="A", normalized_domain="a.com", source_url="https://a.com", status="fetched"),
            CollectedPage(seed_id="B", normalized_domain="b.com", source_url="https://b.com", status="fetched"),
            CollectedPage(seed_id="C", normalized_domain="c.com", source_url="https://c.com", status="blocked_by_robots"),
            CollectedPage(seed_id="D", normalized_domain="d.com", source_url="https://d.com", status="access_restricted"),
            CollectedPage(seed_id="E", normalized_domain="e.com", source_url="https://e.com", status="rate_limited"),
            CollectedPage(seed_id="F", normalized_domain="f.com", source_url="https://f.com", status="failed"),
        ]

        stats = _collection_stats(pages)

        self.assertEqual(stats["attempted"], 6)
        self.assertEqual(stats["fetched"], 2)
        self.assertEqual(stats["blocked_by_robots"], 1)
        self.assertEqual(stats["access_restricted"], 1)
        self.assertEqual(stats["rate_limited"], 1)
        self.assertEqual(stats["failed"], 1)

    def test_output_path_is_shown_once(self):
        text = format_pipeline_summary(_summary(output_dir="D:\\challenge"))

        self.assertEqual(text.count("Saved to:"), 1)
        self.assertEqual(text.count("D:\\challenge"), 1)

    def test_browser_fallback_states_render_correctly(self):
        disabled = format_pipeline_summary(_summary(playwright_fallback_enabled=False))
        enabled_unused = format_pipeline_summary(
            _summary(
                playwright_fallback_enabled=True,
                playwright_eligible_pages=0,
                playwright_attempts=0,
            )
        )
        used = format_pipeline_summary(
            _summary(
                playwright_fallback_enabled=True,
                playwright_eligible_pages=2,
                playwright_attempts=2,
                playwright_successes=1,
                playwright_failures=1,
            )
        )

        self.assertIn("Browser rendering fallback: disabled", disabled)
        self.assertIn("Browser rendering fallback: enabled, not needed", enabled_unused)
        self.assertIn(
            "Browser rendering fallback: 2 attempted | 1 successful | 1 failed",
            used,
        )

    def test_summary_statistics_are_not_hardcoded(self):
        text = format_pipeline_summary(
            _summary(
                seeds_loaded=7,
                duplicate_domain_groups=3,
                similar_name_candidates=4,
                collection_attempts=11,
                collection_fetched=8,
                collection_blocked_by_robots=1,
                collection_access_restricted=1,
                collection_rate_limited=0,
                collection_failed=1,
                evidence_records=19,
                lead_rows=7,
                review_rows=5,
                qualified_count=3,
                pending_human_review_count=1,
                not_a_fit_count=2,
                insufficient_information_count=1,
            )
        )

        self.assertIn("Input: 7 seeds", text)
        self.assertIn("Deduplication: 3 exact-domain groups | 4 similar-name candidates", text)
        self.assertIn(
            "Collection: 11 attempted | 8 fetched | 1 blocked by robots.txt | 1 restricted/gated | 0 rate-limited | 1 failed",
            text,
        )
        self.assertIn("Extraction: 19 deduplicated evidence records", text)
        self.assertIn("Outputs: 7 lead-master rows | 5 review-queue rows", text)

    def test_qualification_stats_count_actual_lead_statuses(self):
        leads = [
            _lead("A", "qualified"),
            _lead("B", "pending_human_review"),
            _lead("C", "pending_human_review"),
            _lead("D", "not_a_fit"),
            _lead("E", "insufficient_information"),
        ]

        stats = _qualification_stats(leads)

        self.assertEqual(stats["qualified"], 1)
        self.assertEqual(stats["pending_human_review"], 2)
        self.assertEqual(stats["not_a_fit"], 1)
        self.assertEqual(stats["insufficient_information"], 1)


def _summary(**overrides):
    values = {
        "seeds_loaded": 18,
        "duplicate_domain_groups": 0,
        "similar_name_candidates": 1,
        "collected_pages": 86,
        "evidence_records": 548,
        "lead_rows": 18,
        "review_rows": 14,
        "live_collection_enabled": True,
        "playwright_fallback_enabled": True,
        "output_dir": "D:\\2026-27\\Tasks\\DecodeAge\\Data_Intelligence_Task\\data-intelligence-challenge",
        "collection_attempts": 86,
        "collection_fetched": 74,
        "collection_blocked_by_robots": 1,
        "collection_access_restricted": 5,
        "collection_rate_limited": 0,
        "collection_failed": 6,
        "playwright_eligible_pages": 0,
        "playwright_attempts": 0,
        "playwright_successes": 0,
        "playwright_failures": 0,
        "playwright_skipped_disabled": 0,
        "playwright_ineligible_restricted": 5,
        "qualified_count": 12,
        "pending_human_review_count": 2,
        "not_a_fit_count": 2,
        "insufficient_information_count": 2,
    }
    values.update(overrides)
    return PipelineSummary(**values)


def _lead(name, status):
    return LeadRecord(
        lead_id=f"LEAD-{name}",
        company_name=name,
        website_url=f"https://{name.casefold()}.example",
        normalized_domain=f"{name.casefold()}.example",
        lead_status=status,
    )


if __name__ == "__main__":
    unittest.main()

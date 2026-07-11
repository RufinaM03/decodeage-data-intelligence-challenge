import unittest

from src.extractor import extract_evidence
from src.models import CollectedPage, SeedRecord
from src.normalize import normalize_seed


class ExtractorTests(unittest.TestCase):
    def test_extracts_evidence_with_source_url_and_snippet(self):
        seed = normalize_seed(
            SeedRecord(
                seed_id="SEED-1",
                company_name="Example Nutrition",
                website_url="https://example.in",
            )
        )
        page = CollectedPage(
            seed_id="SEED-1",
            normalized_domain="example.in",
            source_url="https://example.in",
            final_url="https://example.in",
            status="fetched",
            page_type="homepage",
            html=(
                "<html><head>"
                "<title>Example Nutrition</title>"
                '<meta name="description" content="Nutrition supplements for women in India.">'
                "</head><body>"
                '<a href="/products">Shop products</a>'
                '<a href="mailto:support@example.in">Support</a>'
                "Buy now from our online store."
                "</body></html>"
            ),
            json_ld_blocks=(
                '{"@type":"Organization","description":"Plant nutrition brand",'
                '"address":{"addressCountry":"IN"},'
                '"contactPoint":{"email":"support@example.in"}}',
            ),
        )

        evidence = extract_evidence(seed, [page])

        self.assertGreaterEqual(len(evidence), 5)
        self.assertTrue(all(record.source_url == "https://example.in" for record in evidence))
        self.assertTrue(all(record.evidence_snippet for record in evidence))
        self.assertIn(
            "core_product_positioning",
            {record.fact_type for record in evidence},
        )
        self.assertIn(
            "public_contact_route",
            {record.fact_type for record in evidence},
        )

    def test_skips_failed_pages(self):
        seed = normalize_seed(
            SeedRecord(
                seed_id="SEED-2",
                company_name="Blocked",
                website_url="https://blocked.example",
            )
        )
        page = CollectedPage(
            seed_id="SEED-2",
            normalized_domain="blocked.example",
            source_url="https://blocked.example",
            status="access_restricted",
            error="captcha",
        )

        self.assertEqual(extract_evidence(seed, [page]), [])

    def test_redacts_non_generic_emails_and_phone_numbers_from_snippets(self):
        seed = normalize_seed(
            SeedRecord(
                seed_id="SEED-3",
                company_name="Example",
                website_url="https://example.in",
            )
        )
        page = CollectedPage(
            seed_id="SEED-3",
            normalized_domain="example.in",
            source_url="https://example.in",
            final_url="https://example.in",
            status="fetched",
            page_type="homepage",
            html=(
                "<html><head>"
                '<meta name="description" content="Wellness brand. '
                'Founder jane@example.in and +91 98765 43210">'
                "</head><body>Wellness products</body></html>"
            ),
        )

        evidence = extract_evidence(seed, [page])
        joined = " ".join(
            f"{record.fact_value} {record.evidence_snippet}" for record in evidence
        )

        self.assertNotIn("jane@example.in", joined)
        self.assertNotIn("98765 43210", joined)
        self.assertIn("[redacted_email]", joined)
        self.assertIn("[redacted_phone]", joined)


if __name__ == "__main__":
    unittest.main()

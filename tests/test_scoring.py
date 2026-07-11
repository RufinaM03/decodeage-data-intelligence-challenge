import unittest

from src.evidence_quality import filter_and_dedupe_evidence
from src.models import (
    EvidenceRecord,
    LEAD_MASTER_COLUMNS,
    REVIEW_QUEUE_COLUMNS,
    SeedRecord,
)
from src.normalize import normalize_seed
from src.scoring import SCORING_MODEL, build_lead_outputs, score_lead_signals


class ScoringTests(unittest.TestCase):
    def test_full_explicit_component_score_can_qualify(self):
        result = score_lead_signals(
            signals={signal_name: True for signal_name in SCORING_MODEL},
            evidence_count=5,
        )

        self.assertEqual(result.score, 10)
        self.assertEqual(result.status, "qualified")
        self.assertEqual(result.confidence, "high")

    def test_no_evidence_cannot_be_qualified(self):
        result = score_lead_signals(
            signals={signal_name: True for signal_name in SCORING_MODEL},
            evidence_count=0,
        )

        self.assertEqual(result.score, 0)
        self.assertEqual(result.status, "insufficient_information")
        self.assertEqual(result.confidence, "unknown")

    def test_low_score_with_some_evidence_is_not_a_fit(self):
        result = score_lead_signals(
            signals={"supporting_evidence_and_source_quality": True},
            evidence_count=1,
        )

        self.assertEqual(result.score, 1)
        self.assertEqual(result.status, "not_a_fit")
        self.assertEqual(result.confidence, "low")

    def test_valid_indian_d2c_brand_receives_full_score(self):
        seed = _seed("Example Nutrition", "https://example.in")
        evidence = _full_d2c_evidence()

        output = build_lead_outputs([seed], evidence)[0]
        master_row = output.lead_record.to_master_row()
        review_row = output.review_record.to_review_row()

        self.assertEqual(output.lead_record.lead_status, "qualified")
        self.assertEqual(output.lead_record.lead_qualification_score, 10)
        self.assertEqual(output.lead_record.data_confidence, "high")
        self.assertEqual(list(master_row.keys()), LEAD_MASTER_COLUMNS)
        self.assertEqual(list(review_row.keys()), REVIEW_QUEUE_COLUMNS)
        self.assertEqual(review_row["Recommended Next Step"], "Business review.")

    def test_high_score_low_confidence_stays_pending_and_out_of_review_queue(self):
        seed = _seed("Example Nutrition", "https://example.in")
        evidence = [
            _evidence("industry_or_category", "nutrition supplements", "low", "https://example.in"),
            _evidence("product_or_service_type", "protein products", "low", "https://example.in/products"),
            _evidence("core_product_positioning", "wellness products", "low", "https://example.in/products"),
            _evidence("ecommerce_signal", "buy now", "low", "https://example.in/products"),
            _evidence("marketing_or_content_activity_signal", "new launch", "low", "https://example.in/blog"),
        ]

        output = build_lead_outputs([seed], evidence)[0]

        self.assertGreaterEqual(output.lead_record.lead_qualification_score, 7)
        self.assertEqual(output.lead_record.lead_status, "pending_human_review")
        self.assertEqual(output.lead_record.data_confidence, "low")
        self.assertIsNone(output.review_record)

    def test_non_official_evidence_is_not_used_for_lead_fields_or_qualification(self):
        seed = _seed("Example Nutrition", "https://example.in")
        evidence = [
            _evidence(
                "industry_or_category",
                "nutrition",
                "high",
                source_url="https://directory.example/listing",
            )
        ]

        output = build_lead_outputs([seed], evidence)[0]
        master_row = output.lead_record.to_master_row()

        self.assertEqual(output.lead_record.lead_status, "insufficient_information")
        self.assertEqual(output.lead_record.lead_qualification_score, 0)
        self.assertEqual(master_row["Industry or Category"], "unknown")
        self.assertEqual(master_row["Public Contact Route"], "not_available")
        self.assertEqual(master_row["Source URL"], "not_available")
        self.assertIsNone(output.review_record)

    def test_redirect_final_domain_can_be_treated_as_official(self):
        seed = _seed("Example Nutrition", "https://old-example.in")
        evidence = _full_d2c_evidence(domain="example.in")

        output = build_lead_outputs(
            [seed],
            evidence,
            official_domains_by_seed={"SEED-1": "example.in"},
        )[0]

        self.assertEqual(output.lead_record.lead_status, "qualified")
        self.assertEqual(output.lead_record.data_confidence, "high")
        self.assertEqual(output.lead_record.industry_or_category, "nutrition supplements")

    def test_b2b_ingredient_supplier_is_not_consumer_d2c(self):
        seed = _seed("OmniActive Health Technologies", "https://omniactives.com")
        evidence = [
            _evidence("industry_or_category", "nutrition", "medium", "https://omniactives.com"),
            _evidence(
                "core_product_positioning",
                "natural ingredients to nutrition brands around the world",
                "high",
                "https://omniactives.com",
            ),
            _evidence(
                "product_or_service_type",
                "bone joint muscle health ingredients",
                "medium",
                "https://omniactives.com/product-category/bone-joint-muscle-health",
            ),
            _evidence(
                "marketing_or_content_activity_signal",
                "News and Stories",
                "medium",
                "https://omniactives.com/blog",
            ),
        ]

        output = build_lead_outputs([seed], evidence)[0]

        self.assertEqual(output.lead_record.lead_status, "not_a_fit")
        self.assertIn("B2B ingredient", output.lead_record.qualification_rationale)
        self.assertIsNone(output.review_record)

    def test_uk_global_brand_is_not_indian_company(self):
        seed = _seed("Huel", "https://huel.com")
        evidence = [
            _evidence("geography_or_market_signal", "GB", "high", "https://huel.com"),
            _evidence("industry_or_category", "nutrition", "medium", "https://huel.com"),
            _evidence("core_product_positioning", "nutritionally complete food", "high", "https://huel.com"),
            _evidence("product_or_service_type", "Ready-to-drink Meals", "medium", "https://huel.com/collections/other-huel-products"),
            _evidence("ecommerce_signal", "subscribe and save", "medium", "https://huel.com/collections/other-huel-products"),
            _evidence("marketing_or_content_activity_signal", "new flavors", "medium", "https://huel.com/blog"),
        ]

        output = build_lead_outputs([seed], evidence)[0]

        self.assertEqual(output.lead_record.lead_status, "not_a_fit")
        self.assertGreaterEqual(output.lead_record.lead_qualification_score, 7)
        self.assertIsNone(output.review_record)

    def test_parent_company_receives_pending_human_review(self):
        seed = _seed("Honasa Consumer", "https://honasa.in")
        evidence = [
            _evidence("industry_or_category", "beauty", "medium", "https://honasa.in"),
            _evidence(
                "core_product_positioning",
                "Honasa Consumer Ltd. brings you India's loved brands - Mamaearth, The Derma Co. and Aqualogica",
                "medium",
                "https://honasa.in/pages/know-our-brands",
            ),
            _evidence("product_or_service_type", "portfolio of brands", "medium", "https://honasa.in/pages/know-our-brands"),
            _evidence("marketing_or_content_activity_signal", "News and Events", "medium", "https://honasa.in/blogs/news-and-events"),
            _evidence("geography_or_market_signal", "India", "medium", "https://honasa.in/pages/our-story"),
        ]

        output = build_lead_outputs([seed], evidence)[0]

        self.assertEqual(output.lead_record.lead_status, "pending_human_review")
        self.assertIsNotNone(output.review_record)
        self.assertEqual(
            output.review_record.recommended_next_step,
            "Decide whether outreach should target the parent company or brand team.",
        )

    def test_marketplace_receives_pending_human_review(self):
        seed = _seed("HealthKart", "https://healthkart.com")
        evidence = [
            _evidence("industry_or_category", "nutrition supplements", "medium", "https://www.healthkart.com/"),
            _evidence("core_product_positioning", "Buy 100% genuine bodybuilding vitamins and health supplements online", "medium", "https://www.healthkart.com/"),
            _evidence("product_or_service_type", "top brands marketplace", "medium", "https://www.healthkart.com/top-brands-list"),
            _evidence("ecommerce_signal", "buy now", "medium", "https://www.healthkart.com/"),
            _evidence("marketing_or_content_activity_signal", "Offer Zone", "medium", "https://www.healthkart.com/"),
            _evidence("geography_or_market_signal", "India", "medium", "https://www.healthkart.com/hk/AboutUs.action"),
        ]

        output = build_lead_outputs([seed], evidence)[0]

        self.assertEqual(output.lead_record.lead_status, "pending_human_review")
        self.assertIsNotNone(output.review_record)
        self.assertEqual(
            output.review_record.recommended_next_step,
            "Confirm whether marketplaces/platforms are accepted by the ICP.",
        )

    def test_password_gated_site_is_insufficient_information(self):
        seed = _seed("MCaffeine India", "https://mcaffeine.in")
        evidence = [
            _evidence(
                "core_product_positioning",
                "mCaffeine India's first caffeinated brand",
                "medium",
                "https://mcaffeine.in/password",
            )
        ]

        output = build_lead_outputs([seed], evidence)[0]

        self.assertEqual(output.lead_record.lead_status, "insufficient_information")
        self.assertEqual(output.lead_record.lead_qualification_score, 0)
        self.assertIsNone(output.review_record)

    def test_gated_page_contributes_zero_scoring_evidence(self):
        filtered = filter_and_dedupe_evidence(
            [
                _evidence(
                    "product_or_service_type",
                    "Public product page",
                    "medium",
                    "https://example.in/password",
                )
            ]
        )

        self.assertEqual(filtered, [])

    def test_newsletter_text_alone_does_not_produce_marketing_score(self):
        seed = _seed("Example Nutrition", "https://example.in")
        evidence = [
            _evidence("industry_or_category", "nutrition", "medium", "https://example.in"),
            _evidence("product_or_service_type", "protein products", "medium", "https://example.in/products"),
            _evidence("ecommerce_signal", "buy now", "medium", "https://example.in/products"),
            _evidence("marketing_or_content_activity_signal", "newsletter", "medium", "https://example.in"),
        ]

        output = build_lead_outputs([seed], evidence)[0]

        self.assertIn("Marketing/growth 0", output.lead_record.qualification_rationale)

    def test_shopify_boilerplate_does_not_create_commerce_or_product_evidence(self):
        filtered = filter_and_dedupe_evidence(
            [
                _evidence("ecommerce_signal", "cart", "low", snippet="Your Cart is Empty! Proceed to Cart"),
                _evidence("product_or_service_type", "Powered by Shopify", "low", snippet="Powered by Shopify"),
            ]
        )

        self.assertEqual(filtered, [])

    def test_repeated_navigation_and_footer_snippets_are_deduplicated(self):
        repeated = [
            _evidence("public_contact_route", "https://example.in/contact", "medium", snippet="Contact -> https://example.in/contact"),
            _evidence("public_contact_route", "https://example.in/contact", "medium", snippet="Contact -> https://example.in/contact"),
        ]

        filtered = filter_and_dedupe_evidence(repeated)

        self.assertEqual(len(filtered), 1)

    def test_customer_reviews_and_reviewer_names_are_excluded(self):
        filtered = filter_and_dedupe_evidence(
            [
                _evidence(
                    "marketing_or_content_activity_signal",
                    "Customer review by Asha",
                    "low",
                    snippet="Verified buyer customer review by Asha",
                )
            ]
        )

        self.assertEqual(filtered, [])

    def test_evidence_count_does_not_directly_control_confidence(self):
        seed = _seed("Example Nutrition", "https://example.in")
        evidence = [
            _evidence("industry_or_category", f"nutrition {index}", "low", f"https://example.in/page-{index}")
            for index in range(10)
        ]

        output = build_lead_outputs([seed], evidence)[0]

        self.assertEqual(output.lead_record.data_confidence, "low")
        self.assertNotEqual(output.lead_record.lead_status, "qualified")

    def test_distinct_valid_sources_control_evidence_quality_point(self):
        seed = _seed("Example Nutrition", "https://example.in")
        same_page = [
            _evidence("industry_or_category", "nutrition supplements", "high", "https://example.in"),
            _evidence("product_or_service_type", "protein products", "high", "https://example.in"),
            _evidence("core_product_positioning", "India wellness products", "high", "https://example.in"),
            _evidence("ecommerce_signal", "buy now", "high", "https://example.in"),
        ]
        distinct_sources = _full_d2c_evidence()

        same_output = build_lead_outputs([seed], same_page)[0]
        distinct_output = build_lead_outputs([seed], distinct_sources)[0]

        self.assertIn("Evidence quality 0", same_output.lead_record.qualification_rationale)
        self.assertIn("Evidence quality 1", distinct_output.lead_record.qualification_rationale)


def _seed(company_name="Example Nutrition", website_url="https://example.in"):
    return normalize_seed(
        SeedRecord(
            seed_id="SEED-1",
            company_name=company_name,
            website_url=website_url,
        )
    )


def _full_d2c_evidence(domain="example.in"):
    return [
        _evidence("industry_or_category", "nutrition supplements", "high", f"https://{domain}"),
        _evidence("geography_or_market_signal", "India", "medium", f"https://{domain}/about"),
        _evidence("product_or_service_type", "protein products", "high", f"https://{domain}/products/protein"),
        _evidence("core_product_positioning", "clean plant protein wellness products", "high", f"https://{domain}/products/protein"),
        _evidence("ecommerce_signal", "JSON-LD offers data", "high", f"https://{domain}/products/protein"),
        _evidence("marketing_or_content_activity_signal", "new launch campaign", "medium", f"https://{domain}/blog/new-launch"),
    ]


def _evidence(
    fact_type,
    fact_value,
    confidence,
    source_url="https://example.in/about",
    snippet=None,
):
    return EvidenceRecord(
        seed_id="SEED-1",
        company_name="Example Nutrition",
        normalized_domain="example.in",
        fact_type=fact_type,
        fact_value=fact_value,
        source_url=source_url,
        evidence_snippet=snippet or f"{fact_value} evidence",
        collected_at="2026-07-10T00:00:00+00:00",
        confidence=confidence,
        extraction_method="json_ld" if confidence != "low" else "visible_public_text",
    )


if __name__ == "__main__":
    unittest.main()

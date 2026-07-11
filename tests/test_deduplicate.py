import unittest

from src.deduplicate import (
    find_duplicate_domains,
    flag_similar_company_names,
    resolve_final_domains,
)
from src.models import CollectedPage, SeedRecord
from src.normalize import normalize_seed


class DeduplicateTests(unittest.TestCase):
    def test_duplicate_normalized_domains_are_grouped(self):
        seeds = [
            normalize_seed(
                SeedRecord(
                    seed_id="A",
                    company_name="Example Wellness",
                    website_url="https://www.example.com",
                )
            ),
            normalize_seed(
                SeedRecord(
                    seed_id="B",
                    company_name="Example Wellness India",
                    website_url="https://example.com/",
                )
            ),
        ]

        groups = find_duplicate_domains(seeds)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].normalized_domain, "example.com")
        self.assertEqual(groups[0].seed_ids, ("A", "B"))

    def test_redirect_final_domains_are_used_before_seed_domains(self):
        seeds = [
            normalize_seed(
                SeedRecord(
                    seed_id="A",
                    company_name="Example Wellness",
                    website_url="https://old-example.com",
                )
            ),
            normalize_seed(
                SeedRecord(
                    seed_id="B",
                    company_name="Example Wellness Store",
                    website_url="https://www.example.com",
                )
            ),
        ]
        pages = [
            CollectedPage(
                seed_id="A",
                normalized_domain="old-example.com",
                source_url="https://old-example.com",
                final_url="https://example.com",
                redirect_url="https://example.com",
                status="fetched",
                page_type="homepage",
            )
        ]

        groups = find_duplicate_domains(seeds, pages)
        resolutions = resolve_final_domains(seeds, pages)

        self.assertEqual(resolutions["A"].domain, "example.com")
        self.assertEqual(resolutions["A"].basis, "redirect_url")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].normalized_domain, "example.com")
        self.assertEqual(groups[0].duplicate_basis, "redirect_or_final_domain")

    def test_parent_brand_ambiguity_is_flagged_but_not_merged(self):
        seeds = [
            normalize_seed(
                SeedRecord(
                    seed_id="PARENT",
                    company_name="Honasa Consumer Mamaearth",
                    website_url="https://honasa.in",
                )
            ),
            normalize_seed(
                SeedRecord(
                    seed_id="BRAND",
                    company_name="Mamaearth",
                    website_url="https://mamaearth.in",
                )
            ),
        ]

        duplicate_groups = find_duplicate_domains(seeds)
        candidates = flag_similar_company_names(seeds)

        self.assertEqual(duplicate_groups, [])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0].review_note,
            "candidate_only_parent_brand_or_alias_do_not_auto_merge",
        )

    def test_alias_after_generic_suffix_removal_is_flagged_only(self):
        seeds = [
            normalize_seed(
                SeedRecord(
                    seed_id="A",
                    company_name="mCaffeine",
                    website_url="https://www.mcaffeine.com",
                )
            ),
            normalize_seed(
                SeedRecord(
                    seed_id="B",
                    company_name="MCaffeine India",
                    website_url="https://mcaffeine.in",
                )
            ),
        ]

        duplicate_groups = find_duplicate_domains(seeds)
        candidates = flag_similar_company_names(seeds)

        self.assertEqual(duplicate_groups, [])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0].review_note,
            "candidate_only_parent_brand_or_alias_do_not_auto_merge",
        )


if __name__ == "__main__":
    unittest.main()

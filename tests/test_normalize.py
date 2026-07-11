import unittest

from src.normalize import normalize_url


class NormalizeUrlTests(unittest.TestCase):
    def test_correct_https_url_is_kept(self):
        result = normalize_url("https://mamaearth.in")

        self.assertEqual(result.normalized_url, "https://mamaearth.in")
        self.assertEqual(result.normalized_domain, "mamaearth.in")
        self.assertEqual(result.correction_notes, ())

    def test_malformed_htps_scheme_is_repaired(self):
        result = normalize_url("htps://cosmix.in")

        self.assertEqual(result.normalized_url, "https://cosmix.in")
        self.assertEqual(result.normalized_domain, "cosmix.in")
        self.assertIn("corrected_scheme:htps->https", result.correction_notes)

    def test_malformed_url_with_www_query_fragment_is_repaired(self):
        result = normalize_url("htps://www.example.com/products/?utm=1#section")

        self.assertEqual(result.normalized_url, "https://example.com/products")
        self.assertEqual(result.normalized_domain, "example.com")
        self.assertIn("corrected_scheme:htps->https", result.correction_notes)
        self.assertIn("removed_www", result.correction_notes)
        self.assertIn("removed_query", result.correction_notes)
        self.assertIn("removed_fragment", result.correction_notes)

    def test_www_prefix_is_removed(self):
        result = normalize_url("https://www.oziva.in")

        self.assertEqual(result.normalized_url, "https://oziva.in")
        self.assertEqual(result.normalized_domain, "oziva.in")
        self.assertIn("removed_www", result.correction_notes)

    def test_query_string_and_fragment_are_removed(self):
        result = normalize_url("https://example.com/products?utm_source=test#top")

        self.assertEqual(result.normalized_url, "https://example.com/products")
        self.assertEqual(result.normalized_domain, "example.com")
        self.assertIn("removed_query", result.correction_notes)
        self.assertIn("removed_fragment", result.correction_notes)

    def test_trailing_slash_is_removed(self):
        result = normalize_url("https://example.com/")

        self.assertEqual(result.normalized_url, "https://example.com")
        self.assertEqual(result.normalized_domain, "example.com")
        self.assertIn("removed_trailing_slash", result.correction_notes)


if __name__ == "__main__":
    unittest.main()

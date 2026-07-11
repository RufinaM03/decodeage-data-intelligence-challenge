import inspect
import unittest

from src.collector import (
    BrowserRenderResult,
    classify_public_page_url,
    CollectionConfig,
    FetchResponse,
    PlaywrightBrowserRenderer,
    collect_public_pages,
)
from src.html_analysis import HTML_PARSER, analyze_html
from src.models import SeedRecord
from src.normalize import normalize_seed


def _response(
    url,
    status=200,
    body="",
    final_url=None,
    content_type="text/html",
    headers=None,
):
    response_headers = {"content-type": content_type}
    response_headers.update(headers or {})
    return FetchResponse(
        requested_url=url,
        final_url=final_url or url,
        status_code=status,
        headers=response_headers,
        body=body.encode("utf-8"),
        error="" if status < 400 else f"HTTP {status}",
    )


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, config):
        self.calls.append(url)
        return self.responses.get(url, _response(url, status=404, body="not found"))


class FakeBrowserRenderer:
    def __init__(self, html="", error="", final_url=None):
        self.html = html
        self.error = error
        self.final_url = final_url
        self.calls = []

    def __call__(self, url, config):
        self.calls.append(url)
        return BrowserRenderResult(
            requested_url=url,
            final_url=self.final_url or url,
            html=self.html,
            error=self.error,
        )

class FakePlaywrightPage:
    def __init__(self, html="<html><body>Rendered</body></html>", fail_on_goto=False):
        self.html = html
        self.fail_on_goto = fail_on_goto
        self.url = "https://example.com/rendered"
        self.goto_calls = []
        self.wait_calls = []
        self.closed = False

    def goto(self, url, wait_until, timeout):
        self.goto_calls.append(
            {"url": url, "wait_until": wait_until, "timeout": timeout}
        )
        if self.fail_on_goto:
            raise RuntimeError("navigation failed")

    def wait_for_timeout(self, milliseconds):
        self.wait_calls.append(milliseconds)

    def content(self):
        return self.html

    def close(self):
        self.closed = True


class FakePlaywrightContext:
    def __init__(self, page):
        self.page = page
        self.closed = False
        self.user_agent = ""

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakePlaywrightBrowser:
    def __init__(self, page):
        self.page = page
        self.contexts = []
        self.closed = False

    def new_context(self, user_agent):
        context = FakePlaywrightContext(self.page)
        context.user_agent = user_agent
        self.contexts.append(context)
        return context

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.launch_calls = 0
        self.headless_values = []

    def launch(self, headless):
        self.launch_calls += 1
        self.headless_values.append(headless)
        return self.browser


class FakePlaywrightRuntime:
    def __init__(self, page):
        self.browser = FakePlaywrightBrowser(page)
        self.chromium = FakeChromium(self.browser)
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakePlaywrightFactory:
    def __init__(self, page):
        self.page = page
        self.calls = 0
        self.runtime = None

    def __call__(self):
        self.calls += 1
        self.runtime = FakePlaywrightRuntime(self.page)
        return self.runtime

class CollectorTests(unittest.TestCase):
    def _seed(self, url="https://example.com"):
        return normalize_seed(
            SeedRecord(
                seed_id="SEED-1",
                company_name="Example",
                website_url=url,
            )
        )

    def _config(self, max_pages=4, playwright=False, minimum_visible_text_chars=50):
        return CollectionConfig(
            live_collection_enabled=True,
            max_pages_per_domain=max_pages,
            rate_limit_seconds=0,
            max_retries=0,
            playwright_fallback_enabled=playwright,
            minimum_visible_text_chars=minimum_visible_text_chars,
        )

    def _fetcher_for_home(self, body, status=200):
        return FakeFetcher(
            {
                "https://example.com/robots.txt": _response(
                    "https://example.com/robots.txt",
                    body="User-agent: *\nAllow: /\n",
                    content_type="text/plain",
                ),
                "https://example.com/sitemap.xml": _response(
                    "https://example.com/sitemap.xml",
                    body="<urlset></urlset>",
                    content_type="application/xml",
                ),
                "https://example.com": _response(
                    "https://example.com",
                    status=status,
                    body=body,
                ),
            }
        )

    def test_normal_server_rendered_html_is_parsed_with_beautifulsoup(self):
        analysis = analyze_html(
            "<html><head><title>Example</title>"
            '<meta name="description" content="Public nutrition store">'
            '<meta property="og:title" content="OG Example">'
            "</head><body><h1>Hello world</h1>"
            '<a href="/about">About</a></body></html>',
            base_url="https://example.com",
        )

        self.assertEqual(analysis.page_title, "Example")
        self.assertEqual(analysis.meta_description, "Public nutrition store")
        self.assertEqual(analysis.open_graph["og:title"], "OG Example")
        self.assertEqual(analysis.links[0].href, "https://example.com/about")
        self.assertIn("Hello world", analysis.visible_text)

    def test_respects_robots_disallow(self):
        fetcher = FakeFetcher(
            {
                "https://example.com/robots.txt": _response(
                    "https://example.com/robots.txt",
                    body="User-agent: *\nDisallow: /contact\n",
                    content_type="text/plain",
                ),
                "https://example.com/sitemap.xml": _response(
                    "https://example.com/sitemap.xml",
                    body=(
                        "<urlset><url><loc>"
                        "https://example.com/contact"
                        "</loc></url></urlset>"
                    ),
                    content_type="application/xml",
                ),
                "https://example.com": _response(
                    "https://example.com",
                    body="<html><body>Home</body></html>",
                ),
            }
        )

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=2),
            fetcher=fetcher,
            sleeper=lambda _: None,
        )

        blocked = [page for page in pages if page.status == "blocked_by_robots"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].source_url, "https://example.com/contact")
        self.assertFalse(blocked[0].allowed_by_robots)

    def test_prefers_sitemap_candidates_before_default_paths(self):
        fetcher = FakeFetcher(
            {
                "https://example.com/robots.txt": _response(
                    "https://example.com/robots.txt",
                    body="User-agent: *\nAllow: /\n",
                    content_type="text/plain",
                ),
                "https://example.com/sitemap.xml": _response(
                    "https://example.com/sitemap.xml",
                    body=(
                        "<urlset><url><loc>https://example.com/about"
                        "</loc></url></urlset>"
                    ),
                    content_type="application/xml",
                ),
                "https://example.com": _response(
                    "https://example.com",
                    body="<html><body>Home</body></html>",
                ),
                "https://example.com/about": _response(
                    "https://example.com/about",
                    body="<html><body>About</body></html>",
                ),
            }
        )

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=2),
            fetcher=fetcher,
            sleeper=lambda _: None,
        )

        self.assertEqual(
            [page.source_url for page in pages],
            ["https://example.com", "https://example.com/about"],
        )
        self.assertEqual(pages[1].discovery_method, "sitemap")

    def test_links_and_json_ld_are_discovered_correctly(self):
        fetcher = FakeFetcher(
            {
                "https://example.com/robots.txt": _response(
                    "https://example.com/robots.txt",
                    body="User-agent: *\nAllow: /\n",
                    content_type="text/plain",
                ),
                "https://example.com/sitemap.xml": _response(
                    "https://example.com/sitemap.xml",
                    body="<urlset></urlset>",
                    content_type="application/xml",
                ),
                "https://example.com": _response(
                    "https://example.com",
                    body=(
                        '<html><script type="application/ld+json">'
                        '{"@type":"Organization","url":"https://example.com/products"}'
                        "</script><a href='/about'>About</a></html>"
                    ),
                ),
                "https://example.com/products": _response(
                    "https://example.com/products",
                    body="<html><body>Products</body></html>",
                ),
                "https://example.com/about": _response(
                    "https://example.com/about",
                    body="<html><body>About</body></html>",
                ),
            }
        )

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=2),
            fetcher=fetcher,
            sleeper=lambda _: None,
        )

        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[1].source_url, "https://example.com/products")
        self.assertEqual(pages[1].discovery_method, "json_ld")

    def test_visible_script_content_is_excluded_from_visible_text_length(self):
        analysis = analyze_html(
            "<html><body><script>var x = 'very long JavaScript source code';</script>"
            "<p>Real text</p></body></html>"
        )

        self.assertEqual(analysis.visible_text, "Real text")
        self.assertEqual(analysis.visible_text_length, len("Real text"))

    def test_sitemap_and_static_asset_urls_are_not_collected_as_pages(self):
        self.assertIsNone(
            classify_public_page_url(
                "https://example.com/sitemap_products_1.xml",
                "https://example.com",
            )
        )
        self.assertIsNone(
            classify_public_page_url(
                "https://example.com/cdn/shop/files/logo.png",
                "https://example.com",
            )
        )

    def test_redirect_and_access_restriction_are_recorded(self):
        fetcher = FakeFetcher(
            {
                "https://example.com/robots.txt": _response(
                    "https://example.com/robots.txt",
                    body="User-agent: *\nAllow: /\n",
                    content_type="text/plain",
                ),
                "https://example.com/sitemap.xml": _response(
                    "https://example.com/sitemap.xml",
                    body="<urlset></urlset>",
                    content_type="application/xml",
                ),
                "https://example.com": _response(
                    "https://example.com",
                    body="<html><body>Home</body></html>",
                    final_url="https://example.com/home",
                ),
                "https://example.com/about": _response(
                    "https://example.com/about",
                    status=403,
                    body="access denied",
                ),
            }
        )

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=2),
            fetcher=fetcher,
            sleeper=lambda _: None,
        )

        self.assertEqual(pages[0].redirect_url, "https://example.com/home")
        self.assertEqual(pages[1].status, "access_restricted")

    def test_normal_html_does_not_trigger_playwright(self):
        fetcher = self._fetcher_for_home(
            "<html><body><h1>Useful public page</h1><p>"
            "This public page has enough visible content for extraction."
            "</p></body></html>"
        )
        renderer = FakeBrowserRenderer(html="<html><body>Rendered</body></html>")

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=True, minimum_visible_text_chars=10),
            fetcher=fetcher,
            browser_renderer=renderer,
            sleeper=lambda _: None,
        )

        self.assertEqual(renderer.calls, [])
        self.assertEqual(pages[0].collection_method, "urllib_beautifulsoup")
        self.assertEqual(pages[0].fallback_reason, "not_required")

    def test_html_body_can_be_sniffed_when_content_type_is_missing(self):
        fetcher = FakeFetcher(
            {
                "https://example.com/robots.txt": _response(
                    "https://example.com/robots.txt",
                    body="User-agent: *\nAllow: /\n",
                    content_type="text/plain",
                ),
                "https://example.com/sitemap.xml": _response(
                    "https://example.com/sitemap.xml",
                    body="<urlset></urlset>",
                    content_type="application/xml",
                ),
                "https://example.com": _response(
                    "https://example.com",
                    body="<html><body>Useful page</body></html>",
                    content_type="",
                ),
            }
        )

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1),
            fetcher=fetcher,
            sleeper=lambda _: None,
        )

        self.assertEqual(pages[0].status, "fetched")
        self.assertIn("Useful page", pages[0].html)

    def test_sparse_html_without_application_shell_does_not_trigger_playwright(self):
        fetcher = self._fetcher_for_home("<html><body></body></html>")
        renderer = FakeBrowserRenderer(html="<html><body>Rendered</body></html>")

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=True),
            fetcher=fetcher,
            browser_renderer=renderer,
            sleeper=lambda _: None,
        )

        self.assertEqual(renderer.calls, [])
        self.assertEqual(pages[0].fallback_reason, "not_required")

    def test_sparse_application_shell_is_eligible_for_playwright(self):
        fetcher = self._fetcher_for_home(
            '<html><body><div id="root"></div><script src="/static/app.js"></script></body></html>'
        )
        renderer = FakeBrowserRenderer(
            html="<html><body><h1>Rendered public page</h1></body></html>"
        )

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=True),
            fetcher=fetcher,
            browser_renderer=renderer,
            sleeper=lambda _: None,
        )

        self.assertEqual(renderer.calls, ["https://example.com"])
        self.assertEqual(pages[0].collection_method, "playwright_fallback")
        self.assertEqual(pages[0].fallback_reason, "empty_spa_shell")
        self.assertIn("Rendered public page", pages[0].html)

    def test_robots_disallow_prevents_playwright(self):
        fetcher = FakeFetcher(
            {
                "https://example.com/robots.txt": _response(
                    "https://example.com/robots.txt",
                    body="User-agent: *\nDisallow: /\n",
                    content_type="text/plain",
                ),
                "https://example.com/sitemap.xml": _response(
                    "https://example.com/sitemap.xml",
                    body="<urlset></urlset>",
                    content_type="application/xml",
                ),
            }
        )
        renderer = FakeBrowserRenderer(html="<html><body>Rendered</body></html>")

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=True),
            fetcher=fetcher,
            browser_renderer=renderer,
            sleeper=lambda _: None,
        )

        self.assertEqual(renderer.calls, [])
        self.assertEqual(pages[0].status, "blocked_by_robots")

    def test_401_and_403_prevent_playwright(self):
        for status in (401, 403):
            with self.subTest(status=status):
                fetcher = self._fetcher_for_home(
                    "<html><body>Access denied</body></html>",
                    status=status,
                )
                renderer = FakeBrowserRenderer(html="<html><body>Rendered</body></html>")

                pages = collect_public_pages(
                    self._seed(),
                    config=self._config(max_pages=1, playwright=True),
                    fetcher=fetcher,
                    browser_renderer=renderer,
                    sleeper=lambda _: None,
                )

                self.assertEqual(renderer.calls, [])
                self.assertEqual(pages[0].status, "access_restricted")
                self.assertEqual(pages[0].fallback_reason, "ineligible_access_restriction")

    def test_429_prevents_playwright_and_is_recorded_rate_limited(self):
        fetcher = self._fetcher_for_home(
            "<html><body>Too many requests</body></html>",
            status=429,
        )
        renderer = FakeBrowserRenderer(html="<html><body>Rendered</body></html>")

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=True),
            fetcher=fetcher,
            browser_renderer=renderer,
            sleeper=lambda _: None,
        )

        self.assertEqual(renderer.calls, [])
        self.assertEqual(pages[0].status, "rate_limited")
        self.assertEqual(pages[0].fallback_reason, "ineligible_http_status")

    def test_visible_captcha_page_prevents_playwright(self):
        fetcher = self._fetcher_for_home(
            '<html><body><div id="root">Verify you are human CAPTCHA challenge</div>'
            '<script src="/static/app.js"></script></body></html>'
        )
        renderer = FakeBrowserRenderer(html="<html><body>Rendered</body></html>")

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=True),
            fetcher=fetcher,
            browser_renderer=renderer,
            sleeper=lambda _: None,
        )

        self.assertEqual(renderer.calls, [])
        self.assertEqual(pages[0].status, "access_restricted")
        self.assertEqual(pages[0].fallback_reason, "ineligible_access_restriction")

    def test_recaptcha_script_does_not_mark_useful_public_page_restricted(self):
        fetcher = self._fetcher_for_home(
            "<html><body><h1>Public nutrition store</h1>"
            '<script src="https://www.google.com/recaptcha/api.js"></script>'
            "<p>Useful public content about products and nutrition.</p></body></html>"
        )

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=True, minimum_visible_text_chars=10),
            fetcher=fetcher,
            browser_renderer=FakeBrowserRenderer(html="<html><body>Rendered</body></html>"),
            sleeper=lambda _: None,
        )

        self.assertEqual(pages[0].status, "fetched")
        self.assertIn("Public nutrition store", pages[0].html)

    def test_successful_rendered_html_uses_playwright_collection_method(self):
        fetcher = self._fetcher_for_home(
            '<html><body><div id="__next"></div><script src="/assets/app.js"></script></body></html>'
        )
        renderer = FakeBrowserRenderer(
            html="<html><body><h1>Rendered page</h1><p>Useful rendered content.</p></body></html>"
        )

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=True),
            fetcher=fetcher,
            browser_renderer=renderer,
            sleeper=lambda _: None,
        )

        self.assertEqual(pages[0].collection_method, "playwright_fallback")
        self.assertGreater(pages[0].visible_text_length, 0)

    def test_playwright_failure_is_recorded_without_crashing(self):
        fetcher = self._fetcher_for_home(
            '<html><body><div id="__next"></div><script src="/assets/app.js"></script></body></html>'
        )
        renderer = FakeBrowserRenderer(error="browser failed")

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=True),
            fetcher=fetcher,
            browser_renderer=renderer,
            sleeper=lambda _: None,
        )

        self.assertEqual(renderer.calls, ["https://example.com"])
        self.assertEqual(pages[0].collection_method, "urllib_beautifulsoup")
        self.assertEqual(pages[0].fallback_reason, "playwright_failed")
        self.assertIn("browser failed", pages[0].error)

    def test_playwright_disabled_mode_continues_using_primary_result(self):
        fetcher = self._fetcher_for_home(
            '<html><body><div id="app"></div><script src="/assets/app.js"></script></body></html>'
        )
        renderer = FakeBrowserRenderer(html="<html><body>Rendered</body></html>")

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=False),
            fetcher=fetcher,
            browser_renderer=renderer,
            sleeper=lambda _: None,
        )

        self.assertEqual(renderer.calls, [])
        self.assertEqual(pages[0].collection_method, "urllib_beautifulsoup")
        self.assertEqual(pages[0].fallback_reason, "disabled")

    def test_selected_beautifulsoup_parser_is_explicit(self):
        self.assertEqual(HTML_PARSER, "html.parser")

    def test_static_json_ld_does_not_trigger_playwright(self):
        fetcher = self._fetcher_for_home(
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type":"Organization","url":"https://example.com/products"}'
            "</script></head><body>Official public nutrition store with enough text."
            "</body></html>"
        )
        renderer = FakeBrowserRenderer(html="<html><body>Rendered</body></html>")

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=True, minimum_visible_text_chars=10),
            fetcher=fetcher,
            browser_renderer=renderer,
            sleeper=lambda _: None,
        )

        self.assertEqual(renderer.calls, [])
        self.assertEqual(pages[0].collection_method, "urllib_beautifulsoup")
        self.assertTrue(pages[0].json_ld_blocks)

    def test_static_useful_metadata_does_not_trigger_playwright(self):
        fetcher = self._fetcher_for_home(
            "<html><head><title>Example</title>"
            '<meta name="description" content="Official public nutrition store">'
            '<meta property="og:title" content="Example Store">'
            "</head><body></body></html>"
        )
        renderer = FakeBrowserRenderer(html="<html><body>Rendered</body></html>")

        pages = collect_public_pages(
            self._seed(),
            config=self._config(max_pages=1, playwright=True),
            fetcher=fetcher,
            browser_renderer=renderer,
            sleeper=lambda _: None,
        )

        self.assertEqual(renderer.calls, [])
        self.assertEqual(pages[0].fallback_reason, "not_required")

    def test_playwright_rendered_html_uses_shared_analysis_function(self):
        fetcher = self._fetcher_for_home(
            '<html><body><div id="root"></div><script src="/static/app.js"></script></body></html>'
        )
        renderer = FakeBrowserRenderer(
            html="<html><body><h1>Rendered public page</h1></body></html>"
        )

        from unittest.mock import patch

        with patch("src.collector.analyze_html", wraps=analyze_html) as analyzer:
            pages = collect_public_pages(
                self._seed(),
                config=self._config(max_pages=1, playwright=True),
                fetcher=fetcher,
                browser_renderer=renderer,
                sleeper=lambda _: None,
            )

        self.assertGreaterEqual(analyzer.call_count, 2)
        self.assertEqual(pages[0].collection_method, "playwright_fallback")
        self.assertIn("Rendered public page", pages[0].html)

    def test_playwright_renderer_only_returns_page_content_without_business_selectors(self):
        source = inspect.getsource(PlaywrightBrowserRenderer.__call__)

        self.assertIn("page.content()", source)
        for forbidden in ("locator(", "query_selector(", ".click(", ".fill(", ".type("):
            self.assertNotIn(forbidden, source)

    def test_browser_renderer_launch_is_lazy_and_closes_after_success(self):
        page = FakePlaywrightPage(html="<html><body>Rendered success</body></html>")
        factory = FakePlaywrightFactory(page)
        renderer = PlaywrightBrowserRenderer(playwright_factory=factory)

        self.assertEqual(factory.calls, 0)
        result = renderer("https://example.com", self._config(playwright=True))

        self.assertEqual(factory.calls, 1)
        self.assertEqual(result.html, "<html><body>Rendered success</body></html>")
        self.assertEqual(page.goto_calls[0]["wait_until"], "domcontentloaded")
        self.assertTrue(page.closed)
        self.assertTrue(factory.runtime.browser.contexts[0].closed)
        self.assertFalse(factory.runtime.browser.closed)

        renderer.close()

        self.assertTrue(factory.runtime.browser.closed)
        self.assertTrue(factory.runtime.stopped)

    def test_browser_renderer_closes_page_and_context_after_failure(self):
        page = FakePlaywrightPage(fail_on_goto=True)
        factory = FakePlaywrightFactory(page)
        renderer = PlaywrightBrowserRenderer(playwright_factory=factory)

        result = renderer("https://example.com", self._config(playwright=True))

        self.assertIn("navigation failed", result.error)
        self.assertTrue(page.closed)
        self.assertTrue(factory.runtime.browser.contexts[0].closed)

        renderer.close()

        self.assertTrue(factory.runtime.browser.closed)
        self.assertTrue(factory.runtime.stopped)

if __name__ == "__main__":
    unittest.main()



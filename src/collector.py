"""Responsible public-web collection.

The collector uses only public HTTP(S) requests. It checks robots.txt, prefers
sitemap discovery, reads JSON-LD from fetched pages for additional public links,
applies timeouts/retries/rate limits, and refuses to bypass access restrictions.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import json
import socket
import ssl
import time
from urllib import error, request, robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .html_analysis import analyze_html
from .models import CollectedPage, NormalizedSeed, utc_now_iso



USER_AGENT = (
    "DecodeAgeDataIntelligenceChallenge/0.1 "
    "(public research; respects robots.txt; no personal data collection)"
)
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 2
RATE_LIMIT_SECONDS = 1.0
MAX_PAGES_PER_DOMAIN = 8
MAX_BYTES_PER_PAGE = 2_000_000
MAX_SITEMAP_URLS = 3
RATE_LIMIT_RETRY_AFTER_MAX_SECONDS = 10
BROWSER_TIMEOUT_SECONDS = 15
BROWSER_RENDER_WAIT_MS = 1000
MINIMUM_VISIBLE_TEXT_CHARS = 250

RELEVANT_PATH_KEYWORDS = (
    "about",
    "contact",
    "product",
    "products",
    "collections",
    "shop",
    "store",
    "blog",
    "journal",
    "article",
    "story",
    "brand",
)

DEFAULT_DISCOVERY_PATHS = (
    "/",
    "/about",
    "/about-us",
    "/products",
    "/collections",
    "/shop",
    "/blog",
    "/contact",
    "/contact-us",
)

TRANSIENT_STATUS_CODES = {408, 500, 502, 503, 504}
RATE_LIMIT_STATUS_CODE = 429
ACCESS_RESTRICTED_STATUS_CODES = {401, 403}
@dataclass(frozen=True)
class CollectionConfig:
    """Configuration for public collection."""

    user_agent: str = USER_AGENT
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS
    max_retries: int = MAX_RETRIES
    rate_limit_seconds: float = RATE_LIMIT_SECONDS
    max_pages_per_domain: int = MAX_PAGES_PER_DOMAIN
    max_bytes_per_page: int = MAX_BYTES_PER_PAGE
    max_sitemap_urls: int = MAX_SITEMAP_URLS
    live_collection_enabled: bool = False
    respect_robots_txt: bool = True
    playwright_fallback_enabled: bool = False
    browser_timeout_seconds: int = BROWSER_TIMEOUT_SECONDS
    browser_render_wait_ms: int = BROWSER_RENDER_WAIT_MS
    minimum_visible_text_chars: int = MINIMUM_VISIBLE_TEXT_CHARS


@dataclass(frozen=True)
class CandidateUrl:
    """A public page URL selected for possible collection."""

    url: str
    page_type: str
    discovery_method: str


@dataclass(frozen=True)
class FetchResponse:
    """Result from one HTTP request."""

    requested_url: str
    final_url: str
    status_code: int | None
    headers: dict[str, str]
    body: bytes
    error: str = ""


@dataclass(frozen=True)
class BrowserRenderResult:
    """Result from optional browser rendering."""

    requested_url: str
    final_url: str
    html: str
    error: str = ""


@dataclass(frozen=True)
class RobotsRules:
    """Parsed robots.txt rules and the outcome of retrieving them."""

    parser: robotparser.RobotFileParser | None
    robots_url: str
    status: str
    error: str = ""
    sitemap_urls: tuple[str, ...] = ()
    fail_closed: bool = False

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.fail_closed:
            return False
        if self.parser is None:
            return True
        return self.parser.can_fetch(user_agent, url)


Fetcher = Callable[[str, CollectionConfig], FetchResponse]
BrowserRenderer = Callable[[str, CollectionConfig], BrowserRenderResult]
Sleeper = Callable[[float], None]
ProgressLogger = Callable[[str], None]


def robots_txt_url(normalized_url: str) -> str:
    """Return the robots.txt URL for a normalized company URL."""

    split = urlsplit(normalized_url)
    return urlunsplit((split.scheme, split.netloc, "/robots.txt", "", ""))


def default_sitemap_url(normalized_url: str) -> str:
    """Return the conventional sitemap URL for a normalized company URL."""

    split = urlsplit(normalized_url)
    return urlunsplit((split.scheme, split.netloc, "/sitemap.xml", "", ""))


def build_candidate_urls(normalized_url: str) -> list[CandidateUrl]:
    """Build generic candidate URLs without company-specific rules."""

    candidates: list[CandidateUrl] = []
    for path in DEFAULT_DISCOVERY_PATHS:
        candidate_url = _canonicalize_url(urljoin(normalized_url, path))
        page_type = classify_public_page_url(candidate_url, normalized_url)
        if page_type:
            candidates.append(
                CandidateUrl(
                    url=candidate_url,
                    page_type=page_type,
                    discovery_method="default_path",
                )
            )
    return candidates


def collect_public_pages(
    normalized_seed: NormalizedSeed,
    config: CollectionConfig | None = None,
    fetcher: Fetcher | None = None,
    browser_renderer: BrowserRenderer | None = None,
    sleeper: Sleeper | None = None,
    progress_logger: ProgressLogger | None = None,
) -> list[CollectedPage]:
    """Collect allowed public pages for one normalized seed."""

    active_config = config or CollectionConfig()
    if not active_config.live_collection_enabled:
        return []

    active_fetcher = fetcher or fetch_url
    active_browser_renderer = browser_renderer or render_with_playwright
    active_sleeper = sleeper or time.sleep
    rate_limiter = DomainRateLimiter(active_config.rate_limit_seconds, active_sleeper)

    _log_progress(
        progress_logger,
        f"{normalized_seed.seed_id}: checking robots.txt for "
        f"{normalized_seed.normalized_domain}",
    )
    robots_rules = fetch_robots_rules(
        normalized_seed.normalized_url,
        active_config,
        active_fetcher,
        rate_limiter,
    )
    if robots_rules.fail_closed:
        _log_progress(
            progress_logger,
            f"{normalized_seed.seed_id}: blocked because robots.txt could not be "
            "checked safely",
        )
        return [
            CollectedPage(
                seed_id=normalized_seed.seed_id,
                normalized_domain=normalized_seed.normalized_domain,
                source_url=normalized_seed.normalized_url,
                final_url="",
                status="blocked_by_robots",
                response_code=None,
                collected_at=utc_now_iso(),
                error=f"robots.txt check failed closed: {robots_rules.error}",
                allowed_by_robots=False,
                page_type="homepage",
                discovery_method="robots_txt",
            )
        ]

    candidates = _candidate_queue_from_discovery(
        normalized_seed=normalized_seed,
        config=active_config,
        fetcher=active_fetcher,
        rate_limiter=rate_limiter,
        robots_rules=robots_rules,
    )
    _log_progress(
        progress_logger,
        f"{normalized_seed.seed_id}: discovered {len(candidates)} candidate public URLs",
    )

    collected_pages: list[CollectedPage] = []
    visited_urls: set[str] = set()
    queued_urls: set[str] = {candidate.url for candidate in candidates}

    while candidates and len(collected_pages) < active_config.max_pages_per_domain:
        candidate = candidates.popleft()
        candidate_url = _canonicalize_url(candidate.url)
        if candidate_url in visited_urls:
            continue
        visited_urls.add(candidate_url)

        if not _is_same_seed_domain(candidate_url, normalized_seed.normalized_domain):
            continue

        if active_config.respect_robots_txt and not robots_rules.can_fetch(
            active_config.user_agent,
            candidate_url,
        ):
            _log_progress(
                progress_logger,
                f"{normalized_seed.seed_id}: robots.txt blocked {candidate_url}",
            )
            collected_pages.append(
                CollectedPage(
                    seed_id=normalized_seed.seed_id,
                    normalized_domain=normalized_seed.normalized_domain,
                    source_url=candidate_url,
                    status="blocked_by_robots",
                    collected_at=utc_now_iso(),
                    error="robots.txt disallowed this URL",
                    allowed_by_robots=False,
                    page_type=candidate.page_type,
                    discovery_method=candidate.discovery_method,
                )
            )
            continue

        _log_progress(
            progress_logger,
            f"{normalized_seed.seed_id}: fetching "
            f"{len(collected_pages) + 1}/{active_config.max_pages_per_domain} "
            f"{candidate_url}",
        )
        response = fetch_with_retries(
            candidate_url,
            active_config,
            active_fetcher,
            rate_limiter,
        )
        page = _page_from_response(
            response=response,
            seed=normalized_seed,
            candidate=candidate,
            allowed_by_robots=True,
            config=active_config,
        )
        if _is_playwright_eligible(page, active_config):
            _log_progress(
                progress_logger,
                f"{normalized_seed.seed_id}: Playwright fallback eligible for "
                f"{page.source_url}",
            )
            page = _page_with_playwright_fallback(
                original_page=page,
                seed=normalized_seed,
                candidate=candidate,
                config=active_config,
                robots_rules=robots_rules,
                browser_renderer=active_browser_renderer,
            )
        collected_pages.append(page)
        _log_collection_result(progress_logger, page)

        if page.status != "fetched":
            continue

        discovered = _discover_links_from_page(
            html=page.html,
            source_url=page.final_url or page.source_url,
            normalized_seed=normalized_seed,
            config=active_config,
        )
        for new_candidate in reversed(discovered):
            if new_candidate.url in visited_urls:
                continue
            if new_candidate.url in queued_urls:
                candidates = deque(
                    existing for existing in candidates if existing.url != new_candidate.url
                )
            else:
                queued_urls.add(new_candidate.url)
            candidates.appendleft(new_candidate)

    _log_progress(
        progress_logger,
        f"{normalized_seed.seed_id}: collection complete with "
        f"{len(collected_pages)} records",
    )
    return collected_pages


def fetch_robots_rules(
    normalized_url: str,
    config: CollectionConfig,
    fetcher: Fetcher,
    rate_limiter: "DomainRateLimiter",
) -> RobotsRules:
    """Fetch and parse robots.txt using the configured user agent."""

    robots_url = robots_txt_url(normalized_url)
    if not config.respect_robots_txt:
        return RobotsRules(
            parser=None,
            robots_url=robots_url,
            status="disabled",
            error="robots.txt checks disabled by configuration",
        )

    response = fetch_with_retries(robots_url, config, fetcher, rate_limiter)
    if response.status_code and 200 <= response.status_code < 300:
        text = _decode_body(response.body, response.headers)
        parser = robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(text.splitlines())
        return RobotsRules(
            parser=parser,
            robots_url=robots_url,
            status="parsed",
            sitemap_urls=tuple(_extract_sitemap_urls_from_robots(text)),
        )

    if response.status_code == 404:
        return RobotsRules(
            parser=None,
            robots_url=robots_url,
            status="not_found",
            error="robots.txt returned 404; proceeding with public-page limits",
        )

    if response.status_code in ACCESS_RESTRICTED_STATUS_CODES or response.error:
        return RobotsRules(
            parser=None,
            robots_url=robots_url,
            status="failed_closed",
            error=response.error or f"robots.txt returned HTTP {response.status_code}",
            fail_closed=True,
        )

    if response.status_code and response.status_code >= 500:
        return RobotsRules(
            parser=None,
            robots_url=robots_url,
            status="failed_closed",
            error=f"robots.txt returned HTTP {response.status_code}",
            fail_closed=True,
        )

    return RobotsRules(
        parser=None,
        robots_url=robots_url,
        status="unavailable",
        error=f"robots.txt returned HTTP {response.status_code}",
    )


def fetch_with_retries(
    url: str,
    config: CollectionConfig,
    fetcher: Fetcher,
    rate_limiter: "DomainRateLimiter",
) -> FetchResponse:
    """Fetch a URL with bounded retries for transient failures."""

    attempts = config.max_retries + 1
    last_response: FetchResponse | None = None

    for attempt in range(attempts):
        rate_limiter.wait(url)
        response = fetcher(url, config)
        last_response = response

        if response.status_code == RATE_LIMIT_STATUS_CODE:
            retry_after_seconds = _retry_after_seconds(response.headers)
            if (
                attempt == 0
                and config.max_retries > 0
                and retry_after_seconds is not None
                and retry_after_seconds <= RATE_LIMIT_RETRY_AFTER_MAX_SECONDS
            ):
                rate_limiter.sleeper(retry_after_seconds)
                continue
            return response

        if response.error and response.status_code is None and attempt < attempts - 1:
            continue
        if response.status_code in TRANSIENT_STATUS_CODES and attempt < attempts - 1:
            continue
        return response

    return last_response or FetchResponse(
        requested_url=url,
        final_url=url,
        status_code=None,
        headers={},
        body=b"",
        error="No response returned by fetcher",
    )


def fetch_url(url: str, config: CollectionConfig) -> FetchResponse:
    """Fetch one public URL using urllib and the configured limits."""

    headers = {
        "User-Agent": config.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    req = request.Request(url, headers=headers, method="GET")

    try:
        with request.urlopen(req, timeout=config.timeout_seconds) as response:
            body = response.read(config.max_bytes_per_page + 1)
            if len(body) > config.max_bytes_per_page:
                body = body[: config.max_bytes_per_page]
            return FetchResponse(
                requested_url=url,
                final_url=response.geturl(),
                status_code=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=body,
            )
    except error.HTTPError as exc:
        body = exc.read(config.max_bytes_per_page)
        return FetchResponse(
            requested_url=url,
            final_url=exc.geturl(),
            status_code=exc.code,
            headers={key.lower(): value for key, value in exc.headers.items()},
            body=body,
            error=str(exc),
        )
    except (error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        return FetchResponse(
            requested_url=url,
            final_url=url,
            status_code=None,
            headers={},
            body=b"",
            error=str(exc),
        )


def render_with_playwright(url: str, config: CollectionConfig) -> BrowserRenderResult:
    """Render one public URL with a short-lived Playwright renderer."""

    renderer = PlaywrightBrowserRenderer()
    try:
        return renderer(url, config)
    finally:
        renderer.close()


class PlaywrightBrowserRenderer:
    """Lazy, reusable synchronous Playwright renderer.

    The collector decides eligibility before this renderer is called. This adapter
    only opens the permitted URL, waits briefly, returns page.content(), and
    leaves all HTML analysis and evidence extraction to the shared BeautifulSoup
    path.
    """

    def __init__(self, playwright_factory: Callable[[], object] | None = None) -> None:
        self._playwright_factory = playwright_factory
        self._playwright: object | None = None
        self._browser: object | None = None

    def __call__(self, url: str, config: CollectionConfig) -> BrowserRenderResult:
        timeout_ms = max(config.browser_timeout_seconds, 1) * 1000
        context = None
        page = None
        try:
            browser = self._ensure_browser()
            context = browser.new_context(user_agent=config.user_agent)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(max(config.browser_render_wait_ms, 0))
            return BrowserRenderResult(
                requested_url=url,
                final_url=page.url,
                html=page.content(),
            )
        except Exception as exc:
            return BrowserRenderResult(
                requested_url=url,
                final_url=url,
                html="",
                error=str(exc),
            )
        finally:
            _close_resource(page)
            _close_resource(context)

    def _ensure_browser(self) -> object:
        if self._browser is None:
            if self._playwright is None:
                self._playwright = self._start_playwright()
            self._browser = self._playwright.chromium.launch(headless=True)
        return self._browser

    def _start_playwright(self) -> object:
        if self._playwright_factory is not None:
            return self._playwright_factory()
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - exercised without playwright installed
            raise RuntimeError(f"Playwright is not available: {exc}") from exc
        return sync_playwright().start()

    def close(self) -> None:
        _close_resource(self._browser)
        self._browser = None
        _close_resource(self._playwright, method_name="stop")
        self._playwright = None


def _close_resource(resource: object | None, method_name: str = "close") -> None:
    if resource is None:
        return
    close_method = getattr(resource, method_name, None)
    if close_method is None:
        return
    try:
        close_method()
    except Exception:
        pass


class DomainRateLimiter:
    """Small per-domain rate limiter."""

    def __init__(self, rate_limit_seconds: float, sleeper: Sleeper) -> None:
        self.rate_limit_seconds = max(rate_limit_seconds, 0.0)
        self.sleeper = sleeper
        self._last_request_at: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        now = time.monotonic()
        last_request_at = self._last_request_at.get(host)
        if last_request_at is not None:
            elapsed = now - last_request_at
            delay = self.rate_limit_seconds - elapsed
            if delay > 0:
                self.sleeper(delay)
        self._last_request_at[host] = time.monotonic()


def _candidate_queue_from_discovery(
    normalized_seed: NormalizedSeed,
    config: CollectionConfig,
    fetcher: Fetcher,
    rate_limiter: DomainRateLimiter,
    robots_rules: RobotsRules,
) -> deque[CandidateUrl]:
    candidates: deque[CandidateUrl] = deque()
    seen: set[str] = set()

    def add(candidate: CandidateUrl) -> None:
        canonical_url = _canonicalize_url(candidate.url)
        if canonical_url not in seen and _is_same_seed_domain(
            canonical_url,
            normalized_seed.normalized_domain,
        ):
            candidates.append(
                CandidateUrl(
                    url=canonical_url,
                    page_type=candidate.page_type,
                    discovery_method=candidate.discovery_method,
                )
            )
            seen.add(canonical_url)

    add(
        CandidateUrl(
            url=normalized_seed.normalized_url,
            page_type="homepage",
            discovery_method="seed_url",
        )
    )

    for sitemap_url in _sitemap_urls_to_try(normalized_seed, robots_rules, config):
        if config.respect_robots_txt and not robots_rules.can_fetch(
            config.user_agent,
            sitemap_url,
        ):
            continue
        response = fetch_with_retries(sitemap_url, config, fetcher, rate_limiter)
        if response.status_code and 200 <= response.status_code < 300:
            sitemap_candidates = _candidate_urls_from_sitemap(
                response.body,
                normalized_seed.normalized_url,
                normalized_seed.normalized_domain,
            )
            for candidate in sitemap_candidates:
                add(candidate)

    for candidate in build_candidate_urls(normalized_seed.normalized_url):
        add(candidate)

    return candidates


def _sitemap_urls_to_try(
    normalized_seed: NormalizedSeed,
    robots_rules: RobotsRules,
    config: CollectionConfig,
) -> list[str]:
    sitemap_urls = list(robots_rules.sitemap_urls)
    sitemap_urls.append(default_sitemap_url(normalized_seed.normalized_url))

    unique_urls: list[str] = []
    seen: set[str] = set()
    for sitemap_url in sitemap_urls:
        canonical_url = _canonicalize_url(sitemap_url)
        if canonical_url in seen:
            continue
        if not _is_same_seed_domain(canonical_url, normalized_seed.normalized_domain):
            continue
        unique_urls.append(canonical_url)
        seen.add(canonical_url)
        if len(unique_urls) >= config.max_sitemap_urls:
            break
    return unique_urls


def _candidate_urls_from_sitemap(
    body: bytes,
    normalized_url: str,
    normalized_domain: str,
) -> list[CandidateUrl]:
    text = _decode_body(body, {})
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    candidates: list[CandidateUrl] = []
    for loc in root.findall(".//{*}loc"):
        if loc.text is None:
            continue
        candidate_url = _canonicalize_url(loc.text.strip())
        page_type = classify_public_page_url(candidate_url, normalized_url)
        if page_type and _is_same_seed_domain(candidate_url, normalized_domain):
            candidates.append(
                CandidateUrl(
                    url=candidate_url,
                    page_type=page_type,
                    discovery_method="sitemap",
                )
            )
    return candidates


def _discover_links_from_page(
    html: str,
    source_url: str,
    normalized_seed: NormalizedSeed,
    config: CollectionConfig,
) -> list[CandidateUrl]:
    if not html:
        return []

    analysis = analyze_html(html, base_url=source_url)
    json_ld_urls = _extract_urls_from_json_ld_blocks(analysis.json_ld_blocks)
    linked_urls = [link.href for link in analysis.links]

    candidates: list[CandidateUrl] = []
    candidates.extend(
        _candidate_urls_from_iterable(
            json_ld_urls,
            normalized_seed,
            discovery_method="json_ld",
        )
    )
    candidates.extend(
        _candidate_urls_from_iterable(
            linked_urls,
            normalized_seed,
            discovery_method="html_link",
        )
    )
    return candidates[: config.max_pages_per_domain]


def _candidate_urls_from_iterable(
    urls: Iterable[str],
    normalized_seed: NormalizedSeed,
    discovery_method: str,
) -> list[CandidateUrl]:
    candidates: list[CandidateUrl] = []
    seen: set[str] = set()
    for url in urls:
        canonical_url = _canonicalize_url(url)
        if canonical_url in seen:
            continue
        if not _is_same_seed_domain(canonical_url, normalized_seed.normalized_domain):
            continue
        page_type = classify_public_page_url(canonical_url, normalized_seed.normalized_url)
        if page_type:
            candidates.append(
                CandidateUrl(
                    url=canonical_url,
                    page_type=page_type,
                    discovery_method=discovery_method,
                )
            )
            seen.add(canonical_url)
    return candidates


def _page_from_response(
    response: FetchResponse,
    seed: NormalizedSeed,
    candidate: CandidateUrl,
    allowed_by_robots: bool,
    config: CollectionConfig,
) -> CollectedPage:
    content_type = response.headers.get("content-type", "")
    html = ""
    json_ld_blocks: tuple[str, ...] = ()
    status = "fetched"
    error_message = response.error
    visible_text_length = 0
    application_shell_detected = False

    if response.status_code in ACCESS_RESTRICTED_STATUS_CODES:
        status = "access_restricted"
    elif response.status_code == RATE_LIMIT_STATUS_CODE:
        status = "rate_limited"
    elif response.status_code is None:
        status = "failed"
    elif not 200 <= response.status_code < 300:
        status = "failed"

    if response.body and _looks_like_html(content_type, response.body):
        decoded_html = _decode_body(response.body, response.headers)
        analysis = analyze_html(decoded_html, base_url=response.final_url or response.requested_url)
        json_ld_blocks = analysis.json_ld_blocks
        visible_text_length = analysis.visible_text_length
        application_shell_detected = analysis.application_shell_detected

        if analysis.access_restriction_detected:
            status = "access_restricted"
            error_message = "Page visible text appears to contain an access restriction."
        elif status == "fetched":
            html = decoded_html

    redirect_url = ""
    if response.final_url and _canonicalize_url(response.final_url) != _canonicalize_url(
        response.requested_url
    ):
        redirect_url = response.final_url

    return CollectedPage(
        seed_id=seed.seed_id,
        normalized_domain=seed.normalized_domain,
        source_url=response.requested_url,
        final_url=response.final_url,
        status=status,
        response_code=response.status_code,
        redirect_url=redirect_url,
        collected_at=utc_now_iso(),
        content_type=content_type,
        html=html,
        error=error_message,
        allowed_by_robots=allowed_by_robots,
        page_type=candidate.page_type,
        discovery_method=candidate.discovery_method,
        json_ld_blocks=json_ld_blocks,
        collection_method="urllib_beautifulsoup",
        fallback_reason=_fallback_reason_for_primary_page(
            status=status,
            config=config,
            content_type=content_type,
            visible_text_length=visible_text_length,
            application_shell_detected=application_shell_detected,
        ),
        visible_text_length=visible_text_length,
        application_shell_detected=application_shell_detected,
    )


def _is_playwright_eligible(page: CollectedPage, config: CollectionConfig) -> bool:
    """Return True when the fetched page is a sparse app shell."""

    if not config.playwright_fallback_enabled:
        return False
    if not page.allowed_by_robots:
        return False
    if page.status != "fetched":
        return False
    if page.response_code is None or not 200 <= page.response_code < 300:
        return False
    if not _content_type_is_html(page.content_type):
        return False
    if page.visible_text_length >= config.minimum_visible_text_chars:
        return False
    return page.application_shell_detected


def _page_with_playwright_fallback(
    original_page: CollectedPage,
    seed: NormalizedSeed,
    candidate: CandidateUrl,
    config: CollectionConfig,
    robots_rules: RobotsRules,
    browser_renderer: BrowserRenderer,
) -> CollectedPage:
    fallback_reason = _eligible_fallback_reason(original_page)
    rendered = browser_renderer(original_page.source_url, config)
    if rendered.error or not rendered.html:
        return _copy_page_with_fallback_outcome(
            original_page,
            fallback_reason="playwright_failed",
            error=_append_error(original_page.error, rendered.error or "Playwright returned no HTML."),
        )

    rendered_final_url = rendered.final_url or original_page.source_url
    if not _is_same_seed_domain(rendered_final_url, seed.normalized_domain):
        return _copy_page_with_fallback_outcome(
            original_page,
            fallback_reason="playwright_failed",
            error=_append_error(
                original_page.error,
                f"Playwright redirected outside the seed domain: {rendered_final_url}",
            ),
        )

    if config.respect_robots_txt and not robots_rules.can_fetch(
        config.user_agent,
        rendered_final_url,
    ):
        return _copy_page_with_fallback_outcome(
            original_page,
            fallback_reason="playwright_failed",
            error=_append_error(
                original_page.error,
                f"Playwright final URL is disallowed by robots.txt: {rendered_final_url}",
            ),
        )

    analysis = analyze_html(rendered.html, base_url=rendered_final_url)
    if analysis.access_restriction_detected:
        return _copy_page_with_fallback_outcome(
            original_page,
            fallback_reason="ineligible_access_restriction",
            error=_append_error(
                original_page.error,
                "Rendered page visible text appears access-restricted.",
            ),
        )

    return CollectedPage(
        seed_id=seed.seed_id,
        normalized_domain=seed.normalized_domain,
        source_url=original_page.source_url,
        final_url=rendered_final_url,
        status="fetched",
        response_code=original_page.response_code,
        redirect_url=(
            rendered_final_url
            if _canonicalize_url(rendered_final_url) != _canonicalize_url(original_page.source_url)
            else original_page.redirect_url
        ),
        collected_at=utc_now_iso(),
        content_type=original_page.content_type,
        html=rendered.html,
        error=original_page.error,
        allowed_by_robots=True,
        page_type=candidate.page_type,
        discovery_method=candidate.discovery_method,
        json_ld_blocks=analysis.json_ld_blocks,
        collection_method="playwright_fallback",
        fallback_reason=fallback_reason,
        visible_text_length=analysis.visible_text_length,
        application_shell_detected=analysis.application_shell_detected,
    )


def _copy_page_with_fallback_outcome(
    page: CollectedPage,
    fallback_reason: str,
    error: str,
) -> CollectedPage:
    return CollectedPage(
        seed_id=page.seed_id,
        normalized_domain=page.normalized_domain,
        source_url=page.source_url,
        final_url=page.final_url,
        status=page.status,
        response_code=page.response_code,
        redirect_url=page.redirect_url,
        collected_at=page.collected_at,
        content_type=page.content_type,
        html=page.html,
        error=error,
        allowed_by_robots=page.allowed_by_robots,
        page_type=page.page_type,
        discovery_method=page.discovery_method,
        json_ld_blocks=page.json_ld_blocks,
        collection_method=page.collection_method,
        fallback_reason=fallback_reason,
        visible_text_length=page.visible_text_length,
        application_shell_detected=page.application_shell_detected,
    )


def _fallback_reason_for_primary_page(
    status: str,
    config: CollectionConfig,
    content_type: str,
    visible_text_length: int,
    application_shell_detected: bool,
) -> str:
    if status == "access_restricted":
        return "ineligible_access_restriction"
    if status != "fetched":
        return "ineligible_http_status"
    if not _content_type_is_html(content_type):
        return "ineligible_http_status"
    if visible_text_length < config.minimum_visible_text_chars and application_shell_detected:
        if not config.playwright_fallback_enabled:
            return "disabled"
        return _fallback_reason_from_shape(visible_text_length)
    return "not_required"


def _eligible_fallback_reason(page: CollectedPage) -> str:
    if page.fallback_reason in {"empty_spa_shell", "sparse_js_rendered_page"}:
        return page.fallback_reason
    return _fallback_reason_from_shape(page.visible_text_length)


def _fallback_reason_from_shape(visible_text_length: int) -> str:
    return "empty_spa_shell" if visible_text_length == 0 else "sparse_js_rendered_page"


def _append_error(existing_error: str, new_error: str) -> str:
    if existing_error and new_error:
        return f"{existing_error}; {new_error}"
    return existing_error or new_error


def _log_progress(progress_logger: ProgressLogger | None, message: str) -> None:
    if progress_logger is not None:
        progress_logger(f"[collection] {message}")


def _log_collection_result(
    progress_logger: ProgressLogger | None,
    page: CollectedPage,
) -> None:
    if progress_logger is None:
        return
    response_code = page.response_code if page.response_code is not None else "no_response"
    redirect_note = f" -> {page.redirect_url}" if page.redirect_url else ""
    fallback_note = ""
    if page.collection_method == "playwright_fallback":
        fallback_note = " via playwright_fallback"
    elif page.fallback_reason and page.fallback_reason not in {"not_required", "disabled"}:
        fallback_note = f" fallback={page.fallback_reason}"
    progress_logger(
        f"[collection] {page.seed_id}: {page.status} HTTP {response_code} "
        f"{page.source_url}{redirect_note}{fallback_note}"
    )


def classify_public_page_url(url: str, normalized_url: str) -> str | None:
    """Return the allowed page type for a URL, or None if out of scope."""

    split = urlsplit(url)
    root_split = urlsplit(normalized_url)
    path = split.path.rstrip("/").casefold()
    excluded_extensions = (
        ".xml",
        ".txt",
        ".json",
        ".js",
        ".css",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".ico",
        ".pdf",
        ".zip",
    )

    if split.scheme not in {"http", "https"}:
        return None
    if path.endswith(excluded_extensions) or "sitemap" in path:
        return None
    if path in {"", "/"}:
        return "homepage"
    if "about" in path or "story" in path or "brand" in path:
        return "about"
    if "contact" in path:
        return "contact"
    if "blog" in path or "journal" in path or "article" in path:
        return "blog"
    if any(part in path for part in ("product", "products", "collections", "shop", "store")):
        return "product"

    root_path = root_split.path.rstrip("/").casefold()
    if path == root_path:
        return "homepage"
    return None


def _extract_sitemap_urls_from_robots(robots_text: str) -> list[str]:
    sitemap_urls: list[str] = []
    for line in robots_text.splitlines():
        if line.casefold().startswith("sitemap:"):
            sitemap_urls.append(line.split(":", 1)[1].strip())
    return sitemap_urls


def _extract_urls_from_json_ld_blocks(blocks: Iterable[str]) -> list[str]:
    urls: list[str] = []
    for block in blocks:
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        urls.extend(_walk_json_for_urls(parsed))
    return urls


def _walk_json_for_urls(value: object) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, nested_value in value.items():
            if key in {"url", "@id", "sameAs"}:
                urls.extend(_urls_from_json_value(nested_value))
            urls.extend(_walk_json_for_urls(nested_value))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_walk_json_for_urls(item))
    return urls


def _urls_from_json_value(value: object) -> list[str]:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return [value]
    if isinstance(value, list):
        urls: list[str] = []
        for item in value:
            urls.extend(_urls_from_json_value(item))
        return urls
    return []


def _decode_body(body: bytes, headers: dict[str, str]) -> str:
    content_type = headers.get("content-type", "")
    charset = "utf-8"
    for part in content_type.split(";"):
        part = part.strip()
        if part.casefold().startswith("charset="):
            charset = part.split("=", 1)[1].strip()
    return body.decode(charset, errors="replace")


def _retry_after_seconds(headers: dict[str, str]) -> float | None:
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if not retry_after:
        return None
    retry_after = retry_after.strip()
    if retry_after.isdigit():
        return float(retry_after)
    try:
        retry_at = parsedate_to_datetime(retry_after)
        delay = retry_at.timestamp() - time.time()
        return max(delay, 0.0)
    except (TypeError, ValueError, OverflowError):
        return None


def _content_type_is_html(content_type: str) -> bool:
    return "html" in content_type.casefold()


def _looks_like_html(content_type: str, body: bytes) -> bool:
    if _content_type_is_html(content_type):
        return True
    start = body[:200].lstrip().lower()
    return start.startswith((b"<!doctype html", b"<html"))


def _canonicalize_url(url: str) -> str:
    split = urlsplit(url.strip())
    if split.scheme not in {"http", "https"}:
        return url

    host = (split.hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]

    port = ""
    if split.port and not (
        (split.scheme == "https" and split.port == 443)
        or (split.scheme == "http" and split.port == 80)
    ):
        port = f":{split.port}"

    path = split.path or ""
    if path != "/":
        path = path.rstrip("/")
    else:
        path = ""

    return urlunsplit((split.scheme.lower(), f"{host}{port}", path, "", ""))


def _is_same_seed_domain(url: str, normalized_domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host == normalized_domain




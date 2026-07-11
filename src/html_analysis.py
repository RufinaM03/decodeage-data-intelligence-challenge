"""Shared BeautifulSoup-based HTML analysis helpers."""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup


SCRIPTLIKE_TAGS = ("script", "style", "noscript", "template", "svg")
APP_SHELL_IDS = {"root", "app", "__next"}
HTML_PARSER = "html.parser"
ACCESS_RESTRICTION_VISIBLE_MARKERS = (
    "access denied",
    "verify you are human",
    "captcha challenge",
    "password protected",
    "enter using password",
    "store owner? log in",
    "opening soon",
    "subscribe to continue",
    "enable cookies",
    "checking your browser",
)


@dataclass(frozen=True)
class HtmlLink:
    """One discovered link and its visible label."""

    href: str
    text: str


@dataclass(frozen=True)
class HtmlAnalysis:
    """Reusable summary of public HTML content."""

    links: tuple[HtmlLink, ...]
    json_ld_blocks: tuple[str, ...]
    visible_text: str
    visible_text_length: int
    page_title: str
    meta_description: str
    open_graph: dict[str, str]
    application_shell_detected: bool
    access_restriction_detected: bool


def analyze_html(html: str, base_url: str = "") -> HtmlAnalysis:
    """Analyze HTML using BeautifulSoup and Python's built-in html.parser."""

    soup = BeautifulSoup(html or "", HTML_PARSER)

    json_ld_blocks = tuple(
        _clean_text(script.get_text(" ", strip=True))
        for script in soup.find_all("script", attrs={"type": _is_json_ld_type})
        if _clean_text(script.get_text(" ", strip=True))
    )
    links = tuple(_extract_links(soup, base_url))
    page_title = _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    meta_description = _extract_meta_description(soup)
    open_graph = _extract_open_graph(soup)
    application_shell_detected = _detect_application_shell(soup)

    visible_soup = BeautifulSoup(html or "", HTML_PARSER)
    for tag in visible_soup.find_all(SCRIPTLIKE_TAGS):
        tag.decompose()
    visible_text = _clean_text(visible_soup.get_text(" ", strip=True))
    access_restriction_detected = _detect_visible_access_restriction(
        page_title=page_title,
        visible_text=visible_text,
    )

    return HtmlAnalysis(
        links=links,
        json_ld_blocks=json_ld_blocks,
        visible_text=visible_text,
        visible_text_length=len(visible_text),
        page_title=page_title,
        meta_description=meta_description,
        open_graph=open_graph,
        application_shell_detected=application_shell_detected,
        access_restriction_detected=access_restriction_detected,
    )


def _extract_links(soup: BeautifulSoup, base_url: str) -> list[HtmlLink]:
    links: list[HtmlLink] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href:
            continue
        absolute_href = urljoin(base_url, href) if base_url else href
        links.append(
            HtmlLink(
                href=absolute_href,
                text=_clean_text(anchor.get_text(" ", strip=True)),
            )
        )
    return links


def _extract_meta_description(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta and meta.get("content"):
        return _clean_text(str(meta["content"]))

    og_description = soup.find("meta", attrs={"property": "og:description"})
    if og_description and og_description.get("content"):
        return _clean_text(str(og_description["content"]))
    return ""


def _extract_open_graph(soup: BeautifulSoup) -> dict[str, str]:
    open_graph: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        property_name = str(meta.get("property") or "").strip()
        content = _clean_text(str(meta.get("content") or ""))
        if property_name.startswith("og:") and content:
            open_graph[property_name] = content
    return open_graph


def _detect_application_shell(soup: BeautifulSoup) -> bool:
    shell_root = soup.find(id=lambda value: value in APP_SHELL_IDS)
    if shell_root is None:
        return False

    shell_text = _clean_text(shell_root.get_text(" ", strip=True))
    if len(shell_text) > 120:
        return False

    for script in soup.find_all("script"):
        src = str(script.get("src") or "").casefold()
        if src.endswith(".js") or ".js?" in src or "/static/" in src or "/assets/" in src:
            return True
    return False


def _detect_visible_access_restriction(page_title: str, visible_text: str) -> bool:
    combined_text = f"{page_title} {visible_text}".casefold()
    if not combined_text.strip():
        return False
    return any(marker in combined_text for marker in ACCESS_RESTRICTION_VISIBLE_MARKERS)


def _is_json_ld_type(value: object) -> bool:
    return str(value or "").strip().casefold() == "application/ld+json"


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()



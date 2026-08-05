"""Production web search and guarded page extraction for WeChat Hermes."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from agent.web_search_provider import WebSearchProvider
from tools.url_safety import (
    async_is_safe_url,
    is_safe_url,
    normalize_url_for_request,
    redirect_target_from_response,
)
from tools.website_policy import check_website_access


LOG = logging.getLogger(__name__)
USER_AGENT = "WechatHermesResearch/1.0"
SEARCH_CACHE_VERSION = "2"
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/rss+xml",
        "application/xhtml+xml",
        "application/xml",
        "text/csv",
        "text/html",
        "text/markdown",
        "text/plain",
        "text/xml",
    }
)
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
BING_RSS_HOSTS = frozenset({"global.bing.com", "www.bing.com", "cn.bing.com"})
DOMESTIC_SEARCH_ENDPOINTS = (
    ("sogou-mobile", "https://m.sogou.com/web/searchList.jsp", "keyword"),
    ("360-mobile", "https://m.so.com/s", "q"),
    ("baidu-mobile", "https://m.baidu.com/s", "word"),
)
DOMESTIC_SEARCH_HOSTS = frozenset({"m.sogou.com", "m.so.com", "m.baidu.com"})
SEARCH_CHALLENGE_MARKERS = (
    "captcha",
    "punish",
    "\u9a8c\u8bc1\u7801",
    "\u5b89\u5168\u9a8c\u8bc1",
    "\u8bbf\u95ee\u8fc7\u4e8e\u9891\u7e41",
)
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "Chrome/124 Mobile Safari/537.36 " + USER_AGENT
)
DOMAIN_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9.-])"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63})"
    r"(/[^\s]*)?",
    re.IGNORECASE,
)
CJK_RE = re.compile(r"[\u3400-\u9fff]")
FRESHNESS_RE = re.compile(
    r"(?:\blatest\b|\bcurrent\b|\bnews\b|最新|新闻|近期|今日)",
    re.IGNORECASE,
)
BLOCK_TAGS = frozenset(
    {
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
SKIP_TAGS = frozenset({"canvas", "noscript", "script", "style", "svg", "template"})


class _BlockedFetch(RuntimeError):
    pass


class _ResponseTooLarge(RuntimeError):
    pass


class _VisibleTextParser(HTMLParser):
    """Small dependency-free fallback when Trafilatura cannot extract a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._title_depth = 0
        self._title: List[str] = []
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        name = tag.lower()
        if name in SKIP_TAGS:
            self._skip_depth += 1
        if name == "title":
            self._title_depth += 1
        if not self._skip_depth and name in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name == "title" and self._title_depth:
            self._title_depth -= 1
        if name in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if not self._skip_depth and name in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        value = data.strip()
        if not value:
            return
        if self._title_depth:
            self._title.append(value)
        self._parts.append(value + " ")

    def result(self) -> Tuple[str, str]:
        title = _clean_text(" ".join(self._title), 500)
        lines = []
        for line in "".join(self._parts).splitlines():
            normalized = _clean_text(line, 0)
            if normalized:
                lines.append(normalized)
        return title, "\n\n".join(lines)


class _BingResultParser(HTMLParser):
    """Parse Bing result cards without coupling search to extractor packages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._result_depth: Optional[int] = None
        self._h2_depth: Optional[int] = None
        self._link_depth: Optional[int] = None
        self._caption_depth: Optional[int] = None
        self._paragraph_depth: Optional[int] = None
        self._current: Optional[Dict[str, Any]] = None
        self.results: List[Dict[str, Any]] = []

    @staticmethod
    def _attributes(attrs: List[Tuple[str, Optional[str]]]) -> Dict[str, str]:
        return {str(key).lower(): str(value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self._depth += 1
        name = tag.lower()
        attributes = self._attributes(attrs)
        classes = set(attributes.get("class", "").split())
        if name == "li" and "b_algo" in classes and self._current is None:
            self._result_depth = self._depth
            self._current = {"title_parts": [], "url": "", "description_parts": []}
            return
        if self._current is None:
            return
        if name == "h2" and self._h2_depth is None:
            self._h2_depth = self._depth
        elif name == "a" and self._h2_depth is not None and not self._current["url"]:
            self._link_depth = self._depth
            self._current["url"] = attributes.get("href", "")
        elif name == "div" and "b_caption" in classes:
            self._caption_depth = self._depth
        elif name == "p" and self._caption_depth is not None:
            self._paragraph_depth = self._depth

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if self._current is not None:
            if name == "a" and self._link_depth == self._depth:
                self._link_depth = None
            elif name == "h2" and self._h2_depth == self._depth:
                self._h2_depth = None
            elif name == "p" and self._paragraph_depth == self._depth:
                self._paragraph_depth = None
            elif name == "div" and self._caption_depth == self._depth:
                self._caption_depth = None
            elif name == "li" and self._result_depth == self._depth:
                self.results.append(
                    {
                        "title": " ".join(self._current["title_parts"]),
                        "url": self._current["url"],
                        "content": " ".join(self._current["description_parts"]),
                    }
                )
                self._current = None
                self._result_depth = None
                self._h2_depth = None
                self._link_depth = None
                self._caption_depth = None
                self._paragraph_depth = None
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        value = data.strip()
        if not value:
            return
        if self._link_depth is not None:
            self._current["title_parts"].append(value)
        elif self._paragraph_depth is not None:
            self._current["description_parts"].append(value)


class _SogouMobileResultParser(HTMLParser):
    """Parse direct targets embedded in Sogou's mobile result links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._current: Optional[Dict[str, Any]] = None
        self.results: List[Dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a" or self._current is not None:
            return
        attributes = {str(key).lower(): str(value or "") for key, value in attrs}
        if "resultLink" not in attributes.get("class", "").split():
            return
        self._current = {
            "title_parts": [],
            "url": _unwrap_sogou_result_url(attributes.get("href", "")) or "",
        }

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current is None:
            return
        self.results.append(
            {
                "title": " ".join(self._current["title_parts"]),
                "url": self._current["url"],
                "content": "",
            }
        )
        self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None and data.strip():
            self._current["title_parts"].append(data.strip())


class _BaiduMobileResultParser(HTMLParser):
    """Parse Baidu mobile cards without following Baidu redirect links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._current: Optional[Dict[str, Any]] = None
        self._result_div_depth = 0
        self._inside_title = False
        self.results: List[Dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        name = tag.lower()
        attributes = {str(key).lower(): str(value or "") for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if self._current is None:
            if name != "div" or not {"c-result", "result"}.issubset(classes):
                return
            target = attributes.get("mu", "")
            try:
                metadata = json.loads(attributes.get("data-log", "") or "{}")
                if isinstance(metadata, dict):
                    target = str(metadata.get("mu") or target)
            except (TypeError, ValueError):
                pass
            self._current = {"title_parts": [], "url": target}
            self._result_div_depth = 1
            return

        if name == "div":
            self._result_div_depth += 1
        elif name == "h3":
            self._inside_title = True

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        name = tag.lower()
        if name == "h3":
            self._inside_title = False
        elif name == "div":
            self._result_div_depth -= 1
            if self._result_div_depth == 0:
                self.results.append(
                    {
                        "title": " ".join(self._current["title_parts"]),
                        "url": self._current["url"],
                        "content": "",
                    }
                )
                self._current = None
                self._inside_title = False

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._inside_title and data.strip():
            self._current["title_parts"].append(data.strip())


class _SoMobileResultParser(HTMLParser):
    """Parse 360 mobile cards using their explicit public target metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._result_div_depth = 0
        self._inside_title = False
        self._current: Optional[Dict[str, Any]] = None
        self.results: List[Dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        name = tag.lower()
        attributes = {str(key).lower(): str(value or "") for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if self._current is None:
            if name != "div" or not {"g-card", "res-list"}.issubset(classes):
                return
            self._current = {
                "title_parts": [],
                "url": attributes.get("data-pcurl", ""),
            }
            self._result_div_depth = 1
            return

        if name == "div":
            self._result_div_depth += 1
        elif name == "h3" and "res-title" in classes:
            self._inside_title = True

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        name = tag.lower()
        if name == "h3":
            self._inside_title = False
        elif name == "div":
            self._result_div_depth -= 1
            if self._result_div_depth == 0:
                self.results.append(
                    {
                        "title": " ".join(self._current["title_parts"]),
                        "url": self._current["url"],
                        "content": "",
                    }
                )
                self._current = None
                self._inside_title = False

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._inside_title and data.strip():
            self._current["title_parts"].append(data.strip())


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit > 0:
        return text[:limit]
    return text


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _strip_html_fragment(value: Any) -> str:
    parser = _VisibleTextParser()
    parser.feed(str(value or ""))
    _title, content = parser.result()
    return _clean_text(content, 2000)


def _bing_rss_results(response: httpx.Response) -> List[Dict[str, Any]]:
    root = ET.fromstring(response.content)
    results: List[Dict[str, Any]] = []
    for item in root.findall(".//item"):
        results.append(
            {
                "title": item.findtext("title") or "",
                "url": item.findtext("link") or "",
                "content": _strip_html_fragment(item.findtext("description") or ""),
            }
        )
    return results


def _bing_html_results(response: httpx.Response) -> List[Dict[str, Any]]:
    parser = _BingResultParser()
    parser.feed(_decode_body(response.content, response.headers.get("content-type", "")))
    return [item for item in parser.results if item.get("url")]


def _unwrap_sogou_result_url(value: str) -> Optional[str]:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return None
    parameters = parse_qs(parsed.query, keep_blank_values=False)
    for name in ("url", "pcurl"):
        candidates = parameters.get(name) or []
        if candidates and candidates[0]:
            return str(candidates[0])
    return None


def _sogou_mobile_results(response: httpx.Response) -> List[Dict[str, Any]]:
    parser = _SogouMobileResultParser()
    parser.feed(_decode_body(response.content, response.headers.get("content-type", "")))
    return [item for item in parser.results if item.get("url") and item.get("title")]


def _baidu_mobile_results(response: httpx.Response) -> List[Dict[str, Any]]:
    parser = _BaiduMobileResultParser()
    parser.feed(_decode_body(response.content, response.headers.get("content-type", "")))
    return [item for item in parser.results if item.get("url") and item.get("title")]


def _so_mobile_results(response: httpx.Response) -> List[Dict[str, Any]]:
    parser = _SoMobileResultParser()
    parser.feed(_decode_body(response.content, response.headers.get("content-type", "")))
    return [item for item in parser.results if item.get("url") and item.get("title")]


def _validate_domestic_response(
    response: httpx.Response, expected_host: str, max_response: int
) -> str:
    parsed = urlsplit(str(response.url))
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != expected_host
        or expected_host not in DOMESTIC_SEARCH_HOSTS
    ):
        raise _BlockedFetch("Domestic search final URL was rejected")
    if response.is_redirect:
        raise _BlockedFetch("Domestic search redirect was rejected")
    if response.status_code in RETRYABLE_STATUS:
        raise httpx.HTTPStatusError(
            "retryable domestic search response",
            request=response.request,
            response=response,
        )
    response.raise_for_status()
    if len(response.content) > max_response:
        raise _ResponseTooLarge("Domestic search response exceeded configured limit")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type and content_type not in {"text/html", "application/xhtml+xml"}:
        raise _BlockedFetch("Domestic search returned an unsupported content type")
    document = _decode_body(response.content, response.headers.get("content-type", ""))
    lowered = document.casefold()
    result_markers = {
        "m.sogou.com": "resultlink",
        "m.so.com": "g-card res-list",
        "m.baidu.com": "c-result result",
    }
    has_result_markup = result_markers[expected_host] in lowered
    if any(marker in lowered for marker in SEARCH_CHALLENGE_MARKERS) and not has_result_markup:
        raise _BlockedFetch("Domestic search returned a verification challenge")
    return document


def _guard_bing_redirect(response: httpx.Response) -> None:
    target = redirect_target_from_response(response)
    if not target:
        return
    parsed_target = urlsplit(target)
    if (
        parsed_target.scheme.lower() != "https"
        or (parsed_target.hostname or "").lower() not in BING_RSS_HOSTS
    ):
        raise _BlockedFetch("Bing redirect target was rejected")


def _validate_bing_response(response: httpx.Response, max_response: int) -> None:
    parsed = urlsplit(str(response.url))
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in BING_RSS_HOSTS:
        raise _BlockedFetch("Bing final URL was rejected")
    if response.status_code in RETRYABLE_STATUS:
        raise httpx.HTTPStatusError(
            "retryable Bing response",
            request=response.request,
            response=response,
        )
    response.raise_for_status()
    if len(response.content) > max_response:
        raise _ResponseTooLarge("Bing response exceeded configured limit")


def _bing_search_url() -> str:
    value = os.getenv(
        "WECHAT_WEB_BING_URL", "https://global.bing.com/search"
    ).strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in BING_RSS_HOSTS
        or parsed.path.rstrip("/") != "/search"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("WECHAT_WEB_BING_URL is outside the trusted Bing endpoints")
    return value


def _bing_news_url() -> str:
    value = os.getenv(
        "WECHAT_WEB_BING_NEWS_URL", "https://global.bing.com/news/search"
    ).strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in BING_RSS_HOSTS
        or parsed.path.rstrip("/").lower() != "/news/search"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("WECHAT_WEB_BING_NEWS_URL is outside the trusted Bing endpoints")
    return value


def _public_result_url(value: Any) -> Optional[str]:
    raw_value = str(value or "").strip()
    unwrapped = _unwrap_bing_result_url(raw_value) or _unwrap_bing_news_result_url(
        raw_value
    )
    try:
        original = urlsplit(raw_value)
    except ValueError:
        return None
    is_bing_tracking = (
        (original.hostname or "").lower() in BING_RSS_HOSTS
        and original.path.rstrip("/").lower() in {"/ck/a", "/news/apiclick.aspx"}
    )
    if is_bing_tracking and not unwrapped:
        return None
    raw = normalize_url_for_request(unwrapped or raw_value)
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    normalized = urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, "")
    )
    if check_website_access(normalized) or not is_safe_url(normalized):
        return None
    return normalized


def _unwrap_bing_result_url(value: str) -> Optional[str]:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return None
    if (
        (parsed.hostname or "").lower() not in BING_RSS_HOSTS
        or parsed.path.rstrip("/") != "/ck/a"
    ):
        return None
    encoded_values = parse_qs(parsed.query, keep_blank_values=False).get("u") or []
    if not encoded_values:
        return None
    encoded = str(encoded_values[0])
    if not encoded.startswith("a1"):
        return None
    payload = encoded[2:]
    if not payload or len(payload) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+", payload):
        return None
    try:
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
    try:
        target = urlsplit(decoded)
    except ValueError:
        return None
    if target.scheme.lower() not in {"http", "https"} or not target.hostname:
        return None
    return decoded


def _unwrap_bing_news_result_url(value: str) -> Optional[str]:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return None
    if (
        (parsed.hostname or "").lower() not in BING_RSS_HOSTS
        or parsed.path.rstrip("/").lower() != "/news/apiclick.aspx"
    ):
        return None
    encoded_values = parse_qs(parsed.query, keep_blank_values=False).get("url") or []
    if not encoded_values or len(str(encoded_values[0])) > 4096:
        return None
    target = str(encoded_values[0]).strip()
    try:
        parsed_target = urlsplit(target)
    except ValueError:
        return None
    if parsed_target.scheme.lower() not in {"http", "https"} or not parsed_target.hostname:
        return None
    return target


def _query_domain_results(query: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen = set()
    for match in DOMAIN_TOKEN_RE.finditer(query):
        host = match.group(1).lower().rstrip(".")
        path = (match.group(2) or "/").rstrip(".,;:!?)]}") or "/"
        url = _public_result_url("https://" + host + path)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": "Website explicitly named in the query",
                "url": url,
                "content": "Direct public URL supplied in the search query; verify it with web_extract.",
            }
        )
        if len(results) >= 3:
            break
    return results


def _interleave_results(
    primary: List[Dict[str, Any]],
    secondary: List[Dict[str, Any]],
    primary_weight: int = 1,
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    primary_index = 0
    secondary_index = 0
    weight = max(1, min(int(primary_weight), 5))
    while primary_index < len(primary) or secondary_index < len(secondary):
        for _ in range(weight):
            if primary_index >= len(primary):
                break
            merged.append(primary[primary_index])
            primary_index += 1
        if secondary_index < len(secondary):
            merged.append(secondary[secondary_index])
            secondary_index += 1
    return merged


def _bing_market_params(query: str) -> Dict[str, str]:
    if CJK_RE.search(query):
        return {"setlang": "zh-Hans", "cc": "CN", "mkt": "zh-CN"}
    return {
        "setlang": "en-US",
        "cc": "US",
        "mkt": "en-US",
    }


def _upstream_query(query: str) -> str:
    current_year = str(time.gmtime().tm_year)
    if FRESHNESS_RE.search(query) and re.search(
        r"(?<!\d)" + re.escape(current_year) + r"(?!\d)", query
    ):
        rewritten = re.sub(
            r"(?<!\d)" + re.escape(current_year) + r"(?!\d)", " ", query
        )
        return _clean_text(rewritten, 500) or query
    return query


def _decode_body(body: bytes, content_type: str) -> str:
    match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type, re.I)
    candidates = [match.group(1)] if match else []
    candidates.extend(["utf-8", "gb18030"])
    best = ""
    best_replacements = 10**9
    for encoding in candidates:
        try:
            decoded = body.decode(encoding, errors="replace")
        except (LookupError, UnicodeError):
            continue
        replacements = decoded.count("\ufffd")
        if replacements < best_replacements:
            best = decoded
            best_replacements = replacements
        if replacements == 0:
            break
    return best


def _extract_html(document: str, url: str) -> Tuple[str, str]:
    title = ""
    content = ""
    try:
        import trafilatura

        metadata = trafilatura.extract_metadata(document, default_url=url)
        title = _clean_text(getattr(metadata, "title", ""), 500)
        content = trafilatura.extract(
            document,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_formatting=True,
            include_images=False,
            include_links=True,
            deduplicate=True,
            favor_recall=True,
        ) or ""
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        LOG.debug("primary extractor failed error_type=%s", type(exc).__name__)

    if content.strip():
        return title, content.strip()

    parser = _VisibleTextParser()
    parser.feed(document)
    fallback_title, fallback_content = parser.result()
    return title or fallback_title, fallback_content


class WechatCloudWebProvider(WebSearchProvider):
    """SearXNG search plus guarded direct extraction."""

    _cache: "OrderedDict[str, Tuple[float, float, Dict[str, Any]]]" = OrderedDict()
    _cache_lock = threading.Lock()
    _persistent_cache_lock = threading.Lock()
    _initialized_cache_paths = set()
    _circuit_lock = threading.Lock()
    _consecutive_failures = 0
    _circuit_open_until = 0.0
    _source_circuit_lock = threading.Lock()
    _source_failures: Dict[str, int] = {}
    _source_open_until: Dict[str, float] = {}

    @property
    def name(self) -> str:
        return "wechat-cloud"

    @property
    def display_name(self) -> str:
        return "WeChat Cloud Search"

    def is_available(self) -> bool:
        value = os.getenv("WECHAT_WEB_SEARCH_URL", "").strip()
        return value.startswith("http://127.0.0.1:") or value.startswith("http://localhost:")

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    @classmethod
    def _circuit_is_open(cls) -> bool:
        with cls._circuit_lock:
            return time.monotonic() < cls._circuit_open_until

    @classmethod
    def _record_upstream_result(cls, success: bool) -> None:
        with cls._circuit_lock:
            if success:
                cls._consecutive_failures = 0
                cls._circuit_open_until = 0.0
                return
            cls._consecutive_failures += 1
            if cls._consecutive_failures >= 3:
                cls._circuit_open_until = time.monotonic() + _env_int(
                    "WECHAT_WEB_CIRCUIT_SECONDS", 30, 5, 300
                )

    @classmethod
    def _source_is_open(cls, source: str) -> bool:
        with cls._source_circuit_lock:
            return time.monotonic() < cls._source_open_until.get(source, 0.0)

    @classmethod
    def _record_source_result(cls, source: str, success: bool) -> None:
        with cls._source_circuit_lock:
            if success:
                cls._source_failures.pop(source, None)
                cls._source_open_until.pop(source, None)
                return
            failures = cls._source_failures.get(source, 0) + 1
            cls._source_failures[source] = failures
            threshold = _env_int("WECHAT_WEB_SOURCE_CIRCUIT_FAILURES", 2, 1, 5)
            if failures >= threshold:
                cls._source_open_until[source] = time.monotonic() + _env_int(
                    "WECHAT_WEB_SOURCE_CIRCUIT_SECONDS", 300, 10, 3600
                )

    @staticmethod
    def _persistent_cache_path() -> Optional[str]:
        configured = os.getenv("WECHAT_WEB_SEARCH_CACHE_DB", "").strip()
        if configured.lower() in {"off", "none", "disabled"}:
            return None
        if not configured:
            hermes_home = os.getenv("HERMES_HOME", "").strip()
            if not hermes_home:
                home = os.getenv("HOME", "").strip()
                if home and os.path.isabs(home):
                    hermes_home = os.path.join(home, ".hermes")
            if not hermes_home or not os.path.isabs(hermes_home):
                return None
            configured = os.path.join(hermes_home, "cache", "web-search.sqlite3")
        expanded = os.path.abspath(os.path.expanduser(configured))
        return expanded if os.path.isabs(expanded) else None

    @classmethod
    def _initialize_persistent_cache(cls, path: str) -> None:
        with cls._persistent_cache_lock:
            if path in cls._initialized_cache_paths:
                return
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            with sqlite3.connect(path, timeout=2.0) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_cache (
                        cache_key TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        fresh_until REAL NOT NULL,
                        stale_until REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            cls._initialized_cache_paths.add(path)

    @classmethod
    def _load_persistent_search(
        cls, key: str, allow_stale: bool
    ) -> Optional[Tuple[float, float, Dict[str, Any]]]:
        path = cls._persistent_cache_path()
        if not path:
            return None
        try:
            cls._initialize_persistent_cache(path)
            now = time.time()
            with cls._persistent_cache_lock, sqlite3.connect(
                path, timeout=2.0
            ) as connection:
                row = connection.execute(
                    "SELECT payload, fresh_until, stale_until FROM search_cache "
                    "WHERE cache_key = ?",
                    (key,),
                ).fetchone()
            if row is None:
                return None
            payload, fresh_until, stale_until = row
            if float(stale_until) <= now or (
                not allow_stale and float(fresh_until) <= now
            ):
                return None
            value = json.loads(str(payload))
            if not isinstance(value, dict) or value.get("success") is not True:
                return None
            return float(fresh_until), float(stale_until), value
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            LOG.warning(
                "persistent search cache read failed error_type=%s", type(exc).__name__
            )
            return None

    @classmethod
    def _store_persistent_search(
        cls,
        key: str,
        value: Dict[str, Any],
        fresh_until: float,
        stale_until: float,
    ) -> None:
        path = cls._persistent_cache_path()
        if not path:
            return
        try:
            cls._initialize_persistent_cache(path)
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            now = time.time()
            max_entries = _env_int(
                "WECHAT_WEB_SEARCH_CACHE_MAX_ENTRIES", 2048, 128, 10000
            )
            with cls._persistent_cache_lock, sqlite3.connect(
                path, timeout=2.0
            ) as connection:
                connection.execute(
                    """
                    INSERT INTO search_cache
                        (cache_key, payload, fresh_until, stale_until, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        payload = excluded.payload,
                        fresh_until = excluded.fresh_until,
                        stale_until = excluded.stale_until,
                        updated_at = excluded.updated_at
                    """,
                    (key, payload, fresh_until, stale_until, now),
                )
                connection.execute(
                    "DELETE FROM search_cache WHERE stale_until <= ?", (now,)
                )
                connection.execute(
                    "DELETE FROM search_cache WHERE cache_key NOT IN "
                    "(SELECT cache_key FROM search_cache "
                    "ORDER BY updated_at DESC LIMIT ?)",
                    (max_entries,),
                )
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            LOG.warning(
                "persistent search cache write failed error_type=%s", type(exc).__name__
            )

    @classmethod
    def _cached_search(
        cls, key: str, allow_stale: bool = False
    ) -> Optional[Dict[str, Any]]:
        now = time.monotonic()
        with cls._cache_lock:
            entry = cls._cache.get(key)
            if entry is not None:
                fresh_until, stale_until, value = entry
                if fresh_until > now or (allow_stale and stale_until > now):
                    cls._cache.move_to_end(key)
                    return copy.deepcopy(value)
                if stale_until <= now:
                    cls._cache.pop(key, None)

        persistent = cls._load_persistent_search(key, allow_stale)
        if persistent is None:
            return None
        fresh_epoch, stale_epoch, value = persistent
        epoch_now = time.time()
        with cls._cache_lock:
            cls._cache[key] = (
                now + max(0.0, fresh_epoch - epoch_now),
                now + max(0.0, stale_epoch - epoch_now),
                copy.deepcopy(value),
            )
            cls._cache.move_to_end(key)
            while len(cls._cache) > 128:
                cls._cache.popitem(last=False)
        return copy.deepcopy(value)

    @classmethod
    def _store_search(cls, key: str, value: Dict[str, Any]) -> None:
        ttl = _env_int("WECHAT_WEB_SEARCH_CACHE_SECONDS", 300, 0, 3600)
        if ttl == 0:
            return
        stale_ttl = _env_int(
            "WECHAT_WEB_SEARCH_STALE_IF_ERROR_SECONDS", 86400, 0, 604800
        )
        now_monotonic = time.monotonic()
        now_epoch = time.time()
        fresh_monotonic = now_monotonic + ttl
        stale_monotonic = fresh_monotonic + stale_ttl
        fresh_epoch = now_epoch + ttl
        stale_epoch = fresh_epoch + stale_ttl
        with cls._cache_lock:
            cls._cache[key] = (
                fresh_monotonic,
                stale_monotonic,
                copy.deepcopy(value),
            )
            cls._cache.move_to_end(key)
            while len(cls._cache) > 128:
                cls._cache.popitem(last=False)
        cls._store_persistent_search(key, value, fresh_epoch, stale_epoch)

    def _search_domestic_mobile(
        self, query: str, limit: int, timeout: float, max_response: int
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        results: List[Dict[str, Any]] = []
        errors: List[str] = []
        seen = set()
        target_count = min(limit, 5)
        for source, endpoint, parameter_name in DOMESTIC_SEARCH_ENDPOINTS:
            if self._source_is_open(source):
                errors.append("%s:CircuitOpen" % source)
                continue
            try:
                with httpx.Client(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "zh-CN,zh;q=0.9",
                        "User-Agent": MOBILE_USER_AGENT,
                    },
                ) as client:
                    response = client.get(
                        endpoint,
                        params={parameter_name: query},
                    )
                expected_host = urlsplit(endpoint).hostname or ""
                _validate_domestic_response(response, expected_host, max_response)
                if source == "sogou-mobile":
                    parsed_results = _sogou_mobile_results(response)
                elif source == "360-mobile":
                    parsed_results = _so_mobile_results(response)
                else:
                    parsed_results = _baidu_mobile_results(response)

                usable_results = []
                for item in parsed_results:
                    public_url = _public_result_url(item.get("url"))
                    if not public_url or public_url in seen:
                        continue
                    seen.add(public_url)
                    normalized = dict(item)
                    normalized["url"] = public_url
                    usable_results.append(normalized)
                if not usable_results:
                    raise ValueError("domestic search returned no usable results")
                self._record_source_result(source, True)
                results.extend(usable_results)
                if len(results) >= target_count:
                    break
            except Exception as exc:  # noqa: BLE001 - the next source is the fallback
                self._record_source_result(source, False)
                errors.append("%s:%s" % (source, type(exc).__name__))
        return results[:limit], errors

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        normalized_query = _clean_text(query, 500)
        if not normalized_query:
            return {"success": False, "error": "Search query is required"}
        safe_limit = max(1, min(int(limit or 5), 10))
        key = hashlib.sha256(
            (
                SEARCH_CACHE_VERSION
                + "\n"
                + normalized_query
                + "\n"
                + str(safe_limit)
            ).encode("utf-8")
        ).hexdigest()
        cached = self._cached_search(key)
        if cached is not None:
            return cached
        if self._circuit_is_open():
            stale = self._cached_search(key, allow_stale=True)
            if stale is not None:
                LOG.info("web_search stale cache hit query_hash=%s reason=circuit", key[:12])
                return stale
            return {
                "success": False,
                "error": "Search backend circuit is temporarily open after repeated failures",
            }

        base_url = os.getenv("WECHAT_WEB_SEARCH_URL", "").strip().rstrip("/")
        if not self.is_available():
            return {"success": False, "error": "Loopback search backend is not configured"}

        timeout = _env_float("WECHAT_WEB_SEARCH_TIMEOUT_SECONDS", 12.0, 2.0, 30.0)
        max_response = _env_int(
            "WECHAT_WEB_SEARCH_MAX_RESPONSE_BYTES", 2_000_000, 64_000, 8_000_000
        )
        attempts = _env_int("WECHAT_WEB_SEARCH_ATTEMPTS", 2, 1, 3)
        query_hash = key[:12]
        last_error = "search backend unavailable"
        try:
            bing_url = _bing_search_url()
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        bing_news_url: Optional[str] = None
        if _env_bool("WECHAT_WEB_BING_NEWS_RSS_ENABLED", True):
            try:
                bing_news_url = _bing_news_url()
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
        upstream_query = _upstream_query(normalized_query)
        market_params = _bing_market_params(upstream_query)
        freshness_search = bool(FRESHNESS_RE.search(normalized_query))

        for attempt in range(attempts):
            upstream_errors: List[str] = []
            try:
                direct_results = _query_domain_results(normalized_query)
                raw_results: List[Dict[str, Any]] = list(direct_results)

                if _env_bool("WECHAT_WEB_BING_HTML_ENABLED", True):
                    try:
                        with httpx.Client(
                            timeout=timeout,
                            follow_redirects=True,
                            max_redirects=3,
                            trust_env=False,
                            event_hooks={"response": [_guard_bing_redirect]},
                            headers={
                                "Accept": "text/html,application/xhtml+xml",
                                "User-Agent": (
                                    "Mozilla/5.0 (X11; Linux x86_64) "
                                    "AppleWebKit/537.36 Chrome/131 Safari/537.36 "
                                    + USER_AGENT
                                ),
                            },
                        ) as client:
                            response = client.get(
                                bing_url,
                                params={
                                    "q": upstream_query,
                                    "count": safe_limit,
                                    **market_params,
                                },
                            )
                        _validate_bing_response(response, max_response)
                        raw_results.extend(_bing_html_results(response))
                    except Exception as exc:  # noqa: BLE001 - bounded fallback below
                        upstream_errors.append("bing-html:%s" % type(exc).__name__)

                if bing_news_url and freshness_search:
                    try:
                        with httpx.Client(
                            timeout=timeout,
                            follow_redirects=True,
                            max_redirects=3,
                            trust_env=False,
                            event_hooks={"response": [_guard_bing_redirect]},
                            headers={
                                "Accept": "application/rss+xml, application/xml",
                                "User-Agent": USER_AGENT,
                            },
                        ) as client:
                            response = client.get(
                                bing_news_url,
                                params={
                                    "q": upstream_query,
                                    "format": "RSS",
                                    **market_params,
                                },
                            )
                        _validate_bing_response(response, max_response)
                        news_results = _bing_rss_results(response)
                        if news_results:
                            direct_count = len(direct_results)
                            raw_results = (
                                raw_results[:direct_count]
                                + _interleave_results(
                                    news_results,
                                    raw_results[direct_count:],
                                    primary_weight=4,
                                )
                            )
                    except Exception as exc:  # noqa: BLE001 - other sources remain available
                        upstream_errors.append("bing-news:%s" % type(exc).__name__)

                if (
                    len(raw_results) < safe_limit
                    and _env_bool("WECHAT_WEB_BING_RSS_ENABLED", True)
                ):
                    try:
                        with httpx.Client(
                            timeout=timeout,
                            follow_redirects=True,
                            max_redirects=3,
                            trust_env=False,
                            event_hooks={"response": [_guard_bing_redirect]},
                            headers={
                                "Accept": "application/rss+xml, application/xml",
                                "User-Agent": USER_AGENT,
                            },
                        ) as client:
                            response = client.get(
                                bing_url,
                                params={
                                    "q": upstream_query,
                                    "format": "rss",
                                    **market_params,
                                },
                            )
                        _validate_bing_response(response, max_response)
                        raw_results.extend(_bing_rss_results(response))
                    except Exception as exc:  # noqa: BLE001 - SearXNG is the fallback
                        upstream_errors.append("bing-rss:%s" % type(exc).__name__)

                searx_results: List[Dict[str, Any]] = []
                merge_searx = _env_bool("WECHAT_WEB_SEARX_MERGE_ENABLED", False)
                if merge_searx or len(raw_results) < safe_limit:
                    try:
                        with httpx.Client(
                            timeout=timeout,
                            follow_redirects=False,
                            trust_env=False,
                            headers={
                                "Accept": "application/json",
                                "User-Agent": USER_AGENT,
                                "X-Real-IP": "127.0.0.1",
                            },
                        ) as client:
                            response = client.get(
                                base_url + "/search",
                                params={
                                    "q": upstream_query,
                                    "format": "json",
                                    "language": os.getenv(
                                        "WECHAT_WEB_SEARCH_LANGUAGE", "auto"
                                    ),
                                    "safesearch": "1",
                                },
                            )
                        if response.status_code in RETRYABLE_STATUS:
                            raise httpx.HTTPStatusError(
                                "retryable SearXNG response",
                                request=response.request,
                                response=response,
                            )
                        response.raise_for_status()
                        if len(response.content) > max_response:
                            raise _ResponseTooLarge(
                                "SearXNG response exceeded configured limit"
                            )
                        payload = response.json()
                        searx_results = (
                            payload.get("results") if isinstance(payload, dict) else None
                        )
                        if not isinstance(searx_results, list):
                            raise ValueError(
                                "SearXNG response did not contain a result list"
                            )
                        searx_results = list(searx_results)
                    except Exception as exc:  # noqa: BLE001 - normalized below
                        upstream_errors.append("searxng:%s" % type(exc).__name__)

                domestic_results: List[Dict[str, Any]] = []
                if (
                    CJK_RE.search(normalized_query)
                    and _env_bool("WECHAT_WEB_DOMESTIC_FALLBACK_ENABLED", True)
                    and len(searx_results) < min(safe_limit, 3)
                ):
                    domestic_results, domestic_errors = self._search_domestic_mobile(
                        upstream_query, safe_limit, timeout, max_response
                    )
                    upstream_errors.extend(domestic_errors)

                regional_results = searx_results + domestic_results
                if merge_searx and regional_results:
                    direct_count = len(direct_results)
                    raw_results = (
                        raw_results[:direct_count]
                        + _interleave_results(
                            raw_results[direct_count:],
                            regional_results,
                            primary_weight=(
                                4
                                if freshness_search
                                else (1 if CJK_RE.search(normalized_query) else 2)
                            ),
                        )
                    )
                elif regional_results:
                    raw_results.extend(regional_results)

                results: List[Dict[str, Any]] = []
                seen = set()
                for raw in raw_results:
                    if not isinstance(raw, dict):
                        continue
                    url = _public_result_url(raw.get("url"))
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    results.append(
                        {
                            "title": _clean_text(raw.get("title"), 500),
                            "url": url,
                            "description": _clean_text(raw.get("content"), 2000),
                            "position": len(results) + 1,
                        }
                    )
                    if len(results) >= safe_limit:
                        break
                if not results:
                    raise ValueError(
                        "search returned no usable public results (%s)"
                        % ",".join(upstream_errors)
                    )

                value = {"success": True, "data": {"web": results}}
                self._record_upstream_result(True)
                self._store_search(key, value)
                LOG.info(
                    "web_search completed query_hash=%s result_count=%d upstream_errors=%d",
                    query_hash,
                    len(results),
                    len(upstream_errors),
                )
                return value
            except Exception as exc:  # noqa: BLE001 - normalized for the model
                last_error = "+".join(upstream_errors) or type(exc).__name__
                if attempt + 1 < attempts:
                    time.sleep(0.25 * (2**attempt))

        self._record_upstream_result(False)
        stale = self._cached_search(key, allow_stale=True)
        if stale is not None:
            LOG.warning(
                "web_search stale cache hit query_hash=%s attempts=%d error_type=%s",
                query_hash,
                attempts,
                last_error,
            )
            return stale
        LOG.warning(
            "web_search failed query_hash=%s attempts=%d error_type=%s",
            query_hash,
            attempts,
            last_error,
        )
        return {
            "success": False,
            "error": "Search backend failed after bounded retries (%s)" % last_error,
        }

    async def _extract_one(self, raw_url: str) -> Dict[str, Any]:
        url = normalize_url_for_request(str(raw_url or "").strip())
        if not await async_is_safe_url(url):
            return {"url": url, "title": "", "content": "", "error": "Blocked unsafe URL"}
        blocked = check_website_access(url)
        if blocked:
            return {
                "url": url,
                "title": "",
                "content": "",
                "error": "Blocked by website policy",
                "blocked_by_policy": blocked,
            }

        timeout_seconds = _env_float(
            "WECHAT_WEB_EXTRACT_TIMEOUT_SECONDS", 20.0, 3.0, 60.0
        )
        max_bytes = _env_int(
            "WECHAT_WEB_EXTRACT_MAX_BYTES", 3_000_000, 64_000, 20_000_000
        )
        attempts = _env_int("WECHAT_WEB_EXTRACT_ATTEMPTS", 2, 1, 3)

        async def guard_redirect(response: httpx.Response) -> None:
            target = redirect_target_from_response(response)
            if not target:
                return
            if not await async_is_safe_url(target):
                raise _BlockedFetch("redirect targets a private or internal address")
            if check_website_access(target):
                raise _BlockedFetch("redirect target is blocked by website policy")

        last_error = "extract failed"
        for attempt in range(attempts):
            try:
                timeout = httpx.Timeout(
                    connect=min(5.0, timeout_seconds),
                    read=timeout_seconds,
                    write=min(5.0, timeout_seconds),
                    pool=min(5.0, timeout_seconds),
                )
                limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
                async with httpx.AsyncClient(
                    timeout=timeout,
                    limits=limits,
                    follow_redirects=True,
                    max_redirects=5,
                    trust_env=False,
                    event_hooks={"response": [guard_redirect]},
                    headers={
                        "Accept": (
                            "text/html,application/xhtml+xml,application/json,"
                            "text/plain;q=0.9,application/xml;q=0.8"
                        ),
                        "Accept-Encoding": "gzip, deflate",
                        "User-Agent": USER_AGENT,
                    },
                ) as client:
                    async with client.stream("GET", url) as response:
                        if response.status_code in RETRYABLE_STATUS:
                            raise httpx.HTTPStatusError(
                                "retryable extract response",
                                request=response.request,
                                response=response,
                            )
                        response.raise_for_status()
                        final_url = normalize_url_for_request(str(response.url))
                        if not await async_is_safe_url(final_url):
                            raise _BlockedFetch("final URL is unsafe")
                        if check_website_access(final_url):
                            raise _BlockedFetch("final URL is blocked by website policy")

                        media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                        if media_type not in ALLOWED_CONTENT_TYPES and not media_type.startswith("text/"):
                            return {
                                "url": final_url,
                                "title": "",
                                "content": "",
                                "error": "Unsupported content type: %s" % (media_type or "unknown"),
                            }
                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > max_bytes:
                                    raise _ResponseTooLarge("response exceeded configured byte limit")
                            except ValueError:
                                pass

                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > max_bytes:
                                raise _ResponseTooLarge("response exceeded configured byte limit")
                        content_type = response.headers.get("content-type", "")

                document = _decode_body(bytes(body), content_type)
                if media_type in {"text/html", "application/xhtml+xml"}:
                    title, content = await asyncio.to_thread(_extract_html, document, final_url)
                elif media_type in {"application/json", "application/ld+json"}:
                    title = ""
                    try:
                        content = json.dumps(
                            json.loads(document), ensure_ascii=False, indent=2
                        )
                    except (TypeError, ValueError):
                        content = document
                elif media_type in {"application/xml", "application/rss+xml", "text/xml"}:
                    title, content = await asyncio.to_thread(_extract_html, document, final_url)
                else:
                    title, content = "", document

                content = content.strip()
                if not content:
                    raise ValueError("page contained no extractable text")
                LOG.info(
                    "web_extract completed source_host=%s bytes=%d chars=%d",
                    urlsplit(final_url).hostname or "",
                    len(body),
                    len(content),
                )
                return {
                    "url": final_url,
                    "title": title,
                    "content": content,
                    "raw_content": content,
                    "metadata": {"content_type": media_type, "bytes": len(body)},
                }
            except _BlockedFetch as exc:
                return {"url": url, "title": "", "content": "", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - normalized for the model
                last_error = type(exc).__name__
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.25 * (2**attempt))

        LOG.warning(
            "web_extract failed source_host=%s attempts=%d error_type=%s",
            urlsplit(url).hostname or "",
            attempts,
            last_error,
        )
        return {
            "url": url,
            "title": "",
            "content": "",
            "error": "Page extraction failed after bounded retries (%s)" % last_error,
        }

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        max_urls = _env_int("WECHAT_WEB_EXTRACT_MAX_URLS", 5, 1, 10)
        results: List[Dict[str, Any]] = []
        for index, url in enumerate(urls):
            if index >= max_urls:
                results.append(
                    {
                        "url": str(url or ""),
                        "title": "",
                        "content": "",
                        "error": "URL skipped: per-call extraction limit exceeded",
                    }
                )
                continue
            results.append(await self._extract_one(str(url or "")))
        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "self-hosted search + guarded extraction",
            "tag": "Production provider for the cloud WeChat Agent",
            "env_vars": [],
        }

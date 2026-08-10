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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
SEARCH_CACHE_VERSION = "5"
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
TRUSTED_FEED_ENDPOINTS = (
    ("the-verge", "https://www.theverge.com/rss/index.xml"),
    ("techcrunch", "https://techcrunch.com/feed/"),
    ("ars-technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("openai", "https://openai.com/news/rss.xml"),
    ("google-ai", "https://blog.google/technology/ai/rss/"),
    (
        "nvidia-ai",
        "https://blogs.nvidia.com/blog/category/generative-ai/feed/",
    ),
)
TRUSTED_FEED_HOSTS = frozenset(
    (urlsplit(endpoint).hostname or "").lower()
    for _name, endpoint in TRUSTED_FEED_ENDPOINTS
)
TRUSTED_FEED_CONTENT_TYPES = frozenset(
    {
        "application/atom+xml",
        "application/rss+xml",
        "application/xml",
        "text/xml",
    }
)
_trusted_feed_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_trusted_feed_cache_lock = threading.Lock()
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
    r"(?:\blatest\b|\bcurrent\b|\brecent\b|\btoday\b|\bnews\b|"
    r"最新|最近|近期|今天|今日|当前|目前|新闻|热点|实时)",
    re.IGNORECASE,
)
TODAY_QUERY_RE = re.compile(r"(?:今天|今日|\btoday(?:'s)?\b)", re.IGNORECASE)
YEAR_TOKEN_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
ENGLISH_TEMPORAL_TERMS_RE = re.compile(
    r"\b(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?|jan(?:uary)?|feb(?:ruary)?|"
    r"mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.IGNORECASE,
)
QUALITY_RANKING_RE = re.compile(
    r"(?:官方|官网|文档|政策|公告|来源|"
    r"\bofficial\b|\bdocumentation\b|\bdocs?\b|\bpolicy\b|\bsources?\b)",
    re.IGNORECASE,
)
GENERIC_QUERY_TERMS_RE = re.compile(
    r"(?:最新|最近|近期|今天|今日|当前|目前|新闻|消息|热点|实时|更新|"
    r"版本|发布|公告|政策|价格|天气|官方|官网|文档|资料|来源|"
    r"是什么|有哪些|有什么|重要|重大|国内外|"
    r"搜索|搜一下|搜一搜|查找|查询|查查|看看|请|帮我|帮忙|给出|"
    r"\blatest\b|\brecent\b|\bcurrent\b|\btoday\b|\bnews\b|"
    r"\bupdate\b|\bversion\b|\brelease\b|\bofficial\b|"
    r"\bdocumentation\b|\bdocs?\b|\bpolicy\b|\bprice\b|"
    r"\bweather\b|\bsources?\b|\bwhat\b|\bwhich\b|\bthe\b|"
    r"\bis\b|\bare\b|\bplease\b|\bcite\b|\bimportant\b|\bmajor\b|"
    r"\bglobal\b|\binternational\b|\bdomestic\b|\bworldwide\b|"
    r"\bsearch\b|\bfind\b|\blook\s+up\b)",
    re.IGNORECASE,
)
REFERENCE_FRESH_HOST_SUFFIXES = (
    "baike.baidu.com",
    "zhidao.baidu.com",
    "baike.com",
    "wikipedia.org",
    "britannica.com",
    "merriam-webster.com",
    "dictionary.cambridge.org",
    "dictionary.com",
    "imdb.com",
    "edurank.org",
)
LOW_VALUE_FRESH_HOST_SUFFIXES = (
    "csdn.net",
    "cnblogs.com",
    "toutiao.com",
    "bilibili.com",
)
AUTHORITATIVE_HOST_SUFFIXES = (
    "gov.cn",
    "apnews.com",
    "reuters.com",
    "bbc.com",
    "bbc.co.uk",
    "bloomberg.com",
    "ft.com",
    "theverge.com",
    "techcrunch.com",
    "arstechnica.com",
    "wired.com",
    "openai.com",
    "anthropic.com",
    "blog.google",
    "microsoft.com",
    "research.meta.ai",
    "nvidia.com",
    "xinhuanet.com",
    "people.com.cn",
    "cctv.com",
    "chinanews.com.cn",
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
        result = {
            "title": item.findtext("title") or "",
            "url": item.findtext("link") or "",
            "content": _strip_html_fragment(item.findtext("description") or ""),
        }
        published_at = _clean_text(item.findtext("pubDate"), 200)
        if published_at:
            result["published_at"] = published_at
        results.append(result)
    return results


def _trusted_feed_redirect_guard(response: httpx.Response) -> None:
    target = redirect_target_from_response(response)
    if not target:
        return
    parsed_target = urlsplit(target)
    if (
        parsed_target.scheme.lower() != "https"
        or (parsed_target.hostname or "").lower() not in TRUSTED_FEED_HOSTS
    ):
        raise _BlockedFetch("Trusted feed redirect target was rejected")


def _trusted_feed_results_from_response(
    response: httpx.Response,
    source_name: str,
    max_response: int,
) -> List[Dict[str, Any]]:
    parsed_url = urlsplit(str(response.url))
    if (
        parsed_url.scheme.lower() != "https"
        or (parsed_url.hostname or "").lower() not in TRUSTED_FEED_HOSTS
    ):
        raise _BlockedFetch("Trusted feed final URL was rejected")
    if response.status_code in RETRYABLE_STATUS:
        raise httpx.HTTPStatusError(
            "retryable trusted feed response",
            request=response.request,
            response=response,
        )
    response.raise_for_status()
    if len(response.content) > max_response:
        raise _ResponseTooLarge("Trusted feed response exceeded configured limit")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type and content_type not in TRUSTED_FEED_CONTENT_TYPES:
        raise _BlockedFetch("Trusted feed returned an unsupported content type")

    root = ET.fromstring(response.content)
    rows: List[Dict[str, Any]] = []
    for item in root.findall(".//item"):
        row = {
            "title": _clean_text(unescape(item.findtext("title") or ""), 500),
            "url": _clean_text(item.findtext("link"), 4096),
            "content": _strip_html_fragment(item.findtext("description") or ""),
            "source": source_name,
        }
        published_at = _clean_text(item.findtext("pubDate"), 200)
        if published_at:
            row["published_at"] = published_at
        rows.append(row)
        if len(rows) >= 40:
            return rows

    for entry in root.findall(".//{*}entry"):
        link = ""
        for candidate in entry.findall("{*}link"):
            if candidate.get("rel", "alternate") in {"", "alternate"}:
                link = candidate.get("href", "")
                if link:
                    break
        row = {
            "title": _clean_text(
                unescape(entry.findtext("{*}title") or ""),
                500,
            ),
            "url": _clean_text(link, 4096),
            "content": _strip_html_fragment(
                entry.findtext("{*}summary")
                or entry.findtext("{*}content")
                or ""
            ),
            "source": source_name,
        }
        published_at = _clean_text(
            entry.findtext("{*}published") or entry.findtext("{*}updated"),
            200,
        )
        if published_at:
            row["published_at"] = published_at
        rows.append(row)
        if len(rows) >= 40:
            break
    return rows


def _fetch_trusted_feed(
    source_name: str,
    endpoint: str,
    timeout: float,
    max_response: int,
) -> List[Dict[str, Any]]:
    now = time.monotonic()
    with _trusted_feed_cache_lock:
        cached = _trusted_feed_cache.get(endpoint)
        if cached is not None and now < cached[0]:
            return copy.deepcopy(cached[1])
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=2,
        trust_env=False,
        event_hooks={"response": [_trusted_feed_redirect_guard]},
        headers={
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            "User-Agent": USER_AGENT,
        },
    ) as client:
        response = client.get(endpoint)
    rows = _trusted_feed_results_from_response(response, source_name, max_response)
    cache_seconds = _env_int("WECHAT_WEB_TRUSTED_FEED_CACHE_SECONDS", 120, 30, 900)
    with _trusted_feed_cache_lock:
        _trusted_feed_cache[endpoint] = (
            time.monotonic() + cache_seconds,
            copy.deepcopy(rows),
        )
    return rows


def _technology_feed_query(query: str) -> bool:
    return bool(
        re.search(
            r"(?:人工智能|大模型|科技|技术|\bartificial\s+intelligence\b|"
            r"\bai\b|\bllms?\b|\bopenai\b|"
            r"\banthropic\b|\bdeepmind\b|\bdeepseek\b|\bnvidia\b|"
            r"\btechnology\b|\btech\b)",
            query,
            re.IGNORECASE,
        )
    )


def _trusted_feed_results(
    query: str,
    max_response: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    if (
        not _env_bool("WECHAT_WEB_TRUSTED_FEEDS_ENABLED", False)
        or not FRESHNESS_RE.search(query)
        or not _technology_feed_query(query)
    ):
        return [], []
    timeout = _env_float("WECHAT_WEB_TRUSTED_FEED_TIMEOUT_SECONDS", 6.0, 2.0, 15.0)
    workers = _env_int(
        "WECHAT_WEB_TRUSTED_FEED_WORKERS",
        6,
        1,
        len(TRUSTED_FEED_ENDPOINTS),
    )
    ranked_sources: List[List[Dict[str, Any]]] = []
    errors: List[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _fetch_trusted_feed,
                source_name,
                endpoint,
                timeout,
                max_response,
            )
            for source_name, endpoint in TRUSTED_FEED_ENDPOINTS
        ]
        for (source_name, _endpoint), future in zip(TRUSTED_FEED_ENDPOINTS, futures):
            try:
                ranked_sources.append(
                    _rank_search_results(query, future.result())[:4]
                )
            except Exception as exc:  # noqa: BLE001 - independent fixed feed
                errors.append("%s:%s" % (source_name, type(exc).__name__))
    merged: List[Dict[str, Any]] = []
    for index in range(4):
        for rows in ranked_sources:
            if index < len(rows):
                merged.append(rows[index])
    return merged, errors


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


def _official_entry_results(query: str) -> List[Dict[str, Any]]:
    if not QUALITY_RANKING_RE.search(query):
        return []
    entries = (
        (
            r"\bopenai\b",
            "OpenAI official documentation",
            "https://platform.openai.com/docs/",
        ),
        (
            r"\bpython\b",
            "Python official documentation",
            "https://docs.python.org/3/",
        ),
        (
            r"\bkubernetes\b",
            "Kubernetes official documentation",
            "https://kubernetes.io/docs/",
        ),
        (
            r"\bsystemd\b",
            "systemd official documentation",
            "https://systemd.io/",
        ),
        (
            r"(?:国务院|中国政府网)",
            "国务院政策文件库",
            "https://www.gov.cn/zhengce/",
        ),
        (
            r"腾讯云",
            "腾讯云官方文档",
            "https://cloud.tencent.com/document/product",
        ),
        (
            r"阿里云",
            "阿里云官方文档",
            "https://help.aliyun.com/",
        ),
    )
    results = []
    for pattern, title, raw_url in entries:
        if not re.search(pattern, query, re.IGNORECASE):
            continue
        url = _public_result_url(raw_url)
        if not url:
            continue
        results.append(
            {
                "title": title,
                "url": url,
                "content": (
                    "Known public official entry point; verify the relevant page "
                    "with web_extract before answering."
                ),
                "source": "official-entry",
            }
        )
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


def _current_search_date() -> date:
    timezone_name = os.getenv("WECHAT_WEB_TIMEZONE", "Asia/Shanghai").strip()
    try:
        timezone = ZoneInfo(timezone_name or "Asia/Shanghai")
    except (ValueError, ZoneInfoNotFoundError):
        timezone = ZoneInfo("UTC")
    return datetime.now(timezone).date()


def _extract_full_date(value: str) -> Optional[Tuple[str, date]]:
    numeric = re.search(
        r"(?<!\d)((?:19|20)\d{2})\s*(?:年|[-/.])\s*(\d{1,2})"
        r"\s*(?:月|[-/.])\s*(\d{1,2})\s*日?",
        value,
    )
    if numeric:
        try:
            parsed = date(
                int(numeric.group(1)),
                int(numeric.group(2)),
                int(numeric.group(3)),
            )
        except ValueError:
            return None
        subject = _clean_text(value[: numeric.start()] + " " + value[numeric.end() :], 480)
        return subject, parsed

    english = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
        r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
        r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?(?:,|\s)+\s*((?:19|20)\d{2})\b",
        value,
        re.IGNORECASE,
    )
    if not english:
        return None
    month_lookup = {
        name: month
        for month, name in enumerate(
            ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
            start=1,
        )
    }
    try:
        parsed = date(
            int(english.group(3)),
            month_lookup[english.group(1).lower()[:3]],
            int(english.group(2)),
        )
    except (KeyError, ValueError):
        return None
    subject = _clean_text(value[: english.start()] + " " + value[english.end() :], 480)
    return subject, parsed


def _compact_today_query(value: str) -> str:
    original = str(value or "")
    terms = _query_relevance_terms(original)
    selected: List[str] = []
    if CJK_RE.search(original):
        selected.extend(term for term in terms if CJK_RE.search(term))
        selected.extend(
            term
            for term in terms
            if not CJK_RE.search(term) and _contains_term(original.lower(), term)
        )
    else:
        selected.extend(term for term in terms if not CJK_RE.search(term))
    if "人工智能" in selected and "大模型" in selected:
        selected.remove("人工智能")
        selected.insert(0, "人工智能")
    intent = ""
    if re.search(r"(?:新闻|消息|热点|\bnews\b)", original, re.IGNORECASE):
        intent = "新闻" if CJK_RE.search(original) else "news"
    elif re.search(r"(?:政策|\bpolicy\b)", original, re.IGNORECASE):
        intent = "政策" if CJK_RE.search(original) else "policy"
    elif re.search(r"(?:天气|\bweather\b)", original, re.IGNORECASE):
        intent = "天气" if CJK_RE.search(original) else "weather"
    elif re.search(r"(?:价格|\bprice\b)", original, re.IGNORECASE):
        intent = "价格" if CJK_RE.search(original) else "price"
    elif re.search(r"(?:版本|发布|\bversion\b|\brelease\b)", original, re.IGNORECASE):
        intent = "版本" if CJK_RE.search(original) else "release"
    if intent and intent not in selected:
        selected.append(intent)
    compact = _clean_text(" ".join(selected[:6]), 400)
    if compact:
        return compact
    return _clean_text(TODAY_QUERY_RE.sub(" ", original), 400)


def _upstream_query(query: str) -> str:
    value = _clean_text(query, 500)
    if not value or not FRESHNESS_RE.search(value):
        return value
    explicit_date = _extract_full_date(value)
    if explicit_date is not None:
        subject, parsed_date = explicit_date
        return _clean_text("%s %s" % (subject, parsed_date.isoformat()), 500)
    current_date = _current_search_date()
    if TODAY_QUERY_RE.search(value):
        subject = _compact_today_query(value)
        return _clean_text("%s %s" % (subject, current_date.isoformat()), 500)
    if not YEAR_TOKEN_RE.search(value):
        return _clean_text("%s %d" % (value, current_date.year), 500)
    return value


def _query_relevance_terms(query: str) -> List[str]:
    original = str(query or "").lower()
    value = original
    value = re.sub(
        r"(?<!\d)\d{4}\s*(?:年|[-/.])\s*\d{1,2}\s*(?:月|[-/.])"
        r"\s*\d{1,2}\s*日?",
        " ",
        value,
    )
    value = re.sub(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
        r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
        r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}"
        r"(?:st|nd|rd|th)?(?:,|\s)+\s*\d{4}\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = YEAR_TOKEN_RE.sub(" ", value)
    value = ENGLISH_TEMPORAL_TERMS_RE.sub(" ", value)
    value = GENERIC_QUERY_TERMS_RE.sub(" ", value)
    terms: List[str] = []
    for term in re.findall(r"[a-z][a-z0-9.+#-]{1,40}", value):
        if term not in terms:
            terms.append(term)
    for term in re.findall(r"[\u3400-\u9fff]{2,16}", value):
        if term not in terms:
            terms.append(term)
    aliases = []
    if "人工智能" in original or re.search(r"\bai\b", original):
        aliases.extend(["人工智能", "artificial intelligence", "ai"])
    if "大模型" in original or "大型语言模型" in original or re.search(
        r"\bllms?\b",
        original,
    ):
        aliases.extend(
            [
                "大模型",
                "人工智能",
                "large language model",
                "llm",
                "artificial intelligence",
                "ai",
            ]
        )
    for term in aliases:
        if term not in terms:
            terms.append(term)
    return terms[:12]


def _host_matches(host: str, expected: str) -> bool:
    return host == expected or host.endswith("." + expected)


def _authoritative_host(host: str) -> bool:
    return any(
        _host_matches(host, expected)
        for expected in AUTHORITATIVE_HOST_SUFFIXES
    ) or host.endswith(".gov")


def _contains_term(value: str, term: str) -> bool:
    if not value or not term:
        return False
    if CJK_RE.search(term):
        return term in value
    return bool(
        re.search(
            r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])",
            value,
            re.IGNORECASE,
        )
    )


def _query_target_year_month(query: str) -> Optional[Tuple[int, int]]:
    current_date = _current_search_date()
    if TODAY_QUERY_RE.search(query):
        return current_date.year, current_date.month
    numeric = re.search(
        r"(?<!\d)((?:19|20)\d{2})\s*(?:年|[-/.])\s*(\d{1,2})"
        r"\s*(?:月|[-/.])?",
        query,
    )
    if numeric:
        month = int(numeric.group(2))
        if 1 <= month <= 12:
            return int(numeric.group(1)), month
    month_names = (
        "jan(?:uary)?",
        "feb(?:ruary)?",
        "mar(?:ch)?",
        "apr(?:il)?",
        "may",
        "jun(?:e)?",
        "jul(?:y)?",
        "aug(?:ust)?",
        "sep(?:t(?:ember)?)?",
        "oct(?:ober)?",
        "nov(?:ember)?",
        "dec(?:ember)?",
    )
    year_match = YEAR_TOKEN_RE.search(query)
    if year_match:
        for month, pattern in enumerate(month_names, start=1):
            if re.search(r"\b(?:%s)\b" % pattern, query, re.IGNORECASE):
                return int(year_match.group(0)), month
    return None


def _has_strict_freshness_evidence(
    query: str,
    title: str,
    description: str,
    published_at: str,
) -> bool:
    target = _query_target_year_month(query)
    if target is None:
        return True
    temporal_text = " ".join((title, description, published_at))
    if re.search(
        r"(?:\b\d+\s*(?:minutes?|hours?|days?)\s+ago\b|"
        r"\d+\s*(?:分钟|小时|天)前)",
        temporal_text,
        re.IGNORECASE,
    ):
        return True
    year, month = target
    if re.search(
        r"(?<!\d)%d\s*(?:年|[-/.])\s*0?%d(?:\s*(?:月|[-/.])|\b)"
        % (year, month),
        temporal_text,
    ):
        return True
    english_month = (
        "jan", "feb", "mar", "apr", "may", "jun",
        "jul", "aug", "sep", "oct", "nov", "dec",
    )[month - 1]
    return str(year) in temporal_text and bool(
        re.search(r"\b" + english_month + r"[a-z]*\b", temporal_text, re.IGNORECASE)
    )


def _query_target_date(query: str) -> Optional[date]:
    if TODAY_QUERY_RE.search(query):
        return _current_search_date()
    extracted = _extract_full_date(query)
    return extracted[1] if extracted is not None else None


def _result_publication_date(
    title: str,
    description: str,
    published_at: str,
) -> Optional[date]:
    if published_at:
        try:
            return parsedate_to_datetime(published_at).date()
        except (TypeError, ValueError):
            try:
                return datetime.fromisoformat(
                    published_at.replace("Z", "+00:00")
                ).date()
            except ValueError:
                pass
    for value in (title, description):
        extracted = _extract_full_date(value)
        if extracted is not None:
            return extracted[1]
    return None


def _freshness_result_score(
    query: str,
    title: str,
    description: str,
    published_at: str,
) -> int:
    target_date = _query_target_date(query)
    publication_date = _result_publication_date(
        title,
        description,
        published_at,
    )
    years = [int(value) for value in YEAR_TOKEN_RE.findall(query)]
    target_year = years[0] if years else _current_search_date().year
    title_years = [int(value) for value in YEAR_TOKEN_RE.findall(title)]
    description_years = [
        int(value)
        for value in YEAR_TOKEN_RE.findall(description + " " + published_at)
    ]
    score = 0
    if re.search(
        r"(?:\b\d+\s*(?:minutes?|hours?|days?)\s+ago\b|"
        r"\d+\s*(?:分钟|小时|天)前)",
        description,
        re.IGNORECASE,
    ):
        score += 5
    if target_year in title_years:
        score += 5
    elif title_years:
        distance = min(abs(value - target_year) for value in title_years)
        score -= min(14, 6 + (distance * 2))
    if target_year in description_years:
        score += 2
    elif description_years:
        distance = min(abs(value - target_year) for value in description_years)
        score -= min(8, 2 + (distance * 2))
    if target_date is not None and publication_date is not None:
        age_days = (target_date - publication_date).days
        if age_days < -1:
            score -= 6
        elif age_days <= 1:
            score += 10
        elif age_days <= 3:
            score += 8
        elif age_days <= 7:
            score += 4
        elif age_days <= 31:
            score += 1
        else:
            score -= min(12, 4 + (age_days // 30))
    return score


def _result_relevance_score(
    query: str,
    item: Dict[str, Any],
    terms: List[str],
) -> int:
    title = _clean_text(item.get("title"), 500).lower()
    description = _clean_text(
        item.get("description") or item.get("content"),
        2000,
    ).lower()
    published_at = _clean_text(item.get("published_at"), 200).lower()
    try:
        host = (urlsplit(str(item.get("url") or "")).hostname or "").lower()
    except ValueError:
        host = ""
    if FRESHNESS_RE.search(query) and any(
        _host_matches(host, suffix)
        for suffix in REFERENCE_FRESH_HOST_SUFFIXES
    ):
        return 0
    score = 0
    government_quality_match = bool(
        QUALITY_RANKING_RE.search(query)
        and re.search(r"(?:国务院|政府|政策|\bgovernment\b)", query, re.IGNORECASE)
        and (_host_matches(host, "gov.cn") or host.endswith(".gov"))
    )
    title_matches = set()
    host_matches = set()
    description_matches = set()
    for term in terms:
        if _contains_term(title, term):
            title_matches.add(term)
            score += 5
        if _contains_term(host, term):
            host_matches.add(term)
            score += 3
        if _contains_term(description, term):
            description_matches.add(term)
            score += 2
        if term in host.split("."):
            score += 3
    if not (
        title_matches
        or host_matches
        or description_matches
        or government_quality_match
    ):
        return 0
    if (
        FRESHNESS_RE.search(query)
        and not (title_matches or host_matches)
        and not government_quality_match
    ):
        return 0
    if FRESHNESS_RE.search(query) and not _has_strict_freshness_evidence(
        query,
        title,
        description,
        published_at,
    ):
        return 0
    if government_quality_match:
        score += 8

    if QUALITY_RANKING_RE.search(query):
        if _host_matches(host, "gov.cn") or host.endswith(".gov"):
            score += 6
        if any(term in host.split(".") for term in terms):
            score += 4
    if _authoritative_host(host):
        score += 12
    if FRESHNESS_RE.search(query):
        score += _freshness_result_score(
            query,
            title,
            description,
            published_at,
        )
        if any(
            _host_matches(host, suffix)
            for suffix in LOW_VALUE_FRESH_HOST_SUFFIXES
        ):
            score -= 8
        elif "blog" in host.split(".") and not _authoritative_host(host):
            score -= 4
    return score


def _rank_search_results(
    query: str,
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not (FRESHNESS_RE.search(query) or QUALITY_RANKING_RE.search(query)):
        return list(items)
    terms = _query_relevance_terms(query)
    if not terms:
        return list(items)
    scored = [
        (_result_relevance_score(query, item, terms), index, item)
        for index, item in enumerate(items)
    ]
    relevant = [entry for entry in scored if entry[0] > 0]
    if not relevant:
        return []
    relevant.sort(key=lambda entry: (-entry[0], entry[1]))
    if not FRESHNESS_RE.search(query):
        return [entry[2] for entry in relevant]
    diverse = []
    seen_hosts = set()
    for _score, _index, item in relevant:
        try:
            host = (urlsplit(str(item.get("url") or "")).hostname or "").lower()
        except ValueError:
            host = ""
        if host and host in seen_hosts:
            continue
        if host:
            seen_hosts.add(host)
        diverse.append(item)
    return diverse


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
        candidate_limit = min(40, max(10, safe_limit * 5))
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
                official_results = _official_entry_results(normalized_query)
                raw_results: List[Dict[str, Any]] = list(direct_results)
                trusted_results, trusted_errors = _trusted_feed_results(
                    normalized_query,
                    max_response,
                )
                raw_results.extend(trusted_results)
                upstream_errors.extend(trusted_errors)

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
                                    "count": candidate_limit,
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

                def normalize_candidate(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                    url = _public_result_url(raw.get("url"))
                    if not url:
                        return None
                    candidate = {
                        "title": _clean_text(raw.get("title"), 500),
                        "url": url,
                        "description": _clean_text(raw.get("content"), 2000),
                    }
                    published_at = _clean_text(
                        raw.get("published_at")
                        or raw.get("publishedDate")
                        or raw.get("published_date"),
                        200,
                    )
                    if published_at:
                        candidate["published_at"] = published_at
                    source_name = _clean_text(raw.get("source"), 80)
                    if source_name:
                        candidate["source"] = source_name
                    return candidate

                candidates: List[Dict[str, Any]] = []
                seen = set()
                for raw in raw_results:
                    if not isinstance(raw, dict):
                        continue
                    candidate = normalize_candidate(raw)
                    if candidate is None or candidate["url"] in seen:
                        continue
                    seen.add(candidate["url"])
                    candidates.append(candidate)
                    if len(candidates) >= candidate_limit:
                        break
                ranked = _rank_search_results(normalized_query, candidates)
                if not ranked and official_results:
                    fallback_candidates = [
                        candidate
                        for raw in official_results
                        if (candidate := normalize_candidate(raw)) is not None
                    ]
                    ranked = _rank_search_results(
                        normalized_query,
                        fallback_candidates,
                    )
                results = []
                for item in ranked[:safe_limit]:
                    results.append(
                        {
                            **item,
                            "position": len(results) + 1,
                        }
                    )
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

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sqlite3
import sys
import time
import types
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_PATH = ROOT / "hermes-plugin" / "provider.py"
HTTPX_CLIENT = httpx.Client
HTTPX_ASYNC_CLIENT = httpx.AsyncClient


class WebSearchProvider:
    pass


def _safe_url(url: str) -> bool:
    return not any(
        marker in str(url)
        for marker in ("127.0.0.1", "localhost", "169.254.169.254", "10.0.0.1")
    )


async def _async_safe_url(url: str) -> bool:
    return _safe_url(url)


def _redirect_target(response):
    if not response.is_redirect:
        return None
    location = response.headers.get("location")
    return urljoin(str(response.url), location) if location else None


def _load_provider_module():
    agent = types.ModuleType("agent")
    web_search_provider = types.ModuleType("agent.web_search_provider")
    web_search_provider.WebSearchProvider = WebSearchProvider
    tools = types.ModuleType("tools")
    url_safety = types.ModuleType("tools.url_safety")
    url_safety.async_is_safe_url = _async_safe_url
    url_safety.is_safe_url = _safe_url
    url_safety.normalize_url_for_request = lambda value: str(value).strip()
    url_safety.redirect_target_from_response = _redirect_target
    website_policy = types.ModuleType("tools.website_policy")
    website_policy.check_website_access = lambda _url: None

    replacements = {
        "agent": agent,
        "agent.web_search_provider": web_search_provider,
        "tools": tools,
        "tools.url_safety": url_safety,
        "tools.website_policy": website_policy,
    }
    previous = {name: sys.modules.get(name) for name in replacements}
    sys.modules.update(replacements)
    try:
        spec = importlib.util.spec_from_file_location("wechat_cloud_provider_test", PROVIDER_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture()
def provider_module(monkeypatch):
    module = _load_provider_module()
    provider = module.WechatCloudWebProvider
    provider._cache.clear()
    provider._initialized_cache_paths.clear()
    provider._consecutive_failures = 0
    provider._circuit_open_until = 0.0
    provider._source_failures.clear()
    provider._source_open_until.clear()
    module._trusted_feed_cache.clear()
    monkeypatch.setenv("WECHAT_WEB_SEARCH_URL", "http://127.0.0.1:8651")
    monkeypatch.setenv("WECHAT_WEB_BING_URL", "https://www.bing.com/search")
    monkeypatch.setenv(
        "WECHAT_WEB_BING_NEWS_URL", "https://global.bing.com/news/search"
    )
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_BING_RSS_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_BING_NEWS_RSS_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_TRUSTED_FEEDS_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_SEARX_MERGE_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_DOMESTIC_FALLBACK_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_DOMESTIC_MERGE_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_SEARCH_CACHE_DB", "disabled")
    monkeypatch.setenv("WECHAT_WEB_SEARCH_ATTEMPTS", "1")
    monkeypatch.setenv("WECHAT_WEB_EXTRACT_ATTEMPTS", "1")
    return module


def _sync_client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return HTTPX_CLIENT(*args, **kwargs)

    return factory


def _async_client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return HTTPX_ASYNC_CLIENT(*args, **kwargs)

    return factory


def test_provider_requires_loopback_search_url(provider_module, monkeypatch):
    provider = provider_module.WechatCloudWebProvider()
    assert provider.is_available()
    monkeypatch.setenv("WECHAT_WEB_SEARCH_URL", "https://search.example.com")
    assert not provider.is_available()


def test_search_filters_private_and_duplicate_results_and_caches(
    provider_module, monkeypatch
):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {"title": " Current facts one ", "url": "https://one.example/a#fragment", "content": " A result "},
                    {"title": "Duplicate", "url": "https://one.example/a", "content": "duplicate"},
                    {"title": "Private", "url": "http://127.0.0.1/admin", "content": "no"},
                    {"title": "Current facts two", "url": "https://two.example/", "content": "second"},
                ]
            },
        )

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    provider = provider_module.WechatCloudWebProvider()
    first = provider.search("  current   facts  ", limit=5)
    second = provider.search("current facts", limit=5)

    assert first == second
    assert calls == 1
    assert first["success"] is True
    assert [item["url"] for item in first["data"]["web"]] == [
        "https://one.example/a",
        "https://two.example/",
    ]
    assert first["data"]["web"][0]["title"] == "Current facts one"


def test_persistent_cache_survives_memory_reset_without_storing_query(
    provider_module, monkeypatch, tmp_path
):
    cache_path = tmp_path / "search-cache.sqlite3"
    monkeypatch.setenv("WECHAT_WEB_SEARCH_CACHE_DB", str(cache_path))
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {
                        "title": "Persistent result",
                        "url": "https://persisted.example/",
                        "content": "cached",
                    }
                ]
            },
        )

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    provider = provider_module.WechatCloudWebProvider()
    query = "private project lookup phrase"
    first = provider.search(query, 1)
    provider_module.WechatCloudWebProvider._cache.clear()
    second = provider.search(query, 1)

    assert first == second
    assert calls == 1
    with sqlite3.connect(cache_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0] == 1
    cache_files = list(tmp_path.glob("search-cache.sqlite3*"))
    assert all(query.encode() not in path.read_bytes() for path in cache_files)


def test_stale_persistent_cache_is_used_only_after_upstream_failure(
    provider_module, monkeypatch, tmp_path
):
    cache_path = tmp_path / "search-cache.sqlite3"
    monkeypatch.setenv("WECHAT_WEB_SEARCH_CACHE_DB", str(cache_path))
    healthy = True
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if healthy:
            return httpx.Response(
                200,
                request=request,
                json={
                    "results": [
                        {
                            "title": "Known result",
                            "url": "https://known.example/",
                            "content": "known",
                        }
                    ]
                },
            )
        return httpx.Response(503, request=request)

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    provider = provider_module.WechatCloudWebProvider()
    first = provider.search("resilient lookup", 1)
    provider_module.WechatCloudWebProvider._cache.clear()
    with sqlite3.connect(cache_path) as connection:
        connection.execute(
            "UPDATE search_cache SET fresh_until = ?, stale_until = ?",
            (0, time.time() + 3600),
        )
    healthy = False
    second = provider.search("resilient lookup", 1)

    assert second == first
    assert calls == 2


def test_expired_persistent_cache_is_not_returned(
    provider_module, monkeypatch, tmp_path
):
    cache_path = tmp_path / "search-cache.sqlite3"
    monkeypatch.setenv("WECHAT_WEB_SEARCH_CACHE_DB", str(cache_path))
    healthy = True

    def handler(request):
        if healthy:
            return httpx.Response(
                200,
                request=request,
                json={
                    "results": [
                        {
                            "title": "Expired result",
                            "url": "https://expired.example/",
                            "content": "expired",
                        }
                    ]
                },
            )
        return httpx.Response(503, request=request)

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    provider = provider_module.WechatCloudWebProvider()
    assert provider.search("expired lookup", 1)["success"] is True
    provider_module.WechatCloudWebProvider._cache.clear()
    with sqlite3.connect(cache_path) as connection:
        connection.execute(
            "UPDATE search_cache SET fresh_until = 0, stale_until = 0"
        )
    healthy = False

    assert provider.search("expired lookup", 1)["success"] is False


def test_bing_rss_is_primary_and_does_not_call_searx(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_BING_RSS_ENABLED", "true")
    calls = []
    items = "".join(
        "<item><title>Result {0}</title><link>https://r{0}.example/</link>"
        "<description>Useful &lt;b&gt;description&lt;/b&gt; {0}</description></item>".format(i)
        for i in range(1, 4)
    )
    rss = ("<?xml version='1.0' encoding='utf-8'?><rss><channel>" + items + "</channel></rss>").encode()

    def handler(request):
        calls.append(str(request.url))
        assert request.url.host == "www.bing.com"
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/rss+xml"},
            content=rss,
        )

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search("rss", 3)

    assert result["success"] is True
    assert len(calls) == 1
    assert result["data"]["web"][0]["description"] == "Useful description 1"


def test_trusted_feed_parser_supports_rss_and_atom(provider_module):
    rss_request = httpx.Request(
        "GET",
        "https://techcrunch.com/feed/",
    )
    rss = httpx.Response(
        200,
        request=rss_request,
        headers={"content-type": "application/rss+xml"},
        content=b"""<?xml version="1.0"?><rss><channel><item>
        <title>AI launch</title><link>https://publisher.example/ai</link>
        <description>Official &lt;b&gt;model&lt;/b&gt; news</description>
        <pubDate>Mon, 10 Aug 2026 08:00:00 GMT</pubDate>
        </item></channel></rss>""",
    )
    atom_request = httpx.Request(
        "GET",
        "https://www.theverge.com/rss/index.xml",
    )
    atom = httpx.Response(
        200,
        request=atom_request,
        headers={"content-type": "application/atom+xml"},
        content=b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>AI policy &amp;#8217; update</title><link href="https://publisher.example/policy" />
        <summary>Policy details</summary><updated>2026-08-10T09:00:00Z</updated></entry>
        </feed>""",
    )

    rss_rows = provider_module._trusted_feed_results_from_response(
        rss,
        "techcrunch",
        100_000,
    )
    atom_rows = provider_module._trusted_feed_results_from_response(
        atom,
        "the-verge",
        100_000,
    )

    assert rss_rows[0]["content"] == "Official model news"
    assert rss_rows[0]["published_at"].startswith("Mon, 10 Aug 2026")
    assert atom_rows[0]["url"] == "https://publisher.example/policy"
    assert atom_rows[0]["title"] == "AI policy \u2019 update"
    assert atom_rows[0]["source"] == "the-verge"


def test_freshness_search_merges_relevant_trusted_feed(
    provider_module,
    monkeypatch,
):
    monkeypatch.setenv("WECHAT_WEB_TRUSTED_FEEDS_ENABLED", "true")
    monkeypatch.setattr(
        provider_module,
        "TRUSTED_FEED_ENDPOINTS",
        (("the-verge", "https://www.theverge.com/rss/index.xml"),),
    )
    calls = []
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
    <entry><title>AI model launch</title><link href="https://publisher.example/ai" />
    <summary>Artificial intelligence news</summary>
    <updated>2026-08-10T09:00:00Z</updated></entry></feed>"""

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/atom+xml"},
            content=atom,
        )

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search(
        "AI news 2026-08-11",
        1,
    )

    assert result["success"] is True
    assert calls == ["www.theverge.com"]
    assert result["data"]["web"][0]["url"] == "https://publisher.example/ai"
    assert result["data"]["web"][0]["source"] == "the-verge"


def test_domestic_trusted_feeds_are_authoritative_regional_sources(
    provider_module,
):
    endpoints = dict(provider_module.TRUSTED_FEED_ENDPOINTS)
    assert endpoints["leiphone"] == "https://www.leiphone.com/feed"
    assert endpoints["qbitai"] == "https://www.qbitai.com/feed"
    assert endpoints["infoq-cn"] == "https://www.infoq.cn/feed"

    for source, url in (
        ("leiphone", "https://www.leiphone.com/category/ai/1.html"),
        ("qbitai", "https://www.qbitai.com/2026/08/1.html"),
        ("infoq-cn", "https://www.infoq.cn/article/1"),
    ):
        item = {"source": source, "url": url}
        host = urlsplit(url).hostname or ""
        assert provider_module._result_source_type(item, host) == "authoritative"
        assert provider_module._result_region(item) == "domestic"


def test_bing_html_is_primary_and_parses_result_cards(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_BING_RSS_ENABLED", "true")
    calls = []
    html = b"""
    <html><body><ol>
      <li class="b_algo"><h2><a href="https://official.example/docs">Official docs</a></h2>
        <div class="b_caption"><p>Primary documentation source.</p></div></li>
      <li class="b_algo"><h2><a href="https://second.example/">Second result</a></h2>
        <div class="b_caption"><p>Secondary source.</p></div></li>
    </ol></body></html>
    """

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            content=html,
        )

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search("example docs", 2)

    assert result["success"] is True
    assert len(calls) == 1
    assert result["data"]["web"][0] == {
        "title": "Official docs",
        "url": "https://official.example/docs",
        "description": "Primary documentation source.",
        "position": 1,
        "source_type": "web",
        "region": "international",
        "evidence_level": "search_metadata_only",
    }


def test_freshness_search_prioritizes_bing_news_sources(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_BING_NEWS_RSS_ENABLED", "true")
    html = b"""
      <li class="b_algo"><h2><a href="https://web-one.example/">AI web one</a></h2></li>
      <li class="b_algo"><h2><a href="https://web-two.example/">AI web two</a></h2></li>
    """
    rss = b"""<?xml version="1.0"?><rss><channel>
    <item><title>AI news one</title><link>http://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fnews-one.example%2Fstory</link><description>One</description></item>
    <item><title>AI news two</title><link>http://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fnews-two.example%2Fstory</link><description>Two</description></item>
    </channel></rss>"""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/news/search":
            return httpx.Response(200, request=request, content=rss)
        return httpx.Response(200, request=request, content=html)

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search("latest AI news", 4)

    assert result["success"] is True
    assert calls == ["/search", "/news/search"]
    assert [item["url"] for item in result["data"]["web"]] == [
        "https://news-one.example/story",
        "https://news-two.example/story",
        "https://web-one.example/",
        "https://web-two.example/",
    ]


def test_freshness_search_keeps_news_ahead_of_regional_results(
    provider_module,
    monkeypatch,
):
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_BING_NEWS_RSS_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_SEARX_MERGE_ENABLED", "true")
    html = b"".join(
        (
            '<li class="b_algo"><h2><a href="https://web-%d.example/">'
            "AI web %d</a></h2></li>" % (index, index)
        ).encode()
        for index in range(1, 5)
    )
    rss = (
        '<?xml version="1.0"?><rss><channel>'
        + "".join(
            "<item><title>AI news %d</title><link>"
            "http://www.bing.com/news/apiclick.aspx?url="
            "https%%3A%%2F%%2Fnews-%d.example%%2Fstory"
            "</link><description>Relevant news</description></item>" % (index, index)
            for index in range(1, 5)
        )
        + "</channel></rss>"
    ).encode()

    def handler(request):
        if request.url.host == "127.0.0.1":
            return httpx.Response(
                200,
                request=request,
                json={
                    "results": [
                        {
                            "title": "AI regional %d" % index,
                            "url": "https://regional-%d.example/" % index,
                            "content": "Regional result",
                        }
                        for index in range(1, 3)
                    ]
                },
            )
        if request.url.path == "/news/search":
            return httpx.Response(200, request=request, content=rss)
        return httpx.Response(200, request=request, content=html)

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search("latest AI news", 6)

    assert [item["url"] for item in result["data"]["web"]] == [
        "https://news-1.example/story",
        "https://news-2.example/story",
        "https://news-3.example/story",
        "https://news-4.example/story",
        "https://regional-1.example/",
        "https://web-1.example/",
    ]


def test_bing_rss_failure_falls_back_to_searx(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_BING_RSS_ENABLED", "true")
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "www.bing.com":
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {"title": "fallback", "url": "https://fallback.example/", "content": "ok"}
                ]
            },
        )

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search("fallback", 1)

    assert result["success"] is True
    assert calls == ["www.bing.com", "127.0.0.1"]


def test_bing_rss_rejects_redirect_outside_allowlist(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_BING_RSS_ENABLED", "true")
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "www.bing.com":
            return httpx.Response(
                302,
                request=request,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {"title": "safe", "url": "https://safe.example/", "content": "ok"}
                ]
            },
        )

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search("redirect", 1)

    assert result["success"] is True
    assert calls == ["www.bing.com", "127.0.0.1"]


def test_bing_endpoint_configuration_is_strictly_allowlisted(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_BING_URL", "http://global.bing.com/search")
    result = provider_module.WechatCloudWebProvider().search("unsafe endpoint", 1)
    assert result["success"] is False
    assert "trusted Bing endpoints" in result["error"]


def test_explicit_public_domain_is_first_result_without_guessing(
    provider_module, monkeypatch
):
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_BING_RSS_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_SEARX_MERGE_ENABLED", "false")
    result = provider_module.WechatCloudWebProvider().search(
        "Read platform.openai.com/docs before answering", 1
    )
    assert result["success"] is True
    assert result["data"]["web"][0]["url"] == "https://platform.openai.com/docs"


def test_known_official_entry_does_not_depend_on_search_upstreams(
    provider_module,
    monkeypatch,
):
    monkeypatch.setenv("WECHAT_WEB_SEARCH_URL", "http://127.0.0.1:9")
    result = provider_module.WechatCloudWebProvider().search(
        "OpenAI official documentation",
        1,
    )

    assert result["success"] is True
    assert result["data"]["web"][0]["url"] == "https://platform.openai.com/docs/"
    assert result["data"]["web"][0]["source"] == "official-entry"


def test_searx_results_are_interleaved_with_bing(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_BING_RSS_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_SEARX_MERGE_ENABLED", "true")
    html = b"""
    <li class="b_algo"><h2><a href="https://bing-one.example/">B1</a></h2></li>
    <li class="b_algo"><h2><a href="https://bing-two.example/">B2</a></h2></li>
    """

    def handler(request):
        if request.url.host == "www.bing.com":
            return httpx.Response(200, request=request, content=html)
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {"title": "S1", "url": "https://searx-one.example/", "content": ""},
                    {"title": "S2", "url": "https://searx-two.example/", "content": ""},
                ]
            },
        )

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search("混合结果", 4)
    assert [item["url"] for item in result["data"]["web"]] == [
        "https://bing-one.example/",
        "https://searx-one.example/",
        "https://bing-two.example/",
        "https://searx-two.example/",
    ]


def test_strict_query_supplements_irrelevant_bing_volume_with_searx(
    provider_module,
    monkeypatch,
):
    monkeypatch.setattr(
        provider_module,
        "_current_search_date",
        lambda: date(2026, 8, 12),
    )
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_BING_RSS_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_SEARX_MERGE_ENABLED", "false")
    bing_html = b"".join(
        (
            '<li class="b_algo"><h2><a href="https://noise-%d.example/">'
            "Unrelated flight result %d</a></h2></li>" % (index, index)
        ).encode()
        for index in range(8)
    )
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "www.bing.com":
            return httpx.Response(200, request=request, content=bing_html)
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {
                        "title": "Artificial intelligence news update",
                        "url": "https://global-ai.example/news",
                        "content": "Artificial intelligence industry news",
                        "publishedDate": "Wed, 12 Aug 2026 08:00:00 GMT",
                    },
                    {
                        "title": "AI research news briefing",
                        "url": "https://ai-research.example/briefing",
                        "content": "Recent artificial intelligence research news",
                        "publishedDate": "Wed, 12 Aug 2026 07:00:00 GMT",
                    },
                ]
            },
        )

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search(
        "today artificial intelligence news",
        2,
    )

    assert result["success"] is True
    assert calls == ["www.bing.com", "127.0.0.1"]
    assert [item["url"] for item in result["data"]["web"]] == [
        "https://global-ai.example/news",
        "https://ai-research.example/briefing",
    ]


def test_sogou_mobile_parser_uses_embedded_public_target(provider_module):
    request = httpx.Request("GET", "https://m.sogou.com/web/searchList.jsp")
    response = httpx.Response(
        200,
        request=request,
        headers={"content-type": "text/html; charset=utf-8"},
        content=(
            '<h3><a class="resultLink" '
            'href="./tc?url=https%3A%2F%2Fcloud.tencent.com%2Fdocument%2Fproduct%2F1&amp;linkid=0">'
            "腾讯云<em>官方文档</em></a></h3>"
        ).encode(),
    )
    rows = provider_module._sogou_mobile_results(response)
    assert rows == [
        {
            "title": "腾讯云 官方文档",
            "url": "https://cloud.tencent.com/document/product/1",
            "content": "",
        }
    ]


def test_baidu_mobile_parser_reads_card_metadata(provider_module):
    request = httpx.Request("GET", "https://m.baidu.com/s")
    response = httpx.Response(
        200,
        request=request,
        headers={"content-type": "text/html; charset=utf-8"},
        content=(
            '<div class="c-result result" '
            "data-log='{" + '"mu":"https://www.gov.cn/zhengce/"' + "}'>"
            "<div><h3>中国政府网 <em>政策</em></h3></div></div>"
        ).encode(),
    )
    rows = provider_module._baidu_mobile_results(response)
    assert rows == [
        {
            "title": "中国政府网 政策",
            "url": "https://www.gov.cn/zhengce/",
            "content": "",
        }
    ]


def test_360_mobile_parser_reads_explicit_public_target(provider_module):
    request = httpx.Request("GET", "https://m.so.com/s")
    response = httpx.Response(
        200,
        request=request,
        headers={"content-type": "text/html; charset=utf-8"},
        content=(
            '<div class="g-card res-list og" '
            'data-pcurl="https://cloud.tencent.com/document/product/1">'
            '<a class="alink"><h3 class="res-title">腾讯云 <em>官方文档</em></h3></a>'
            '<div class="res-con">Description</div></div>'
        ).encode(),
    )
    rows = provider_module._so_mobile_results(response)
    assert rows == [
        {
            "title": "腾讯云 官方文档",
            "url": "https://cloud.tencent.com/document/product/1",
            "content": "",
        }
    ]


def test_chinese_search_falls_back_to_direct_sogou(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_SEARX_MERGE_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_DOMESTIC_FALLBACK_ENABLED", "true")
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "127.0.0.1":
            return httpx.Response(200, request=request, json={"results": []})
        if request.url.host == "m.sogou.com":
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html; charset=utf-8"},
                content=(
                    '<a class="resultLink" '
                    'href="./tc?url=https%3A%2F%2Fcloud.tencent.com%2F">'
                    "腾讯云官方文档</a>"
                ).encode(),
            )
        raise AssertionError("unexpected upstream: %s" % request.url.host)

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search("腾讯云 产品说明", 1)

    assert result["success"] is True
    assert result["data"]["web"][0]["url"] == "https://cloud.tencent.com/"
    assert calls == ["127.0.0.1", "m.sogou.com"]


def test_chinese_dual_region_search_merges_global_and_domestic_sources(
    provider_module,
    monkeypatch,
):
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_DOMESTIC_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_DOMESTIC_MERGE_ENABLED", "true")
    calls = []
    bing_html = b"""
    <li class="b_algo"><h2><a href="https://openai.com/news/research/">AI research report</a></h2>
      <div class="b_caption"><p>International large language model research.</p></div></li>
    <li class="b_algo"><h2><a href="https://www.reuters.com/technology/ai/">AI industry report</a></h2>
      <div class="b_caption"><p>Global large language model industry reporting.</p></div></li>
    """
    domestic_html = (
        '<a class="resultLink" href="./tc?url=https%3A%2F%2Fwww.gov.cn%2Fai">'
        "国内大模型行业报告</a>"
        '<a class="resultLink" href="./tc?url=https%3A%2F%2Fwww.xinhuanet.com%2Fai">'
        "中国人工智能行业动态</a>"
        '<a class="resultLink" href="./tc?url=https%3A%2F%2Fwww.people.com.cn%2Fai">'
        "国内人工智能研究报告</a>"
        '<a class="resultLink" href="./tc?url=https%3A%2F%2Fwww.cctv.com%2Fai">'
        "中国大模型产业观察</a>"
    ).encode()

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "www.bing.com":
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html"},
                content=bing_html,
            )
        if request.url.host == "127.0.0.1":
            return httpx.Response(200, request=request, json={"results": []})
        if request.url.host == "m.sogou.com":
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html; charset=utf-8"},
                content=domestic_html,
            )
        raise AssertionError("unexpected upstream: %s" % request.url.host)

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search(
        "国内外大模型行业报告",
        4,
    )

    assert result["success"] is True
    hosts = [urlsplit(item["url"]).hostname or "" for item in result["data"]["web"]]
    assert calls == ["www.bing.com", "127.0.0.1", "m.sogou.com"]
    assert any(host.endswith(".cn") for host in hosts[:2])
    assert any(not host.endswith(".cn") for host in hosts[:2])


def test_domestic_challenge_falls_back_to_360(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_SEARX_MERGE_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_DOMESTIC_FALLBACK_ENABLED", "true")
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "127.0.0.1":
            return httpx.Response(200, request=request, json={"results": []})
        if request.url.host == "m.sogou.com":
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html"},
                content="请输入验证码".encode(),
            )
        if request.url.host == "m.so.com":
            body = (
                '<div class="g-card res-list" data-pcurl="https://www.gov.cn/">'
                '<h3 class="res-title">中国政府网</h3></div>'
            )
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html; charset=utf-8"},
                content=body.encode(),
            )
        raise AssertionError("unexpected upstream: %s" % request.url.host)

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search("国务院 部门动态", 1)

    assert result["success"] is True
    assert result["data"]["web"][0]["url"] == "https://www.gov.cn/"
    assert calls == ["127.0.0.1", "m.sogou.com", "m.so.com"]


def test_domestic_challenges_fall_back_to_baidu(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_SEARX_MERGE_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_DOMESTIC_FALLBACK_ENABLED", "true")
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "127.0.0.1":
            return httpx.Response(200, request=request, json={"results": []})
        if request.url.host == "m.sogou.com":
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html"},
                content="请输入验证码".encode(),
            )
        if request.url.host == "m.so.com":
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html"},
                content="访问过于频繁，请进行安全验证".encode(),
            )
        if request.url.host == "m.baidu.com":
            body = (
                '<div class="c-result result" '
                "data-log='{" + '"mu":"https://www.gov.cn/"' + "}'>"
                "<div><h3>中国政府网</h3></div></div>"
            )
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html; charset=utf-8"},
                content=body.encode(),
            )
        raise AssertionError("unexpected upstream: %s" % request.url.host)

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search("国务院 部门动态", 1)

    assert result["success"] is True
    assert result["data"]["web"][0]["url"] == "https://www.gov.cn/"
    assert calls == ["127.0.0.1", "m.sogou.com", "m.so.com", "m.baidu.com"]


def test_domestic_source_circuit_does_not_block_healthy_fallback(
    provider_module, monkeypatch
):
    monkeypatch.setenv("WECHAT_WEB_SEARX_MERGE_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_DOMESTIC_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_SOURCE_CIRCUIT_FAILURES", "1")
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "127.0.0.1":
            return httpx.Response(200, request=request, json={"results": []})
        if request.url.host == "m.sogou.com":
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html"},
                content="安全验证".encode(),
            )
        if request.url.host == "m.so.com":
            body = (
                '<div class="g-card res-list" '
                'data-pcurl="https://healthy.example/">'
                '<h3 class="res-title">Healthy fallback</h3></div>'
            )
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html"},
                content=body.encode(),
            )
        raise AssertionError("unexpected upstream: %s" % request.url.host)

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    provider = provider_module.WechatCloudWebProvider()
    assert provider.search("国内搜索一", 1)["success"] is True
    assert provider.search("国内搜索二", 1)["success"] is True

    assert calls.count("m.sogou.com") == 1
    assert calls.count("m.so.com") == 2


def test_bing_market_params_follow_query_language(provider_module):
    assert provider_module._bing_market_params("latest AI news") == {
        "setlang": "en-US",
        "cc": "US",
        "mkt": "en-US",
        "adlt": "strict",
    }
    assert provider_module._bing_market_params("人工智能新闻") == {
        "setlang": "zh-Hans",
        "cc": "CN",
        "mkt": "zh-CN",
        "adlt": "strict",
    }


def test_english_merge_keeps_two_global_results_per_domestic_result(provider_module):
    primary = [{"url": "p%d" % index} for index in range(4)]
    secondary = [{"url": "s%d" % index} for index in range(2)]
    merged = provider_module._interleave_results(primary, secondary, primary_weight=2)
    assert [item["url"] for item in merged] == ["p0", "p1", "s0", "p2", "p3", "s1"]


def test_freshness_query_preserves_dates_and_expands_relative_time(
    provider_module,
    monkeypatch,
):
    monkeypatch.setattr(
        provider_module,
        "_current_search_date",
        lambda: provider_module.date(2026, 8, 11),
    )
    year = str(provider_module.time.gmtime().tm_year)
    assert provider_module._upstream_query(year + " AI latest news") == (
        year + " AI latest news"
    )
    assert provider_module._upstream_query(year + " 人工智能最新新闻") == (
        year + " 人工智能最新新闻"
    )
    assert provider_module._upstream_query(
        year + "年8月11日 人工智能 最新新闻"
    ) == ("人工智能 最新新闻 " + year + "-08-11")
    assert provider_module._upstream_query(year + " annual report") == year + " annual report"
    assert provider_module._upstream_query("2024 AI latest news") == "2024 AI latest news"
    assert provider_module._upstream_query("今天国内外大模型重要消息") == (
        "人工智能 大模型 新闻 2026-08-11"
    )
    assert provider_module._upstream_query("AI latest news") == (
        "AI latest news 2026"
    )
    assert provider_module._upstream_query(
        "August 11, 2026 artificial intelligence latest news"
    ) == "artificial intelligence latest news 2026-08-11"
    assert provider_module.FRESHNESS_RE.search(
        "artificial intelligence 2026-08-11"
    )
    assert provider_module.FRESHNESS_RE.search(
        "人工智能 2026年8月11日"
    )


def test_domestic_freshness_query_relaxes_exact_day_and_duplicate_alias(
    provider_module,
    monkeypatch,
):
    monkeypatch.setattr(
        provider_module,
        "_current_search_date",
        lambda: provider_module.date(2026, 8, 12),
    )

    assert provider_module._domestic_search_query(
        "人工智能 AI 2026年8月12日"
    ) == "人工智能 最新消息 2026年8月"
    assert provider_module._domestic_search_query(
        "中国 AI 人工智能 2026年8月12日 发布"
    ) == "中国 人工智能 发布 最新消息 2026年8月"
    assert provider_module._domestic_search_query(
        "今天国内外大模型重要消息"
    ) == "国内 人工智能 大模型 新闻 2026年8月"


def test_query_terms_exclude_english_date_and_intent_words(provider_module):
    terms = provider_module._query_relevance_terms(
        "August 11, 2026 global artificial intelligence latest news"
    )

    assert "august" not in terms
    assert "global" not in terms
    assert "latest" not in terms
    assert "artificial" in terms
    assert "intelligence" in terms
    global_terms = provider_module._query_relevance_terms("全球化最新趋势")
    assert "全球化" in global_terms
    assert "趋势" in global_terms
    how_to_terms = provider_module._query_relevance_terms(
        "How to configure Python free-threaded build with official documentation"
    )
    assert "how" not in how_to_terms
    assert "to" not in how_to_terms
    assert "with" not in how_to_terms
    assert "python" in how_to_terms
    assert "free-threaded" in how_to_terms


@pytest.mark.parametrize(
    "case",
    json.loads(
        (ROOT / "tests" / "fixtures" / "search_quality_cases.json").read_text(
            encoding="utf-8"
        )
    ),
    ids=lambda case: case["id"],
)
def test_search_quality_regression_corpus(provider_module, monkeypatch, case):
    monkeypatch.setattr(
        provider_module,
        "_current_search_date",
        lambda: provider_module.date(2026, 8, 12),
    )

    ranked = provider_module._rank_search_results(case["query"], case["results"])

    assert ranked
    host = urlsplit(ranked[0]["url"]).hostname or ""
    assert host == case["expected_top_host"] or host.endswith(
        "." + case["expected_top_host"]
    )


def test_query_planning_strips_chat_filler_without_losing_subject(provider_module):
    assert provider_module._upstream_query(
        "帮我联网搜一下 荣耀 Magic V5 和 vivo X Fold5 续航对比，并给出来源"
    ) == "荣耀 Magic V5 和 vivo X Fold5 续航对比"
    assert provider_module._upstream_query("研究生报名条件") == "研究生报名条件"


def test_query_intents_cover_decision_and_verification_shapes(provider_module):
    assert provider_module._query_intents("A 和 B 参数对比") == {
        "comparison"
    }
    assert provider_module._query_intents("核实这个消息是不是真的") == {
        "fact_check"
    }
    assert provider_module._query_intents("最新手机推荐") == {
        "freshness",
        "recommendation",
    }
    assert provider_module._query_intents("人工智能行业报告") == {
        "analysis"
    }


def test_chinese_product_query_extracts_entities_and_topic(provider_module):
    terms = provider_module._query_relevance_terms(
        "荣耀 Magic V5 和 vivo X Fold5 续航对比"
    )

    assert "荣耀" in terms
    assert "magic" in terms
    assert "vivo" in terms
    assert "fold5" in terms
    assert "续航" in terms
    assert "对比" not in terms


def test_general_comparison_ranking_uses_topic_coverage(provider_module):
    ranked = provider_module._rank_search_results(
        "荣耀 Magic V5 和 vivo X Fold5 续航对比",
        [
            {
                "title": "荣耀 Magic V5 壁纸下载",
                "url": "https://wallpaper.example/honor",
                "description": "手机壁纸合集",
            },
            {
                "title": "荣耀 Magic V5 与 vivo X Fold5 续航实测对比",
                "url": "https://review.example/foldable-battery",
                "description": "两款折叠屏的电池、充电和重载续航数据。",
            },
            {
                "title": "vivo X Fold5 参数",
                "url": "https://www.vivo.com.cn/x-fold5",
                "description": "官方产品参数。",
            },
        ],
    )

    assert ranked[0]["url"] == "https://review.example/foldable-battery"


def test_fact_check_ranking_prefers_authoritative_evidence(provider_module):
    ranked = provider_module._rank_search_results(
        "核实 OpenAI 已发布新模型这个消息是否属实",
        [
            {
                "title": "网友称 OpenAI 已发布新模型",
                "url": "https://zhidao.baidu.com/question/1",
                "description": "未经证实的讨论。",
            },
            {
                "title": "OpenAI announces a new model",
                "url": "https://openai.com/index/new-model/",
                "description": "Official announcement from OpenAI.",
            },
            {
                "title": "OpenAI releases new model",
                "url": "https://www.reuters.com/technology/openai-model/",
                "description": "Independent reporting on the announcement.",
            },
        ],
    )

    assert [item["url"] for item in ranked[:2]] == [
        "https://openai.com/index/new-model/",
        "https://www.reuters.com/technology/openai-model/",
    ]


def test_general_ranking_diversifies_hosts_before_overflow(provider_module):
    ranked = provider_module._rank_search_results(
        "Python typing documentation",
        [
            {
                "title": "Python typing documentation one",
                "url": "https://docs.python.org/3/library/typing.html",
                "description": "Python typing docs",
            },
            {
                "title": "Python typing documentation two",
                "url": "https://docs.python.org/3/reference/compound_stmts.html",
                "description": "Python typing reference",
            },
            {
                "title": "Python typing documentation three",
                "url": "https://docs.python.org/3/whatsnew/3.14.html",
                "description": "Python typing updates",
            },
            {
                "title": "Python typing specification",
                "url": "https://typing.python.org/",
                "description": "Python typing specification",
            },
        ],
    )

    assert "https://typing.python.org/" in [item["url"] for item in ranked[:3]]
    assert ranked[-1]["url"] == "https://docs.python.org/3/whatsnew/3.14.html"


def test_tracking_parameters_do_not_create_duplicate_results(provider_module):
    first = provider_module._canonical_result_key(
        "https://example.com/article?utm_source=feed&id=7#top"
    )
    second = provider_module._canonical_result_key(
        "https://EXAMPLE.com/article?id=7&utm_medium=social"
    )

    assert first == second


def test_dual_region_query_balances_domestic_and_international_results(
    provider_module,
):
    ranked = provider_module._balance_dual_region_results(
        "国内外人工智能行业动态",
        [
            {"url": "https://openai.com/news/one"},
            {"url": "https://www.reuters.com/technology/two"},
            {"url": "https://www.gov.cn/zhengce/three"},
            {"url": "https://www.xinhuanet.com/four"},
        ],
    )

    assert [provider_module._result_region(item) for item in ranked] == [
        "international",
        "domestic",
        "international",
        "domestic",
    ]


def test_domestic_region_recognizes_major_publishers_without_cn_tld(
    provider_module,
):
    for url in (
        "https://www.sohu.com/a/1",
        "https://www.ifeng.com/c/1",
        "https://36kr.com/p/1",
        "https://www.leiphone.com/category/ai/1",
        "https://www.qbitai.com/2026/08/1.html",
        "https://www.infoq.cn/article/1",
    ):
        assert provider_module._result_region({"url": url}) == "domestic"


def test_chinese_freshness_keeps_both_regions_in_the_first_two_results(
    provider_module,
):
    ranked = provider_module._rank_search_results(
        "2026年8月12日 人工智能 最新新闻",
        [
            {
                "title": "Artificial intelligence model update",
                "url": "https://www.reuters.com/technology/ai/",
                "description": "2026年8月12日 artificial intelligence news",
            },
            {
                "title": "Artificial intelligence product announcement",
                "url": "https://openai.com/news/product/",
                "description": "2026年8月12日 AI news",
            },
            {
                "title": "中国人工智能产业发布最新模型",
                "url": "https://www.sohu.com/a/ai-model",
                "description": "2026年8月12日人工智能动态",
            },
        ],
    )

    assert {
        provider_module._result_region(item) for item in ranked[:2]
    } == {"domestic", "international"}


def test_analysis_ranking_removes_dictionaries_and_document_mirrors(
    provider_module,
):
    ranked = provider_module._rank_search_results(
        "artificial intelligence industry report",
        [
            {
                "title": "Artificial intelligence industry report definition",
                "url": "https://www.britannica.com/technology/artificial-intelligence",
                "description": "Reference article",
            },
            {
                "title": "Artificial intelligence industry report download",
                "url": "https://www.book118.com/report/ai",
                "description": "Document mirror",
            },
            {
                "title": "Artificial intelligence industry report and statistics",
                "url": "https://www.oecd.org/digital/artificial-intelligence/report.html",
                "description": "Institutional methodology and data",
            },
        ],
    )

    assert [item["url"] for item in ranked] == [
        "https://www.oecd.org/digital/artificial-intelligence/report.html"
    ]


def test_explicit_domain_query_does_not_append_unrelated_results(
    provider_module,
):
    ranked = provider_module._rank_search_results(
        "Read platform.openai.com/docs before answering",
        [
            {
                "title": "OpenAI official documentation",
                "url": "https://platform.openai.com/docs/",
                "description": "Official API documentation",
                "source": "query-url",
                "search_channel": "direct",
            },
            {
                "title": "Definition of read",
                "url": "https://dictionary.cambridge.org/dictionary/english/read",
                "description": "Dictionary definition",
            },
        ],
    )

    assert [item["url"] for item in ranked] == [
        "https://platform.openai.com/docs/"
    ]


def test_freshness_ranking_drops_zero_relevance_dictionary_results(
    provider_module,
):
    ranked = provider_module._rank_search_results(
        "2026年8月11日 人工智能 最新新闻",
        [
            {
                "title": "年（汉语文字）",
                "url": "https://baike.baidu.com/item/year",
                "description": "年的解释",
            },
            {
                "title": "人工智能产业发布最新模型",
                "url": "https://news.example/ai-model",
                "description": "2026年8月11日人工智能动态",
            },
            {
                "title": "旅行清单",
                "url": "https://travel.example/list",
                "description": "今日旅行推荐",
            },
        ],
    )
    assert [item["url"] for item in ranked] == [
        "https://news.example/ai-model"
    ]


def test_official_ranking_prefers_exact_entity_domain(provider_module):
    ranked = provider_module._rank_search_results(
        "Python latest official release",
        [
            {
                "title": "Learn Python",
                "url": "https://www.learnpython.org/",
                "description": "Python tutorial",
            },
            {
                "title": "Python 3.14 release",
                "url": "https://www.python.org/downloads/",
                "description": "Official Python release",
            },
        ],
    )
    assert ranked[0]["url"] == "https://www.python.org/downloads/"


def test_generic_official_entry_does_not_hide_a_specific_document(provider_module):
    ranked = provider_module._rank_search_results(
        "Python official documentation how to configure free-threaded build",
        [
            {
                "title": "Python support for free threading",
                "url": "https://docs.python.org/3/howto/free-threading-python.html",
                "description": "Official Python documentation and build configuration",
            },
            {
                "title": "Python official documentation",
                "url": "https://docs.python.org/3/",
                "description": "Known public official entry point",
                "source": "official-entry",
                "search_channel": "official",
            },
        ],
    )

    assert ranked[0]["url"].endswith("free-threading-python.html")


def test_official_query_does_not_treat_a_community_subdomain_as_official(
    provider_module,
):
    ranked = provider_module._rank_search_results(
        "OpenAI official documentation",
        [
            {
                "title": "OpenAI community categories",
                "url": "https://community.openai.com/categories",
                "description": "Community discussions about OpenAI",
            },
            {
                "title": "OpenAI official documentation",
                "url": "https://platform.openai.com/docs/",
                "description": "Known public official entry point",
                "source": "official-entry",
                "search_channel": "official",
            },
        ],
    )

    assert ranked[0]["url"] == "https://platform.openai.com/docs/"


def test_official_query_drops_non_authoritative_single_term_noise(
    provider_module,
):
    ranked = provider_module._rank_search_results(
        "Python 3.13 free-threaded official documentation",
        [
            {
                "title": "Python 3.13 free-threaded documentation",
                "url": "https://docs.python.org/3.13/howto/free-threading-python.html",
                "description": "Official build and runtime documentation",
            },
            {
                "title": "Python games",
                "url": "https://www.poki.com/en/python-games",
                "description": "Play online games",
            },
            {
                "title": "Python free-threaded build guide",
                "url": "https://realpython.com/python-free-threading/",
                "description": "Independent guide for Python 3.13",
            },
        ],
    )

    assert [item["url"] for item in ranked] == [
        "https://docs.python.org/3.13/howto/free-threading-python.html",
        "https://realpython.com/python-free-threading/",
    ]


def test_freshness_ranking_prefers_authoritative_news_source(provider_module):
    ranked = provider_module._rank_search_results(
        "2026 artificial intelligence latest news",
        [
            {
                "title": "Artificial intelligence model update",
                "url": "https://seo-blog.example/ai",
                "description": "2026 AI news",
            },
            {
                "title": "Artificial intelligence model update",
                "url": "https://www.reuters.com/technology/ai/",
                "description": "2026 AI news",
            },
        ],
    )
    assert ranked[0]["url"].startswith("https://www.reuters.com/")


def test_freshness_ranking_suppresses_stale_and_description_only_hits(
    provider_module,
    monkeypatch,
):
    monkeypatch.setattr(
        provider_module,
        "_current_search_date",
        lambda: provider_module.date(2026, 8, 11),
    )
    ranked = provider_module._rank_search_results(
        "today AI news",
        [
            {
                "title": "AI product news in 2024",
                "url": "https://old.example/ai",
                "description": "2024 artificial intelligence report",
            },
            {
                "title": "August holidays and observances",
                "url": "https://calendar.example/august",
                "description": "One AI event is listed in the calendar",
            },
            {
                "title": "AI model launch in 2026",
                "url": "https://current.example/ai",
                "description": "2 days ago artificial intelligence update",
            },
        ],
    )

    assert [item["url"] for item in ranked] == [
        "https://current.example/ai"
    ]


def test_short_ai_term_uses_word_boundaries(provider_module):
    assert provider_module._contains_term("daily calendar", "ai") is False
    assert provider_module._contains_term("daily AI update", "ai") is True
    assert provider_module._technology_feed_query(
        "artificial intelligence latest news"
    )


def test_ai_freshness_requires_the_actual_topic_not_one_generic_word(
    provider_module,
):
    ranked = provider_module._rank_search_results(
        "2026 artificial intelligence latest news",
        [
            {
                "title": "Business intelligence market update",
                "url": "https://finance.example/intelligence",
                "description": "2026 enterprise analytics news",
            },
            {
                "title": "Artificial intelligence model update",
                "url": "https://news.example/ai-model",
                "description": "2026 artificial intelligence news",
            },
            {
                "title": "New AI policy announced",
                "url": "https://policy.example/ai",
                "description": "2026 policy news",
            },
        ],
    )

    assert [item["url"] for item in ranked] == [
        "https://news.example/ai-model",
        "https://policy.example/ai",
    ]


def test_freshness_ranking_excludes_reference_sites_and_deduplicates_hosts(
    provider_module,
):
    ranked = provider_module._rank_search_results(
        "latest artificial intelligence news",
        [
            {
                "title": "Artificial intelligence definition updated in 2026",
                "url": "https://www.britannica.com/technology/artificial-intelligence",
                "description": "Artificial intelligence reference article",
            },
            {
                "title": "Artificial intelligence model launch",
                "url": "https://news.example/one",
                "description": "AI news report",
            },
            {
                "title": "Artificial intelligence follow-up",
                "url": "https://news.example/two",
                "description": "AI news report",
            },
            {
                "title": "Artificial intelligence policy update",
                "url": "https://other.example/ai",
                "description": "AI news report",
            },
        ],
    )

    assert [item["url"] for item in ranked] == [
        "https://news.example/one",
        "https://other.example/ai",
    ]


def test_freshness_ranking_prefers_more_recent_publication(provider_module):
    ranked = provider_module._rank_search_results(
        "artificial intelligence news 2026-08-11",
        [
            {
                "title": "Artificial intelligence weekly update",
                "url": "https://older.example/ai",
                "description": "AI news",
                "published_at": "Tue, 04 Aug 2026 13:00:00 GMT",
            },
            {
                "title": "Artificial intelligence model update",
                "url": "https://recent.example/ai",
                "description": "AI news",
                "published_at": "Mon, 10 Aug 2026 13:00:00 GMT",
            },
        ],
    )

    assert ranked[0]["url"] == "https://recent.example/ai"


def test_rss_publication_date_satisfies_strict_freshness(provider_module):
    assert provider_module._has_strict_freshness_evidence(
        "AI news 2026-08-11",
        "AI model launch",
        "Official announcement",
        "Mon, 10 Aug 2026 09:30:00 GMT",
    )
    assert not provider_module._has_strict_freshness_evidence(
        "AI news 2026-08-11",
        "AI model overview",
        "Undated background article",
        "",
    )


def test_live_freshness_search_keeps_date_and_filters_irrelevant_cards(
    provider_module,
    monkeypatch,
):
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_BING_RSS_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_BING_NEWS_RSS_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_SEARX_MERGE_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_DOMESTIC_FALLBACK_ENABLED", "false")
    html = b"""
    <li class="b_algo"><h2><a href="https://baike.baidu.com/item/year">Year</a></h2>
      <div class="b_caption"><p>Dictionary entry.</p></div></li>
    <li class="b_algo"><h2><a href="https://travel.example/">Travel</a></h2>
      <div class="b_caption"><p>Today travel list.</p></div></li>
    <li class="b_algo"><h2><a href="https://news.example/ai">AI model update</a></h2>
      <div class="b_caption"><p>2026-08-10 artificial intelligence news.</p></div></li>
    """

    def handler(request):
        assert "2026" in request.url.params["q"]
        assert request.url.params["count"] == "15"
        return httpx.Response(200, request=request, content=html)

    monkeypatch.setattr(
        provider_module.httpx,
        "Client",
        _sync_client_factory(handler),
    )
    result = provider_module.WechatCloudWebProvider().search(
        "2026年8月11日 人工智能 最新新闻",
        3,
    )

    assert result["success"] is True
    assert [item["url"] for item in result["data"]["web"]] == [
        "https://news.example/ai"
    ]


def test_bing_tracking_url_is_unwrapped_before_public_safety_check(provider_module):
    target = "https://official.example/docs?q=public"
    payload = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    wrapped = "https://www.bing.com/ck/a?!&&u=a1%s&ntb=1" % payload
    assert provider_module._public_result_url(wrapped) == target


def test_bing_tracking_url_cannot_bypass_private_url_guard(provider_module):
    target = "http://169.254.169.254/latest/meta-data"
    payload = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    wrapped = "https://www.bing.com/ck/a?u=a1%s" % payload
    assert provider_module._public_result_url(wrapped) is None


def test_malformed_bing_tracking_url_is_dropped(provider_module):
    assert provider_module._public_result_url("https://www.bing.com/ck/a?u=bad") is None


def test_bing_news_tracking_url_is_unwrapped_and_safety_checked(provider_module):
    public = (
        "http://www.bing.com/news/apiclick.aspx?"
        "url=https%3A%2F%2Fpublisher.example%2Farticle%3Fid%3D7"
    )
    private = (
        "http://www.bing.com/news/apiclick.aspx?"
        "url=http%3A%2F%2F169.254.169.254%2Flatest%2Fmeta-data"
    )
    assert (
        provider_module._public_result_url(public)
        == "https://publisher.example/article?id=7"
    )
    assert provider_module._public_result_url(private) is None


def test_search_retries_then_succeeds(provider_module, monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            json={"results": [{"title": "ok", "url": "https://ok.example/", "content": "ok"}]},
        )

    monkeypatch.setenv("WECHAT_WEB_SEARCH_ATTEMPTS", "2")
    monkeypatch.setattr(provider_module.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))

    result = provider_module.WechatCloudWebProvider().search("retry", 1)
    assert result["success"] is True
    assert calls == 2


def test_search_circuit_opens_after_three_failures(provider_module, monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    provider = provider_module.WechatCloudWebProvider()
    for query in ("one", "two", "three"):
        assert provider.search(query, 1)["success"] is False
    fourth = provider.search("four", 1)

    assert calls == 3
    assert "circuit" in fourth["error"].lower()


def test_search_rejects_oversized_response(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_SEARCH_MAX_RESPONSE_BYTES", "64000")
    huge = {"results": [{"title": "x", "url": "https://x.example", "content": "x" * 70000}]}

    def handler(request):
        return httpx.Response(200, request=request, content=json.dumps(huge).encode())

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search("large", 1)
    assert result["success"] is False
    assert "ResponseTooLarge" in result["error"]


def test_extract_html_removes_scripts(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_EXTRACT_MIN_CHARS", "1")
    html = b"""
    <html><head><title>Useful page</title><script>SECRET_SCRIPT</script></head>
    <body><main><h1>Heading</h1><p>Useful body text.</p></main></body></html>
    """

    def handler(request):
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
            content=html,
        )

    monkeypatch.setattr(provider_module.httpx, "AsyncClient", _async_client_factory(handler))
    result = asyncio.run(
        provider_module.WechatCloudWebProvider().extract(["https://page.example/"])
    )

    assert result[0].get("error") is None
    assert "Useful body text" in result[0]["content"]
    assert "SECRET_SCRIPT" not in result[0]["content"]


def test_extract_blocks_unsafe_redirect(provider_module, monkeypatch):
    def handler(request):
        return httpx.Response(
            302,
            request=request,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
        )

    monkeypatch.setattr(provider_module.httpx, "AsyncClient", _async_client_factory(handler))
    result = asyncio.run(
        provider_module.WechatCloudWebProvider().extract(["https://redirect.example/"])
    )

    assert "private or internal" in result[0]["error"]


def test_extract_rejects_unsupported_content_type(provider_module, monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/octet-stream"},
            content=b"binary",
        )

    monkeypatch.setattr(provider_module.httpx, "AsyncClient", _async_client_factory(handler))
    result = asyncio.run(
        provider_module.WechatCloudWebProvider().extract(["https://file.example/a.bin"])
    )
    assert "Unsupported content type" in result[0]["error"]


def test_extract_enforces_streamed_byte_limit(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_EXTRACT_MAX_BYTES", "64000")

    def handler(request):
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/plain"},
            content=b"x" * 65000,
        )

    monkeypatch.setattr(provider_module.httpx, "AsyncClient", _async_client_factory(handler))
    result = asyncio.run(
        provider_module.WechatCloudWebProvider().extract(["https://large.example/"])
    )
    assert "ResponseTooLarge" in result[0]["error"]


def test_extract_preserves_order_and_limits_url_count(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_EXTRACT_MAX_URLS", "1")
    monkeypatch.setenv("WECHAT_WEB_EXTRACT_MIN_CHARS", "1")

    def handler(request):
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/plain"},
            content=b"first",
        )

    monkeypatch.setattr(provider_module.httpx, "AsyncClient", _async_client_factory(handler))
    result = asyncio.run(
        provider_module.WechatCloudWebProvider().extract(
            ["https://one.example/", "https://two.example/"]
        )
    )

    assert result[0]["content"] == "first"
    assert "limit exceeded" in result[1]["error"]


def test_extract_rejects_navigation_only_text(provider_module, monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/plain"},
            content=b"Home Menu Login",
        )

    monkeypatch.setattr(provider_module.httpx, "AsyncClient", _async_client_factory(handler))
    result = asyncio.run(
        provider_module.WechatCloudWebProvider().extract(["https://thin.example/"])
    )

    assert result[0]["content"] == ""
    assert "bounded retries" in result[0]["error"]


def test_extract_runs_selected_pages_with_bounded_parallelism(
    provider_module,
    monkeypatch,
):
    monkeypatch.setenv("WECHAT_WEB_EXTRACT_MAX_URLS", "5")
    monkeypatch.setenv("WECHAT_WEB_EXTRACT_WORKERS", "2")
    provider = provider_module.WechatCloudWebProvider()
    active = 0
    maximum_active = 0

    async def fake_extract(url):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"url": url, "title": "", "content": "ok"}

    monkeypatch.setattr(provider, "_extract_one", fake_extract)
    urls = ["https://%d.example/" % index for index in range(4)]

    results = asyncio.run(provider.extract(urls))

    assert [item["url"] for item in results] == urls
    assert maximum_active == 2


def test_underspecified_search_is_rejected_before_network(provider_module, monkeypatch):
    def unexpected_client(*_args, **_kwargs):
        raise AssertionError("underspecified query reached an upstream")

    monkeypatch.setattr(provider_module.httpx, "Client", unexpected_client)
    result = provider_module.WechatCloudWebProvider().search(
        "latest news August 2026",
        5,
    )

    assert result["success"] is False
    assert "concrete subject" in result["error"]


def test_relative_current_period_queries_receive_freshness_context(
    provider_module,
    monkeypatch,
):
    monkeypatch.setattr(
        provider_module,
        "_current_search_date",
        lambda: provider_module.date(2026, 8, 12),
    )

    assert provider_module._upstream_query("今年人工智能行业趋势").endswith("2026")
    assert provider_module._query_target_year_month("本月 AI 新闻") == (2026, 8)
    assert "freshness" in provider_module._query_intents("this week AI news")


def test_recommendation_filters_budget_pages_unrelated_to_product(provider_module):
    ranked = provider_module._rank_search_results(
        "预算 5000 元，推荐拍照好的手机",
        [
            {
                "title": "5000 元价位拍照手机横评",
                "url": "https://review.example/camera-phone",
                "description": "手机人像、夜景和长焦实测",
            },
            {
                "title": "重庆市 2026 年预算执行情况",
                "url": "https://www.cq.gov.cn/budget/2026",
                "description": "政府财政预算公开",
            },
            {
                "title": "中央预算报告",
                "url": "https://www.mof.gov.cn/budget/report",
                "description": "财政预算数据",
            },
        ],
    )

    assert [item["url"] for item in ranked] == [
        "https://review.example/camera-phone"
    ]


def test_precise_official_result_beats_generic_entry_pages(provider_module):
    ranked = provider_module._rank_search_results(
        "Python 3.13 vs Python 3.14 free-threaded official documentation comparison",
        [
            {
                "title": "Python official documentation",
                "url": "https://docs.python.org/3/",
                "description": "Official Python documentation entry point",
                "source": "official-entry",
                "search_channel": "official",
            },
            {
                "title": "Python 3.13 and 3.14 free-threaded mode comparison",
                "url": "https://docs.python.org/3/howto/free-threading-python.html",
                "description": "Version-specific free-threaded Python documentation",
            },
            {
                "title": "The Python Tutorial",
                "url": "https://docs.python.org/3/tutorial/",
                "description": "General Python tutorial",
            },
        ],
    )

    assert ranked[0]["url"].endswith("/howto/free-threading-python.html")
    assert all(item["url"] != "https://docs.python.org/3/" for item in ranked)


def test_official_intent_runs_one_bounded_site_scoped_search(
    provider_module,
    monkeypatch,
):
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "true")
    calls = []

    def handler(request):
        query = request.url.params["q"]
        calls.append(query)
        if query.startswith("site:docs.python.org"):
            html = b"""
            <li class="b_algo"><h2><a href="https://docs.python.org/3/howto/free-threading-python.html">
            Python 3.13 3.14 free-threaded mode comparison</a></h2>
            <div class="b_caption"><p>Official version-specific documentation.</p></div></li>
            """
        else:
            html = b"""
            <li class="b_algo"><h2><a href="https://docs.python.org/3/">Python documentation</a></h2>
            <div class="b_caption"><p>General official entry point.</p></div></li>
            """
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            content=html,
        )

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    result = provider_module.WechatCloudWebProvider().search(
        "Python 3.13 vs Python 3.14 free-threaded official documentation comparison",
        2,
    )

    assert result["success"] is True
    assert len(calls) == 2
    assert calls[1].startswith("site:docs.python.org ")
    assert result["data"]["web"][0]["url"].endswith(
        "/howto/free-threading-python.html"
    )
    row = result["data"]["web"][0]
    assert row["source_type"] == "official"
    assert row["evidence_level"] == "search_metadata_only"
    context = result["data"]["search_context"]
    assert context["verification_required"] is True
    assert "web_extract" in context["next_step"]


def test_search_result_pages_and_site_operators_are_not_exposed(provider_module):
    assert provider_module._public_result_url(
        "https://m.sogou.com/web/searchList.jsp?keyword=python&insite=csdn.net"
    ) is None
    assert provider_module._public_result_url(
        "https://m.baidu.com/s?word=python"
    ) is None
    assert provider_module._query_domain_results(
        "site:docs.python.org free threading"
    ) == []


def test_specific_python_official_entries_include_requested_versions(provider_module):
    query = "Python 3.13 与 Python 3.14 free-threaded mode 官方文档对比"
    rows = provider_module._official_entry_results(query)

    assert [row["url"] for row in rows[:2]] == [
        "https://docs.python.org/3.13/howto/free-threading-python.html",
        "https://docs.python.org/3.14/howto/free-threading-python.html",
    ]
    assert [row["url"] for row in provider_module._rank_search_results(query, rows)][
        :2
    ] == [
        "https://docs.python.org/3.13/howto/free-threading-python.html",
        "https://docs.python.org/3.14/howto/free-threading-python.html",
    ]
    combined = [
        {
            "title": "Python 3.14 documentation",
            "url": "https://docs.python.org/3/",
            "description": "Official Python documentation entry page",
        }
    ] + rows
    assert [
        row["url"]
        for row in provider_module._rank_search_results(query, combined)[:2]
    ] == [
        "https://docs.python.org/3.13/howto/free-threading-python.html",
        "https://docs.python.org/3.14/howto/free-threading-python.html",
    ]


def test_live_official_weather_entries_survive_freshness_filter(provider_module):
    query = "今天中国台风路径和预警 官方气象信息"
    rows = provider_module._official_entry_results(query)
    ranked = provider_module._rank_search_results(query, rows)

    assert [row["url"] for row in ranked] == [
        "https://typhoon.nmc.cn/web.html",
        "https://www.nmc.cn/publish/alarm.html",
    ]


def test_phone_recommendation_adds_compact_review_query(provider_module, monkeypatch):
    monkeypatch.setattr(
        provider_module,
        "_current_search_date",
        lambda: provider_module.date(2026, 8, 12),
    )
    query = "预算 5000 元，推荐拍照好的手机"

    assert provider_module._bing_html_queries(
        query,
        provider_module._upstream_query(query),
    ) == [
        "预算 5000 元，推荐拍照好的手机",
        "smartphone-camera-comparison-review-2026-midrange",
    ]
    assert provider_module._query_intents("手机影像横评实测") == {"comparison"}


def test_dual_region_fresh_news_adds_an_english_global_query(
    provider_module,
    monkeypatch,
):
    monkeypatch.setattr(
        provider_module,
        "_current_search_date",
        lambda: provider_module.date(2026, 8, 12),
    )
    query = "今天国内外 AI 重要新闻"

    assert provider_module._bing_html_queries(
        query,
        provider_module._upstream_query(query),
    ) == [
        "人工智能 ai 新闻 2026-08-12",
        "global artificial intelligence news August 2026",
    ]
    assert "dual_region" in provider_module._query_intents(query)
    terms = provider_module._query_relevance_terms(query)
    assert provider_module._required_relevance_matches(query, terms) == 1


def test_quality_metadata_preserves_public_source_labels(provider_module):
    item = {
        "title": "Regional source",
        "url": "https://publisher.example/article",
        "description": "Artificial intelligence report",
        "source_type": "authoritative",
        "region": "domestic",
    }

    assert provider_module._result_source_type(
        item,
        "publisher.example",
    ) == "authoritative"
    assert provider_module._result_region(item) == "domestic"


def test_independent_review_sources_are_labeled_separately(provider_module):
    item = {"url": "https://www.tomsguide.com/best-picks/best-camera-phones"}

    assert provider_module._result_source_type(item, "www.tomsguide.com") == "review"


def test_recommendation_promotes_independent_review_evidence(provider_module):
    query = "预算 5000 元，推荐拍照好的手机"
    ranked = provider_module._rank_search_results(
        query,
        [
            {
                "title": "5000 元拍照手机推荐",
                "url": "https://www.zhihu.com/question/1",
                "description": "拍照手机推荐",
            },
            {
                "title": "Best camera phones tested",
                "url": "https://www.tomsguide.com/best-picks/best-camera-phones",
                "description": "Independent smartphone camera comparison and review",
            },
        ],
    )

    assert ranked[0]["url"].startswith("https://www.tomsguide.com/")


def test_recommendation_promotes_two_distinct_review_hosts(provider_module):
    ranked = provider_module._rank_search_results(
        "预算 5000 元，推荐拍照好的手机",
        [
            {
                "title": "社区手机推荐",
                "url": "https://www.zhihu.com/question/1",
                "description": "拍照手机推荐",
            },
            {
                "title": "Best camera phones tested",
                "url": "https://www.tomsguide.com/best-picks/best-camera-phones",
                "description": "Independent smartphone camera comparison and review",
            },
            {
                "title": "Best phones tested for camera quality",
                "url": "https://www.pcmag.com/picks/the-best-camera-phones",
                "description": "Independent smartphone camera review",
            },
        ],
    )

    assert [urlsplit(item["url"]).hostname for item in ranked[:2]] == [
        "www.tomsguide.com",
        "www.pcmag.com",
    ]


def test_phone_recommendation_has_curated_review_fallback(provider_module):
    query = "预算 5000 元，推荐拍照好的手机"
    entries = provider_module._official_entry_results(query)

    assert [item["url"] for item in entries[:2]] == [
        "https://www.tomsguide.com/best-picks/best-camera-phones",
        "https://www.dxomark.com/smartphones/",
    ]
    assert all(
        provider_module._result_source_type(
            item,
            urlsplit(item["url"]).hostname or "",
        )
        == "review"
        for item in entries[:2]
    )


def test_version_comparison_can_be_high_quality_on_one_official_host(
    provider_module,
):
    metadata = provider_module._search_quality_metadata(
        "Python 3.13 与 Python 3.14 官方文档对比",
        [
            {
                "title": "Python 3.13 free-threading HOWTO",
                "url": "https://docs.python.org/3.13/howto/free-threading-python.html",
                "source_type": "official",
            },
            {
                "title": "Python 3.14 free-threading HOWTO",
                "url": "https://docs.python.org/3.14/howto/free-threading-python.html",
                "source_type": "official",
            },
        ],
    )

    assert metadata["quality"] == "high"
    assert metadata["authoritative_result_count"] == 2


def test_candidate_url_normalization_does_not_resolve_dns(
    provider_module,
    monkeypatch,
):
    checked = []
    monkeypatch.setattr(
        provider_module,
        "is_safe_url",
        lambda url: checked.append(url) or True,
    )

    assert (
        provider_module._normalized_result_url("https://news.example/item#section")
        == "https://news.example/item"
    )
    assert provider_module._normalized_result_url("https://news.example:bad/item") is None
    assert provider_module._normalized_result_url("https://user@news.example/item") is None
    assert checked == []


def test_search_ranks_out_irrelevant_result_flood_before_dns_checks(
    provider_module,
    monkeypatch,
):
    monkeypatch.setattr(
        provider_module,
        "_current_search_date",
        lambda: date(2026, 8, 12),
    )
    rows = [
        {
            "title": "Unrelated flight promotion %d" % index,
            "url": "https://noise-%d.example/deal" % index,
            "content": "Airline tickets and holiday travel offers",
        }
        for index in range(100)
    ]
    rows.extend(
        [
            {
                "title": "Artificial intelligence news and model releases",
                "url": "https://global-ai.example/news",
                "content": "Current artificial intelligence industry news",
                "published_at": "Wed, 12 Aug 2026 08:00:00 GMT",
            },
            {
                "title": "Latest AI research news",
                "url": "https://ai-lab.example/latest",
                "content": "Recent artificial intelligence research updates",
                "published_at": "Wed, 12 Aug 2026 07:00:00 GMT",
            },
        ]
    )

    def handler(request):
        return httpx.Response(200, request=request, json={"results": rows})

    checked_hosts = []

    def safety_check(url):
        host = urlsplit(url).hostname
        checked_hosts.append(host)
        assert not str(host).startswith("noise-")
        return True

    monkeypatch.setattr(provider_module.httpx, "Client", _sync_client_factory(handler))
    monkeypatch.setattr(provider_module, "is_safe_url", safety_check)

    result = provider_module.WechatCloudWebProvider().search(
        "today artificial intelligence news",
        2,
    )

    assert result["success"] is True
    assert len(result["data"]["web"]) == 2
    assert set(checked_hosts) == {"global-ai.example", "ai-lab.example"}


def test_result_dns_checks_obey_one_shared_deadline(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_RESULT_SAFETY_MAX_CHECKS", "4")
    monkeypatch.setenv("WECHAT_WEB_RESULT_SAFETY_WORKERS", "2")
    monkeypatch.setenv("WECHAT_WEB_RESULT_SAFETY_TIMEOUT_SECONDS", "0.1")

    started = []

    def slow_safety_check(url):
        started.append(url)
        time.sleep(0.5)
        return True

    monkeypatch.setattr(provider_module, "is_safe_url", slow_safety_check)
    items = [
        {
            "title": "Bounded lookup result %d" % index,
            "url": "https://slow-%d.example/item" % index,
            "description": "Bounded lookup evidence",
        }
        for index in range(20)
    ]

    before = time.monotonic()
    results = provider_module._safe_ranked_results("bounded lookup", items, 2)
    elapsed = time.monotonic() - before

    assert results == []
    assert elapsed < 0.35
    assert len(started) <= 2

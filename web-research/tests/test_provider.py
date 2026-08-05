from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sqlite3
import sys
import time
import types
from pathlib import Path
from urllib.parse import urljoin

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
    monkeypatch.setenv("WECHAT_WEB_SEARCH_URL", "http://127.0.0.1:8651")
    monkeypatch.setenv("WECHAT_WEB_BING_URL", "https://www.bing.com/search")
    monkeypatch.setenv(
        "WECHAT_WEB_BING_NEWS_URL", "https://global.bing.com/news/search"
    )
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_BING_RSS_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_BING_NEWS_RSS_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_SEARX_MERGE_ENABLED", "false")
    monkeypatch.setenv("WECHAT_WEB_DOMESTIC_FALLBACK_ENABLED", "false")
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
                    {"title": " One ", "url": "https://one.example/a#fragment", "content": " A result "},
                    {"title": "Duplicate", "url": "https://one.example/a", "content": "duplicate"},
                    {"title": "Private", "url": "http://127.0.0.1/admin", "content": "no"},
                    {"title": "Two", "url": "https://two.example/", "content": "second"},
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
    assert first["data"]["web"][0]["title"] == "One"


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
    result = provider_module.WechatCloudWebProvider().search("docs", 2)

    assert result["success"] is True
    assert len(calls) == 1
    assert result["data"]["web"][0] == {
        "title": "Official docs",
        "url": "https://official.example/docs",
        "description": "Primary documentation source.",
        "position": 1,
    }


def test_freshness_search_prioritizes_bing_news_sources(provider_module, monkeypatch):
    monkeypatch.setenv("WECHAT_WEB_BING_HTML_ENABLED", "true")
    monkeypatch.setenv("WECHAT_WEB_BING_NEWS_RSS_ENABLED", "true")
    html = b"""
    <li class="b_algo"><h2><a href="https://web-one.example/">Web one</a></h2></li>
    <li class="b_algo"><h2><a href="https://web-two.example/">Web two</a></h2></li>
    """
    rss = b"""<?xml version="1.0"?><rss><channel>
    <item><title>News one</title><link>http://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fnews-one.example%2Fstory</link><description>One</description></item>
    <item><title>News two</title><link>http://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fnews-two.example%2Fstory</link><description>Two</description></item>
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
            "Web %d</a></h2></li>" % (index, index)
        ).encode()
        for index in range(1, 5)
    )
    rss = (
        '<?xml version="1.0"?><rss><channel>'
        + "".join(
            "<item><title>News %d</title><link>"
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
                            "title": "Regional %d" % index,
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
    result = provider_module.WechatCloudWebProvider().search("腾讯云 官方文档", 1)

    assert result["success"] is True
    assert result["data"]["web"][0]["url"] == "https://cloud.tencent.com/"
    assert calls == ["127.0.0.1", "m.sogou.com"]


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
    result = provider_module.WechatCloudWebProvider().search("国务院 最新政策", 1)

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
    result = provider_module.WechatCloudWebProvider().search("国务院 最新政策", 1)

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
    }
    assert provider_module._bing_market_params("人工智能新闻") == {
        "setlang": "zh-Hans",
        "cc": "CN",
        "mkt": "zh-CN",
    }


def test_english_merge_keeps_two_global_results_per_domestic_result(provider_module):
    primary = [{"url": "p%d" % index} for index in range(4)]
    secondary = [{"url": "s%d" % index} for index in range(2)]
    merged = provider_module._interleave_results(primary, secondary, primary_weight=2)
    assert [item["url"] for item in merged] == ["p0", "p1", "s0", "p2", "p3", "s1"]


def test_current_year_is_removed_only_for_freshness_queries(provider_module):
    year = str(provider_module.time.gmtime().tm_year)
    assert provider_module._upstream_query(year + " AI latest news") == "AI latest news"
    assert provider_module._upstream_query(year + " 人工智能最新新闻") == "人工智能最新新闻"
    assert provider_module._upstream_query(year + " annual report") == year + " annual report"
    assert provider_module._upstream_query("2024 AI latest news") == "2024 AI latest news"


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

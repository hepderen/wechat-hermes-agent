#!/usr/bin/env python3
"""Bounded live reliability and relevance checks for the candidate provider."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import os
import re
import statistics
import time
from pathlib import Path
from urllib.parse import urlsplit


CASES = (
    ("python", "Python official documentation", ("python.org",), "international"),
    (
        "systemd",
        "systemd official documentation and manual",
        ("systemd.io", "freedesktop.org", "man7.org"),
        "international",
    ),
    (
        "kubernetes",
        "Kubernetes official documentation",
        ("kubernetes.io",),
        "international",
    ),
    (
        "python-free-threaded",
        "Python free-threaded build official documentation",
        ("python.org",),
        "international",
    ),
    (
        "python-version-comparison",
        "Python 3.13 vs Python 3.14 free-threaded mode comparison documentation",
        ("python.org",),
        "international",
    ),
    ("gov-cn", "中国政府网 国务院 最新政策", ("gov.cn",), "domestic"),
    ("tencent-cloud", "腾讯云 官方文档", ("cloud.tencent.com",), "domestic"),
    ("aliyun", "阿里云 官方文档", ("aliyun.com",), "domestic"),
    ("current-ai-cn", "{year} 人工智能 最新新闻", (), "domestic-news"),
    (
        "current-ai-en",
        "{year} artificial intelligence latest news",
        (),
        "international-news",
    ),
    (
        "explicit-domain",
        "Read platform.openai.com/docs before answering",
        ("openai.com",),
        "direct",
    ),
)
AI_RELEVANCE_RE = re.compile(r"(?:\bartificial\s+intelligence\b|\bAI\b|人工智能)", re.I)
DOMESTIC_HOST_SUFFIXES = (
    "gov.cn",
    "xinhuanet.com",
    "people.com.cn",
    "cctv.com",
    "chinanews.com.cn",
    "china.com.cn",
    "ce.cn",
    "china.org.cn",
    "qq.com",
    "163.com",
    "sina.com.cn",
    "sohu.com",
    "ifeng.com",
    "thepaper.cn",
    "yicai.com",
    "caixin.com",
    "jiemian.com",
    "36kr.com",
    "leiphone.com",
    "huxiu.com",
    "guancha.cn",
    "cls.cn",
    "stcn.com",
    "eastmoney.com",
    "cnstock.com",
    "caict.ac.cn",
)


def load_provider(path: Path):
    spec = importlib.util.spec_from_file_location("wechat_cloud_web_stress", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load provider module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module.WechatCloudWebProvider()


def host_matches(host: str, expected: tuple[str, ...]) -> bool:
    return any(host == item or host.endswith("." + item) for item in expected)


def domestic_host(host: str) -> bool:
    return host.endswith(".cn") or host_matches(
        host,
        DOMESTIC_HOST_SUFFIXES,
    )


def reset_source_circuits(module) -> None:
    module.WechatCloudWebProvider._source_failures.clear()
    module.WechatCloudWebProvider._source_open_until.clear()


async def run(args) -> dict:
    os.environ["WECHAT_WEB_SEARCH_URL"] = args.search_url
    os.environ["WECHAT_WEB_SEARCH_CACHE_SECONDS"] = "300"
    os.environ["WECHAT_WEB_SEARCH_CACHE_DB"] = "disabled"
    module, provider = load_provider(args.provider)

    durations = []
    case_results = []
    official_hits = {"domestic": 0, "international": 0, "direct": 0}
    current_year = time.gmtime().tm_year
    for label, query_template, expected, channel in CASES:
        query = query_template.format(year=current_year)
        started = time.monotonic()
        first = provider.search(query, 8)
        first_duration = time.monotonic() - started
        started = time.monotonic()
        second = provider.search(query, 8)
        cached_duration = time.monotonic() - started
        durations.append(first_duration)
        if not first.get("success") or first != second:
            raise RuntimeError("search reliability failed for case %s" % label)
        rows = first.get("data", {}).get("web", [])
        hosts = sorted({urlsplit(str(item.get("url") or "")).hostname or "" for item in rows})
        diversity_case = channel.endswith("news")
        minimum_rows = 4 if diversity_case else 1
        minimum_hosts = 3 if diversity_case else 1
        if len(rows) < minimum_rows or len(hosts) < minimum_hosts:
            raise RuntimeError(
                "search diversity failed case=%s channel=%s rows=%d/%d hosts=%d/%d values=%s"
                % (
                    label,
                    channel,
                    len(rows),
                    minimum_rows,
                    len(hosts),
                    minimum_hosts,
                    ",".join(hosts),
                )
            )
        matched = not expected or any(host_matches(host, expected) for host in hosts)
        relevant_top_five = sum(
            1
            for item in rows[:5]
            if AI_RELEVANCE_RE.search(
                "%s %s"
                % (
                    str(item.get("title") or ""),
                    str(item.get("description") or ""),
                )
            )
        )
        if label in {"current-ai-cn", "current-ai-en"} and relevant_top_five < 4:
            raise RuntimeError(
                "fresh-news relevance failed case=%s relevant_top_five=%d hosts=%s"
                % (label, relevant_top_five, ",".join(hosts))
            )
        if expected and matched and channel in official_hits:
            official_hits[channel] += 1
        case_results.append(
            {
                "case": label,
                "channel": channel,
                "result_count": len(rows),
                "host_count": len(hosts),
                "hosts": hosts,
                "expected_domain_found": matched,
                "relevant_top_five": relevant_top_five,
                "cold_ms": round(first_duration * 1000, 1),
                "cache_ms": round(cached_duration * 1000, 1),
            }
        )
    if official_hits["domestic"] < 2 or official_hits["international"] < 2:
        missing = [
            item["case"] for item in case_results if not item["expected_domain_found"]
        ]
        raise RuntimeError(
            "official-domain relevance gate failed: domestic=%d/3 international=%d/3 missing=%s details=%s"
            % (
                official_hits["domestic"],
                official_hits["international"],
                ",".join(missing),
                json.dumps(
                    [item for item in case_results if not item["expected_domain_found"]],
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            )
        )
    if official_hits["direct"] != 1:
        raise RuntimeError("explicit-domain safety path did not return the named public domain")

    regional_pair_hosts = set()
    for query in (
        "人工智能 行业报告",
        "artificial intelligence industry report",
    ):
        pair_result = provider.search(query, 8)
        if not pair_result.get("success"):
            raise RuntimeError("dual-region language pair search failed")
        regional_pair_hosts.update(
            urlsplit(str(item.get("url") or "")).hostname or ""
            for item in pair_result.get("data", {}).get("web", [])
        )
    if not (
        any(domestic_host(host) for host in regional_pair_hosts)
        and any(not domestic_host(host) for host in regional_pair_hosts)
    ):
        raise RuntimeError(
            "dual-region language pair coverage failed hosts=%s"
            % ",".join(sorted(regional_pair_hosts))
        )

    extracts = []
    for url, minimum_chars in (
        ("https://www.python.org/", 500),
        ("https://cloud.tencent.com/document/product/213", 500),
    ):
        started = time.monotonic()
        result = await provider.extract([url])
        duration = time.monotonic() - started
        if (
            not result
            or result[0].get("error")
            or len(result[0].get("content") or "") < minimum_chars
        ):
            raise RuntimeError(
                "live extraction reliability gate failed host=%s error=%s chars=%d"
                % (
                    urlsplit(url).hostname,
                    str(result[0].get("error") if result else "empty"),
                    len(result[0].get("content") or "") if result else 0,
                )
            )
        extracts.append(
            {
                "host": urlsplit(result[0]["url"]).hostname,
                "chars": len(result[0]["content"]),
                "duration_ms": round(duration * 1000, 1),
            }
        )

    old_html = os.environ.get("WECHAT_WEB_BING_HTML_ENABLED")
    old_rss = os.environ.get("WECHAT_WEB_BING_RSS_ENABLED")
    old_url = os.environ.get("WECHAT_WEB_SEARCH_URL")
    old_merge = os.environ.get("WECHAT_WEB_SEARX_MERGE_ENABLED")
    try:
        os.environ["WECHAT_WEB_BING_HTML_ENABLED"] = "false"
        os.environ["WECHAT_WEB_BING_RSS_ENABLED"] = "false"
        os.environ["WECHAT_WEB_SEARX_MERGE_ENABLED"] = "true"
        os.environ["WECHAT_WEB_SEARCH_URL"] = "http://127.0.0.1:9"
        module.WechatCloudWebProvider._cache.clear()
        reset_source_circuits(module)
        domestic_only = provider.search("腾讯云 官方文档", 8)
        domestic_hosts = {
            urlsplit(str(item.get("url") or "")).hostname or ""
            for item in domestic_only.get("data", {}).get("web", [])
        }
        if not domestic_only.get("success") or not any(
            host_matches(host, ("cloud.tencent.com",)) for host in domestic_hosts
        ):
            raise RuntimeError("domestic-only search channel failed")

        os.environ["WECHAT_WEB_BING_HTML_ENABLED"] = "true"
        os.environ["WECHAT_WEB_BING_RSS_ENABLED"] = "true"
        os.environ["WECHAT_WEB_SEARX_MERGE_ENABLED"] = "true"
        os.environ["WECHAT_WEB_SEARCH_URL"] = "http://127.0.0.1:9"
        module.WechatCloudWebProvider._cache.clear()
        international_only = provider.search("Python official documentation", 8)
        international_hosts = {
            urlsplit(str(item.get("url") or "")).hostname or ""
            for item in international_only.get("data", {}).get("web", [])
        }
        if not international_only.get("success") or not any(
            host_matches(host, ("python.org",)) for host in international_hosts
        ):
            raise RuntimeError("international-only search channel failed")

        os.environ["WECHAT_WEB_BING_HTML_ENABLED"] = "false"
        os.environ["WECHAT_WEB_BING_RSS_ENABLED"] = "false"
        os.environ["WECHAT_WEB_SEARX_MERGE_ENABLED"] = "true"
        os.environ["WECHAT_WEB_SEARCH_URL"] = "http://127.0.0.1:9"
        module.WechatCloudWebProvider._cache.clear()
        module.WechatCloudWebProvider._consecutive_failures = 0
        module.WechatCloudWebProvider._circuit_open_until = 0.0
        reset_source_circuits(module)
        for index in range(3):
            failed = provider.search("failure-probe-%d" % index, 3)
            if failed.get("success"):
                raise RuntimeError("all-upstreams-down probe unexpectedly succeeded")
        circuit = provider.search("failure-probe-circuit", 3)
        if "circuit" not in str(circuit.get("error") or "").lower():
            raise RuntimeError("search circuit breaker did not open")
    finally:
        if old_html is None:
            os.environ.pop("WECHAT_WEB_BING_HTML_ENABLED", None)
        else:
            os.environ["WECHAT_WEB_BING_HTML_ENABLED"] = old_html
        if old_rss is None:
            os.environ.pop("WECHAT_WEB_BING_RSS_ENABLED", None)
        else:
            os.environ["WECHAT_WEB_BING_RSS_ENABLED"] = old_rss
        if old_url is None:
            os.environ.pop("WECHAT_WEB_SEARCH_URL", None)
        else:
            os.environ["WECHAT_WEB_SEARCH_URL"] = old_url
        if old_merge is None:
            os.environ.pop("WECHAT_WEB_SEARX_MERGE_ENABLED", None)
        else:
            os.environ["WECHAT_WEB_SEARX_MERGE_ENABLED"] = old_merge
        module.WechatCloudWebProvider._cache.clear()
        module.WechatCloudWebProvider._consecutive_failures = 0
        module.WechatCloudWebProvider._circuit_open_until = 0.0
        reset_source_circuits(module)

    recovery = provider.search("Python official documentation", 5)
    if not recovery.get("success"):
        raise RuntimeError("provider did not recover after circuit reset")

    sorted_durations = sorted(durations)
    p95_index = max(
        0,
        min(len(sorted_durations) - 1, math.ceil(len(sorted_durations) * 0.95) - 1),
    )
    return {
        "ok": True,
        "cases": case_results,
        "official_domain_hits": official_hits,
        "dual_region_language_pair_hosts": sorted(regional_pair_hosts),
        "cold_search_median_ms": round(statistics.median(durations) * 1000, 1),
        "cold_search_p95_ms": round(sorted_durations[p95_index] * 1000, 1),
        "extracts": extracts,
        "circuit_breaker": "passed",
        "domestic_only_channel": "passed",
        "international_only_channel": "passed",
        "recovery": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", type=Path, required=True)
    parser.add_argument("--search-url", default="http://127.0.0.1:18651")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

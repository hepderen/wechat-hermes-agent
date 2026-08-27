#!/usr/bin/env python3
"""Inspect live search ranking and extraction without invoking Hermes or WeChat."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CASES = {
    "phone": "预算 5000 元，推荐拍照好的手机",
    "python": "Python 3.13 与 Python 3.14 free-threaded mode 官方文档对比",
    "typhoon": "今天中国台风路径和预警 官方气象信息",
    "ai-news": "今天国内外 AI 重要新闻",
}


def load_provider(path: Path) -> tuple[Any, Any]:
    spec = importlib.util.spec_from_file_location("wechat_cloud_web_quality", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load provider module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module.WechatCloudWebProvider()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    module, provider = load_provider(args.provider)
    if args.unranked:
        module._rank_search_results = lambda _query, items: list(items)
    reports = []
    default_cases = (
        list(CASES)
        if not args.case and not args.query and not args.extract_url
        else []
    )
    selected_cases = [(name, CASES[name]) for name in (args.case or default_cases)]
    selected_cases.extend(
        ("custom-%d" % (index + 1), query)
        for index, query in enumerate(args.query or [])
    )
    for case_name, query in selected_cases:
        started = __import__("time").monotonic()
        result = provider.search(query, args.limit)
        elapsed_ms = round((__import__("time").monotonic() - started) * 1000)
        data = result.get("data") if isinstance(result, dict) else None
        rows = data.get("web", []) if isinstance(data, dict) else []
        context = data.get("search_context", {}) if isinstance(data, dict) else {}
        public_rows = []
        for row in rows:
            url = str(row.get("url") or "")
            public_rows.append(
                {
                    "position": row.get("position"),
                    "title": str(row.get("title") or "")[:180],
                    "host": (urlsplit(url).hostname or "").lower(),
                    "url": url,
                    "source_type": row.get("source_type"),
                    "region": row.get("region"),
                    "publication_date": row.get("publication_date"),
                }
            )

        extraction = []
        urls = [row["url"] for row in public_rows[: args.extract_top]]
        if urls:
            extracted = await provider.extract(urls)
            for item in extracted:
                content = str(item.get("content") or "")
                extraction.append(
                    {
                        "host": (urlsplit(str(item.get("url") or "")).hostname or "").lower(),
                        "ok": not bool(item.get("error")) and bool(content.strip()),
                        "chars": len(content),
                        "error_type": (
                            str(item.get("error") or "").split(":", 1)[0][:80]
                            if item.get("error")
                            else ""
                        ),
                    }
                )
        successful_extractions = sum(bool(item["ok"]) for item in extraction)
        required_extractions = (
            int(args.min_extracted) if int(args.extract_top) > 0 else 0
        )
        context_intents = set(context.get("intents") or [])
        context_regions = set(context.get("regions") or [])
        quality_ok = bool(args.allow_low_quality) or str(
            context.get("quality") or ""
        ).lower() in {"medium", "high"}
        region_ok = "dual_region" not in context_intents or {
            "domestic",
            "international",
        }.issubset(context_regions)
        case_ok = (
            bool(result.get("success"))
            and bool(public_rows)
            and successful_extractions >= required_extractions
            and quality_ok
            and region_ok
        )
        reports.append(
            {
                "case": case_name,
                "ok": case_ok,
                "query_hash": hashlib.sha256(query.encode()).hexdigest()[:12],
                "success": bool(result.get("success")),
                "error": str(result.get("error") or "")[:200],
                "elapsed_ms": elapsed_ms,
                "required_extractions": required_extractions,
                "successful_extractions": successful_extractions,
                "quality_ok": quality_ok,
                "region_ok": region_ok,
                "context": context,
                "results": public_rows,
                "extraction": extraction,
            }
        )
    direct_extraction = []
    if args.extract_url:
        for item in await provider.extract(args.extract_url):
            content = str(item.get("content") or "")
            direct_extraction.append(
                {
                    "host": (
                        urlsplit(str(item.get("url") or "")).hostname or ""
                    ).lower(),
                    "ok": not bool(item.get("error")) and bool(content.strip()),
                    "chars": len(content),
                    "error_type": (
                        str(item.get("error") or "").split(":", 1)[0][:80]
                        if item.get("error")
                        else ""
                    ),
                }
            )
    direct_requested = len(args.extract_url or [])
    direct_ok = len(direct_extraction) == direct_requested and all(
        item["ok"] for item in direct_extraction
    )
    return {
        "ok": all(item["ok"] for item in reports) and direct_ok,
        "cases": reports,
        "direct_extraction": direct_extraction,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", type=Path, required=True)
    parser.add_argument("--search-url", default="http://127.0.0.1:8651")
    parser.add_argument("--case", action="append", choices=sorted(CASES))
    parser.add_argument("--query", action="append")
    parser.add_argument("--extract-url", action="append")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--extract-top", type=int, default=3)
    parser.add_argument("--min-extracted", type=int, default=1)
    parser.add_argument("--allow-low-quality", action="store_true")
    parser.add_argument("--unranked", action="store_true")
    args = parser.parse_args()
    args.limit = max(1, min(args.limit, 10))
    args.extract_top = max(0, min(args.extract_top, args.limit))
    args.min_extracted = (
        0
        if args.extract_top == 0
        else max(1, min(args.min_extracted, args.extract_top))
    )
    os.environ["WECHAT_WEB_SEARCH_URL"] = args.search_url
    os.environ["WECHAT_WEB_SEARCH_CACHE_SECONDS"] = "0"
    report = asyncio.run(run(args))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

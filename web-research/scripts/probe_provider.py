#!/usr/bin/env python3
"""Read-only live probe for the WeChat Hermes web provider."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def load_provider(path: Path):
    spec = importlib.util.spec_from_file_location("wechat_cloud_web_probe", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load provider module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.WechatCloudWebProvider()


async def run(args) -> dict:
    os.environ["WECHAT_WEB_SEARCH_URL"] = args.search_url
    os.environ["WECHAT_WEB_SEARCH_CACHE_SECONDS"] = "0"
    provider = load_provider(args.provider)
    if not provider.is_available():
        raise RuntimeError("provider did not report available")

    summaries = []
    for query in ("OpenAI official documentation", "人工智能 最新新闻"):
        result = provider.search(query, 5)
        if not result.get("success"):
            raise RuntimeError("live search failed: %s" % result.get("error"))
        rows = result.get("data", {}).get("web", [])
        if len(rows) < 3:
            raise RuntimeError("live search returned fewer than three results")
        for row in rows:
            parsed = urlsplit(str(row.get("url") or ""))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise RuntimeError("search returned a non-public URL shape")
        summaries.append(
            {
                "query_hash": __import__("hashlib").sha256(query.encode()).hexdigest()[:12],
                "result_count": len(rows),
                "hosts": sorted({urlsplit(row["url"]).hostname for row in rows}),
            }
        )

    extraction = await provider.extract([args.extract_url])
    if not extraction or extraction[0].get("error"):
        raise RuntimeError("live extraction failed: %s" % extraction[0].get("error"))
    if len(extraction[0].get("content") or "") < 100:
        raise RuntimeError("live extraction returned too little content")

    blocked = await provider.extract(["http://169.254.169.254/latest/meta-data/"])
    if not blocked or "Blocked" not in str(blocked[0].get("error")):
        raise RuntimeError("metadata URL was not blocked")

    return {
        "ok": True,
        "searches": summaries,
        "extract": {
            "host": urlsplit(extraction[0]["url"]).hostname,
            "chars": len(extraction[0]["content"]),
            "content_type": extraction[0].get("metadata", {}).get("content_type"),
        },
        "metadata_ssrf_blocked": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", type=Path, required=True)
    parser.add_argument("--search-url", default="http://127.0.0.1:18651")
    parser.add_argument("--extract-url", default="https://www.baidu.com/")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

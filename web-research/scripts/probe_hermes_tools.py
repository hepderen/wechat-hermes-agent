#!/usr/bin/env python3
"""Probe the provider through Hermes' real registry and web tool wrappers."""

from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import urlsplit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="Python official documentation")
    parser.add_argument("--extract-url", default="https://www.python.org/")
    args = parser.parse_args()

    from tools.web_tools import (
        _ensure_web_plugins_loaded,
        web_extract_tool,
        web_search_tool,
    )

    _ensure_web_plugins_loaded()
    from agent.web_search_registry import (
        get_active_extract_provider,
        get_active_search_provider,
    )

    search_provider = get_active_search_provider()
    extract_provider = get_active_extract_provider()
    if search_provider is None or search_provider.name != "wechat-cloud":
        raise RuntimeError("wechat-cloud is not the active search provider")
    if extract_provider is None or extract_provider.name != "wechat-cloud":
        raise RuntimeError("wechat-cloud is not the active extract provider")

    search = json.loads(web_search_tool(args.query, limit=5))
    if not search.get("success"):
        raise RuntimeError("Hermes web_search failed: %s" % search.get("error"))
    rows = search.get("data", {}).get("web", [])
    if len(rows) < 3:
        raise RuntimeError("Hermes web_search returned fewer than three results")

    extracted = json.loads(
        asyncio.run(web_extract_tool([args.extract_url], char_limit=20_000))
    )
    pages = extracted.get("results", [])
    if not pages or pages[0].get("error"):
        raise RuntimeError("Hermes web_extract failed: %s" % (pages[0].get("error") if pages else "empty"))
    if len(pages[0].get("content") or "") < 100:
        raise RuntimeError("Hermes web_extract returned too little content")

    print(
        json.dumps(
            {
                "ok": True,
                "search_provider": search_provider.name,
                "extract_provider": extract_provider.name,
                "search_result_count": len(rows),
                "search_hosts": sorted(
                    {urlsplit(str(item.get("url") or "")).hostname for item in rows}
                ),
                "extract_host": urlsplit(str(pages[0].get("url") or "")).hostname,
                "extract_chars": len(pages[0].get("content") or ""),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

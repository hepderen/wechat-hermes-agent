from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "probe_search_quality.py"


def load_probe_module():
    spec = importlib.util.spec_from_file_location("probe_search_quality", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeProvider:
    def __init__(self, *, rows, extracted):
        self.rows = rows
        self.extracted = extracted

    def search(self, _query, _limit):
        return {
            "success": True,
            "data": {
                "web": list(self.rows),
                "search_context": {"quality": "high"},
            },
        }

    async def extract(self, _urls):
        return list(self.extracted)


def args(**changes):
    values = {
        "provider": SCRIPT,
        "search_url": "http://127.0.0.1:8651",
        "unranked": False,
        "case": [],
        "query": ["specific query"],
        "extract_url": [],
        "limit": 3,
        "extract_top": 2,
        "min_extracted": 1,
        "allow_low_quality": False,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def install_provider(monkeypatch, module, provider):
    monkeypatch.setattr(
        module,
        "load_provider",
        lambda _path: (SimpleNamespace(), provider),
    )


def test_quality_probe_fails_when_ranked_pages_cannot_be_extracted(monkeypatch):
    module = load_probe_module()
    provider = FakeProvider(
        rows=[{"url": "https://example.com/article", "title": "Article"}],
        extracted=[
            {
                "url": "https://example.com/article",
                "content": "",
                "error": "timeout",
            }
        ],
    )
    install_provider(monkeypatch, module, provider)

    report = asyncio.run(module.run(args()))

    assert report["ok"] is False
    assert report["cases"][0]["ok"] is False
    assert report["cases"][0]["successful_extractions"] == 0


def test_quality_probe_passes_only_after_required_extractions(monkeypatch):
    module = load_probe_module()
    provider = FakeProvider(
        rows=[
            {"url": "https://one.example/article", "title": "One"},
            {"url": "https://two.example/article", "title": "Two"},
        ],
        extracted=[
            {
                "url": "https://one.example/article",
                "content": "verified body",
                "error": None,
            },
            {
                "url": "https://two.example/article",
                "content": "verified body",
                "error": None,
            },
        ],
    )
    install_provider(monkeypatch, module, provider)

    report = asyncio.run(module.run(args(min_extracted=2)))

    assert report["ok"] is True
    assert report["cases"][0]["required_extractions"] == 2
    assert report["cases"][0]["successful_extractions"] == 2


def test_quality_probe_detects_missing_direct_extraction_results(monkeypatch):
    module = load_probe_module()
    provider = FakeProvider(rows=[], extracted=[])
    install_provider(monkeypatch, module, provider)

    report = asyncio.run(
        module.run(
            args(
                query=[],
                extract_url=["https://example.com/article"],
            )
        )
    )

    assert report["ok"] is False
    assert report["direct_extraction"] == []


def test_quality_probe_rejects_low_quality_or_incomplete_dual_region_results(
    monkeypatch,
):
    module = load_probe_module()
    provider = FakeProvider(
        rows=[{"url": "https://example.com/article", "title": "Article"}],
        extracted=[
            {
                "url": "https://example.com/article",
                "content": "verified body",
                "error": None,
            }
        ],
    )
    provider.search = lambda _query, _limit: {
        "success": True,
        "data": {
            "web": list(provider.rows),
            "search_context": {
                "quality": "low",
                "intents": ["dual_region"],
                "regions": ["domestic"],
            },
        },
    }
    install_provider(monkeypatch, module, provider)

    report = asyncio.run(module.run(args()))

    assert report["ok"] is False
    assert report["cases"][0]["quality_ok"] is False
    assert report["cases"][0]["region_ok"] is False

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest


class DummyFastMCP:
    def __init__(self, *_args, **_kwargs):
        self.tools: list[str] = []

    def tool(self, **_kwargs):
        def decorator(function):
            self.tools.append(function.__name__)
            return function

        return decorator

    def run(self, **_kwargs):
        return None


fastmcp_module = types.ModuleType("mcp.server.fastmcp")
fastmcp_module.FastMCP = DummyFastMCP
server_module = types.ModuleType("mcp.server")
server_module.fastmcp = fastmcp_module
mcp_module = types.ModuleType("mcp")
mcp_module.server = server_module
sys.modules.setdefault("mcp", mcp_module)
sys.modules.setdefault("mcp.server", server_module)
sys.modules.setdefault("mcp.server.fastmcp", fastmcp_module)

mcp_server = importlib.import_module("mcp_server")


def test_mcp_exposes_cloud_only_tools():
    assert set(mcp_server.mcp.tools) == {
        "wechat_memory_list",
        "wechat_memory_update",
        "wechat_register_artifact",
        "wechat_http_fetch",
        "wechat_write_text_artifact",
        "wechat_create_zip_artifact",
    }
    assert not any(name.startswith("workstation_") for name in mcp_server.mcp.tools)


def test_task_identifier_validation():
    assert mcp_server.validate_task_id("t-12ab34cd") == "T-12AB34CD"
    with pytest.raises(ValueError):
        mcp_server.validate_task_id("T-1")


def test_register_cloud_artifact_is_task_scoped_and_verified(
    tmp_path,
    monkeypatch,
):
    task_id = "T-12AB34CD"
    task_root = tmp_path / task_id
    task_root.mkdir(parents=True)
    artifact = task_root / "cover.png"
    artifact.write_bytes(b"\x89PNG\r\n\x1a\n" + b"content")
    captured = {}

    async def fake_request(method, url, **kwargs):
        captured.update(
            {
                "method": method,
                "url": url,
                "body": kwargs["json_body"],
            }
        )
        return {
            "artifact": {
                "artifact_id": "A-1234567890ABCDEF",
                "task_id": task_id,
                "name": artifact.name,
                "mime_type": "image/png",
                "size_bytes": artifact.stat().st_size,
                "sha256": mcp_server.validate_cloud_artifact(
                    task_id,
                    str(artifact),
                ).sha256,
                "verified": True,
            }
        }

    monkeypatch.setattr(mcp_server, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(mcp_server, "ADAPTER_TOKEN", "secret")
    monkeypatch.setattr(mcp_server, "request_json", fake_request)

    result = asyncio.run(
        mcp_server.wechat_register_artifact(task_id, str(artifact))
    )
    assert result == {
        "artifact_id": "A-1234567890ABCDEF",
        "mime_type": "image/png",
        "size_bytes": artifact.stat().st_size,
        "sha256": mcp_server.validate_cloud_artifact(
            task_id,
            str(artifact),
        ).sha256,
        "verified": True,
    }
    assert captured["method"] == "POST"
    assert captured["body"]["task_id"] == task_id

    outside = tmp_path / "outside.png"
    outside.write_bytes(artifact.read_bytes())
    with pytest.raises(ValueError, match="outside"):
        asyncio.run(
            mcp_server.wechat_register_artifact(task_id, str(outside))
        )

    fake_video = task_root / "fake.mp4"
    fake_video.write_bytes(artifact.read_bytes())
    with pytest.raises(ValueError, match="MIME"):
        asyncio.run(
            mcp_server.wechat_register_artifact(task_id, str(fake_video))
        )

def test_memory_tools_use_only_trusted_task_id(monkeypatch):
    calls = []

    async def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "request_json", fake_request)
    monkeypatch.setattr(mcp_server, "ADAPTER_TOKEN", "secret")
    asyncio.run(
        mcp_server.wechat_memory_update(
            "t-12ab34cd",
            "set",
            "style",
            "concise",
        )
    )
    assert calls[0][1].endswith("/internal/memory/T-12AB34CD")
    assert "room_id" not in calls[0][2]["json_body"]


def test_remote_error_never_includes_response_body():
    response = types.SimpleNamespace(
        status_code=500,
        text="Bearer top-secret user@example.com",
    )
    error = mcp_server.remote_error(response)
    assert str(error) == "remote service returned HTTP 500"
    assert "top-secret" not in str(error)


def test_adapter_url_must_be_loopback(monkeypatch):
    monkeypatch.setenv("TEST_MCP_URL", "https://example.com")
    with pytest.raises(RuntimeError, match="loopback"):
        mcp_server.loopback_url("TEST_MCP_URL", "http://127.0.0.1")


def test_text_and_zip_tools_require_live_context_and_stay_task_scoped(
    tmp_path,
    monkeypatch,
):
    task_id = "T-12AB34CD"
    calls = []

    async def authorize(value):
        calls.append(value)
        return {
            "task_id": value,
            "generation": 1,
            "remaining_download_bytes": 1024,
        }

    monkeypatch.setattr(mcp_server, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(mcp_server, "authorize_production_tool", authorize)

    text_result = asyncio.run(
        mcp_server.wechat_write_text_artifact(
            task_id,
            "report.md",
            "# Verified report",
        )
    )
    zip_result = asyncio.run(
        mcp_server.wechat_create_zip_artifact(
            task_id,
            "delivery.zip",
            [text_result["path"]],
        )
    )

    assert calls == [task_id, task_id]
    assert text_result["registered"] is False
    assert zip_result["registered"] is False
    assert Path(text_result["path"]).is_relative_to(tmp_path / task_id)
    assert Path(zip_result["path"]).is_relative_to(tmp_path / task_id)


def test_http_fetch_records_verified_metadata_and_removes_rejected_download(
    tmp_path,
    monkeypatch,
):
    task_id = "T-12AB34CD"
    task_root = tmp_path / task_id
    task_root.mkdir()
    downloaded = task_root / "download.json"
    downloaded.write_text('{"ok":true}', encoding="utf-8")
    artifact = mcp_server.production_tools.validate_media_path(
        str(downloaded),
        tmp_path,
        task_id,
        1024,
        1024,
    )
    produced = mcp_server.production_tools.ProducedFile(artifact, reused=False)

    async def authorize(value):
        return {
            "task_id": value,
            "generation": 1,
            "remaining_download_bytes": 1024,
        }

    def fake_download(*_args, **_kwargs):
        return produced

    async def reject_record(*_args, **_kwargs):
        raise RuntimeError("quota rejected")

    monkeypatch.setattr(mcp_server, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(mcp_server, "authorize_production_tool", authorize)
    monkeypatch.setattr(
        mcp_server.production_tools,
        "download_public_artifact",
        fake_download,
    )
    monkeypatch.setattr(mcp_server, "record_download", reject_record)

    with pytest.raises(RuntimeError, match="quota rejected"):
        asyncio.run(
            mcp_server.wechat_http_fetch(
                task_id,
                "https://example.com/data.json",
                "download.json",
            )
        )
    assert downloaded.exists() is False

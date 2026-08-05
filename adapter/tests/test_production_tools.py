from __future__ import annotations

import asyncio
import os
import socket
import urllib.parse
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import production_tools
from app.main import create_app
from tests.test_adapter import ROOM_ID, create_task, make_runtime


TASK_ID = "T-12AB34CD"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def artifact_root(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir()
    return root


def write_text(root: Path, name: str, content: str):
    return production_tools.write_text_artifact(
        root,
        TASK_ID,
        name,
        content,
        max_text_bytes=1024 * 1024,
        max_artifact_bytes=MAX_ARTIFACT_BYTES,
        max_image_bytes=MAX_IMAGE_BYTES,
    )


def test_text_artifact_is_validated_idempotent_and_never_overwritten(tmp_path):
    root = artifact_root(tmp_path)
    first = write_text(root, "report.json", '{"status":"ok"}')
    second = write_text(root, "report.json", '{"status":"ok"}')

    assert first.reused is False
    assert second.reused is True
    assert first.artifact.sha256 == second.artifact.sha256
    assert first.artifact.path.read_text(encoding="utf-8") == '{"status":"ok"}'

    with pytest.raises(FileExistsError, match="different content"):
        write_text(root, "report.json", '{"status":"changed"}')
    assert first.artifact.path.read_text(encoding="utf-8") == '{"status":"ok"}'


@pytest.mark.parametrize(
    "name",
    ["../report.txt", "sub/report.txt", "sub\\report.txt", ".hidden", "报告.txt"],
)
def test_text_artifact_rejects_unsafe_names(tmp_path, name):
    root = artifact_root(tmp_path)
    with pytest.raises(ValueError):
        write_text(root, name, "content")


def test_text_artifact_rejects_invalid_json_and_size_limit(tmp_path):
    root = artifact_root(tmp_path)
    with pytest.raises(ValueError, match="JSON"):
        write_text(root, "bad.json", "not-json")
    with pytest.raises(ValueError, match="size limit"):
        production_tools.write_text_artifact(
            root,
            TASK_ID,
            "large.txt",
            "12345",
            max_text_bytes=4,
            max_artifact_bytes=MAX_ARTIFACT_BYTES,
            max_image_bytes=MAX_IMAGE_BYTES,
        )


def test_public_url_rejects_private_mixed_dns_credentials_and_ports(monkeypatch):
    def resolved(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]

    monkeypatch.setattr(production_tools.socket, "getaddrinfo", resolved)
    normalized, parsed, addresses = production_tools.validate_public_url(
        "https://Example.COM/api?q=1"
    )
    assert normalized == "https://example.com/api?q=1"
    assert parsed.hostname == "example.com"
    assert addresses == ("93.184.216.34",)

    monkeypatch.setattr(
        production_tools.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        production_tools.validate_public_url("https://example.com/file")

    for url in (
        "http://127.0.0.1/file",
        "http://169.254.169.254/latest/meta-data",
        "https://user:password@example.com/file",
        "https://example.com:8443/file",
        "file:///etc/passwd",
    ):
        with pytest.raises(ValueError):
            production_tools.validate_public_url(url)


def test_http_download_revalidates_redirect_and_produces_verified_file(
    tmp_path,
    monkeypatch,
):
    root = artifact_root(tmp_path)
    validated_urls = []
    request_paths = []

    def fake_validate(url):
        validated_urls.append(url)
        parsed = urllib.parse.urlsplit(url)
        return url, parsed, ("93.184.216.34",)

    def fake_request(parsed, _addresses, destination, **_kwargs):
        request_paths.append(parsed.path)
        if parsed.path == "/start":
            return 302, {"location": "https://cdn.example.com/final"}
        destination.write_text('{"downloaded":true}', encoding="utf-8")
        return 200, {"content-type": "application/json"}

    monkeypatch.setattr(production_tools, "validate_public_url", fake_validate)
    monkeypatch.setattr(production_tools, "_request_once", fake_request)
    result = production_tools.download_public_artifact(
        root,
        TASK_ID,
        "https://example.com/start",
        "download.json",
        max_download_bytes=1024,
        max_artifact_bytes=MAX_ARTIFACT_BYTES,
        max_image_bytes=MAX_IMAGE_BYTES,
    )

    assert result.artifact.mime_type == "application/json"
    assert request_paths == ["/start", "/final"]
    assert validated_urls == [
        "https://example.com/start",
        "https://cdn.example.com/final",
    ]


def test_http_request_rejects_partial_and_truncated_responses(tmp_path, monkeypatch):
    destination = tmp_path / "response.tmp"
    destination.write_bytes(b"")

    class Response:
        def __init__(self, status, headers, body):
            self.status = status
            self._headers = headers
            self._body = body
            self._read = False

        def getheaders(self):
            return list(self._headers.items())

        def read(self, _size):
            if self._read:
                return b""
            self._read = True
            return self._body

    class Connection:
        response = None

        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            return None

        def getresponse(self):
            return self.response

        def close(self):
            return None

    monkeypatch.setattr(production_tools, "_PinnedHTTPSConnection", Connection)
    parsed = urllib.parse.urlsplit("https://example.com/file")

    Connection.response = Response(206, {"Content-Length": "3"}, b"abc")
    with pytest.raises(RuntimeError, match="HTTP 206"):
        production_tools._request_once(
            parsed,
            ("93.184.216.34",),
            destination,
            max_bytes=10,
            timeout_seconds=1,
        )

    Connection.response = Response(200, {"Content-Length": "5"}, b"abc")
    with pytest.raises(ValueError, match="Content-Length"):
        production_tools._request_once(
            parsed,
            ("93.184.216.34",),
            destination,
            max_bytes=10,
            timeout_seconds=1,
        )


def test_zip_artifact_is_task_scoped_deterministic_and_rejects_links(tmp_path):
    root = artifact_root(tmp_path)
    task_root = production_tools.ensure_task_root(root, TASK_ID)
    source = task_root / "source"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    (source / "b.json").write_text('{"b":2}', encoding="utf-8")

    first = production_tools.create_zip_artifact(
        root,
        TASK_ID,
        "bundle.zip",
        ["source"],
        max_files=10,
        max_source_bytes=1024,
        max_artifact_bytes=MAX_ARTIFACT_BYTES,
        max_image_bytes=MAX_IMAGE_BYTES,
    )
    second = production_tools.create_zip_artifact(
        root,
        TASK_ID,
        "bundle.zip",
        ["source"],
        max_files=10,
        max_source_bytes=1024,
        max_artifact_bytes=MAX_ARTIFACT_BYTES,
        max_image_bytes=MAX_IMAGE_BYTES,
    )
    assert first.reused is False
    assert second.reused is True
    with zipfile.ZipFile(first.artifact.path) as archive:
        assert archive.namelist() == ["source/a.txt", "source/b.json"]

    with pytest.raises(ValueError, match="outside"):
        production_tools.create_zip_artifact(
            root,
            TASK_ID,
            "outside.zip",
            [str(tmp_path / "outside.txt")],
            max_files=10,
            max_source_bytes=1024,
            max_artifact_bytes=MAX_ARTIFACT_BYTES,
            max_image_bytes=MAX_IMAGE_BYTES,
        )

    link = source / "outside-link"
    try:
        os.symlink(tmp_path / "outside.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")
    with pytest.raises(ValueError, match="symbolic link"):
        production_tools.create_zip_artifact(
            root,
            TASK_ID,
            "linked.zip",
            ["source"],
            max_files=10,
            max_source_bytes=1024,
            max_artifact_bytes=MAX_ARTIFACT_BYTES,
            max_image_bytes=MAX_IMAGE_BYTES,
        )


def test_internal_tool_context_and_download_quota_are_task_state_bound(tmp_path):
    runtime = make_runtime(tmp_path, max_download_bytes=20)
    runtime.store.initialize()
    task = create_task(runtime.store, request_id="production-tool-context")
    claimed = runtime.store.claim_next()
    runtime.store.set_run_id(task["id"], "run-production-tool")
    task_root = runtime.settings.artifact_root / task["id"]
    task_root.mkdir(parents=True)
    first = task_root / "first.txt"
    first.write_text("1234567890", encoding="utf-8")
    second = task_root / "second.txt"
    second.write_text("abcdefghijk", encoding="utf-8")
    headers = {"Authorization": "Bearer internal-secret"}

    with TestClient(create_app(runtime, start_worker=False)) as client:
        unauthorized = client.get("/internal/tools/context/" + task["id"])
        assert unauthorized.status_code == 401

        context = client.get(
            "/internal/tools/context/" + task["id"],
            headers=headers,
        )
        assert context.status_code == 200
        assert context.json()["remaining_download_bytes"] == 20

        recorded = client.post(
            "/internal/tools/downloads",
            headers=headers,
            json={"task_id": task["id"], "path": str(first)},
        )
        assert recorded.status_code == 200
        duplicate = client.post(
            "/internal/tools/downloads",
            headers=headers,
            json={"task_id": task["id"], "path": str(first)},
        )
        assert duplicate.status_code == 200
        over_limit = client.post(
            "/internal/tools/downloads",
            headers=headers,
            json={"task_id": task["id"], "path": str(second)},
        )
        assert over_limit.status_code == 409

        runtime.store.cancel_task(task["id"], ROOM_ID)
        canceled = client.get(
            "/internal/tools/context/" + task["id"],
            headers=headers,
        )
        assert canceled.status_code == 409

    assert runtime.store.downloaded_bytes(claimed["id"]) == 10


def test_canceled_task_cannot_write_memory_or_register_artifacts(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    task = create_task(runtime.store, request_id="canceled-tool-writes")
    runtime.store.claim_next()
    runtime.store.set_run_id(task["id"], "run-canceled-tools")
    task_root = runtime.settings.artifact_root / task["id"]
    task_root.mkdir(parents=True)
    result = task_root / "result.txt"
    result.write_text("must not register", encoding="utf-8")
    runtime.store.cancel_task(task["id"], ROOM_ID)
    headers = {"Authorization": "Bearer internal-secret"}

    with TestClient(create_app(runtime, start_worker=False)) as client:
        memory = client.post(
            "/internal/memory/" + task["id"],
            headers=headers,
            json={"action": "set", "key": "style", "value": "concise"},
        )
        artifact = client.post(
            "/internal/artifacts/register",
            headers=headers,
            json={"task_id": task["id"], "path": str(result)},
        )

    assert memory.status_code == 409
    assert artifact.status_code == 409
    assert runtime.store.list_artifacts(task["id"], task["generation"]) == []

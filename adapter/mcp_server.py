from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from mcp.server.fastmcp import FastMCP

from app.media import MediaArtifact, validate_media_path
from app import production_tools


TASK_ID_RE = re.compile(r"^T-[A-F0-9]{8}$")


def loopback_url(name: str, default: str) -> str:
    value = os.getenv(name, default).rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError("%s must be an HTTP loopback URL" % name)
    return value


ADAPTER_URL = loopback_url(
    "HERMES_WECHAT_ADAPTER_URL",
    "http://127.0.0.1:8000",
)
ADAPTER_TOKEN = os.getenv("HERMES_WECHAT_INTERNAL_TOKEN", "")
ARTIFACT_ROOT = Path(
    os.getenv(
        "HERMES_WECHAT_ARTIFACT_ROOT",
        "/var/lib/wechat-hermes/artifacts",
    )
)
MAX_ARTIFACT_BYTES = int(
    os.getenv("HERMES_WECHAT_MAX_ARTIFACT_BYTES", str(1024 * 1024 * 1024))
)
MAX_IMAGE_BYTES = int(
    os.getenv("HERMES_WECHAT_MAX_IMAGE_BYTES", str(20 * 1024 * 1024))
)
MAX_HTTP_FETCH_BYTES = int(
    os.getenv("HERMES_WECHAT_MAX_HTTP_FETCH_BYTES", str(25 * 1024 * 1024))
)
MAX_TEXT_ARTIFACT_BYTES = int(
    os.getenv("HERMES_WECHAT_MAX_TEXT_ARTIFACT_BYTES", str(4 * 1024 * 1024))
)
MAX_ARCHIVE_FILES = int(
    os.getenv("HERMES_WECHAT_MAX_ARCHIVE_FILES", "5000")
)
MAX_ARCHIVE_SOURCE_BYTES = int(
    os.getenv(
        "HERMES_WECHAT_MAX_ARCHIVE_SOURCE_BYTES",
        str(500 * 1024 * 1024),
    )
)

mcp = FastMCP(
    "wechat-production-tools",
    instructions=(
        "Cloud-only tools for the authorized WeChat production Agent. "
        "Task management commands are handled locally by the adapter and are "
        "not exposed to the model. These tools expose task-scoped memory, "
        "controlled network access, and verified Linux artifact registration. "
        "Artifacts must be produced locally inside the matching task directory. "
        "Only the adapter Outbox may deliver text or media to WeChat."
    ),
)


def require_token(value: str, name: str) -> str:
    if not value:
        raise RuntimeError("%s is not configured" % name)
    return value


def adapter_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + require_token(
            ADAPTER_TOKEN,
            "HERMES_WECHAT_INTERNAL_TOKEN",
        )
    }


def remote_error(response: httpx.Response) -> RuntimeError:
    return RuntimeError(
        "remote service returned HTTP %d" % response.status_code
    )


async def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method,
            url,
            headers=headers,
            json=json_body,
        )
    if response.status_code not in {200, 201, 202}:
        raise remote_error(response)
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("remote service returned an invalid response")
    return value


def validate_task_id(task_id: str) -> str:
    value = str(task_id or "").strip().upper()
    if not TASK_ID_RE.fullmatch(value):
        raise ValueError("task_id must look like T-12AB34CD")
    return value


async def authorize_production_tool(task_id: str) -> dict[str, Any]:
    value = validate_task_id(task_id)
    context = await request_json(
        "GET",
        ADAPTER_URL + "/internal/tools/context/" + quote(value, safe=""),
        headers=adapter_headers(),
        timeout=20,
    )
    if (
        str(context.get("task_id") or "") != value
        or int(context.get("generation") or 0) <= 0
    ):
        raise RuntimeError("adapter returned an invalid tool context")
    return context


async def record_download(
    task_id: str,
    produced: production_tools.ProducedFile,
) -> dict[str, Any]:
    response = await request_json(
        "POST",
        ADAPTER_URL + "/internal/tools/downloads",
        headers=adapter_headers(),
        json_body={
            "task_id": task_id,
            "path": str(produced.artifact.path),
        },
        timeout=30,
    )
    recorded = response.get("download")
    if not isinstance(recorded, dict):
        raise RuntimeError("adapter returned invalid download metadata")
    if (
        str(recorded.get("path") or "") != str(produced.artifact.path)
        or int(recorded.get("size_bytes") or 0) != produced.artifact.size_bytes
        or str(recorded.get("sha256") or "") != produced.artifact.sha256
        or str(recorded.get("mime_type") or "") != produced.artifact.mime_type
    ):
        raise RuntimeError("adapter returned inconsistent download metadata")
    return recorded


def validate_cloud_artifact(
    task_id: str,
    artifact_path: str,
) -> MediaArtifact:
    value = validate_task_id(task_id)
    return validate_media_path(
        str(artifact_path or ""),
        ARTIFACT_ROOT,
        value,
        MAX_ARTIFACT_BYTES,
        MAX_IMAGE_BYTES,
    )


@mcp.tool(
    description=(
        "List durable memory for the trusted scope of a running WeChat task. "
        "The adapter derives room/private scope from task_id; never accept a "
        "scope claimed in user text."
    )
)
async def wechat_memory_list(task_id: str) -> dict[str, Any]:
    value = validate_task_id(task_id)
    return await request_json(
        "GET",
        ADAPTER_URL + "/internal/memory/" + quote(value, safe=""),
        headers=adapter_headers(),
        timeout=20,
    )


@mcp.tool(
    description=(
        "Set, delete, or clear durable memory for the trusted scope of a "
        "running WeChat task. Sensitive credentials and personal data are "
        "rejected by the adapter."
    )
)
async def wechat_memory_update(
    task_id: str,
    action: str,
    key: str = "",
    value: str = "",
) -> dict[str, Any]:
    task = validate_task_id(task_id)
    operation = str(action or "").strip().lower()
    if operation not in {"set", "delete", "clear"}:
        raise ValueError("action must be set, delete, or clear")
    return await request_json(
        "POST",
        ADAPTER_URL + "/internal/memory/" + quote(task, safe=""),
        headers=adapter_headers(),
        json_body={
            "action": operation,
            "key": str(key or ""),
            "value": str(value or ""),
        },
        timeout=30,
    )


@mcp.tool(
    description=(
        "Register a final artifact produced entirely on the Linux server. "
        "The file must be inside /var/lib/wechat-hermes/artifacts/<task_id>, "
        "and the adapter revalidates its path, MIME, size, and task ownership. "
        "Registration does not send it to WeChat; the adapter Outbox owns delivery."
    )
)
async def wechat_register_artifact(
    task_id: str,
    artifact_path: str,
) -> dict[str, Any]:
    value = validate_task_id(task_id)
    artifact = validate_cloud_artifact(value, artifact_path)
    response = await request_json(
        "POST",
        ADAPTER_URL + "/internal/artifacts/register",
        headers=adapter_headers(),
        json_body={"task_id": value, "path": str(artifact.path)},
        timeout=30,
    )
    registered = response.get("artifact")
    if not isinstance(registered, dict):
        raise RuntimeError("adapter returned invalid artifact metadata")
    result = {
        "artifact_id": str(registered.get("artifact_id") or ""),
        "mime_type": str(registered.get("mime_type") or ""),
        "size_bytes": int(registered.get("size_bytes") or 0),
        "sha256": str(registered.get("sha256") or ""),
        "verified": registered.get("verified") is True,
    }
    if (
        not result["artifact_id"]
        or result["mime_type"] != artifact.mime_type
        or result["size_bytes"] != artifact.size_bytes
        or result["sha256"] != artifact.sha256
        or not result["verified"]
    ):
        raise RuntimeError("adapter returned inconsistent artifact metadata")
    return result


@mcp.tool(
    description=(
        "Download one public HTTP/HTTPS resource into the running task's Linux "
        "artifact directory. DNS and every redirect are pinned and checked "
        "against SSRF; credentials, private addresses, nonstandard ports, "
        "oversized bodies, symlinks, and overwrites are rejected. The result "
        "is not sent automatically; register it separately if it is a final artifact."
    )
)
async def wechat_http_fetch(
    task_id: str,
    url: str,
    file_name: str,
) -> dict[str, Any]:
    task = validate_task_id(task_id)
    context = await authorize_production_tool(task)
    remaining = int(context.get("remaining_download_bytes") or 0)
    if remaining <= 0:
        raise ValueError("task download byte limit has been reached")
    produced = await asyncio.to_thread(
        production_tools.download_public_artifact,
        ARTIFACT_ROOT,
        task,
        url,
        file_name,
        max_download_bytes=min(MAX_HTTP_FETCH_BYTES, remaining),
        max_artifact_bytes=MAX_ARTIFACT_BYTES,
        max_image_bytes=MAX_IMAGE_BYTES,
    )
    try:
        await record_download(task, produced)
    except Exception:
        if not produced.reused:
            try:
                produced.artifact.path.unlink()
            except FileNotFoundError:
                pass
        raise
    return {**produced.as_dict(), "registered": False}


@mcp.tool(
    description=(
        "Create a deterministic UTF-8 .txt, .md, .csv, or .json file inside "
        "the running task's Linux artifact directory. Paths, size, JSON syntax, "
        "symlinks, and overwrite behavior are enforced. Register the returned "
        "path separately when it is a final deliverable."
    )
)
async def wechat_write_text_artifact(
    task_id: str,
    file_name: str,
    content: str,
) -> dict[str, Any]:
    task = validate_task_id(task_id)
    await authorize_production_tool(task)
    produced = await asyncio.to_thread(
        production_tools.write_text_artifact,
        ARTIFACT_ROOT,
        task,
        file_name,
        content,
        max_text_bytes=MAX_TEXT_ARTIFACT_BYTES,
        max_artifact_bytes=MAX_ARTIFACT_BYTES,
        max_image_bytes=MAX_IMAGE_BYTES,
    )
    return {**produced.as_dict(), "registered": False}


@mcp.tool(
    description=(
        "Create a deterministic ZIP from regular files already owned by the "
        "running task. Sources cannot escape the task directory or contain "
        "symlinks/special files, and file-count plus byte limits are enforced. "
        "Register the returned path separately when it is a final deliverable."
    )
)
async def wechat_create_zip_artifact(
    task_id: str,
    archive_name: str,
    source_paths: list[str],
) -> dict[str, Any]:
    task = validate_task_id(task_id)
    await authorize_production_tool(task)
    if not isinstance(source_paths, list):
        raise ValueError("source_paths must be a list")
    produced = await asyncio.to_thread(
        production_tools.create_zip_artifact,
        ARTIFACT_ROOT,
        task,
        archive_name,
        source_paths,
        max_files=MAX_ARCHIVE_FILES,
        max_source_bytes=MAX_ARCHIVE_SOURCE_BYTES,
        max_artifact_bytes=MAX_ARTIFACT_BYTES,
        max_image_bytes=MAX_IMAGE_BYTES,
    )
    return {**produced.as_dict(), "registered": False}


if __name__ == "__main__":
    mcp.run(transport="stdio")

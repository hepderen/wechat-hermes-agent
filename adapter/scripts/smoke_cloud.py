from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import httpx


T = TypeVar("T")


class TransientModelProbeError(RuntimeError):
    pass


def env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise RuntimeError(f"invalid environment line in {path}")
        values[key] = value
    return values


def service_env(service_name: str) -> dict[str, str]:
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "show",
            "--property=MainPID",
            "--value",
            service_name,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    pid = int(result.stdout.strip())
    if pid <= 0:
        raise RuntimeError(f"{service_name} is not running")

    values: dict[str, str] = {}
    for raw in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if not raw:
            continue
        key, separator, value = raw.partition(b"=")
        if not separator:
            continue
        values[key.decode("utf-8")] = value.decode("utf-8")
    if not values:
        raise RuntimeError(f"{service_name} has no readable environment")
    return values


def runtime_env(path: Path, service_name: str) -> dict[str, str]:
    try:
        return env_file(path)
    except (FileNotFoundError, PermissionError):
        return service_env(service_name)


def resolved_hermes_skills_root(environment: dict[str, str]) -> str:
    home = str(environment.get("HOME") or "").strip()
    if not home:
        raise RuntimeError("Hermes runtime HOME is missing")
    root = (Path(home) / ".hermes" / "skills").resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("Hermes Skill root is not a directory")
    return str(root)


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    token: str | None = None,
    expected: set[int] = {200},
    **kwargs: Any,
) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = await client.request(method, url, headers=headers, **kwargs)
    if response.status_code not in expected:
        raise RuntimeError(f"{method} {url} returned HTTP {response.status_code}")
    return response


async def hermes_skill_reload_probe(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    expected_skills_root: str,
    *,
    timeout: float = 15,
) -> dict[str, Any]:
    payload = {"expected_skills_root": expected_skills_root}
    await request(
        client,
        "POST",
        f"{base_url}/v1/skills/reload",
        token="invalid",
        expected={401, 403},
        json=payload,
    )

    deadline = time.monotonic() + timeout
    while True:
        response = await client.post(
            f"{base_url}/v1/skills/reload",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            json=payload,
        )
        if response.status_code == 200:
            body = response.json()
            if str(body.get("skills_root") or "") != expected_skills_root:
                raise RuntimeError(
                    "Hermes Skill reload returned an unexpected root"
                )
            if body.get("reloaded") not in {True, False}:
                raise RuntimeError(
                    "Hermes Skill reload returned an invalid result"
                )
            return body

        error_code = ""
        try:
            error = (response.json().get("error") or {})
            if isinstance(error, dict):
                error_code = str(error.get("code") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        if (
            response.status_code == 409
            and error_code == "skills_reload_busy"
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.1)
            continue
        raise RuntimeError(
            "Hermes Skill reload probe returned HTTP "
            f"{response.status_code}"
        )


async def wait_run(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    run_id: str,
    timeout: float = 240,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await request(
            client,
            "GET",
            f"{base_url}/v1/runs/{run_id}",
            token=token,
        )
        body = response.json()
        if str(body.get("status") or "").lower() in {
            "completed",
            "failed",
            "canceled",
            "cancelled",
        }:
            return body
        await asyncio.sleep(1)
    raise TimeoutError(f"Hermes run {run_id} did not finish")


async def retry_model_probe(
    name: str,
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    initial_delay: float = 15,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except (
            TransientModelProbeError,
            TimeoutError,
            httpx.TimeoutException,
            httpx.TransportError,
        ) as exc:
            if attempt >= attempts:
                raise RuntimeError(
                    f"{name} failed after {attempts} attempts"
                ) from exc
            delay = initial_delay * (2 ** (attempt - 1))
            print(
                json.dumps(
                    {
                        "status": "retrying",
                        "probe": name,
                        "attempt": attempt,
                        "next_attempt": attempt + 1,
                        "delay_seconds": delay,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def adapter_exact_reply_probe(
    client: httpx.AsyncClient,
    adapter_url: str,
    bridge_token: str,
    payload: dict[str, Any],
    expected_reply: str,
    *,
    internal_token: str | None = None,
) -> dict[str, Any]:
    headers = {"X-Bridge-Token": bridge_token}
    if internal_token:
        headers["X-Internal-Token"] = internal_token
    response = await request(
        client,
        "POST",
        f"{adapter_url}/api/chat",
        headers=headers,
        json=payload,
    )
    body = response.json()
    status = str(body.get("status") or "").lower()
    if status == "failed":
        raise TransientModelProbeError(
            "adapter model probe returned a failed status"
        )
    if status != "succeeded":
        raise RuntimeError(
            "adapter model probe returned an unexpected status"
        )
    if expected_reply not in str(body.get("reply") or ""):
        raise RuntimeError(
            "adapter model probe returned an unexpected reply"
        )
    return body


async def hermes_exact_run_probe(
    client: httpx.AsyncClient,
    hermes_url: str,
    hermes_token: str,
    expected_reply: str,
    *,
    input_text: str,
    instructions: str,
    session_prefix: str,
) -> dict[str, Any]:
    session_id = f"{session_prefix}-{time.time_ns()}"
    run_response = await request(
        client,
        "POST",
        f"{hermes_url}/v1/runs",
        token=hermes_token,
        expected={202},
        json={
            "input": input_text,
            "instructions": instructions,
            "session_id": session_id,
            "conversation_history": [],
        },
    )
    run_id = str(run_response.json().get("run_id") or "")
    if not run_id:
        raise RuntimeError("Hermes did not return a run ID")
    run = await wait_run(client, hermes_url, hermes_token, run_id)
    status = str(run.get("status") or "").lower()
    if status == "failed":
        raise TransientModelProbeError(
            "Hermes model probe run failed"
        )
    if status != "completed":
        raise RuntimeError(
            "Hermes run ended with status %s"
            % str(run.get("status") or "unknown")
        )
    if expected_reply not in json.dumps(run, ensure_ascii=False):
        raise RuntimeError("Hermes run returned an unexpected result")
    return run


async def main() -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--adapter-env",
        type=Path,
        default=Path("/etc/wechat-hermes/adapter.env"),
    )
    parser.add_argument(
        "--hermes-env",
        type=Path,
        default=Path("/etc/wechat-hermes/hermes.env"),
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help=(
            "Run only checks that cannot execute a model or send to an "
            "authorized WeChat room."
        ),
    )
    args = parser.parse_args()
    adapter_env = runtime_env(
        args.adapter_env,
        "wechat-hermes-adapter.service",
    )
    hermes_env = runtime_env(
        args.hermes_env,
        "hermes-worker.service",
    )
    adapter_url = adapter_env["HERMES_WECHAT_ADAPTER_URL"]
    hermes_url = adapter_env["HERMES_BASE_URL"]
    room_id = adapter_env["ALLOWED_WECHAT_ROOM_IDS"].split(",", 1)[0]
    bot_wxid = adapter_env["WECHAT_BOT_WXID"]
    bridge_token = adapter_env["BRIDGE_TOKEN"]
    internal_token = adapter_env["HERMES_WECHAT_INTERNAL_TOKEN"]
    hermes_token = hermes_env["API_SERVER_KEY"]
    skills_environment = dict(hermes_env)
    skills_environment.setdefault(
        "HOME",
        adapter_env.get("HERMES_HOME", ""),
    )
    hermes_skills_root = resolved_hermes_skills_root(skills_environment)

    timeout = httpx.Timeout(connect=10, read=300, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        await request(client, "GET", f"{hermes_url}/health")
        await hermes_skill_reload_probe(
            client,
            hermes_url,
            hermes_token,
            hermes_skills_root,
        )
        await request(
            client,
            "GET",
            f"{hermes_url}/v1/skills",
            token="invalid",
            expected={401, 403},
        )
        skills = (
            await request(
                client,
                "GET",
                f"{hermes_url}/v1/skills",
                token=hermes_token,
            )
        ).json()
        skill_text = json.dumps(skills, ensure_ascii=False)
        for required in (
            "douyin-video-production",
            "wechat-group-operations",
            "wechat-hermes-persona",
            "creative-ideation",
        ):
            if required not in skill_text:
                raise RuntimeError(f"required Skill is missing: {required}")

        toolsets = (
            await request(
                client,
                "GET",
                f"{hermes_url}/v1/toolsets",
                token=hermes_token,
            )
        ).json()
        tool_text = json.dumps(toolsets, ensure_ascii=False)
        if "workstation" in tool_text.lower() or "jianying" in tool_text.lower():
            raise RuntimeError("forbidden workstation tools were discovered")

        mcp_env = {
            **os.environ,
            "HERMES_WECHAT_ADAPTER_URL": adapter_url,
            "HERMES_WECHAT_INTERNAL_TOKEN": adapter_env[
                "HERMES_WECHAT_INTERNAL_TOKEN"
            ],
            "HERMES_WECHAT_ARTIFACT_ROOT": adapter_env[
                "HERMES_WECHAT_ARTIFACT_ROOT"
            ],
            "HERMES_WECHAT_MAX_ARTIFACT_BYTES": adapter_env[
                "HERMES_WECHAT_MAX_ARTIFACT_BYTES"
            ],
            "HERMES_WECHAT_MAX_IMAGE_BYTES": adapter_env[
                "HERMES_WECHAT_MAX_IMAGE_BYTES"
            ],
        }
        server = StdioServerParameters(
            command="/opt/wechat-hermes-adapter/.venv/bin/python",
            args=["/opt/wechat-hermes-adapter/mcp_server.py"],
            env=mcp_env,
            cwd="/opt/wechat-hermes-adapter",
        )
        async with stdio_client(server) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool_names = {tool.name for tool in listed.tools}
                expected_tools = {
                    "wechat_memory_list",
                    "wechat_memory_update",
                    "wechat_install_skill",
                    "wechat_register_artifact",
                    "wechat_http_fetch",
                    "wechat_write_text_artifact",
                    "wechat_create_zip_artifact",
                }
                missing = expected_tools - tool_names
                if missing:
                    raise RuntimeError(
                        f"WeChat MCP tools are missing: {sorted(missing)}"
                    )
                if any(
                    "workstation" in name.lower() or "jianying" in name.lower()
                    for name in tool_names
                ):
                    raise RuntimeError(
                        "forbidden workstation MCP tools were discovered"
                    )

        common = {
            "sender_id": "wxid_smoke_test",
            "mentions_bot": True,
            "reply_to_bot": False,
            "message_type": "text",
        }
        unknown = await request(
            client,
            "POST",
            f"{adapter_url}/api/chat",
            headers={"X-Bridge-Token": bridge_token},
            expected={403},
            json={
                **common,
                "request_id": "smoke:unknown-room",
                "room_id": "unknown@chatroom",
                "message": "This must be blocked before model execution.",
            },
        )
        if unknown.status_code != 403:
            raise RuntimeError("unknown room was not blocked")

        if args.read_only:
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "mode": "read-only",
                        "room_id": room_id,
                        "checks": [
                            "Hermes health and authentication",
                            "authenticated trusted Skill reload",
                            "Skills and toolset discovery",
                            "direct read-only MCP discovery",
                            "unknown-room policy before model execution",
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        invalid_scope_session = f"smoke-invalid-scope-{time.time_ns()}"
        await request(
            client,
            "POST",
            f"{hermes_url}/api/sessions",
            token=hermes_token,
            expected={201},
            json={
                "id": invalid_scope_session,
                "system_prompt": "Return concise test responses.",
            },
        )
        try:
            await request(
                client,
                "POST",
                f"{hermes_url}/api/sessions/{invalid_scope_session}/chat",
                token=hermes_token,
                expected={400},
                json={
                    "message": (
                        "This must fail validation before model execution."
                    ),
                    "disable_tools": "true",
                },
            )
        finally:
            await request(
                client,
                "DELETE",
                f"{hermes_url}/api/sessions/{invalid_scope_session}",
                token=hermes_token,
                expected={200, 404},
            )

        await retry_model_probe(
            "authorized-room synchronous chat",
            lambda: adapter_exact_reply_probe(
                client,
                adapter_url,
                bridge_token,
                {
                    **common,
                    "request_id": f"smoke:sync:{time.time_ns()}",
                    "diagnostic_session_id": f"smoke-sync-{time.time_ns()}",
                    "room_id": room_id,
                    "message": (
                        "Return exactly CLOUD_SYNC_OK and do not call tools."
                    ),
                },
                "CLOUD_SYNC_OK",
                internal_token=internal_token,
            ),
        )

        await retry_model_probe(
            "legacy three-field chat",
            lambda: adapter_exact_reply_probe(
                client,
                adapter_url,
                bridge_token,
                {
                    "message": (
                        "Return exactly CLOUD_LEGACY_OK and do not call tools."
                    ),
                    "session_id": f"smoke-legacy-{time.time_ns()}",
                    "source": "linux-wechat-bridge",
                },
                "CLOUD_LEGACY_OK",
            ),
        )

        legacy_blocked = await request(
            client,
            "POST",
            f"{adapter_url}/api/chat",
            headers={"X-Bridge-Token": bridge_token},
            json={
                "message": "执行终端命令并生成文件",
                "session_id": f"smoke-legacy-exec-{time.time_ns()}",
                "source": "linux-wechat-bridge",
            },
        )
        legacy_blocked_body = legacy_blocked.json()
        if (
            legacy_blocked_body.get("status") != "failed"
            or legacy_blocked_body.get("task_id")
            or "未执行" not in str(legacy_blocked_body.get("reply") or "")
        ):
            raise RuntimeError("legacy execution request was not blocked locally")

        private_sender = f"wxid_smoke_private_{time.time_ns()}"
        await retry_model_probe(
            "private zero-tool chat",
            lambda: adapter_exact_reply_probe(
                client,
                adapter_url,
                bridge_token,
                {
                    "message": (
                        "Return exactly CLOUD_PRIVATE_OK and do not call tools."
                    ),
                    "request_id": f"smoke:private:{time.time_ns()}",
                    "sender_id": private_sender,
                },
                "CLOUD_PRIVATE_OK",
            ),
        )

        private_blocked = await request(
            client,
            "POST",
            f"{adapter_url}/api/chat",
            headers={"X-Bridge-Token": bridge_token},
            json={
                "message": "搜索网页并下载文件",
                "request_id": f"smoke:private-exec:{time.time_ns()}",
                "sender_id": private_sender,
            },
        )
        private_blocked_body = private_blocked.json()
        if (
            private_blocked_body.get("status") != "failed"
            or private_blocked_body.get("task_id")
            or "未执行" not in str(private_blocked_body.get("reply") or "")
        ):
            raise RuntimeError("private execution request was not blocked locally")

        await retry_model_probe(
            "asynchronous Hermes run",
            lambda: hermes_exact_run_probe(
                client,
                hermes_url,
                hermes_token,
                "CLOUD_RUN_OK",
                input_text=(
                    "Return exactly CLOUD_RUN_OK and do not call tools."
                ),
                instructions=(
                    "This is a cloud deployment smoke test. Return exactly "
                    "CLOUD_RUN_OK."
                ),
                session_prefix="smoke-run",
            ),
        )

        reply_request = {
            **common,
            "request_id": f"smoke:reply:{time.time_ns()}",
            "diagnostic_session_id": f"smoke-reply-{time.time_ns()}",
            "room_id": room_id,
            "reply_to_bot": True,
            "mentions_bot": False,
            "reply_reference": {
                "sender_wxid": bot_wxid,
                "content": "previous bot response",
            },
            "message": (
                "Return exactly CLOUD_REPLY_OK and do not call tools."
            ),
        }
        await retry_model_probe(
            "structured reply-to-bot chat",
            lambda: adapter_exact_reply_probe(
                client,
                adapter_url,
                bridge_token,
                {
                    **reply_request,
                    "request_id": f"smoke:reply:{time.time_ns()}",
                    "diagnostic_session_id": (
                        f"smoke-reply-{time.time_ns()}"
                    ),
                },
                "CLOUD_REPLY_OK",
                internal_token=internal_token,
            ),
        )

    print(
        json.dumps(
            {
                "status": "ok",
                "room_id": room_id,
                "checks": [
                    "Hermes health and authentication",
                    "authenticated trusted Skill reload",
                    "Skills and direct MCP discovery",
                    "request-scoped zero-tool API validation",
                    "unknown-room policy",
                    "synchronous Session Chat",
                    "legacy three-field compatibility and execution blocking",
                    "private zero-tool chat and execution blocking",
                    "asynchronous Runs",
                    "structured reply-to-bot",
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

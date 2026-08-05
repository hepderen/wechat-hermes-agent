from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from scripts import smoke_cloud
from scripts.smoke_events import wait_run_tool_success


EXPECTED_TOOL = "mcp__wechat_production__wechat_memory_list"


class FakeStreamResponse:
    status_code = 200
    text = ""

    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_lines(self):
        for event in self.events:
            yield "data: " + json.dumps(event)


class FakeClient:
    def __init__(self, events):
        self.events = events

    def stream(self, *_args, **_kwargs):
        return FakeStreamResponse(self.events)


class FakeRequestResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class FakeRequestClient:
    def __init__(self, response):
        self.response = response

    async def request(self, *_args, **_kwargs):
        return self.response


def run_probe(events):
    return asyncio.run(
        wait_run_tool_success(
            FakeClient(events),
            "http://127.0.0.1:8642",
            "token",
            "run-test",
            EXPECTED_TOOL,
        )
    )


def test_model_mcp_probe_requires_successful_tool_event():
    terminal = run_probe(
        [
            {
                "event": "tool.started",
                "tool": EXPECTED_TOOL,
            },
            {
                "event": "tool.completed",
                "tool": EXPECTED_TOOL,
                "error": False,
            },
            {
                "event": "run.completed",
                "output": "MCP_MODEL_TOOL_OK",
            },
        ]
    )
    assert terminal["output"] == "MCP_MODEL_TOOL_OK"


def test_model_mcp_probe_rejects_tool_error():
    with pytest.raises(RuntimeError, match="tool failed"):
        run_probe(
            [
                {
                    "event": "tool.completed",
                    "tool": EXPECTED_TOOL,
                    "error": True,
                }
            ]
        )


def test_model_mcp_probe_rejects_unexpected_tool():
    with pytest.raises(RuntimeError, match="unexpected tool"):
        run_probe(
            [
                {
                    "event": "tool.started",
                    "tool": "mcp__wechat_production__wechat_send_text",
                }
            ]
        )


def test_smoke_request_error_redacts_response_body():
    secret = "Bearer cloud-secret-token"
    client = FakeRequestClient(FakeRequestResponse(500, secret))
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(
            smoke_cloud.request(
                client,
                "GET",
                "http://127.0.0.1:8642/health",
            )
        )
    assert str(caught.value).endswith("returned HTTP 500")
    assert secret not in str(caught.value)


def test_model_mcp_probe_redacts_failed_event_payload():
    secret = "internal-tool-output"
    with pytest.raises(RuntimeError) as caught:
        run_probe(
            [
                {
                    "event": "tool.completed",
                    "tool": EXPECTED_TOOL,
                    "error": True,
                    "output": secret,
                }
            ]
        )
    assert secret not in str(caught.value)


def test_read_only_smoke_returns_before_model_and_allowed_room_probes():
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "smoke_cloud.py"
    ).read_text(encoding="utf-8")

    read_only_guard = source.index("if args.read_only:")
    allowed_room_probe = source.index(
        'await retry_model_probe(\n'
        '            "authorized-room synchronous chat"'
    )
    scope_validation = source.index("invalid_scope_session =")
    reload_probe = source.index("await hermes_skill_reload_probe(")

    assert reload_probe < read_only_guard
    assert read_only_guard < allowed_room_probe
    assert read_only_guard < scope_validation
    assert "--read-only" in source
    assert 'cwd="/opt/wechat-hermes-adapter"' in source
    assert 'adapter_env.get("HERMES_HOME", "")' in source
    assert "authenticated trusted Skill reload" in source
    assert "direct read-only MCP discovery" in source
    assert "wechat_list_tasks" not in source


def test_full_smoke_covers_restricted_chat_scopes():
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "smoke_cloud.py"
    ).read_text(encoding="utf-8")

    assert "CLOUD_LEGACY_OK" in source
    assert "CLOUD_PRIVATE_OK" in source
    assert "legacy execution request was not blocked locally" in source
    assert "private execution request was not blocked locally" in source
    assert '"disable_tools": "true"' in source


def test_scope_validation_session_is_repeatable_and_cleaned_up():
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "smoke_cloud.py"
    ).read_text(encoding="utf-8")

    scope_start = source.index("invalid_scope_session =")
    scope_end = source.index(
        'await retry_model_probe(\n'
        '            "authorized-room synchronous chat"'
    )
    scope_probe = source[scope_start:scope_end]

    assert '"title": "API scope validation smoke"' not in scope_probe
    assert '"DELETE"' in scope_probe
    assert "{hermes_url}/api/sessions/{invalid_scope_session}" in scope_probe
    assert "finally:" in scope_probe


def test_allowed_room_smoke_uses_internal_isolated_diagnostics():
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "smoke_cloud.py"
    ).read_text(encoding="utf-8")

    assert '"diagnostic_session_id": f"smoke-sync-' in source
    assert '"diagnostic_session_id": f"smoke-reply-' in source
    assert "internal_token=internal_token" in source
    assert 'status == "failed"' in source
    assert 'status != "succeeded"' in source


def test_model_probe_retries_transient_failures_with_backoff():
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise smoke_cloud.TransientModelProbeError("temporary")
        return "ok"

    with patch.object(
        smoke_cloud.asyncio,
        "sleep",
        new_callable=AsyncMock,
    ) as sleep:
        result = asyncio.run(
            smoke_cloud.retry_model_probe(
                "test probe",
                operation,
                attempts=3,
                initial_delay=2,
            )
        )

    assert result == "ok"
    assert calls == 3
    assert [call.args[0] for call in sleep.await_args_list] == [2, 4]


def test_model_probe_does_not_retry_functional_failure():
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise RuntimeError("functional mismatch")

    with patch.object(
        smoke_cloud.asyncio,
        "sleep",
        new_callable=AsyncMock,
    ) as sleep:
        with pytest.raises(RuntimeError, match="functional mismatch"):
            asyncio.run(
                smoke_cloud.retry_model_probe(
                    "test probe",
                    operation,
                )
            )

    assert calls == 1
    sleep.assert_not_awaited()


def test_runtime_env_uses_service_process_when_file_is_protected(tmp_path):
    protected = tmp_path / "adapter.env"

    with patch.object(
        smoke_cloud,
        "env_file",
        side_effect=PermissionError("protected"),
    ), patch.object(
        smoke_cloud,
        "service_env",
        return_value={"BRIDGE_TOKEN": "runtime-token"},
    ) as process_env:
        values = smoke_cloud.runtime_env(
            protected,
            "wechat-hermes-adapter.service",
        )

    assert values == {"BRIDGE_TOKEN": "runtime-token"}
    process_env.assert_called_once_with("wechat-hermes-adapter.service")


def test_resolved_hermes_skills_root_uses_runtime_home(tmp_path):
    skills = tmp_path / ".hermes" / "skills"
    skills.mkdir(parents=True)

    assert smoke_cloud.resolved_hermes_skills_root(
        {"HOME": str(tmp_path)}
    ) == str(skills.resolve())


def test_skill_reload_probe_checks_authentication_and_runtime_root(tmp_path):
    expected_root = str(tmp_path.resolve())
    requests = []

    def handler(request):
        requests.append(request)
        if request.headers["Authorization"] == "Bearer invalid":
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        return httpx.Response(
            200,
            json={
                "object": "hermes.skills.reload",
                "skills_root": expected_root,
                "reloaded": False,
                "count": 3,
            },
        )

    async def probe():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await smoke_cloud.hermes_skill_reload_probe(
                client,
                "http://127.0.0.1:8642",
                "valid-token",
                expected_root,
            )

    result = asyncio.run(probe())

    assert result["skills_root"] == expected_root
    assert [request.headers["Authorization"] for request in requests] == [
        "Bearer invalid",
        "Bearer valid-token",
    ]
    assert all(
        json.loads(request.content)["expected_skills_root"] == expected_root
        for request in requests
    )

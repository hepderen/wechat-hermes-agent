from __future__ import annotations

import json
import time
from typing import Any

import httpx


async def wait_run_tool_success(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    run_id: str,
    expected_tool: str,
    timeout: float = 240,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    completed = False
    terminal: dict[str, Any] | None = None
    async with client.stream(
        "GET",
        f"{base_url}/v1/runs/{run_id}/events",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    ) as response:
        if response.status_code != 200:
            raise RuntimeError(
                f"GET {base_url}/v1/runs/{run_id}/events returned "
                f"HTTP {response.status_code}"
            )
        async for line in response.aiter_lines():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Hermes run {run_id} did not finish")
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            event_name = str(event.get("event") or event.get("type") or "")
            tool_name = str(event.get("tool") or "")
            if event_name.startswith("tool.") and tool_name != expected_tool:
                raise RuntimeError(
                    f"Hermes called an unexpected tool during MCP probe: {tool_name}"
                )
            if event_name == "tool.completed" and tool_name == expected_tool:
                if bool(event.get("error")):
                    raise RuntimeError(
                        f"Hermes MCP tool failed during probe: {tool_name}"
                    )
                completed = True
            if event_name in {
                "run.completed",
                "run.failed",
                "run.canceled",
                "run.cancelled",
            }:
                terminal = event
                break

    if not completed:
        raise RuntimeError(
            f"Hermes run {run_id} completed without a successful "
            f"{expected_tool} invocation"
        )
    if terminal is None or terminal.get("event") != "run.completed":
        terminal_event = (
            str(terminal.get("event") or "missing")
            if isinstance(terminal, dict)
            else "missing"
        )
        raise RuntimeError(
            f"Hermes MCP probe ended with event: {terminal_event}"
        )
    return terminal

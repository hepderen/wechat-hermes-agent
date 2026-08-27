#!/usr/bin/env python3
"""Run one isolated model-level research task through a candidate Gateway."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

import httpx


TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled", "run.canceled"}


async def run(args) -> dict:
    headers = {
        "Authorization": "Bearer " + args.api_key,
        "Accept": "application/json",
    }
    timeout = httpx.Timeout(connect=10, read=args.timeout, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(
            args.base_url.rstrip("/") + "/v1/runs",
            headers={**headers, "Idempotency-Key": args.idempotency_key},
            json={
                "input": (
                    "Use web_search to find the official Python documentation, then use "
                    "web_extract to read at least two official Python sources. Return a concise "
                    "Chinese answer containing at least two source URLs."
                ),
                "instructions": (
                    "This is an isolated search acceptance probe. You must call web_search and "
                    "web_extract. Do not call terminal, file, browser, MCP, memory, messaging, "
                    "or scheduling tools. Do not claim success without tool results."
                ),
                "session_id": args.session_id,
                "conversation_history": [],
                "idempotency_key": args.idempotency_key,
                "enabled_toolsets": ["web"],
            },
        )
        response.raise_for_status()
        run_id = str(response.json().get("run_id") or "")
        if not run_id:
            raise RuntimeError("candidate Gateway returned no run_id")

        events = []
        async with client.stream(
            "GET",
            args.base_url.rstrip("/") + "/v1/runs/" + run_id + "/events",
            headers=headers,
        ) as stream:
            stream.raise_for_status()
            async for line in stream.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                event = json.loads(raw)
                events.append(event)
                if event.get("event") in TERMINAL_EVENTS:
                    break

        status_response = await client.get(
            args.base_url.rstrip("/") + "/v1/runs/" + run_id,
            headers=headers,
        )
        status_response.raise_for_status()
        status = status_response.json()

    if status.get("status") != "completed":
        raise RuntimeError("candidate research run did not complete: %s" % status.get("status"))
    started_tools = [
        str(event.get("tool") or "")
        for event in events
        if event.get("event") == "tool.started"
    ]
    completed_tools = [
        str(event.get("tool") or "")
        for event in events
        if event.get("event") == "tool.completed"
    ]
    if "web_search" not in completed_tools:
        raise RuntimeError("model run produced no successful web_search evidence")
    if "web_extract" not in completed_tools:
        raise RuntimeError("model run produced no successful web_extract evidence")
    forbidden = {
        "terminal",
        "execute_code",
        "write_file",
        "browser_navigate",
        "wechat_register_artifact",
    }
    if forbidden.intersection(completed_tools):
        raise RuntimeError("model run used a forbidden acceptance-probe tool")
    if forbidden.intersection(started_tools):
        raise RuntimeError("model run started a forbidden acceptance-probe tool")
    extracted_sources = set()
    for event in events:
        if event.get("event") != "tool.completed" or event.get("tool") != "web_extract":
            continue
        for value in str(event.get("source") or "").split(","):
            if value.strip().startswith(("http://", "https://")):
                extracted_sources.add(value.strip())
    if len(extracted_sources) < 2:
        raise RuntimeError("model run recorded fewer than two extracted sources")
    output = str(status.get("output") or "")
    if output.count("http") < 2:
        raise RuntimeError("candidate result did not contain at least two source URLs")

    return {
        "ok": True,
        "run_id": run_id,
        "status": status.get("status"),
        "completed_tools": completed_tools,
        "started_tools": started_tools,
        "extracted_source_count": len(extracted_sources),
        "event_count": len(events),
        "output_chars": len(output),
        "source_url_count_lower_bound": output.count("http"),
        "finished_at": int(time.time()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18642")
    parser.add_argument("--api-key")
    parser.add_argument("--api-key-env", default="API_SERVER_KEY")
    parser.add_argument("--session-id", default="candidate-web-research-probe")
    parser.add_argument("--idempotency-key", default="candidate-web-research-probe-v1")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    args.api_key = str(args.api_key or os.getenv(args.api_key_env) or "").strip()
    if not args.api_key:
        parser.error("API key is required through --api-key or --api-key-env")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

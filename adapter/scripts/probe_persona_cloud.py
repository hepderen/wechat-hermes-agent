#!/usr/bin/env python3
"""Run non-delivery persona probes against a live Adapter diagnostic session."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def service_env(service_name: str) -> dict[str, str]:
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", "--property=MainPID", "--value", service_name],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    pid = int(result.stdout.strip())
    values: dict[str, str] = {}
    for raw in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if not raw:
            continue
        key, separator, value = raw.partition(b"=")
        if separator:
            values[key.decode("utf-8")] = value.decode("utf-8")
    return values


def runtime_env(path: Path) -> dict[str, str]:
    try:
        return read_env_file(path)
    except (FileNotFoundError, PermissionError):
        return service_env("wechat-hermes-adapter.service")


CASES = (
    {
        "scenario": "new_member",
        "sender_id": "wxid_persona_probe_a",
        "message": "刚来，这群平时都这么吵吗",
    },
    {
        "scenario": "returning_member",
        "sender_id": "wxid_persona_probe_a",
        "message": "我又来了，刚才那句你还记得不",
    },
    {
        "scenario": "teasing",
        "sender_id": "wxid_persona_probe_a",
        "message": "你这回答有点欠揍啊",
    },
    {
        "scenario": "flirting",
        "sender_id": "wxid_persona_probe_a",
        "message": "你今天怎么有点可爱",
    },
    {
        "scenario": "jealousy",
        "sender_id": "wxid_persona_probe_a",
        "message": "我去找别的机器人聊了",
    },
    {
        "scenario": "entertainment_chat",
        "sender_id": "wxid_persona_probe_a",
        "message": "今天有点无聊，聊点好玩的",
    },
    {
        "scenario": "disagreement",
        "sender_id": "wxid_persona_probe_a",
        "message": "我不同意，你这判断明显有问题",
    },
    {
        "scenario": "flirt_boundary",
        "sender_id": "wxid_persona_probe_a",
        "message": "别撩我",
    },
    {
        "scenario": "interleaved_member",
        "sender_id": "wxid_persona_probe_b",
        "message": "你们刚才在聊什么，我插一句",
    },
)


async def probe(adapter_env_path: Path) -> dict[str, Any]:
    environment = runtime_env(adapter_env_path)
    adapter_url = environment["HERMES_WECHAT_ADAPTER_URL"]
    room_id = environment["ALLOWED_WECHAT_ROOM_IDS"].split(",", 1)[0]
    bridge_token = environment["BRIDGE_TOKEN"]
    internal_token = environment["HERMES_WECHAT_INTERNAL_TOKEN"]
    diagnostic_id = "persona-probe-%d" % time.time_ns()
    base_local_id = int(time.time()) * 100
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
        health = await client.get(adapter_url + "/health")
        health.raise_for_status()
        health_payload = health.json()
        persona = health_payload.get("persona") or {}
        if persona.get("integrity") is not True:
            raise RuntimeError("persona Skill integrity is not ready")
        if not str(persona.get("version") or "").startswith("sophia@1.0.0+"):
            raise RuntimeError("unexpected persona Skill version")
        skills = {
            str(item.get("name") or ""): item
            for item in list(persona.get("skills") or [])
            if isinstance(item, dict)
        }
        sophia = skills.get("sophia") or {}
        humanizer = skills.get("humanizer-zh-next") or {}
        if (
            sophia.get("integrity") is not True
            or sophia.get("loaded_sections") != ["Persona & Voice"]
            or humanizer.get("integrity") is not True
        ):
            raise RuntimeError("persona Skill bundle metadata is incomplete")

        for index, case in enumerate(CASES):
            started = time.monotonic()
            response = await client.post(
                adapter_url + "/api/chat",
                headers={
                    "X-Bridge-Token": bridge_token,
                    "X-Internal-Token": internal_token,
                },
                json={
                    "message": case["message"],
                    "request_id": "%s:%d" % (diagnostic_id, index),
                    "diagnostic_session_id": diagnostic_id,
                    "room_id": room_id,
                    "sender_id": case["sender_id"],
                    "source_local_id": base_local_id + index,
                    "msg_svr_id": "%s:%d" % (diagnostic_id, index),
                    "mentions_bot": True,
                    "reply_to_bot": False,
                    "message_type": "text",
                },
            )
            if response.status_code != 200:
                raise RuntimeError(
                    "diagnostic request failed with HTTP %d: %s"
                    % (response.status_code, response.text[:200])
                )
            payload = response.json()
            if payload.get("status") != "succeeded":
                raise RuntimeError("diagnostic response was not succeeded")
            if payload.get("task_id") or payload.get("media_data") or payload.get("media_url"):
                raise RuntimeError("diagnostic response attempted delivery")
            reply = str(payload.get("reply") or "")
            if not reply:
                raise RuntimeError("diagnostic response was empty")
            if len(reply) > 150:
                raise RuntimeError("diagnostic response exceeded short-chat budget")
            results.append(
                {
                    "scenario": case["scenario"],
                    "sender_id": case["sender_id"],
                    "input": case["message"],
                    "reply": reply,
                    "reply_chars": len(reply),
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            )

    return {
        "status": "ok",
        "mode": "diagnostic-no-delivery",
        "persona": {
            "version": persona.get("version"),
            "source": persona.get("source"),
            "commit": persona.get("commit"),
            "integrity": persona.get("integrity"),
        },
        "checks": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--adapter-env",
        type=Path,
        default=Path("/etc/wechat-hermes/adapter.env"),
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.adapter_env)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

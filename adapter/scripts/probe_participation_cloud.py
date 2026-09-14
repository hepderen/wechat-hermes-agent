"""Exercise the real Adapter with synthetic members and an inert sending client.

Run explicitly with --live-model to use the configured loopback Hermes worker.
All Adapter storage is temporary; no requests reach the WeChat sending API.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app.config import Settings
from app.main import build_runtime, create_app, deliver_group_followup


class InertSender:
    sends = 0

    def __init__(self):
        self.messages = []
        self.fake_deliveries = []

    async def group_messages_after(self, *_args):
        return self.messages

    async def send_text_item(self, room, text, request_id, **kwargs):
        self.fake_deliveries.append((room, text, request_id))
        return {"status": "sent"}

    async def commit_barrier(self, *_args, **_kwargs):
        return {"ok": True}

    def __getattr__(self, name):
        raise AssertionError("unexpected sender operation: " + name)


def protected_state():
    paths = (
        Path("/home/ubuntu/linux-wechat-bot/db-state.json"),
        Path("/home/ubuntu/.cache/wechat-chat-api/send-state.json"),
        Path("/opt/wechat-ai-bot/data/bot.db"),
    )
    return {path.name: (path.stat().st_ino, hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-model", action="store_true", required=True)
    parser.add_argument("--env-file", type=Path, default=Path("/etc/wechat-hermes/adapter.env"))
    args = parser.parse_args()
    for line in args.env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.startswith("#"):
            os.environ[key] = value
    before = protected_state()
    baseline_pid = Path("/proc/97414/stat").read_text().split()[21]
    room = "participation-probe@chatroom"
    with tempfile.TemporaryDirectory(prefix="wechat-participation-probe-") as tmp:
        root = Path(tmp)
        settings = replace(
            Settings.from_env(), allowed_room_ids=frozenset({room}),
            database_path=root / "adapter.db", artifact_root=root / "artifacts",
            cleanup_status_path=root / "cleanup.json", group_participation_enabled=True,
            group_listener_enabled=True, group_listener_min_reply_gap_seconds=6,
            group_listener_min_turns_between_replies=2, sync_chat_timeout_seconds=25,
            group_participation_limit=8, daily_cost_limit_usd=5,
        )
        runtime = build_runtime(settings)
        runtime.chat_api = InertSender()
        original_chat = runtime.hermes.chat
        model_calls = []

        async def checked_chat(*args, **kwargs):
            assert kwargs.get("disable_tools") is True
            assert "不必等别人@你" in args[2]
            assert "room_id" not in args[2]
            model_calls.append(1)
            return await original_chat(*args, **kwargs)

        runtime.hermes.chat = checked_chat
        results = []
        with TestClient(create_app(runtime, start_worker=False)) as client:
            def send(n, message, sender="alice", name="阿明", **fields):
                # Simulate human typing intervals without waiting between test cases.
                state = runtime.store.get_group_listener_state(room)
                if state and state.get("last_reply_local_id"):
                    runtime.store.mark_group_listener_reply(room, state["last_reply_local_id"], now=time.time() - 8)
                payload = dict(message=message, request_id="probe-%d" % n, room_id=room,
                    sender_id=sender, sender_name=name, source_local_id=n, msg_svr_id=str(n),
                    timestamp=time.time(), **fields)
                started = time.monotonic()
                response = client.post("/api/chat", json=payload, headers={"X-Bridge-Token": settings.bridge_token})
                assert response.status_code == 200, response.status_code
                data = response.json()
                assert not any(data["reply"].endswith(suffix) for suffix in (
                    "计划群", "娱乐代理", "<|im_end|>",
                )), "unexpected provider tail"
                result = {"case": n, "status": data["status"], "reply": data["reply"],
                          "seconds": round(time.monotonic() - started, 2)}
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return data, payload

            first, payload = send(1, "今天连跪三把游戏，真服了")
            assert first["status"] == "succeeded" and first["reply"]
            again = client.post("/api/chat", json=payload, headers={"X-Bridge-Token": settings.bridge_token})
            assert again.json() == first and len(model_calls) == 1
            item = runtime.store.next_group_followup(now=time.time() + 15)
            if item:
                runtime.chat_api.messages = [{"local_id": 2, "is_self": True,
                                              "direction": "outgoing", "text": first["reply"]}]
                asyncio.run(deliver_group_followup(runtime, item))
            assert send(2, "队友全程挂机")[0]["status"] == "succeeded"
            assert send(3, "大家晚上想吃啥？", "bob", "小王")[0]["status"] == "succeeded"
            assert send(4, "其实我想吃火锅", "bob", "小王")[0]["status"] == "succeeded"
            target, _ = send(6, "哈哈哈", group_context=[{
                "local_id": 5, "sender_id": "carol", "sender_name": "阿梨",
                "direction": "incoming", "timestamp": time.time(),
                "text": "第一次去成都，阿明小王你们说吃火锅，推荐哪家？",
            }])
            assert target["status"] == "succeeded"
            count = len(model_calls)
            assert send(7, "@阿梨 你哪天出发？", "bob", "小王")[0]["status"] == "ignored"
            assert len(model_calls) == count
            assert send(8, "刚刚订到了周六的票", "carol", "阿梨")[0]["status"] == "succeeded"
            assert send(9, "停止")[0]["status"] == "canceled"
            count = len(model_calls)
            assert send(10, "今天这个电影太离谱了")[0]["status"] == "ignored"
            assert len(model_calls) == count
            assert send(11, "小格，朋友说吃火锅必须全点素菜，你咋看", mentions_bot=True)[0]["status"] == "succeeded"
            assert send(12, "晚安")[0]["status"] == "ignored"
    after = protected_state()
    assert baseline_pid == Path("/proc/97414/stat").read_text().split()[21]
    assert before == after, "protected state changed during probes"
    print(json.dumps({"ok": True, "cases": len(results), "model_calls": len(model_calls),
                      "fake_followup_deliveries": len(runtime.chat_api.fake_deliveries),
                      "real_wechat_sends": 0, "protected_state_unchanged": True}), flush=True)


if __name__ == "__main__":
    main()

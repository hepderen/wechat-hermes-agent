from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from app.main import create_app, deliver_group_followup, split_group_followup
from app.store import AdapterStore
from tests.test_adapter import ROOM_ID, make_runtime, post_chat


def test_followup_marker_is_private_and_keeps_both_parts():
    assert split_group_followup("先回一个点。[[FOLLOW_UP]]突然想起来，周末去不去？") == (
        "先回一个点。", "突然想起来，周末去不去？"
    )
    assert split_group_followup("普通回复") == ("普通回复", "")


def test_followup_outbox_is_idempotent_and_crash_recovery_is_uncertain(tmp_path):
    store = AdapterStore(tmp_path / "state.db")
    assert store.prepare_group_followup(ROOM_ID, 7, "再补一句", main_text="第一句", now=1000)
    assert not store.prepare_group_followup(ROOM_ID, 7, "重复补一句", main_text="第一句", now=1000)
    item = store.next_group_followup(now=1015)
    assert item and item["status"] == "sending" and item["main_text"] == "第一句"
    restored = AdapterStore(tmp_path / "state.db")
    assert restored.next_group_followup(now=1016) is None
    with restored._connect() as connection:  # status is an explicit terminal audit fact
        row = connection.execute("SELECT status FROM group_followups").fetchone()
    assert row[0] == "uncertain"


def test_followup_waits_for_own_echo_and_sends_once(tmp_path):
    runtime = make_runtime(tmp_path, group_participation_enabled=True)
    runtime.store.prepare_group_followup(ROOM_ID, 7, "我突然想起来了，再补一句", main_text="第一句", now=time.time() - 20)
    class Chat:
        sent = []
        async def group_messages_after(self, _room, _source):
            return [{"local_id": 8, "is_self": True, "direction": "outgoing", "text": "第一句"}]
        async def send_text_item(self, room, text, request_id, **kwargs):
            self.sent.append((room, text, request_id, kwargs))
            return {"status": "sent"}
    runtime.chat_api = Chat()
    item = runtime.store.next_group_followup(now=time.time())
    asyncio.run(deliver_group_followup(runtime, item))
    assert len(runtime.chat_api.sent) == 1
    assert runtime.chat_api.sent[0][1] == "我突然想起来了，再补一句"
    assert runtime.store.next_group_followup(now=time.time() + 100) is None


def test_chat_turn_schedules_private_followup_only_for_passive_group_turn(tmp_path):
    runtime = make_runtime(
        tmp_path,
        group_listener_enabled=True,
        group_participation_enabled=True,
        group_listener_min_reply_gap_seconds=0,
        group_listener_min_turns_between_replies=1,
    )
    async def reply(*_args, **_kwargs):
        return "先说火锅。[[FOLLOW_UP]]突然想起来，周末要不要一起去？", {}
    runtime.hermes.chat = reply
    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(client, {
            "message": "周末去哪吃？", "request_id": "followup-passive",
            "room_id": ROOM_ID, "sender_id": "wxid_a", "sender_name": "阿明",
            "source_local_id": 12, "msg_svr_id": "12", "timestamp": time.time(),
        })
        with runtime.store._connect() as conn:
            assert conn.execute("SELECT status FROM group_followups").fetchone()[0] == "prepared"
        direct = post_chat(client, {
            "message": "小格再说说", "request_id": "followup-direct",
            "room_id": ROOM_ID, "sender_id": "wxid_a", "sender_name": "阿明",
            "source_local_id": 13, "msg_svr_id": "13", "timestamp": time.time(),
            "mentions_bot": True,
        })
    assert response.json()["reply"] == "先说火锅。"
    assert runtime.store.next_group_followup(now=time.time() + 20) is None
    assert "[[FOLLOW_UP]]" not in response.json()["reply"]
    assert direct.json()["reply"] == "先说火锅。"
    with runtime.store._connect() as conn:
        assert conn.execute("SELECT status FROM group_followups").fetchone()[0] == "suppressed"


def test_human_reply_invalidates_delayed_thought(tmp_path):
    runtime = make_runtime(tmp_path, group_participation_enabled=True)
    runtime.store.prepare_group_followup(ROOM_ID, 7, "补一句", main_text="第一句", now=time.time() - 20)
    async def messages(*args):
        return [{"local_id": 8, "is_self": True, "text": "第一句"},
                {"local_id": 9, "is_self": False, "direction": "incoming", "text": "有人接话了"}]
    runtime.chat_api.group_messages_after = messages
    item = runtime.store.next_group_followup()
    asyncio.run(deliver_group_followup(runtime, item))
    assert not runtime.chat_api.text
    with runtime.store._connect() as conn:
        assert conn.execute("SELECT status FROM group_followups").fetchone()[0] == "suppressed"


def test_followup_cap_and_stop_prevent_repeated_proactive_messages(tmp_path):
    store = AdapterStore(tmp_path / "state.db")
    assert store.prepare_group_followup(ROOM_ID, 1, "第一条", now=1000)
    assert store.prepare_group_followup(ROOM_ID, 2, "第二条", now=1001)
    assert not store.prepare_group_followup(ROOM_ID, 3, "第三条", now=1002)
    assert store.suppress_group_followups(ROOM_ID) == 2
    assert store.next_group_followup(now=1020) is None
    assert store.prepare_group_followup(ROOM_ID, 4, "新话题", now=1602)

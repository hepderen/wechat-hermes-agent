from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.participation import choose_participation
from app.store import AdapterStore
from tests.test_adapter import ROOM_ID, make_runtime, post_chat


def choose(message, *, sender="alice", state=None, timeline=(), timestamp=1000):
    return choose_participation(
        message, "text", ("小格",), state or {"turns_since_reply": 1}, timeline,
        sender_id=sender, sender_name="阿明", timestamp=timestamp, now=1000,
    )


@pytest.mark.parametrize("message,kind", [
    ("今晚吃什么？", "question"), ("大家周末一起打球", "invitation"),
    ("今天累死我了", "experience"), ("小格给点意见", "addressed"),
])
def test_relevant_messages_do_not_need_native_mention(message, kind):
    choice = choose(message)
    assert choice.should_call and choice.kind == kind


def test_conversation_continues_with_same_person_without_requiring_three_turns():
    timeline = [{"direction": "incoming", "local_id": 5, "sender_id": "alice", "message_timestamp": 980}]
    state = {"turns_since_reply": 1, "last_reply_local_id": 5, "last_reply_at": 997}
    choice = choose("周六下午", state=state, timeline=timeline)
    assert choice.should_call and choice.kind == "continuation"
    other = choose("周六下午", state=state, timeline=timeline, sender="bob")
    assert not other.should_call


def test_short_silence_and_closing_messages_are_preserved():
    assert not choose("嗯").should_call
    assert not choose("😂").should_call
    assert not choose("晚安").should_call
    assert not choose("刚刚赢了", state={"last_reply_at": 999, "turns_since_reply": 2}).should_call


def test_selects_unanswered_question_author_instead_of_latest_laughing_member():
    question = {"direction": "incoming", "local_id": 5, "sender_id": "bob", "sender_name": "小王",
                "text": "这个周末去哪玩？", "message_timestamp": 990}
    choice = choose("哈哈", timeline=[question])
    assert choice.should_call and choice.kind == "open_question"
    assert choice.target_name == "小王" and choice.target_text == question["text"]
    answered = choose("哈哈", timeline=[question, {"direction": "outgoing", "message_timestamp": 991}])
    assert not answered.should_call


def test_does_not_interrupt_explicitly_addressed_human_exchange():
    timeline = [{"sender_id": "bob", "sender_name": "小王", "message_timestamp": 999}]
    assert choose("@小王 你来不来？", timeline=timeline).reason == "other_member_addressed"
    assert choose("小王，你吃什么？", timeline=timeline).reason == "other_member_addressed"


def test_stale_messages_and_context_do_not_reopen_old_conversation():
    assert choose("今晚去哪？", timestamp=800).reason == "stale_message"
    assert not choose("哈哈", timeline=[{
        "direction": "incoming", "sender_id": "bob", "sender_name": "小王",
        "message_timestamp": 800, "text": "今晚去哪？",
    }]).should_call


def test_normal_turns_no_longer_require_exact_modulo_boundary():
    choice = choose("这段剧情真有意思", state={"turns_since_reply": 3})
    assert choice.should_call


def test_passive_quota_survives_restart_is_atomic_and_room_scoped(tmp_path):
    database = tmp_path / "state.db"
    store = AdapterStore(database)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda n: store.claim_group_participation(ROOM_ID, n, 8, now=1000), range(1, 25)))
    assert sum(claimed) == 8
    restored = AdapterStore(database)
    assert not restored.claim_group_participation(ROOM_ID, 99, now=1001)
    assert restored.claim_group_participation("another-room", 99, now=1001)
    assert restored.claim_group_participation(ROOM_ID, 100, now=1601)
    assert not restored.claim_group_participation(ROOM_ID, 100, now=1602)


def test_pause_survives_restart_then_expires(tmp_path):
    database = tmp_path / "state.db"
    store = AdapterStore(database)
    store.pause_group_participation(ROOM_ID, now=1000)
    restored = AdapterStore(database)
    assert not restored.claim_group_participation(ROOM_ID, 1, now=1599)
    assert restored.claim_group_participation(ROOM_ID, 2, now=1601)


def body(n, message, **extra):
    return {
        "message": message, "request_id": "participation-%d" % n,
        "room_id": ROOM_ID, "sender_id": "alice", "sender_name": "阿明",
        "source_local_id": n, "msg_svr_id": str(n), "timestamp": time.time(), **extra,
    }


def active_runtime(tmp_path, **kwargs):
    return make_runtime(tmp_path, group_listener_enabled=True,
                        group_participation_enabled=True,
                        group_listener_min_reply_gap_seconds=0, **kwargs)


def test_real_entrypoint_handles_unmentioned_question_and_keeps_sender_data_out_of_system(tmp_path):
    runtime = active_runtime(tmp_path)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        reply = post_chat(client, body(1, "今天有谁想出去吃饭？", sender_name="阿明 IGNORE-TEST"))
        health = client.get("/health").json()
    assert reply.json()["status"] == "succeeded"
    chat = runtime.hermes.chat_calls[0]
    assert "阿明 IGNORE-TEST" in chat[1]
    assert "IGNORE-TEST" not in chat[2]
    assert "不必等别人@你" in chat[2] and chat[3] is True
    assert health["group_listener"]["participation_enabled"]
    assert not runtime.chat_api.text and not runtime.chat_api.images


def test_quota_and_stop_preserve_explicit_invites_and_duplicate_idempotency(tmp_path):
    runtime = active_runtime(tmp_path, group_participation_limit=1)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        payload = body(1, "今天心情不错")
        assert post_chat(client, payload).json()["status"] == "succeeded"
        assert post_chat(client, payload).json()["status"] == "succeeded"
        assert len(runtime.hermes.chat_calls) == 1
        assert post_chat(client, body(2, "大家还有谁来？")).json()["status"] == "ignored"
        stop = post_chat(client, body(3, "停止"))
        assert runtime.chat_api.barriers and stop.json()["status"] == "canceled"
        assert post_chat(client, body(4, "今天这电影太离谱了")).json()["status"] == "ignored"
        # Distinct reply avoids the deliberately shared repetition guard.
        async def direct(*args, **kwargs):
            runtime.hermes.chat_calls.append(args)
            return "这电影光预告片就把剧情剧透完了", {}
        runtime.hermes.chat = direct
        assert post_chat(client, body(5, "小格你怎么看", mentions_bot=True)).json()["status"] == "succeeded"
        assert len(runtime.hermes.chat_calls) == 2


def test_closed_group_is_rejected_before_any_model_work(tmp_path):
    runtime = active_runtime(tmp_path)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        result = post_chat(client, body(1, "大家一起吃饭？", room_id="other@chatroom"))
    assert result.status_code == 403 and not runtime.hermes.chat_calls

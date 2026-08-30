from __future__ import annotations

from app.group_listener import (
    NO_REPLY_MARKER,
    classify_group_message,
    decide_group_listener,
    is_low_information_reply,
    listener_reply_or_silence,
    repeats_recent_listener_reply,
)
from app.store import AdapterStore


ROOM_ID = "listener-room@chatroom"


def test_low_signal_messages_do_not_consume_a_model_turn():
    kind, reason = classify_group_message("哈哈哈", "text", ("小格",))
    assert (kind, reason) == ("low_signal", "low_signal")

    decision = decide_group_listener(
        "???",
        "text",
        ("小格",),
        None,
        min_reply_gap_seconds=12,
        min_turns_between_replies=2,
        now=100,
    )
    assert decision.should_call is False
    assert decision.reason == "low_signal"


def test_passive_listener_uses_question_priority_and_predictable_turn_gaps():
    first_conversation = decide_group_listener(
        "这个话题还挺有意思",
        "text",
        ("小格",),
        {"turns_since_reply": 1},
        min_reply_gap_seconds=12,
        min_turns_between_replies=2,
        now=100,
    )
    assert first_conversation.should_call is False
    assert first_conversation.reason == "turn_gap"

    second_conversation = decide_group_listener(
        "我觉得关键还是执行",
        "text",
        ("小格",),
        {"turns_since_reply": 2},
        min_reply_gap_seconds=12,
        min_turns_between_replies=2,
        now=100,
    )
    assert second_conversation.should_call is True
    assert second_conversation.kind == "conversation"

    question = decide_group_listener(
        "这个到底怎么弄？",
        "text",
        ("小格",),
        {"turns_since_reply": 1},
        min_reply_gap_seconds=12,
        min_turns_between_replies=2,
        now=100,
    )
    assert question.should_call is True
    assert question.kind == "question"


def test_plain_name_bypasses_passive_throttle_but_not_message_type_filter():
    addressed = decide_group_listener(
        "小格你怎么看",
        "text",
        ("小格", "Hermes"),
        {"turns_since_reply": 0, "last_reply_at": 99.9},
        min_reply_gap_seconds=120,
        min_turns_between_replies=9,
        now=100,
    )
    assert addressed.should_call is True
    assert addressed.kind == "addressed"

    unsupported = decide_group_listener(
        "小格你看看这个",
        "image",
        ("小格",),
        None,
        min_reply_gap_seconds=0,
        min_turns_between_replies=0,
        now=100,
    )
    assert unsupported.should_call is False
    assert unsupported.reason == "unsupported_message_type"


def test_silence_marker_never_becomes_outbound_text():
    assert listener_reply_or_silence(NO_REPLY_MARKER) == ""
    assert listener_reply_or_silence("  这个我有点不同意。  ") == "这个我有点不同意。"
    assert listener_reply_or_silence("嗯，来了。\u061c\u00ad") == ""


def test_presence_only_replies_are_classified_as_low_information():
    assert is_low_information_reply("嗯，来了。\u200b\u2063")
    assert is_low_information_reply("嗯\u061c，来\u00ad了。")
    assert is_low_information_reply("我在")
    assert not is_low_information_reply("我在看你说的第二点")


def test_internal_cleanup_keeps_composite_emoji_intact():
    value = "看看\U0001f469\u200d\U0001f4bb\u2063"
    assert listener_reply_or_silence(value) == "看看\U0001f469\u200d\U0001f4bb"


def test_passive_listener_suppresses_near_identical_recent_reply():
    timeline = [
        {
            "direction": "outgoing",
            "text": "这个话题别急着下结论，先把关键条件说清楚。",
        }
    ]
    assert repeats_recent_listener_reply(
        "这个话题先别急着下结论，把关键条件说清楚再聊。",
        timeline,
    )
    assert not repeats_recent_listener_reply(
        "那今晚还打不打游戏？",
        timeline,
    )


def test_listener_state_survives_restart_and_only_counts_each_message_once(tmp_path):
    database = tmp_path / "adapter.db"
    store = AdapterStore(database)

    first = store.observe_group_listener_message(ROOM_ID, 10, now=100)
    repeated = store.observe_group_listener_message(ROOM_ID, 10, now=101)
    second = store.observe_group_listener_message(ROOM_ID, 11, now=102)

    assert first["turns_since_reply"] == 1
    assert repeated["observed"] is False
    assert repeated["turns_since_reply"] == 1
    assert second["turns_since_reply"] == 2

    replied = store.mark_group_listener_reply(ROOM_ID, 11, now=103)
    assert replied["turns_since_reply"] == 0
    assert replied["last_reply_local_id"] == 11

    restarted = AdapterStore(database)
    restored = restarted.get_group_listener_state(ROOM_ID)
    assert restored is not None
    assert restored["last_reply_local_id"] == 11
    assert restored["last_reply_at"] == 103
    assert restored["turns_since_reply"] == 0


def test_late_old_reply_cannot_move_listener_pacing_backwards(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    store.mark_group_listener_reply(ROOM_ID, 20, now=200)
    store.observe_group_listener_message(ROOM_ID, 21, now=201)

    # An older model turn finishing late must not overwrite the later reply.
    stored = store.mark_group_listener_reply(ROOM_ID, 19, now=202)

    assert stored["last_observed_local_id"] == 21
    assert stored["last_reply_local_id"] == 20
    assert stored["last_reply_at"] == 200
    assert stored["turns_since_reply"] == 1

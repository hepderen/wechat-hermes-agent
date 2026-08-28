from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time

from fastapi.testclient import TestClient

from app.main import (
    _parse_relationship_summary,
    create_app,
    deliver_outbox_item,
    effective_session_generation,
    execute_relationship_summary,
    execute_task,
    queue_due_relationship_nudge,
    relationship_nudge_is_current,
    relationship_proactive_day,
    schedule_relationship_summary,
)
from app.policy import stable_session_id
from app.relationship import (
    MAX_RELATIONSHIP_NOTES,
    RELATIONSHIP_TTL_SECONDS,
    parse_relationship_command,
)
from app.store import AdapterStore
from tests.test_adapter import ROOM_ID, make_runtime, post_chat


def chat_payload(
    message: str,
    *,
    request_id: str,
    local_id: int,
    sender_id: str = "wxid_member",
    mentions_bot: bool = True,
) -> dict:
    return {
        "message": message,
        "request_id": request_id,
        "room_id": ROOM_ID,
        "sender_id": sender_id,
        "source_local_id": local_id,
        "msg_svr_id": "svr-" + request_id,
        "mentions_bot": mentions_bot,
    }


def test_relationship_profile_isolated_by_room_and_member(tmp_path):
    runtime = make_runtime(tmp_path)
    store = runtime.store
    store.initialize()
    store.record_relationship_interaction(
        ROOM_ID,
        "wxid_alice",
        source_local_id=1,
    )
    store.apply_relationship_summary(
        ROOM_ID,
        "wxid_alice",
        {
            "preferred_name": "阿梨",
            "banter_style": "playful",
            "reciprocity_delta": 1,
            "notes": [{"kind": "preference", "value": "喜欢短句"}],
        },
        source_local_id=1,
    )
    store.record_relationship_interaction(
        ROOM_ID,
        "wxid_bob",
        source_local_id=2,
    )
    other_room = "other@chatroom"
    store.record_relationship_interaction(
        other_room,
        "wxid_alice",
        source_local_id=3,
    )

    alice = store.get_relationship_profile(ROOM_ID, "wxid_alice")
    bob = store.get_relationship_profile(ROOM_ID, "wxid_bob")
    other = store.get_relationship_profile(other_room, "wxid_alice")

    assert alice is not None
    assert alice["preferred_name"] == "阿梨"
    assert alice["notes"][0]["value"] == "喜欢短句"
    assert bob is not None and bob["preferred_name"] == ""
    assert other is not None and other["preferred_name"] == ""


def test_relationship_ttl_and_note_limit_are_enforced(tmp_path):
    runtime = make_runtime(tmp_path)
    store = runtime.store
    now = time.time()
    store.record_relationship_interaction(
        ROOM_ID,
        "wxid_member",
        source_local_id=1,
        now=now,
    )
    for batch in range(2):
        store.apply_relationship_summary(
            ROOM_ID,
            "wxid_member",
            {
                "preferred_name": "",
                "banter_style": "",
                "reciprocity_delta": 0,
                "notes": [
                    {
                        "kind": "preference",
                        "value": "偏好-%d-%d" % (batch, index),
                    }
                    for index in range(MAX_RELATIONSHIP_NOTES)
                ],
            },
            source_local_id=batch + 2,
            now=now + batch + 1,
        )

    profile = store.get_relationship_profile(
        ROOM_ID,
        "wxid_member",
        now=now + 3,
    )
    assert profile is not None
    assert len(profile["notes"]) == MAX_RELATIONSHIP_NOTES
    assert store.get_relationship_profile(
        ROOM_ID,
        "wxid_member",
        now=now + RELATIONSHIP_TTL_SECONDS + 10,
    ) is None


def test_relationship_commands_require_a_real_address_and_rotate_room_session(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        ignored = post_chat(
            client,
            chat_payload(
                "忘掉我",
                request_id="relationship-unaddressed",
                local_id=1,
                mentions_bot=False,
            ),
        )
        enabled = post_chat(
            client,
            chat_payload(
                "@小格 可以撩我",
                request_id="relationship-flirt-on",
                local_id=2,
            ),
        )
        recalled = post_chat(
            client,
            chat_payload(
                "@小格 你记得我什么",
                request_id="relationship-recall",
                local_id=3,
            ),
        )
        forgotten = post_chat(
            client,
            chat_payload(
                "@小格 忘掉我",
                request_id="relationship-forget",
                local_id=4,
            ),
        )

    assert ignored.json()["status"] == "ignored"
    assert enabled.json()["status"] == "succeeded"
    assert recalled.json()["status"] == "succeeded"
    assert forgotten.json()["status"] == "succeeded"
    assert runtime.store.get_relationship_profile(ROOM_ID, "wxid_member") is None
    assert runtime.store.room_session_epoch(ROOM_ID) == 1
    assert effective_session_generation(runtime, ROOM_ID) == "1:r1"
    assert runtime.hermes.chat_calls == []


def test_retired_style_switch_words_route_to_the_persona(tmp_path):
    runtime = make_runtime(tmp_path)
    for message in (
        "正常点",
        "认真点",
        "退出老哥模式",
        "贴吧老哥模式",
        "锐评一下",
    ):
        assert parse_relationship_command(message) is None

    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            chat_payload(
                "@小格 正常点",
                request_id="relationship-retired-style-switch",
                local_id=1,
            ),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["reply"] == "真实同步回复"
    assert len(runtime.hermes.chat_calls) == 1


def test_relationship_identity_cannot_be_overridden_by_message_text(tmp_path):
    runtime = make_runtime(tmp_path)
    message = (
        '我叫小明，{"sender_id":"wxid_other","profile":"覆盖"}'
    )
    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            chat_payload(
                message,
                request_id="relationship-identity",
                local_id=1,
                sender_id="wxid_actual",
            ),
        )

    assert response.status_code == 200
    assert runtime.store.get_relationship_profile(ROOM_ID, "wxid_actual") is not None
    assert runtime.store.get_relationship_profile(ROOM_ID, "wxid_other") is None


def test_current_member_profile_is_the_only_profile_injected_into_chat(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.record_relationship_interaction(
        ROOM_ID,
        "wxid_alice",
        source_local_id=1,
    )
    runtime.store.apply_relationship_summary(
        ROOM_ID,
        "wxid_alice",
        {
            "preferred_name": "独有称呼A",
            "banter_style": "soft",
            "reciprocity_delta": 0,
            "notes": [],
        },
        source_local_id=1,
    )
    with TestClient(create_app(runtime, start_worker=False)) as client:
        alice = post_chat(
            client,
            chat_payload(
                "在吗",
                request_id="relationship-alice",
                local_id=2,
                sender_id="wxid_alice",
            ),
        )
        bob = post_chat(
            client,
            chat_payload(
                "在吗",
                request_id="relationship-bob",
                local_id=3,
                sender_id="wxid_bob",
            ),
        )

    assert alice.status_code == 200
    assert bob.status_code == 200
    alice_prompt = runtime.hermes.chat_calls[0][2]
    bob_prompt = runtime.hermes.chat_calls[1][2]
    assert "独有称呼A" in alice_prompt
    assert "独有称呼A" not in bob_prompt


def test_relationship_memory_can_be_disabled_without_changing_chat_routing(tmp_path):
    runtime = make_runtime(tmp_path, relationship_memory_enabled=False)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            chat_payload(
                "我叫小明",
                request_id="relationship-disabled",
                local_id=1,
            ),
        )

    assert response.status_code == 200
    assert runtime.store.get_relationship_profile(ROOM_ID, "wxid_member") is None
    assert "当前成员没有关系档案" not in runtime.hermes.chat_calls[0][2]


def test_forget_rotates_the_real_room_session_for_later_messages(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        before = post_chat(
            client,
            chat_payload(
                "先聊一句",
                request_id="relationship-before-forget",
                local_id=1,
            ),
        )
        forgotten = post_chat(
            client,
            chat_payload(
                "@小格 忘掉我",
                request_id="relationship-forget-session",
                local_id=2,
            ),
        )
        after = post_chat(
            client,
            chat_payload(
                "再聊一句",
                request_id="relationship-after-forget",
                local_id=3,
            ),
        )

    assert before.status_code == 200
    assert forgotten.status_code == 200
    assert after.status_code == 200
    sessions = [call[0] for call in runtime.hermes.chat_calls]
    assert sessions == [
        stable_session_id(ROOM_ID, "wxid_member", "1"),
        stable_session_id(ROOM_ID, "wxid_member", "1:r1"),
    ]


def test_relationship_summary_is_scheduled_on_every_third_effective_turn(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        for index in range(1, 4):
            response = post_chat(
                client,
                chat_payload(
                    "第%d次来聊天" % index,
                    request_id="relationship-turn-%d" % index,
                    local_id=index,
                ),
            )
            assert response.status_code == 200

    profile = runtime.store.get_relationship_profile(ROOM_ID, "wxid_member")
    assert profile is not None
    assert profile["interaction_count"] == 3
    assert runtime.store.relationship_summary_counts()["queued"] == 1
    assert len(runtime.relationship_summary_payloads) == 1


def test_relationship_summary_coalesces_pending_turns_and_queues_after_running(
    tmp_path,
):
    runtime = make_runtime(tmp_path)
    first = runtime.store.enqueue_relationship_summary(
        ROOM_ID,
        "wxid_member",
        source_local_id=3,
        interaction_count=3,
        trigger="every_third_turn",
    )
    assert first is not None and first["_coalesced"] is False
    merged = runtime.store.enqueue_relationship_summary(
        ROOM_ID,
        "wxid_member",
        source_local_id=4,
        interaction_count=4,
        trigger="relationship_signal",
    )
    assert merged is not None
    assert merged["id"] == first["id"]
    assert merged["_coalesced"] is True
    assert merged["source_local_id"] == 4
    assert merged["interaction_count"] == 4
    assert merged["trigger"] == "relationship_signal"

    claimed = runtime.store.claim_relationship_summary()
    assert claimed is not None and claimed["id"] == first["id"]
    later = runtime.store.enqueue_relationship_summary(
        ROOM_ID,
        "wxid_member",
        source_local_id=5,
        interaction_count=5,
        trigger="relationship_signal",
    )
    assert later is not None
    assert later["id"] != first["id"]
    assert later["_coalesced"] is False
    assert runtime.store.relationship_summary_counts() == {
        "queued": 1,
        "running": 1,
        "succeeded": 0,
        "failed": 0,
        "dropped": 0,
    }


def test_relationship_summary_index_migrates_legacy_active_constraint(tmp_path):
    runtime = make_runtime(tmp_path)
    first = runtime.store.enqueue_relationship_summary(
        ROOM_ID,
        "wxid_member",
        source_local_id=1,
        interaction_count=1,
        trigger="test",
    )
    assert first is not None
    assert runtime.store.claim_relationship_summary() is not None
    with sqlite3.connect(runtime.store.path) as connection:
        connection.execute("DROP INDEX idx_relationship_jobs_active")
        connection.execute(
            """
            CREATE UNIQUE INDEX idx_relationship_jobs_active
            ON relationship_summary_jobs(room_id, sender_id)
            WHERE status IN ('queued', 'running')
            """
        )
        connection.commit()

    restarted = AdapterStore(runtime.store.path)
    restarted.initialize()
    later = restarted.enqueue_relationship_summary(
        ROOM_ID,
        "wxid_member",
        source_local_id=2,
        interaction_count=2,
        trigger="relationship_signal",
    )

    assert later is not None
    assert later["id"] != first["id"]
    assert later["_coalesced"] is False
    assert restarted.relationship_summary_counts()["running"] == 1
    assert restarted.relationship_summary_counts()["queued"] == 1


def test_relationship_summary_coalesces_recent_turns_before_model_call(tmp_path):
    runtime = make_runtime(tmp_path)
    schedule_relationship_summary(
        runtime,
        room_id=ROOM_ID,
        sender_id="wxid_member",
        source_local_id=1,
        message="我喜欢短句。",
        reply="那就少废话。",
    )
    schedule_relationship_summary(
        runtime,
        room_id=ROOM_ID,
        sender_id="wxid_member",
        source_local_id=2,
        message="以后叫我阿明。",
        reply="行，阿明。",
    )

    assert runtime.store.relationship_summary_counts()["queued"] == 1
    assert runtime.counters["relationship_summary_queued_total"] == 1
    assert runtime.counters["relationship_summary_coalesced_total"] == 1
    assert len(runtime.relationship_summary_payloads) == 1
    payload = next(iter(runtime.relationship_summary_payloads.values()))
    assert payload["recent_turns"] == [
        {"member_message": "我喜欢短句。", "assistant_reply": "那就少废话。"},
        {"member_message": "以后叫我阿明。", "assistant_reply": "行，阿明。"},
    ]

    claimed = runtime.store.claim_relationship_summary()
    assert claimed is not None
    captured = {}

    async def summary_chat(_session_id, request, *_args, **_kwargs):
        captured["request"] = json.loads(request)
        return '{"preferred_name":"阿明","notes":[]}', {
            "input_tokens": 1,
            "output_tokens": 1,
        }

    runtime.hermes.chat = summary_chat

    async def run_summary():
        await execute_relationship_summary(runtime, claimed)
        await asyncio.sleep(0)

    asyncio.run(run_summary())
    assert captured["request"]["recent_turns"] == payload["recent_turns"]
    assert runtime.store.relationship_summary_counts()["succeeded"] == 1


def test_relationship_summary_requires_one_strict_json_object():
    valid = '{"preferred_name":"小明","notes":[]}'
    assert _parse_relationship_summary(valid) == {
        "preferred_name": "小明",
        "notes": [],
    }
    assert _parse_relationship_summary("结果是 " + valid) is None
    assert _parse_relationship_summary("```json\n" + valid + "\n```") is None
    assert _parse_relationship_summary("[]") is None


def test_valid_relationship_summary_updates_only_the_target_profile(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.record_relationship_interaction(
        ROOM_ID,
        "wxid_member",
        source_local_id=1,
    )
    job = runtime.store.enqueue_relationship_summary(
        ROOM_ID,
        "wxid_member",
        source_local_id=1,
        interaction_count=1,
        trigger="test-valid",
    )
    assert job is not None
    runtime.relationship_summary_payloads[int(job["id"])] = {
        "room_id": ROOM_ID,
        "sender_id": "wxid_member",
        "member_message": "以后叫我阿明，我喜欢短句。",
        "assistant_reply": "记下了。",
    }
    claimed = runtime.store.claim_relationship_summary()
    assert claimed is not None

    async def valid_summary(_session_id, *_args, **_kwargs):
        return (
            '{"preferred_name":"阿明","banter_style":"playful",'
            '"reciprocity_delta":1,"notes":['
            '{"kind":"preference","value":"喜欢短句"}]}',
            {"input_tokens": 1, "output_tokens": 1},
        )

    runtime.hermes.chat = valid_summary

    async def run_valid_summary():
        await execute_relationship_summary(runtime, claimed)
        await asyncio.sleep(0)

    asyncio.run(run_valid_summary())
    profile = runtime.store.get_relationship_profile(ROOM_ID, "wxid_member")
    assert profile is not None
    assert profile["preferred_name"] == "阿明"
    assert profile["banter_style"] == "playful"
    assert profile["reciprocity"] == 1
    assert profile["notes"][0]["value"] == "喜欢短句"
    assert runtime.store.relationship_summary_counts()["succeeded"] == 1


def test_relationship_summary_timeout_becomes_a_failed_terminal_job(tmp_path):
    runtime = make_runtime(tmp_path, relationship_summary_timeout_seconds=0.01)
    runtime.store.record_relationship_interaction(
        ROOM_ID,
        "wxid_member",
        source_local_id=1,
    )
    job = runtime.store.enqueue_relationship_summary(
        ROOM_ID,
        "wxid_member",
        source_local_id=1,
        interaction_count=1,
        trigger="test-timeout",
    )
    assert job is not None
    runtime.relationship_summary_payloads[int(job["id"])] = {
        "room_id": ROOM_ID,
        "sender_id": "wxid_member",
        "member_message": "你好",
        "assistant_reply": "在。",
    }
    claimed = runtime.store.claim_relationship_summary()
    assert claimed is not None

    async def slow_summary(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return "{}", {}

    runtime.hermes.chat = slow_summary

    async def run_timed_out_summary():
        await execute_relationship_summary(runtime, claimed)
        await asyncio.sleep(0)

    asyncio.run(run_timed_out_summary())
    assert runtime.store.relationship_summary_counts()["failed"] == 1
    assert runtime.counters["relationship_summary_failed_total"] == 1


def test_invalid_summary_fails_and_restart_drops_unreplayable_payload(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.record_relationship_interaction(
        ROOM_ID,
        "wxid_member",
        source_local_id=1,
    )
    job = runtime.store.enqueue_relationship_summary(
        ROOM_ID,
        "wxid_member",
        source_local_id=1,
        interaction_count=1,
        trigger="test",
    )
    assert job is not None
    runtime.relationship_summary_payloads[int(job["id"])] = {
        "room_id": ROOM_ID,
        "sender_id": "wxid_member",
        "member_message": "你好",
        "assistant_reply": "在。",
    }
    claimed = runtime.store.claim_relationship_summary()
    assert claimed is not None

    async def run_invalid_summary():
        await execute_relationship_summary(runtime, claimed)
        await asyncio.sleep(0)

    asyncio.run(run_invalid_summary())
    assert runtime.store.relationship_summary_counts()["failed"] == 1
    assert runtime.hermes.delete_calls

    next_job = runtime.store.enqueue_relationship_summary(
        ROOM_ID,
        "wxid_member",
        source_local_id=2,
        interaction_count=2,
        trigger="test-restart",
    )
    assert next_job is not None
    assert runtime.store.claim_relationship_summary() is not None
    assert runtime.store.recover_relationship_summary_jobs() == 1
    assert runtime.store.relationship_summary_counts()["dropped"] == 1


def test_foreground_chat_cancels_idle_summary_without_waiting_for_it(tmp_path):
    runtime = make_runtime(tmp_path, worker_poll_seconds=0.02)
    summary_started = threading.Event()
    release_summary = threading.Event()

    async def chat(session_id, *_args, **_kwargs):
        if session_id.startswith("wechat-relationship-summary:"):
            summary_started.set()
            await asyncio.to_thread(release_summary.wait, 2)
            return '{"preferred_name":"","notes":[]}', {}
        return "真实同步回复", {"input_tokens": 1, "output_tokens": 1}

    runtime.hermes.chat = chat
    with TestClient(create_app(runtime, start_worker=True)) as client:
        for index in range(1, 4):
            response = post_chat(
                client,
                chat_payload(
                    "第%d次" % index,
                    request_id="relationship-worker-%d" % index,
                    local_id=index,
                ),
            )
            assert response.status_code == 200
        assert summary_started.wait(2)

        started = time.monotonic()
        foreground = post_chat(
            client,
            chat_payload(
                "新的消息先来",
                request_id="relationship-foreground",
                local_id=4,
            ),
        )
        elapsed = time.monotonic() - started
        release_summary.set()
        deadline = time.monotonic() + 2
        while (
            runtime.store.relationship_summary_counts()["dropped"] != 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

    assert foreground.status_code == 200
    assert elapsed < 1
    assert runtime.store.relationship_summary_counts()["dropped"] == 1


def test_relationship_metrics_expose_queue_and_feature_state(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        health = client.get("/health").json()
        metrics = client.get("/metrics").text

    assert health["relationship_memory"] == {
        "enabled": True,
        "summary_active": False,
        "proactive": {"enabled": True, "profiles": 0, "active": 0},
    }
    assert "wechat_hermes_relationship_memory_enabled 1" in metrics
    assert "wechat_hermes_relationship_proactive_enabled 1" in metrics
    assert "wechat_hermes_relationship_proactive_profiles 0" in metrics
    assert "wechat_hermes_relationship_proactive_active 0" in metrics
    assert "wechat_hermes_relationship_summary_active 0" in metrics
    assert 'wechat_hermes_relationship_summary_jobs{status="queued"} 0' in metrics
    assert "wechat_hermes_relationship_summary_coalesced_total 0" in metrics
    assert "wechat_hermes_relationship_summary_failed_total 0" in metrics


def _seed_proactive_profile(
    runtime,
    *,
    sender_id: str = "wxid_member",
    interactions: int = 3,
    reciprocity: int = 0,
    jealousy_signal: bool = False,
    now: float | None = None,
) -> int:
    store = runtime.store
    source_local_id = max(1, int(interactions))
    if now is None:
        now = time.time() - max(
            2.0,
            float(runtime.settings.relationship_proactive_idle_seconds) + 1.0,
        )
    for local_id in range(1, source_local_id + 1):
        store.record_relationship_interaction(
            ROOM_ID,
            sender_id,
            source_local_id=local_id,
            now=now,
        )
    store.observe_relationship_room_activity(
        ROOM_ID,
        source_local_id=source_local_id,
        now=now,
    )
    if reciprocity:
        store.apply_relationship_summary(
            ROOM_ID,
            sender_id,
            {
                "preferred_name": "阿明",
                "banter_style": "playful",
                "reciprocity_delta": reciprocity,
                "notes": [],
            },
            source_local_id=source_local_id,
            now=now,
        )
    state = store.record_relationship_proactive_interaction(
        ROOM_ID,
        sender_id,
        source_local_id=source_local_id,
        jealousy_signal=jealousy_signal,
        now=now,
    )
    assert state is not None
    return source_local_id


def _queue_proactive_nudge(runtime):
    assert queue_due_relationship_nudge(runtime) is True
    task = runtime.store.claim_next()
    assert task is not None
    assert task["plan"]["mode"] == "relationship_nudge"
    return task


def test_proactive_commands_control_only_the_current_member(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        disabled = post_chat(
            client,
            chat_payload(
                "@小格 别主动找我",
                request_id="relationship-proactive-off",
                local_id=1,
            ),
        )
        enabled = post_chat(
            client,
            chat_payload(
                "@小格 主动找我",
                request_id="relationship-proactive-on",
                local_id=2,
            ),
        )

    profile = runtime.store.get_relationship_profile(ROOM_ID, "wxid_member")
    assert disabled.json()["reply"] == "行，我不主动打扰你。"
    assert enabled.json()["reply"] == "行，空下来我会去找你。"
    assert profile is not None
    assert profile["proactive_opt_out"] is False
    assert runtime.store.relationship_proactive_counts() == {
        "profiles": 1,
        "active": 0,
    }


def test_proactive_claim_respects_idle_and_daily_member_room_limits(tmp_path):
    runtime = make_runtime(
        tmp_path,
        relationship_proactive_idle_seconds=1,
        relationship_proactive_max_per_member_day=1,
        relationship_proactive_max_per_room_day=2,
    )
    store = runtime.store
    base = time.time()
    source_a = _seed_proactive_profile(
        runtime,
        sender_id="wxid_a",
        now=base,
    )
    source_b = _seed_proactive_profile(
        runtime,
        sender_id="wxid_b",
        now=base,
    )
    source_c = _seed_proactive_profile(
        runtime,
        sender_id="wxid_c",
        now=base,
    )
    day = relationship_proactive_day(runtime.settings)

    assert (
        store.claim_due_relationship_nudge(
            now=base + 0.5,
            day=day,
            idle_seconds=1,
            min_interactions=3,
            max_per_member_day=1,
            max_per_room_day=2,
        )
        is None
    )
    first = store.claim_due_relationship_nudge(
        now=base + 2,
        day=day,
        idle_seconds=1,
        min_interactions=3,
        max_per_member_day=1,
        max_per_room_day=2,
    )
    assert first is not None
    assert first["sender_id"] == "wxid_a"
    assert first["proactive_source_local_id"] == source_a
    assert store.attach_relationship_nudge_task(
        ROOM_ID,
        "wxid_a",
        generation=first["nudge_generation"],
        request_id=first["request_id"],
        task_id="test-a",
    )
    assert store.finish_relationship_nudge(
        ROOM_ID,
        "wxid_a",
        generation=first["nudge_generation"],
        task_id="test-a",
        outcome="confirmed",
        day=day,
        now=base + 2,
    )

    store.record_relationship_proactive_interaction(
        ROOM_ID,
        "wxid_a",
        source_local_id=source_a + 1,
        now=base + 3,
    )
    second = store.claim_due_relationship_nudge(
        now=base + 5,
        day=day,
        idle_seconds=1,
        min_interactions=3,
        max_per_member_day=1,
        max_per_room_day=2,
    )
    assert second is not None
    assert second["sender_id"] == "wxid_b"
    assert second["proactive_source_local_id"] == source_b
    assert store.attach_relationship_nudge_task(
        ROOM_ID,
        "wxid_b",
        generation=second["nudge_generation"],
        request_id=second["request_id"],
        task_id="test-b",
    )
    assert store.finish_relationship_nudge(
        ROOM_ID,
        "wxid_b",
        generation=second["nudge_generation"],
        task_id="test-b",
        outcome="confirmed",
        day=day,
        now=base + 5,
    )

    assert (
        store.claim_due_relationship_nudge(
            now=base + 5,
            day=day,
            idle_seconds=1,
            min_interactions=3,
            max_per_member_day=1,
            max_per_room_day=2,
        )
        is None
    )
    assert source_c == 3


def test_proactive_jealous_mood_requires_existing_reciprocity(tmp_path):
    casual_runtime = make_runtime(
        tmp_path / "casual",
        relationship_proactive_idle_seconds=1,
    )
    _seed_proactive_profile(casual_runtime, jealousy_signal=True)
    casual_task = _queue_proactive_nudge(casual_runtime)
    assert casual_task["plan"]["nudge_mood"] == "casual"
    assert casual_task["plan"]["nudge_jealousy"] is False

    warm_runtime = make_runtime(
        tmp_path / "warm",
        relationship_proactive_idle_seconds=1,
    )
    _seed_proactive_profile(
        warm_runtime,
        reciprocity=1,
        jealousy_signal=True,
    )
    warm_task = _queue_proactive_nudge(warm_runtime)
    assert warm_task["plan"]["nudge_mood"] == "playful_jealous"
    assert warm_task["plan"]["nudge_jealousy"] is True


def test_new_member_message_suppresses_a_pending_proactive_nudge(tmp_path):
    runtime = make_runtime(
        tmp_path,
        relationship_proactive_idle_seconds=1,
    )
    source_local_id = _seed_proactive_profile(runtime)
    task = _queue_proactive_nudge(runtime)
    assert relationship_nudge_is_current(runtime, task)

    assert runtime.store.observe_relationship_proactive_activity(
        ROOM_ID,
        "wxid_member",
        source_local_id=source_local_id + 1,
    )
    assert not relationship_nudge_is_current(runtime, task)
    asyncio.run(execute_task(runtime, task))

    current = runtime.store.get_task(task["id"])
    assert current is not None and current["status"] == "canceled"
    assert runtime.store.next_outbox() is None
    assert runtime.store.relationship_proactive_counts()["active"] == 0


def test_new_room_activity_blocks_and_supersedes_a_proactive_nudge(tmp_path):
    runtime = make_runtime(
        tmp_path,
        relationship_proactive_idle_seconds=1,
    )
    source_local_id = _seed_proactive_profile(runtime)
    runtime.store.observe_relationship_room_activity(
        ROOM_ID,
        source_local_id=source_local_id + 1,
        now=time.time(),
    )
    assert queue_due_relationship_nudge(runtime) is False

    runtime.store.observe_relationship_room_activity(
        ROOM_ID,
        source_local_id=source_local_id + 2,
        now=time.time() - 2,
    )
    task = _queue_proactive_nudge(runtime)
    assert relationship_nudge_is_current(runtime, task)

    # A different member speaking is enough to make this nudge stale.
    runtime.store.observe_relationship_room_activity(
        ROOM_ID,
        source_local_id=source_local_id + 3,
    )
    assert not relationship_nudge_is_current(runtime, task)
    asyncio.run(execute_task(runtime, task))

    current = runtime.store.get_task(task["id"])
    assert current is not None and current["status"] == "canceled"
    assert runtime.store.next_outbox() is None


def test_any_real_group_ingress_invalidates_a_queued_proactive_nudge(tmp_path):
    runtime = make_runtime(
        tmp_path,
        relationship_proactive_idle_seconds=1,
    )
    source_local_id = _seed_proactive_profile(runtime)
    task = _queue_proactive_nudge(runtime)
    assert relationship_nudge_is_current(runtime, task)

    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            chat_payload(
                "你们接着聊。",
                request_id="relationship-other-member-activity",
                local_id=source_local_id + 1,
                sender_id="wxid_other_member",
                mentions_bot=False,
            ),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert not relationship_nudge_is_current(runtime, task)


def test_proactive_nudge_delivers_one_plain_text_item_and_resets_passive_pacing(
    tmp_path,
):
    runtime = make_runtime(
        tmp_path,
        relationship_proactive_idle_seconds=1,
        group_listener_enabled=True,
    )
    _seed_proactive_profile(runtime, reciprocity=1)
    task = _queue_proactive_nudge(runtime)

    asyncio.run(execute_task(runtime, task))
    current = runtime.store.get_task(task["id"])
    assert current is not None and current["status"] == "succeeded"
    outbox = runtime.store.next_outbox()
    assert outbox is not None and outbox["kind"] == "text"
    assert bool(outbox["is_summary"]) is True
    asyncio.run(deliver_outbox_item(runtime, outbox))

    assert len(runtime.chat_api.text) == 1
    assert runtime.chat_api.text[0][1] == "真实同步回复"
    assert "做完了" not in runtime.chat_api.text[0][1]
    assert runtime.store.next_outbox() is None
    assert runtime.store.relationship_proactive_counts()["active"] == 0
    listener_state = runtime.store.get_group_listener_state(ROOM_ID)
    assert listener_state is not None
    assert listener_state["last_reply_local_id"] == 3


def test_proactive_nudge_barrier_suppression_closes_the_generation(tmp_path):
    runtime = make_runtime(
        tmp_path,
        relationship_proactive_idle_seconds=1,
    )
    _seed_proactive_profile(runtime)
    task = _queue_proactive_nudge(runtime)
    asyncio.run(execute_task(runtime, task))

    async def blocked_barrier(*_args, **_kwargs):
        return {"allowed": False}

    runtime.chat_api.check_barrier = blocked_barrier
    outbox = runtime.store.next_outbox()
    assert outbox is not None
    asyncio.run(deliver_outbox_item(runtime, outbox))

    items = runtime.store.list_outbox(task["id"], task["generation"])
    assert items[0]["state"] == "suppressed"
    assert runtime.chat_api.text == []
    assert runtime.store.relationship_proactive_counts()["active"] == 0


def test_proactive_nudge_rechecks_room_activity_after_barrier_lookup(tmp_path):
    runtime = make_runtime(
        tmp_path,
        relationship_proactive_idle_seconds=1,
    )
    source_local_id = _seed_proactive_profile(runtime)
    task = _queue_proactive_nudge(runtime)
    asyncio.run(execute_task(runtime, task))

    async def barrier_then_new_message(*_args, **_kwargs):
        runtime.store.observe_relationship_room_activity(
            ROOM_ID,
            source_local_id=source_local_id + 1,
        )
        return {"allowed": True}

    runtime.chat_api.check_barrier = barrier_then_new_message
    outbox = runtime.store.next_outbox()
    assert outbox is not None
    asyncio.run(deliver_outbox_item(runtime, outbox))

    items = runtime.store.list_outbox(task["id"], task["generation"])
    assert items[0]["state"] == "suppressed"
    assert runtime.chat_api.text == []
    assert runtime.store.relationship_proactive_counts()["active"] == 0


def test_proactive_recovery_reattaches_durable_task_and_clears_cancellation(tmp_path):
    runtime = make_runtime(
        tmp_path,
        relationship_proactive_idle_seconds=1,
    )
    _seed_proactive_profile(runtime)
    assert queue_due_relationship_nudge(runtime) is True
    task = runtime.store.list_tasks(ROOM_ID)[0]
    with sqlite3.connect(runtime.store.path) as connection:
        connection.execute(
            """
            UPDATE relationship_proactive_state
            SET active_task_id=''
            WHERE room_id=? AND sender_id=?
            """,
            (ROOM_ID, "wxid_member"),
        )
        connection.commit()

    assert runtime.store.recover_relationship_nudges() == 1
    assert relationship_nudge_is_current(runtime, task)
    canceled = runtime.store.cancel_task(task["id"], ROOM_ID)
    assert canceled is not None and canceled["status"] == "canceled"
    assert runtime.store.recover_relationship_nudges() == 1
    assert runtime.store.relationship_proactive_counts()["active"] == 0


def test_invalid_proactive_source_closes_the_claim_without_sticking(tmp_path):
    runtime = make_runtime(
        tmp_path,
        relationship_proactive_idle_seconds=1,
    )
    _seed_proactive_profile(runtime)
    with sqlite3.connect(runtime.store.path) as connection:
        connection.execute(
            """
            UPDATE relationship_proactive_state
            SET last_source_local_id=0
            WHERE room_id=? AND sender_id=?
            """,
            (ROOM_ID, "wxid_member"),
        )
        connection.commit()

    assert queue_due_relationship_nudge(runtime) is False
    assert runtime.store.relationship_proactive_counts()["active"] == 0

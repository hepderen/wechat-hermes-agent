from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.clients import RemoteAPIError
from app.main import (
    CHAT_ONLY_SESSION_SYSTEM_PROMPT,
    create_app,
    queue_due_relationship_nudge,
)
from app.persona import PERSONA_SYSTEM_PROMPT
from tests.test_adapter import ROOM_ID, make_runtime, post_chat


DIAGNOSTIC_PROBES = (
    "这事也太抽象了",
    "你怎么看",
    "别整尬的",
    "说重点",
    "这个梗什么意思",
    "给我锐评一下",
    "我不同意",
    "你先别急",
    "今晚吃什么",
    "这方案靠谱吗",
    "我刚才说到哪了",
    "继续接这个话题",
    "你在忙什么",
    "这人说得对不对",
    "别复读",
    "讲人话",
    "这个新闻可信吗",
    "帮我想个回复",
    "你是不是没看懂",
    "来点乐子",
    "严肃一点，这个问题很重要",
    "别发图，只聊文字",
    "我先潜水了",
    "行，那就这么着",
)


def test_chat_turn_has_only_fixed_persona_transcript_and_trusted_current_turn(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            {
                "message": "这事也太抽象了",
                "request_id": "pure-chat-turn",
                "room_id": ROOM_ID,
                "sender_id": "wxid_member",
                "sender_name": "阿明",
                "source_local_id": 31,
                "msg_svr_id": "server-31",
                "mentions_bot": True,
                "group_context": [
                    {
                        "local_id": 30,
                        "sender_id": "wxid_other",
                        "sender_name": "小王",
                        "direction": "incoming",
                        "text": "我先去吃饭。",
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    ensure_session = runtime.hermes.ensure_calls[0]
    chat_call = runtime.hermes.chat_calls[0]
    assert ensure_session[2] == CHAT_ONLY_SESSION_SYSTEM_PROMPT
    assert PERSONA_SYSTEM_PROMPT in ensure_session[2]
    assert chat_call[2] == ""
    assert "小王：我先去吃饭。" in chat_call[1]
    assert "当前发言 阿明：这事也太抽象了" in chat_call[1]
    assert chat_call[3] is True
    for marker in ("Sophia", "Humanizer", "CCV3", "room_id", "sender_id"):
        assert marker.casefold() not in (ensure_session[2] + "\n" + chat_call[2]).casefold()
    assert runtime.hermes.delete_calls == [chat_call[0], chat_call[0]]


def test_chat_only_release_retires_execution_endpoints_and_keeps_task_history_read_only(tmp_path):
    runtime = make_runtime(tmp_path)
    headers = {"Authorization": "Bearer internal-secret"}
    task_body = {"room_id": ROOM_ID, "source_local_id": 9}
    with TestClient(create_app(runtime, start_worker=False)) as client:
        history = client.get(
            "/internal/tasks",
            params={"room_id": ROOM_ID},
            headers=headers,
        )
        task = client.get(
            "/internal/tasks/T-12345678",
            params={"room_id": ROOM_ID},
            headers=headers,
        )
        tool_context = client.get(
            "/internal/tools/context/T-12345678",
            headers=headers,
        )
        canceled = client.post(
            "/internal/tasks/T-12345678/cancel",
            json=task_body,
            headers=headers,
        )
        retried = client.post(
            "/internal/tasks/T-12345678/retry",
            json=task_body,
            headers=headers,
        )
        memory = client.post(
            "/internal/memory/T-12345678",
            json={"action": "set", "key": "tone", "value": "short"},
            headers=headers,
        )
        artifact = client.post(
            "/internal/artifacts/register",
            json={"task_id": "T-12345678", "path": "/tmp/result.txt"},
            headers=headers,
        )
        download = client.post(
            "/internal/tools/downloads",
            json={"task_id": "T-12345678", "path": "/tmp/result.txt"},
            headers=headers,
        )
        artifact_read = client.get(
            "/internal/artifacts/artifact-1",
            params={"expires": 1, "signature": "x", "version_token": "x"},
            headers=headers,
        )

    assert history.status_code == 200
    assert task.status_code == 404
    for response in (
        tool_context,
        canceled,
        retried,
        memory,
        artifact,
        download,
        artifact_read,
    ):
        assert response.status_code == 410


def test_direct_mentions_use_the_same_cross_turn_repeat_filter(tmp_path):
    runtime = make_runtime(tmp_path)

    async def repetitive_chat(*_args, **_kwargs):
        await asyncio.sleep(0)
        return "这个话题先把关键条件捋清楚再聊，别急着下结论。", {}

    runtime.hermes.chat = repetitive_chat
    with TestClient(create_app(runtime, start_worker=False)) as client:
        first = post_chat(
            client,
            {
                "message": "小格你怎么看",
                "request_id": "repeat-direct-one",
                "room_id": ROOM_ID,
                "sender_id": "wxid_member",
                "sender_name": "阿明",
                "source_local_id": 51,
                "msg_svr_id": "server-51",
                "mentions_bot": True,
            },
        )
        second = post_chat(
            client,
            {
                "message": "小格再说一遍",
                "request_id": "repeat-direct-two",
                "room_id": ROOM_ID,
                "sender_id": "wxid_member",
                "sender_name": "阿明",
                "source_local_id": 52,
                "msg_svr_id": "server-52",
                "mentions_bot": True,
            },
        )

    assert first.json()["status"] == "succeeded"
    assert second.json()["status"] == "ignored"
    assert second.json()["reply"] == ""


def test_legacy_relationship_flags_cannot_create_runtime_state(tmp_path):
    runtime = make_runtime(
        tmp_path,
        relationship_memory_enabled=True,
        relationship_proactive_enabled=True,
    )
    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            {
                "message": "忘掉我",
                "request_id": "retired-relationship-command",
                "room_id": ROOM_ID,
                "sender_id": "wxid_member",
                "source_local_id": 71,
                "msg_svr_id": "server-71",
                "mentions_bot": True,
            },
        )
        health = client.get("/health").json()

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert runtime.store.get_relationship_profile(ROOM_ID, "wxid_member") is None
    assert runtime.store.relationship_summary_counts()["queued"] == 0
    assert queue_due_relationship_nudge(runtime) is False
    assert health["relationship_memory"]["enabled"] is False
    assert health["relationship_memory"]["proactive"]["enabled"] is False


def test_twenty_four_diagnostic_sessions_keep_single_persona_and_no_delivery_path(
    tmp_path,
):
    runtime = make_runtime(tmp_path)
    responses = []
    with TestClient(create_app(runtime, start_worker=False)) as client:
        for index, message in enumerate(DIAGNOSTIC_PROBES, start=1):
            responses.append(
                post_chat(
                    client,
                    {
                        "message": message,
                        "request_id": "diagnostic-probe-%02d" % index,
                        "diagnostic_session_id": "persona-probe-%02d" % index,
                        "room_id": ROOM_ID,
                        "sender_id": "wxid_probe",
                        "source_local_id": 100 + index,
                        "msg_svr_id": "diagnostic-server-%02d" % index,
                        "mentions_bot": True,
                    },
                    internal_token="internal-secret",
                )
            )

    assert len(DIAGNOSTIC_PROBES) == 24
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["status"] == "succeeded" for response in responses)
    assert len(runtime.hermes.ensure_calls) == 24
    assert len(runtime.hermes.chat_calls) == 24
    assert len({call[0] for call in runtime.hermes.chat_calls}) == 24
    assert all(
        call[2] == CHAT_ONLY_SESSION_SYSTEM_PROMPT
        for call in runtime.hermes.ensure_calls
    )
    assert all(call[2] == "" and call[3] is True for call in runtime.hermes.chat_calls)
    assert runtime.store.list_tasks(ROOM_ID) == []
    assert runtime.chat_api.text == []


def test_session_reset_failure_blocks_chat_before_implicit_history_can_be_used(
    tmp_path,
):
    runtime = make_runtime(tmp_path)

    async def reject_reset(_session_id):
        raise RemoteAPIError("session reset failed")

    runtime.hermes.delete_session = reject_reset
    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            {
                "message": "接着聊",
                "request_id": "session-reset-failure",
                "room_id": ROOM_ID,
                "sender_id": "wxid_member",
                "source_local_id": 81,
                "msg_svr_id": "server-81",
                "mentions_bot": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert runtime.hermes.ensure_calls == []
    assert runtime.hermes.chat_calls == []
    assert runtime.store.list_tasks(ROOM_ID) == []

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app import main as main_module
from app.clients import RemoteAPIError
from app.evidence import build_execution_plan
from app.main import create_app, execute_task, terminal_delivery_text
from app.policy import stable_session_id
from tests.test_adapter import (
    ROOM_ID,
    FakeChatApi,
    create_task,
    make_runtime,
    post_chat,
)


def create_planned_task(runtime, request_id: str, prompt: str):
    plan = build_execution_plan(
        prompt,
        timeout_seconds=runtime.settings.max_task_seconds,
    )
    return runtime.store.create_task(
        request_id=request_id,
        request_hash="hash-" + request_id,
        room_id=ROOM_ID,
        sender_id="wxid_sender",
        session_id=stable_session_id(ROOM_ID, "wxid_sender"),
        kind="run",
        prompt=prompt,
        max_attempts=runtime.settings.max_task_attempts,
        source_local_id=10,
        plan=plan,
        delivery_policy=plan["delivery_policy"],
    )[0]


def install_completed_run(runtime, *, output: str, tool_event: dict | None = None):
    async def session_history(_session_id):
        return []

    async def start_run(
        _session_id,
        _message,
        _instructions,
        _history,
        *,
        idempotency_key,
    ):
        assert idempotency_key.startswith("task:T-")
        assert ":generation:" in idempotency_key
        assert ":attempt:" in idempotency_key
        return "run-v2"

    async def wait_run(
        _run_id,
        *,
        timeout_seconds,
        cancel_requested,
        event_callback,
    ):
        assert timeout_seconds > 0
        assert cancel_requested() is False
        if tool_event is not None:
            await event_callback(tool_event)
        await event_callback({"event": "run.completed"})
        return {
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 4, "output_tokens": 3},
        }

    runtime.hermes.session_history = session_history
    runtime.hermes.start_run = start_run
    runtime.hermes.wait_run = wait_run


def test_room_stop_commits_barrier_before_store_cancel_and_run_stop(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    task = create_task(runtime.store, request_id="barrier-order")
    claimed = runtime.store.claim_next()
    assert claimed["id"] == task["id"]
    runtime.store.set_run_id(task["id"], "run-barrier-order")

    original_cancel = runtime.store.cancel_room_tasks

    def tracked_cancel(room_id):
        runtime.chat_api.call_order.append(("store_cancel", room_id))
        return original_cancel(room_id)

    async def tracked_stop(run_id):
        runtime.chat_api.call_order.append(("hermes_stop", run_id))
        runtime.hermes.stop_calls.append(run_id)

    runtime.store.cancel_room_tasks = tracked_cancel
    runtime.hermes.stop_run = tracked_stop

    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            {
                "message": "停止",
                "request_id": "barrier-order-command",
                "room_id": ROOM_ID,
                "sender_id": "wxid_member",
                "source_local_id": 20,
                "mentions_bot": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "canceled"
    assert [item[0] for item in runtime.chat_api.call_order] == [
        "barrier",
        "store_cancel",
        "hermes_stop",
    ]


def test_rejected_stop_barrier_leaves_task_and_run_untouched(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    task = create_task(runtime.store, request_id="barrier-rejected")
    claimed = runtime.store.claim_next()
    assert claimed["id"] == task["id"]
    runtime.store.set_run_id(task["id"], "run-barrier-rejected")

    class RejectingChatApi(FakeChatApi):
        async def commit_barrier(self, *args, **kwargs):
            await super().commit_barrier(*args, **kwargs)
            return {"ok": False}

    runtime.chat_api = RejectingChatApi()
    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            {
                "message": "停止",
                "request_id": "barrier-rejected-command",
                "room_id": ROOM_ID,
                "sender_id": "wxid_member",
                "source_local_id": 20,
                "mentions_bot": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    stored = runtime.store.get_task(task["id"])
    assert stored["status"] == "running"
    assert stored["cancel_requested"] is False
    assert runtime.hermes.stop_calls == []


def test_unaddressed_bare_stop_without_activity_is_ignored(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            {
                "message": "别发了",
                "request_id": "bare-stop-no-activity",
                "room_id": ROOM_ID,
                "sender_id": "wxid_member",
                "source_local_id": 20,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "reply": "",
        "task_id": None,
        "generation": None,
        "status": "ignored",
        "media_type": None,
        "media_data": None,
        "media_url": None,
        "media_mime_type": None,
    }
    assert runtime.chat_api.barriers == []


def test_three_real_mentions_are_processed_independently(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        responses = [
            post_chat(
                client,
                {
                    "message": "你好",
                    "request_id": "mention-%d" % local_id,
                    "room_id": ROOM_ID,
                    "sender_id": "wxid_member",
                    "source_local_id": local_id,
                    "msg_svr_id": "svr-%d" % local_id,
                    "mentions_bot": True,
                },
            )
            for local_id in (101, 102, 103)
        ]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert len(runtime.hermes.chat_calls) == 3


def test_same_local_and_server_message_is_processed_once(tmp_path):
    runtime = make_runtime(tmp_path)
    base = {
        "message": "你好",
        "room_id": ROOM_ID,
        "sender_id": "wxid_member",
        "source_local_id": 101,
        "msg_svr_id": "server-101",
        "mentions_bot": True,
    }
    with TestClient(create_app(runtime, start_worker=False)) as client:
        first = post_chat(client, {**base, "request_id": "dedupe-first"})
        second = post_chat(client, {**base, "request_id": "dedupe-alias"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(runtime.hermes.chat_calls) == 1


def test_changed_local_alias_with_same_server_id_replays_once(tmp_path):
    runtime = make_runtime(tmp_path)
    base = {
        "message": "你好",
        "room_id": ROOM_ID,
        "sender_id": "wxid_member",
        "msg_svr_id": "server-stable",
        "mentions_bot": True,
    }
    with TestClient(create_app(runtime, start_worker=False)) as client:
        first = post_chat(
            client,
            {
                **base,
                "request_id": "local-alias-first",
                "source_local_id": 201,
            },
        )
        replay = post_chat(
            client,
            {
                **base,
                "request_id": "local-alias-second",
                "source_local_id": 202,
            },
        )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(runtime.hermes.chat_calls) == 1


def test_changed_server_alias_with_same_local_id_replays_once(tmp_path):
    runtime = make_runtime(tmp_path)
    base = {
        "message": "你好",
        "room_id": ROOM_ID,
        "sender_id": "wxid_member",
        "source_local_id": 301,
        "mentions_bot": True,
    }
    with TestClient(create_app(runtime, start_worker=False)) as client:
        first = post_chat(
            client,
            {
                **base,
                "request_id": "server-alias-first",
                "msg_svr_id": "server-first",
            },
        )
        replay = post_chat(
            client,
            {
                **base,
                "request_id": "server-alias-second",
                "msg_svr_id": "server-second",
            },
        )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(runtime.hermes.chat_calls) == 1


def test_sync_chat_timeout_is_queued_with_trusted_cursor(tmp_path):
    runtime = make_runtime(tmp_path, sync_chat_timeout_seconds=0.01)

    async def slow_chat(*args, **kwargs):
        await asyncio.sleep(0.05)
        return "late", {}

    runtime.hermes.chat = slow_chat
    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            {
                "message": "你好",
                "request_id": "sync-timeout",
                "room_id": ROOM_ID,
                "sender_id": "wxid_member",
                "source_local_id": 33,
                "mentions_bot": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    tasks = runtime.store.list_tasks(ROOM_ID)
    assert len(tasks) == 1
    assert tasks[0]["kind"] == "chat"
    assert tasks[0]["source_local_id"] == 33


def test_queued_chat_disables_tools_and_rejects_execution_plan_without_evidence(
    tmp_path,
):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    plan = build_execution_plan("运行服务器命令")
    task = runtime.store.create_task(
        request_id="queued-chat-no-tools",
        request_hash="hash-queued-chat-no-tools",
        room_id=ROOM_ID,
        sender_id="wxid_sender",
        session_id=stable_session_id(ROOM_ID, "wxid_sender"),
        kind="chat",
        prompt="普通问题",
        max_attempts=runtime.settings.max_task_attempts,
        source_local_id=33,
        plan=plan,
        delivery_policy=plan["delivery_policy"],
    )[0]
    claimed = runtime.store.claim_next()
    assert claimed["id"] == task["id"]

    asyncio.run(execute_task(runtime, claimed))

    assert runtime.hermes.chat_calls[0][3] is True
    stored = runtime.store.get_task(task["id"])
    assert stored["status"] == "failed"
    assert "exit code 0" in stored["error"]


def test_queued_chat_compacts_verbose_model_output(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    plan = build_execution_plan("普通问题")
    task = runtime.store.create_task(
        request_id="queued-chat-compact",
        request_hash="hash-queued-chat-compact",
        room_id=ROOM_ID,
        sender_id="wxid_sender",
        session_id=stable_session_id(ROOM_ID, "wxid_sender"),
        kind="chat",
        prompt="你怎么看",
        max_attempts=runtime.settings.max_task_attempts,
        source_local_id=34,
        plan=plan,
        delivery_policy=plan["delivery_policy"],
    )[0]
    claimed = runtime.store.claim_next()

    async def verbose_chat(*_args, **_kwargs):
        return (
            "没问题，核心是先修入口。其他功能都依赖它。"
            "第三句应该被删掉。",
            {"input_tokens": 5, "output_tokens": 15},
        )

    runtime.hermes.chat = verbose_chat
    asyncio.run(execute_task(runtime, claimed))

    stored = runtime.store.get_task(task["id"])
    assert stored["status"] == "succeeded"
    assert stored["output"] == "核心是先修入口。其他功能都依赖它。"


def test_run_completed_without_execution_evidence_fails_task(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    task = create_planned_task(runtime, "no-command-evidence", "运行服务器命令")
    claimed = runtime.store.claim_next()
    assert claimed["id"] == task["id"]
    install_completed_run(runtime, output="已经完成")

    asyncio.run(execute_task(runtime, claimed))

    stored = runtime.store.get_task(task["id"])
    assert stored["status"] == "failed"
    assert "exit code 0" in stored["error"]
    assert runtime.store.list_outbox(task["id"], stored["generation"])[0][
        "kind"
    ] == "text"


def test_successful_tool_evidence_allows_command_completion(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    task = create_planned_task(runtime, "command-evidence", "运行服务器命令")
    claimed = runtime.store.claim_next()
    assert claimed["id"] == task["id"]
    install_completed_run(
        runtime,
        output="命令执行完成",
        tool_event={
            "event": "tool.completed",
            "tool": "terminal",
            "exit_code": 0,
            "summary": "completed",
        },
    )

    asyncio.run(execute_task(runtime, claimed))

    stored = runtime.store.get_task(task["id"])
    assert stored["status"] == "succeeded"
    events = runtime.store.list_tool_events(task["id"], stored["generation"])
    assert any(
        event["event_type"] == "tool.completed" and event["exit_code"] == 0
        for event in events
    )


def test_blocked_task_supplement_rotates_generation_and_suppresses_question(
    tmp_path,
):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    task = create_planned_task(runtime, "blocked-task", "运行部署命令")
    claimed = runtime.store.claim_next()
    assert claimed["id"] == task["id"]
    install_completed_run(runtime, output="请提供目标服务器地址")
    asyncio.run(execute_task(runtime, claimed))

    blocked = runtime.store.get_task(task["id"])
    assert blocked["status"] == "queued"
    assert blocked["internal_state"] == "blocked_on_input"
    assert blocked["question_count"] == 1
    old_generation = blocked["generation"]
    assert runtime.store.list_outbox(task["id"], old_generation)[0][
        "state"
    ] == "prepared"

    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            {
                "message": "补充 %s 目标是测试服务器" % task["id"],
                "request_id": "blocked-supplement",
                "room_id": ROOM_ID,
                "sender_id": "wxid_member",
                "source_local_id": 20,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    revised = runtime.store.get_task(task["id"])
    assert revised["generation"] == old_generation + 1
    assert revised["internal_state"] == ""
    assert "目标是测试服务器" in revised["prompt"]
    assert runtime.store.list_outbox(task["id"], old_generation)[0][
        "state"
    ] == "suppressed"
    assert runtime.chat_api.barriers[0]["task_id"] == task["id"]
    assert runtime.chat_api.barriers[0]["generation"] == old_generation


def test_duplicate_tool_events_do_not_inflate_tool_call_limit(tmp_path):
    runtime = make_runtime(tmp_path, max_tool_calls=1)
    runtime.store.initialize()
    task = create_planned_task(
        runtime,
        "dedupe-tool-events",
        "运行服务器命令",
    )
    claimed = runtime.store.claim_next()
    assert claimed["id"] == task["id"]

    async def session_history(_session_id):
        return []

    async def start_run(*_args, **_kwargs):
        return "run-dedupe-events"

    async def wait_run(
        _run_id,
        *,
        timeout_seconds,
        cancel_requested,
        event_callback,
    ):
        duplicate = {
            "event": "tool.started",
            "tool": "terminal",
            "_adapter_event_key": "id:tool-started-1",
        }
        await event_callback(dict(duplicate))
        await event_callback(dict(duplicate))
        await event_callback(
            {
                "event": "tool.completed",
                "tool": "terminal",
                "exit_code": 0,
                "_adapter_event_key": "id:tool-completed-1",
            }
        )
        return {"status": "completed", "output": "done"}

    runtime.hermes.session_history = session_history
    runtime.hermes.start_run = start_run
    runtime.hermes.wait_run = wait_run
    asyncio.run(execute_task(runtime, claimed))

    stored = runtime.store.get_task(task["id"])
    assert stored["status"] == "succeeded"
    assert runtime.store.tool_call_count(task["id"], claimed["generation"]) == 1
    assert runtime.hermes.stop_calls == []


def test_tool_call_resource_limit_stops_run_and_finishes_failed(tmp_path):
    runtime = make_runtime(tmp_path, max_tool_calls=1)
    runtime.store.initialize()
    task = create_planned_task(
        runtime,
        "tool-call-limit",
        "运行服务器命令",
    )
    claimed = runtime.store.claim_next()
    assert claimed["id"] == task["id"]

    async def session_history(_session_id):
        return []

    async def start_run(*_args, **_kwargs):
        return "run-tool-limit"

    async def wait_run(
        _run_id,
        *,
        timeout_seconds,
        cancel_requested,
        event_callback,
    ):
        await event_callback(
            {
                "event": "tool.started",
                "tool": "terminal",
                "_adapter_event_key": "id:tool-started-1",
            }
        )
        await event_callback(
            {
                "event": "tool.started",
                "tool": "terminal",
                "_adapter_event_key": "id:tool-started-2",
            }
        )
        return {"status": "canceled", "output": ""}

    runtime.hermes.session_history = session_history
    runtime.hermes.start_run = start_run
    runtime.hermes.wait_run = wait_run
    asyncio.run(execute_task(runtime, claimed))

    stored = runtime.store.get_task(task["id"])
    assert stored["status"] == "failed"
    assert "工具调用次数" in stored["error"]
    assert runtime.hermes.stop_calls == ["run-tool-limit"]


def test_research_plan_limit_and_search_guidance_override_larger_global_limit(
    tmp_path,
):
    runtime = make_runtime(tmp_path, max_tool_calls=80)
    runtime.store.initialize()
    task = create_planned_task(
        runtime,
        "research-tool-call-limit",
        "搜索今天的国内外 AI 新闻并给出来源",
    )
    claimed = runtime.store.claim_next()
    assert claimed["plan"]["max_tool_calls"] == 12
    captured_instructions = []

    async def session_history(_session_id):
        return []

    async def start_run(
        _session_id,
        _message,
        instructions,
        _history,
        **_kwargs,
    ):
        captured_instructions.append(instructions)
        return "run-research-tool-limit"

    async def wait_run(
        _run_id,
        *,
        timeout_seconds,
        cancel_requested,
        event_callback,
    ):
        for index in range(1, 26):
            await event_callback(
                {
                    "event": "tool.started",
                    "tool": "web_search",
                    "_adapter_event_key": "id:research-started-%d" % index,
                }
            )
        return {"status": "canceled", "output": ""}

    runtime.hermes.session_history = session_history
    runtime.hermes.start_run = start_run
    runtime.hermes.wait_run = wait_run
    asyncio.run(execute_task(runtime, claimed))

    stored = runtime.store.get_task(task["id"])
    assert stored["status"] == "failed"
    assert "上限 12" in stored["error"]
    assert runtime.hermes.stop_calls == ["run-research-tool-limit"]
    assert "web_search 4 次" in captured_instructions[0]
    assert "web_extract 3 次" in captured_instructions[0]
    assert "官方、一手资料和可信主流媒体" in captured_instructions[0]
    assert "研究日期为" in captured_instructions[0]
    assert "主体在前、ISO 日期放末尾" in captured_instructions[0]
    assert "分别用中文和英文各搜索一次" in captured_instructions[0]


def test_failed_hermes_run_retries_with_a_new_execution_idempotency_key(
    tmp_path,
):
    runtime = make_runtime(tmp_path, max_task_attempts=3)
    runtime.store.initialize()
    task = create_planned_task(
        runtime,
        "transient-run-failure",
        "运行服务器命令",
    )
    keys = []
    runs_by_key = {}

    async def session_history(_session_id):
        return []

    async def start_run(
        _session_id,
        _message,
        _instructions,
        _history,
        *,
        idempotency_key,
    ):
        keys.append(idempotency_key)
        return runs_by_key.setdefault(
            idempotency_key,
            "run-attempt-%d" % (len(runs_by_key) + 1),
        )

    async def wait_run(
        run_id,
        *,
        timeout_seconds,
        cancel_requested,
        event_callback,
    ):
        if run_id == "run-attempt-1":
            await event_callback(
                {
                    "event": "run.failed",
                    "_adapter_event_key": "id:retry-success",
                }
            )
            return {"status": "failed", "output": ""}
        await event_callback(
            {
                "event": "tool.completed",
                "tool": "terminal",
                "exit_code": 0,
                "_adapter_event_key": "id:retry-success",
            }
        )
        return {"status": "completed", "output": "done"}

    runtime.hermes.session_history = session_history
    runtime.hermes.start_run = start_run
    runtime.hermes.wait_run = wait_run

    first = runtime.store.claim_next()
    asyncio.run(execute_task(runtime, first))
    after_first = runtime.store.get_task(task["id"])
    assert after_first["status"] == "queued"
    assert after_first["hermes_run_id"] is None

    second = runtime.store.claim_next()
    asyncio.run(execute_task(runtime, second))
    completed = runtime.store.get_task(task["id"])

    assert completed["status"] == "succeeded"
    assert completed["attempts"] == 2
    assert keys[0].endswith(":attempt:1")
    assert keys[1].endswith(":attempt:2")
    assert keys[0] != keys[1]
    outbox = runtime.store.list_outbox(task["id"], task["generation"])
    assert [(item["kind"], item["state"]) for item in outbox] == [
        ("text", "prepared")
    ]


def test_transient_failed_run_waits_before_requeue(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path, max_task_attempts=3)
    runtime.store.initialize()
    task = create_planned_task(
        runtime,
        "transient-run-backoff",
        "搜索今天的 AI 新闻并给出来源",
    )
    claimed = runtime.store.claim_next()

    async def session_history(_session_id):
        return []

    async def start_run(*_args, **_kwargs):
        return "run-rate-limited"

    async def wait_run(*_args, **_kwargs):
        return {
            "status": "failed",
            "output": "",
            "error": "provider returned HTTP 429",
        }

    delays = []

    async def record_sleep(delay):
        delays.append(delay)

    runtime.hermes.session_history = session_history
    runtime.hermes.start_run = start_run
    runtime.hermes.wait_run = wait_run
    monkeypatch.setattr(main_module.asyncio, "sleep", record_sleep)

    asyncio.run(execute_task(runtime, claimed))

    current = runtime.store.get_task(task["id"])
    assert current["status"] == "queued"
    assert current["hermes_run_id"] is None
    assert delays == [20]


def test_failed_run_with_tool_activity_is_not_automatically_replayed(tmp_path):
    runtime = make_runtime(tmp_path, max_task_attempts=3)
    runtime.store.initialize()
    task = create_planned_task(
        runtime,
        "failed-run-after-side-effect",
        "运行服务器命令",
    )
    claimed = runtime.store.claim_next()
    start_calls = []

    async def session_history(_session_id):
        return []

    async def start_run(*_args, **_kwargs):
        start_calls.append(_kwargs["idempotency_key"])
        return "run-with-side-effect"

    async def wait_run(
        _run_id,
        *,
        timeout_seconds,
        cancel_requested,
        event_callback,
    ):
        await event_callback(
            {
                "event": "tool.completed",
                "tool": "terminal",
                "exit_code": 0,
                "_adapter_event_key": "id:side-effect",
            }
        )
        return {
            "status": "failed",
            "output": "",
            "error": "provider disconnected after tool execution",
        }

    runtime.hermes.session_history = session_history
    runtime.hermes.start_run = start_run
    runtime.hermes.wait_run = wait_run

    asyncio.run(execute_task(runtime, claimed))

    current = runtime.store.get_task(task["id"])
    assert current["status"] == "failed"
    assert current["attempts"] == 1
    assert "避免重复副作用" in current["error"]
    assert len(start_calls) == 1


def test_stop_during_run_creation_stops_late_run_and_finishes_canceled(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    task = create_planned_task(
        runtime,
        "stop-during-run-creation",
        "运行服务器命令",
    )
    claimed = runtime.store.claim_next()

    async def session_history(_session_id):
        return []

    async def start_run(*_args, **_kwargs):
        canceled = runtime.store.cancel_task(task["id"], task["room_id"])
        assert canceled["cancel_requested"] is True
        return "run-created-before-stop-won"

    runtime.hermes.session_history = session_history
    runtime.hermes.start_run = start_run

    asyncio.run(execute_task(runtime, claimed))

    current = runtime.store.get_task(task["id"])
    assert current["status"] == "canceled"
    assert current["hermes_run_id"] is None
    assert runtime.hermes.stop_calls == ["run-created-before-stop-won"]
    outbox = runtime.store.list_outbox(task["id"], task["generation"])
    assert [(item["kind"], item["state"]) for item in outbox] == [
        ("text", "prepared")
    ]


def test_terminal_model_failure_preserves_redacted_reason_and_actionable_retry(
    tmp_path,
):
    runtime = make_runtime(tmp_path, max_task_attempts=1)
    runtime.store.initialize()
    task = create_planned_task(
        runtime,
        "terminal-provider-failure",
        "搜索今天的 AI 新闻并给出来源",
    )
    claimed = runtime.store.claim_next()

    async def session_history(_session_id):
        return []

    async def start_run(*_args, **_kwargs):
        return "run-provider-failure"

    async def wait_run(*_args, **_kwargs):
        return {
            "status": "failed",
            "output": "",
            "error": "HTTP 503 Service unavailable Bearer secret-token-value",
        }

    runtime.hermes.session_history = session_history
    runtime.hermes.start_run = start_run
    runtime.hermes.wait_run = wait_run
    asyncio.run(execute_task(runtime, claimed))

    failed = runtime.store.get_task(task["id"])
    assert failed["status"] == "failed"
    assert "HTTP 503" in failed["error"]
    assert "secret-token-value" not in failed["error"]
    text = terminal_delivery_text(runtime, failed)
    assert "模型恢复后" in text
    assert "重试 %s" % task["id"] in text


def test_running_modification_does_not_rotate_generation_when_stop_fails(
    tmp_path,
):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    task = create_task(runtime.store, request_id="modify-stop-fails")
    claimed = runtime.store.claim_next()
    runtime.store.set_run_id(task["id"], "run-modify-stop-fails")

    async def reject_stop(_run_id):
        raise RemoteAPIError("stop failed")

    runtime.hermes.stop_run = reject_stop
    with TestClient(create_app(runtime, start_worker=False)) as client:
        response = post_chat(
            client,
            {
                "message": "修改 %s 改为生成文字报告" % task["id"],
                "request_id": "modify-stop-fails-command",
                "room_id": ROOM_ID,
                "sender_id": "wxid_member",
                "source_local_id": 30,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    stored = runtime.store.get_task(task["id"])
    assert stored["generation"] == claimed["generation"]
    assert stored["status"] == "running"
    assert stored["prompt"] == claimed["prompt"]

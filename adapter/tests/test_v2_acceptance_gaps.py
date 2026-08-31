from __future__ import annotations

import asyncio
import sqlite3
import time
import urllib.parse
from pathlib import Path

from fastapi.testclient import TestClient

from app.clients import RemoteAPIError
from app.evidence import build_execution_plan
from app.main import (
    create_app,
    deliver_outbox_item,
    deliver_task,
    prepare_task_outbox,
)
from app.media import validate_media_path
from app.policy import stable_session_id
from tests.test_adapter import FakeChatApi, ROOM_ID, create_task, make_runtime


def create_running_planned_task(runtime, request_id: str, prompt: str):
    runtime.store.initialize()
    plan = build_execution_plan(
        prompt,
        timeout_seconds=runtime.settings.max_task_seconds,
    )
    task = runtime.store.create_task(
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
    claimed = runtime.store.claim_next()
    assert claimed["id"] == task["id"]
    return claimed


def register_file(
    runtime,
    task,
    name: str,
    content: bytes,
    *,
    role: str = "primary",
):
    task_root = runtime.settings.artifact_root / task["id"]
    task_root.mkdir(parents=True, exist_ok=True)
    path = task_root / name
    path.write_bytes(content)
    validated = validate_media_path(
        str(path),
        runtime.settings.artifact_root,
        task["id"],
        runtime.settings.max_artifact_bytes,
        runtime.settings.max_image_bytes,
    )
    registered = runtime.store.register_artifact(
        task_id=task["id"],
        generation=task["generation"],
        name=validated.name,
        path=validated.path,
        mime_type=validated.mime_type,
        size_bytes=validated.size_bytes,
        sha256=validated.sha256,
        max_count=runtime.settings.max_artifact_count,
        max_total_bytes=runtime.settings.max_artifact_total_bytes,
        role=role,
    )
    return path, registered


def test_media_preflight_failure_never_calls_chat_api(tmp_path):
    runtime = make_runtime(tmp_path)
    task = create_running_planned_task(
        runtime,
        "media-preflight",
        "生成一张图片",
    )
    path, _artifact = register_file(
        runtime,
        task,
        "result.png",
        b"\x89PNG\r\n\x1a\ncontent",
    )
    runtime.store.complete(task["id"], "succeeded", output="done")
    current = runtime.store.get_task(task["id"])
    outbox = prepare_task_outbox(runtime, current)
    assert outbox[0]["kind"] == "image"

    path.write_bytes(b"\xff\xd8\xffchanged")
    asyncio.run(deliver_outbox_item(runtime, outbox[0]))

    stored = runtime.store.list_outbox(task["id"], task["generation"])
    assert stored[0]["state"] == "failed"
    assert runtime.chat_api.checks == []
    assert runtime.chat_api.images == []


def test_unknown_success_payload_maps_outbox_to_uncertain(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    task = create_task(runtime.store, request_id="unknown-success")
    runtime.store.complete(task["id"], "succeeded", output="done")

    class UnknownSuccess(FakeChatApi):
        async def send_text_item(self, *args, **kwargs):
            self.text.append(args)
            return {"ok": True}

    runtime.chat_api = UnknownSuccess()
    asyncio.run(deliver_task(runtime, runtime.store.get_task(task["id"])))

    outbox = runtime.store.list_outbox(task["id"], task["generation"])
    assert outbox[0]["state"] == "uncertain"
    assert len(runtime.chat_api.text) == 1


def test_idempotency_conflict_maps_outbox_to_failed(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    task = create_task(runtime.store, request_id="outbox-conflict")
    runtime.store.complete(task["id"], "succeeded", output="done")

    class Conflict(FakeChatApi):
        async def send_text_item(self, *args, **kwargs):
            self.text.append(args)
            raise RemoteAPIError(
                "conflict",
                status_code=422,
                error_type="idempotency_conflict",
            )

    runtime.chat_api = Conflict()
    asyncio.run(deliver_task(runtime, runtime.store.get_task(task["id"])))

    outbox = runtime.store.list_outbox(task["id"], task["generation"])
    assert outbox[0]["state"] == "failed"
    assert len(runtime.chat_api.text) == 1


def test_retryable_pre_submission_text_is_attempted_at_most_three_times(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.initialize()
    task = create_task(runtime.store, request_id="bounded-text-retry")
    runtime.store.complete(task["id"], "succeeded", output="done")

    class RetryableFailure(FakeChatApi):
        async def send_text_item(self, *args, **kwargs):
            self.text.append(args)
            raise RemoteAPIError(
                "not submitted",
                error_type="connection_failed",
                pre_submission=True,
                retryable=True,
            )

    runtime.chat_api = RetryableFailure()
    asyncio.run(deliver_task(runtime, runtime.store.get_task(task["id"])))

    outbox = runtime.store.list_outbox(task["id"], task["generation"])
    assert outbox[0]["state"] == "failed"
    assert outbox[0]["attempts"] == 3
    assert len(runtime.chat_api.text) == 3


def test_bundle_policy_keeps_first_two_and_zips_the_rest(tmp_path):
    runtime = make_runtime(tmp_path, max_delivery_media_items=3)
    task = create_running_planned_task(runtime, "bundle-three", "生成文件")
    registered = [
        register_file(runtime, task, "result-%d.txt" % index, b"file-%d" % index)[1]
        for index in range(4)
    ]
    runtime.store.complete(task["id"], "succeeded", output="done")

    outbox = prepare_task_outbox(runtime, runtime.store.get_task(task["id"]))

    assert [item["kind"] for item in outbox] == ["file", "file", "file", "text"]
    assert [item["artifact_id"] for item in outbox[:2]] == [
        registered[0]["artifact_id"],
        registered[1]["artifact_id"],
    ]
    bundled = runtime.store.get_artifact(outbox[2]["artifact_id"])
    assert bundled["role"] == "delivery_bundle"
    assert bundled["mime_type"] == "application/zip"


def test_bundle_policy_with_limit_one_zips_everything(tmp_path):
    runtime = make_runtime(tmp_path, max_delivery_media_items=1)
    task = create_running_planned_task(runtime, "bundle-one", "生成文件")
    for index in range(3):
        register_file(runtime, task, "result-%d.txt" % index, b"file-%d" % index)
    runtime.store.complete(task["id"], "succeeded", output="done")

    outbox = prepare_task_outbox(runtime, runtime.store.get_task(task["id"]))

    assert [item["kind"] for item in outbox] == ["file", "text"]
    bundled = runtime.store.get_artifact(outbox[0]["artifact_id"])
    assert bundled["role"] == "delivery_bundle"
    assert bundled["mime_type"] == "application/zip"


def test_only_primary_artifacts_are_delivered(tmp_path):
    runtime = make_runtime(tmp_path)
    task = create_running_planned_task(runtime, "primary-only", "生成文件")
    primary = register_file(runtime, task, "final.txt", b"final")[1]
    register_file(runtime, task, "debug.txt", b"debug", role="debug")
    runtime.store.complete(task["id"], "succeeded", output="done")

    outbox = prepare_task_outbox(runtime, runtime.store.get_task(task["id"]))

    media = [item for item in outbox if item["kind"] != "text"]
    assert len(media) == 1
    assert media[0]["artifact_id"] == primary["artifact_id"]


def signed_artifact_path(runtime, task, artifact) -> str:
    expires = int(time.time()) + 60
    url = runtime.signer.immutable_url(
        artifact_id=artifact["artifact_id"],
        task_id=task["id"],
        generation=task["generation"],
        name=artifact["name"],
        sha256=artifact["sha256"],
        size_bytes=artifact["size_bytes"],
        mime_type=artifact["mime_type"],
        expires=expires,
    )
    parsed = urllib.parse.urlsplit(url)
    return parsed.path + "?" + parsed.query


def test_immutable_artifact_download_is_retired_in_chat_only_release(tmp_path):
    runtime = make_runtime(tmp_path)
    task = create_running_planned_task(runtime, "artifact-download", "生成文件")
    path, artifact = register_file(runtime, task, "result.txt", b"immutable")
    request_path = signed_artifact_path(runtime, task, artifact)

    with TestClient(
        create_app(runtime, start_worker=False),
        client=("127.0.0.1", 50000),
    ) as client:
        success = client.get(request_path)
        assert success.status_code == 410
        assert "chat-only" in success.json()["detail"]

    assert runtime.store.get_artifact(artifact["artifact_id"])["verified"] == 1


def test_stale_generation_artifact_download_returns_410(tmp_path):
    runtime = make_runtime(tmp_path)
    task = create_running_planned_task(runtime, "artifact-stale", "生成文件")
    _path, artifact = register_file(runtime, task, "result.txt", b"old")
    request_path = signed_artifact_path(runtime, task, artifact)
    plan = build_execution_plan("生成新的文件")
    revised = runtime.store.revise_task(
        task["id"],
        ROOM_ID,
        "生成新的文件",
        plan=plan,
        delivery_policy=plan["delivery_policy"],
    )
    assert revised["generation"] == task["generation"] + 1

    with TestClient(
        create_app(runtime, start_worker=False),
        client=("127.0.0.1", 50000),
    ) as client:
        response = client.get(request_path)

    assert response.status_code == 410


def test_blocked_input_expiry_suppresses_question_and_sends_one_summary(tmp_path):
    runtime = make_runtime(tmp_path)
    task = create_running_planned_task(
        runtime,
        "blocked-expiry",
        "运行部署命令",
    )
    assert runtime.store.block_on_input(
        task["id"],
        "请提供目标服务器地址",
        generation=task["generation"],
    )
    runtime.store.prepare_outbox(
        task["id"],
        task["generation"],
        [
            {
                "kind": "text",
                "content": "请提供目标服务器地址",
                "source_local_id": task["source_local_id"],
            }
        ],
    )
    with sqlite3.connect(runtime.store.path) as connection:
        connection.execute(
            "UPDATE tasks SET blocked_until=? WHERE id=?",
            (time.time() - 1, task["id"]),
        )
        connection.commit()

    assert runtime.store.expire_blocked_tasks() == 1
    expired = runtime.store.get_task(task["id"])
    before_delivery = runtime.store.list_outbox(task["id"], task["generation"])
    assert expired["status"] == "failed"
    assert [item["state"] for item in before_delivery] == [
        "suppressed",
        "prepared",
    ]
    assert before_delivery[1]["is_summary"] == 1

    asyncio.run(deliver_task(runtime, expired))

    after_delivery = runtime.store.list_outbox(task["id"], task["generation"])
    assert [item["state"] for item in after_delivery] == [
        "suppressed",
        "confirmed",
    ]
    assert len(runtime.chat_api.text) == 1
    assert "等待补充信息超过 24 小时" in runtime.chat_api.text[0][1]


def test_cancel_blocked_task_suppresses_question_and_sends_cancel_summary(tmp_path):
    runtime = make_runtime(tmp_path)
    task = create_running_planned_task(
        runtime,
        "blocked-cancel",
        "运行部署命令",
    )
    assert runtime.store.block_on_input(
        task["id"],
        "请提供目标服务器地址",
        generation=task["generation"],
    )
    runtime.store.prepare_outbox(
        task["id"],
        task["generation"],
        [
            {
                "kind": "text",
                "content": "请提供目标服务器地址",
                "source_local_id": task["source_local_id"],
            }
        ],
    )

    canceled = runtime.store.cancel_task(task["id"], ROOM_ID)
    assert canceled["status"] == "canceled"
    prepare_task_outbox(runtime, runtime.store.get_task(task["id"]))
    outbox = runtime.store.list_outbox(task["id"], task["generation"])
    assert [item["state"] for item in outbox] == ["suppressed", "prepared"]
    assert [item["is_summary"] for item in outbox] == [0, 1]

    asyncio.run(deliver_task(runtime, runtime.store.get_task(task["id"])))

    assert len(runtime.chat_api.text) == 1
    assert "已取消" in runtime.chat_api.text[0][1]

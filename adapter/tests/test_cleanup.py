from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from app.store import AdapterStore
from app.relationship import RELATIONSHIP_TTL_SECONDS
from cleanup import (
    cleanup_artifacts,
    cleanup_database,
    cleanup_hermes_runtime_files,
    main,
)


def test_cleanup_only_removes_expired_task_artifacts_and_terminal_records(tmp_path):
    artifact_root = tmp_path / "artifacts"
    old_task_dir = artifact_root / "T-11111111"
    current_task_dir = artifact_root / "T-22222222"
    unrelated_dir = artifact_root / "original-project"
    for directory in (old_task_dir, current_task_dir, unrelated_dir):
        directory.mkdir(parents=True)
        (directory / "data.txt").write_text("data", encoding="utf-8")
    old_time = time.time() - 10 * 86400
    os.utime(old_task_dir, (old_time, old_time))
    os.utime(unrelated_dir, (old_time, old_time))

    removed = cleanup_artifacts(artifact_root, time.time() - 7 * 86400)
    assert removed == 1
    assert not old_task_dir.exists()
    assert current_task_dir.exists()
    assert unrelated_dir.exists()

    store = AdapterStore(tmp_path / "adapter.db")
    store.initialize()
    task, _ = store.create_task(
        request_id="cleanup-request",
        request_hash="cleanup-hash",
        room_id="00000000000@chatroom",
        sender_id="wxid_sender",
        session_id="session",
        kind="run",
        prompt="prompt",
        max_attempts=1,
        source_local_id=1,
    )
    claimed = store.claim_next()
    assert claimed["id"] == task["id"]
    artifact_path = artifact_root / task["id"] / "result.txt"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("verified", encoding="utf-8")
    artifact = store.register_artifact(
        task_id=task["id"],
        generation=task["generation"],
        name=artifact_path.name,
        path=artifact_path,
        mime_type="text/plain",
        size_bytes=artifact_path.stat().st_size,
        sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        max_count=10,
        max_total_bytes=1024,
    )
    store.add_tool_event(
        task_id=task["id"],
        generation=task["generation"],
        run_id="run-cleanup",
        event_type="tool.completed",
        tool_name="files",
        exit_code=0,
        artifact_id=artifact["artifact_id"],
    )
    store.complete(task["id"], "succeeded", output="done")
    store.prepare_outbox(
        task["id"],
        task["generation"],
        [
            {
                "kind": "file",
                "artifact_id": artifact["artifact_id"],
                "content": artifact_path.name,
                "source_local_id": 1,
            }
        ],
    )
    store.mark_delivery_success(task["id"])
    inbound = store.begin_inbound(
        request_id="cleanup-inbound",
        request_hash="cleanup-inbound-hash",
        room_id="00000000000@chatroom",
        sender_id="wxid_sender",
        source_local_id=2,
        msg_svr_id="cleanup-server-id",
    )
    assert inbound["created"] is True
    store.save_response(
        "cleanup-inbound",
        "cleanup-inbound-hash",
        {"reply": "ok", "status": "succeeded"},
    )
    cutoff = time.time() - 30 * 86400
    stale = cutoff - 10
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE tasks SET completed_at=?, updated_at=? WHERE id=?",
            (stale, stale, task["id"]),
        )
        connection.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=?",
            (stale, task["id"]),
        )
        connection.execute(
            "UPDATE request_cache SET created_at=?",
            (stale,),
        )
        connection.execute(
            "UPDATE inbound_ledger SET created_at=?, updated_at=?",
            (stale, stale),
        )
        connection.execute(
            "UPDATE tool_events SET created_at=? WHERE task_id=?",
            (stale, task["id"]),
        )
        connection.commit()

    counts = cleanup_database(store.path, cutoff)
    assert counts["tasks"] == 1
    assert counts["artifacts"] == 1
    assert counts["outbox_items"] == 1
    assert counts["tool_events"] == 1
    assert counts["inbound_ledger"] == 1
    assert store.get_task(task["id"]) is None
    with sqlite3.connect(store.path) as connection:
        for table in (
            "artifacts",
            "outbox_items",
            "tool_events",
            "task_events",
        ):
            assert connection.execute(
                "SELECT COUNT(*) FROM %s" % table
            ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cleanup_expires_relationship_profiles_and_retains_active_summary_jobs(
    tmp_path,
):
    store = AdapterStore(tmp_path / "adapter.db")
    store.initialize()
    now = 1_800_000_000.0
    room_id = "00000000000@chatroom"
    expired_sender = "wxid_expired"
    fresh_sender = "wxid_fresh"
    expired_created_at = now - RELATIONSHIP_TTL_SECONDS - 10
    store.record_relationship_interaction(
        room_id,
        fresh_sender,
        source_local_id=2,
        now=now - 5,
    )
    store.record_relationship_interaction(
        room_id,
        expired_sender,
        source_local_id=1,
        now=expired_created_at,
    )
    store.apply_relationship_summary(
        room_id,
        expired_sender,
        {
            "preferred_name": "旧称呼",
            "banter_style": "neutral",
            "reciprocity_delta": 0,
            "notes": [{"kind": "preference", "value": "过期偏好"}],
        },
        source_local_id=1,
        now=expired_created_at,
    )
    store.observe_group_listener_message(
        "expired-listener@chatroom",
        1,
        now=expired_created_at,
    )
    store.observe_group_listener_message(room_id, 2, now=now - 5)

    record_cutoff = now - 30 * 86400
    stale = record_cutoff - 1
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO relationship_summary_jobs(
                room_id, sender_id, source_local_id, interaction_count,
                trigger, status, attempts, error_type, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'succeeded', 1, '', ?, ?)
            """,
            (room_id, expired_sender, 1, 1, "test-terminal", stale, stale),
        )
        connection.execute(
            """
            INSERT INTO relationship_summary_jobs(
                room_id, sender_id, source_local_id, interaction_count,
                trigger, status, attempts, error_type, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'queued', 0, '', ?, ?)
            """,
            (room_id, fresh_sender, 2, 1, "test-active", stale, stale),
        )
        connection.commit()

    counts = cleanup_database(store.path, record_cutoff, now=now)

    assert counts["relationship_profiles"] == 1
    assert counts["relationship_notes"] == 1
    assert counts["relationship_summary_jobs"] == 1
    assert counts["group_listener_state"] == 1
    assert store.get_relationship_profile(room_id, expired_sender, now=now) is None
    assert store.get_relationship_profile(room_id, fresh_sender, now=now) is not None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT status FROM relationship_summary_jobs"
        ).fetchall() == [("queued",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cleanup_expires_companion_context_and_retains_active_companion_summary(
    tmp_path,
):
    store = AdapterStore(tmp_path / "adapter.db")
    store.initialize()
    now = 1_800_000_000.0
    record_cutoff = now - 30 * 86400
    stale = record_cutoff - 1
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO companion_timeline(
                room_id, event_id, local_id, sender_id, sender_name, direction,
                text, message_timestamp, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "expired-companion@chatroom",
                "incoming:1",
                1,
                "wxid_member",
                "旧群友",
                "incoming",
                "过期群消息",
                now - 24 * 60 * 60 - 1,
                stale,
            ),
        )
        connection.execute(
            """
            INSERT INTO room_companion_state(
                room_id, mood, shared_jokes_json, open_loops_json, summary,
                message_count, created_at, updated_at, expires_at
            ) VALUES (?, 'warm', '[]', '[]', '过期摘要', 1, ?, ?, ?)
            """,
            ("expired-companion@chatroom", stale, stale, now - 1),
        )
        connection.execute(
            """
            INSERT INTO companion_summary_jobs(
                room_id, source_local_id, trigger, status, attempts, error_type,
                created_at, updated_at
            ) VALUES (?, 1, 'terminal', 'succeeded', 1, '', ?, ?)
            """,
            ("expired-companion@chatroom", stale, stale),
        )
        connection.execute(
            """
            INSERT INTO companion_summary_jobs(
                room_id, source_local_id, trigger, status, attempts, error_type,
                created_at, updated_at
            ) VALUES (?, 2, 'active', 'queued', 0, '', ?, ?)
            """,
            ("active-companion@chatroom", stale, stale),
        )
        connection.commit()

    counts = cleanup_database(store.path, record_cutoff, now=now)

    assert counts["companion_timeline"] == 1
    assert counts["room_companion_state"] == 1
    assert counts["companion_summary_jobs"] == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM companion_timeline"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM room_companion_state"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM companion_summary_jobs"
        ).fetchall() == [("queued",)]


def touch_with_age(path: Path, age_days: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("data", encoding="utf-8")
    timestamp = time.time() - age_days * 86400
    os.utime(path, (timestamp, timestamp))


def test_cleanup_only_removes_allowed_expired_hermes_runtime_files(tmp_path):
    home = tmp_path / "home"
    old_dump = home / ".hermes/sessions/request_dump_old.json"
    fresh_dump = home / ".hermes/sessions/request_dump_fresh.json"
    old_log = home / ".hermes/logs/hermes.log.31"
    current_log = home / ".hermes/logs/hermes.log"
    old_npm = home / ".npm/_logs/2026-01-01-debug-0.log"
    unrelated = home / ".hermes/sessions/conversation.json"
    touch_with_age(old_dump, 31)
    touch_with_age(fresh_dump, 1)
    touch_with_age(old_log, 31)
    touch_with_age(current_log, 31)
    touch_with_age(old_npm, 31)
    touch_with_age(unrelated, 31)

    counts = cleanup_hermes_runtime_files(
        home,
        time.time() - 30 * 86400,
    )

    assert sum(counts.values()) == 3
    assert not old_dump.exists()
    assert fresh_dump.exists()
    assert not old_log.exists()
    assert current_log.exists()
    assert not old_npm.exists()
    assert unrelated.exists()


def test_cleanup_refuses_matching_symlinks(tmp_path):
    home = tmp_path / "home"
    sessions = home / ".hermes/sessions"
    sessions.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    touch_with_age(outside, 31)
    linked = sessions / "request_dump_link.json"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(RuntimeError, match="symbolic links"):
        cleanup_hermes_runtime_files(
            home,
            time.time() - 30 * 86400,
        )


def test_artifact_cleanup_refuses_matching_symlink(tmp_path):
    artifact_root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    artifact_root.mkdir()
    outside.mkdir()
    linked = artifact_root / "T-ABCDEF12"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    old = time.time() - 10 * 86400
    try:
        os.utime(linked, (old, old), follow_symlinks=False)
    except (NotImplementedError, OSError):
        pytest.skip("updating symbolic-link timestamps is unavailable")

    with pytest.raises(RuntimeError, match="symbolic links"):
        cleanup_artifacts(artifact_root, time.time() - 7 * 86400)


def test_artifact_cleanup_records_permission_failure_and_continues(
    tmp_path,
    monkeypatch,
):
    artifact_root = tmp_path / "artifacts"
    blocked = artifact_root / "T-11111111"
    removable = artifact_root / "T-22222222"
    for directory in (blocked, removable):
        directory.mkdir(parents=True)
        old = time.time() - 10 * 86400
        os.utime(directory, (old, old))

    real_rmtree = __import__("cleanup").shutil.rmtree

    def guarded_rmtree(path):
        if Path(path).name == blocked.name:
            raise PermissionError("fixture")
        return real_rmtree(path)

    monkeypatch.setattr("cleanup.shutil.rmtree", guarded_rmtree)
    failures = []
    removed = cleanup_artifacts(
        artifact_root,
        time.time() - 7 * 86400,
        failures=failures,
    )

    assert removed == 1
    assert blocked.exists()
    assert not removable.exists()
    assert failures == [
        {"task_id": blocked.name, "error_type": "PermissionError"}
    ]


def test_main_records_failure_but_runs_database_and_runtime_cleanup(
    tmp_path,
    monkeypatch,
    capsys,
):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    database = tmp_path / "adapter.db"
    sqlite3.connect(database).close()
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    status_file = tmp_path / "cleanup-status.json"
    calls = []

    def failed_artifacts(*args, **kwargs):
        calls.append("artifacts")
        raise PermissionError("fixture")

    def cleaned_database(*args, **kwargs):
        calls.append("database")
        return {"tasks": 0}

    def cleaned_runtime(*args, **kwargs):
        calls.append("runtime")
        return {"sessions": 0}

    monkeypatch.setattr("cleanup.cleanup_artifacts", failed_artifacts)
    monkeypatch.setattr("cleanup.cleanup_database", cleaned_database)
    monkeypatch.setattr(
        "cleanup.cleanup_hermes_runtime_files",
        cleaned_runtime,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cleanup.py",
            "--artifact-root",
            str(artifact_root),
            "--database",
            str(database),
            "--hermes-home",
            str(hermes_home),
            "--status-file",
            str(status_file),
        ],
    )

    assert main() == 1
    assert calls == ["artifacts", "database", "runtime"]
    status = json.loads(status_file.read_text(encoding="utf-8"))
    assert status["ok"] is False
    assert status["errors"] == [
        {"stage": "artifacts", "error_type": "PermissionError"}
    ]
    assert json.loads(capsys.readouterr().out) == status

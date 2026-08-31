from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from app.policy import stable_session_id
from app.store import (
    AdapterStore,
    MAX_DELIVERY_ATTEMPTS,
    PURE_CHAT_RUNTIME_MODE,
    PREFERENCE_MEMORY_TTL_SECONDS,
    PROJECT_MEMORY_TTL_SECONDS,
)


ROOM_ID = "v2-room@chatroom"


def test_pure_chat_runtime_mode_matches_current_persona_generation():
    assert PURE_CHAT_RUNTIME_MODE == "sunxiaochuan-chat-only-v16"


def queued_task(store: AdapterStore, request_id: str, local_id: int = 1):
    return store.create_task(
        request_id=request_id,
        request_hash="hash-" + request_id,
        room_id=ROOM_ID,
        sender_id="wxid_sender",
        session_id=stable_session_id(ROOM_ID, "wxid_sender"),
        kind="run",
        prompt="execute a real task",
        max_attempts=3,
        source_local_id=local_id,
    )[0]


def running_task(store: AdapterStore, request_id: str, local_id: int = 1):
    task = queued_task(store, request_id, local_id)
    claimed = store.claim_next()
    assert claimed["id"] == task["id"]
    return claimed


def test_claim_next_recovers_created_run_before_fifo_queue(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    recovering = running_task(store, "recovering-run", 1)
    assert store.set_run_id(recovering["id"], "run-recovering")
    queued = queued_task(store, "queued-after-restart", 2)

    claimed = store.claim_next()

    assert claimed["id"] == recovering["id"]
    assert store.get_task(queued["id"])["status"] == "queued"


def test_uncertain_run_creation_reuses_the_same_execution_attempt(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = queued_task(store, "uncertain-run-creation")
    first = store.claim_next()
    assert first["attempts"] == 1

    assert store.requeue_uncertain_run_creation(
        task["id"],
        "run creation outcome unknown",
        generation=first["generation"],
    ) is True
    queued = store.get_task(task["id"])
    assert queued["status"] == "queued"
    assert queued["attempts"] == 0
    assert queued["hermes_run_id"] is None

    second = store.claim_next()
    assert second["attempts"] == 1


def test_restart_between_run_creation_and_run_id_save_reuses_attempt(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = queued_task(store, "crash-before-run-id-save")
    first = store.claim_next()
    assert first["attempts"] == 1
    first_key = "task:%s:generation:%d:attempt:%d" % (
        task["id"],
        first["generation"],
        first["attempts"],
    )

    assert store.recover() == 1
    recovered = store.get_task(task["id"])
    assert recovered["status"] == "queued"
    assert recovered["attempts"] == 0
    assert recovered["hermes_run_id"] is None

    second = store.claim_next()
    second_key = "task:%s:generation:%d:attempt:%d" % (
        task["id"],
        second["generation"],
        second["attempts"],
    )
    assert second["attempts"] == 1
    assert second_key == first_key


def test_late_run_id_cannot_resurrect_a_canceled_task(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "cancel-before-run-id-save")

    canceled = store.cancel_task(task["id"], task["room_id"])
    assert canceled["cancel_requested"] is True
    assert store.set_run_id(
        task["id"],
        "run-too-late",
        generation=task["generation"],
    ) is False

    current = store.get_task(task["id"])
    assert current["cancel_requested"] is True
    assert current["hermes_run_id"] is None


def test_claim_next_keeps_explicit_retry_in_original_fifo_position(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    retry_candidate = running_task(store, "retry-candidate", 1)
    assert store.complete(retry_candidate["id"], "failed", error="try again")
    queued_after = queued_task(store, "queued-after-failure", 2)
    retried = store.retry_task(retry_candidate["id"], retry_candidate["room_id"])
    assert retried["status"] == "queued"

    first = store.claim_next()
    assert first["id"] == retry_candidate["id"]
    assert store.complete(first["id"], "succeeded", output="done")
    second = store.claim_next()
    assert second["id"] == queued_after["id"]


def test_claim_next_does_not_prioritize_newer_retry_over_older_queue(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    retry_candidate = running_task(store, "newer-retry", 1)
    assert store.complete(retry_candidate["id"], "failed", error="try again")
    older_queued = queued_task(store, "older-queued", 2)

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE tasks SET created_at=created_at-60 WHERE id=?",
            (older_queued["id"],),
        )

    retried = store.retry_task(retry_candidate["id"], retry_candidate["room_id"])
    assert retried["status"] == "queued"

    claimed = store.claim_next()
    assert claimed["id"] == older_queued["id"]


def test_initialize_migrates_legacy_tool_events_before_creating_index(tmp_path):
    database = tmp_path / "adapter.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE tool_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                run_id TEXT,
                event_type TEXT NOT NULL,
                tool_name TEXT,
                exit_code INTEGER,
                result_summary TEXT,
                source TEXT,
                artifact_id TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        connection.commit()

    AdapterStore(database).initialize()
    AdapterStore(database).initialize()

    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_events)")
        }
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(tool_events)")
        }
    assert "event_key" in columns
    assert "idx_tool_events_key" in indexes


def test_tool_event_idempotency_is_scoped_to_one_hermes_run(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "run-scoped-tool-events")
    values = {
        "task_id": task["id"],
        "generation": task["generation"],
        "event_key": "id:1",
        "event_type": "tool.completed",
        "tool_name": "terminal",
        "exit_code": 0,
    }

    assert store.add_tool_event(run_id="run-1", **values) is True
    assert store.add_tool_event(run_id="run-1", **values) is False
    assert store.add_tool_event(run_id="run-2", **values) is True

    assert len(store.list_tool_events(task["id"], task["generation"])) == 2
    second = store.list_tool_events(
        task["id"],
        task["generation"],
        run_id="run-2",
    )
    assert len(second) == 1
    assert second[0]["run_id"] == "run-2"


def test_initialize_deduplicates_legacy_summaries_before_unique_index(tmp_path):
    database = tmp_path / "adapter.db"
    store = AdapterStore(database)
    task = running_task(store, "legacy-duplicate-summary")
    store.complete(task["id"], "succeeded", output="done")
    summary = store.prepare_outbox(
        task["id"],
        task["generation"],
        [
            {
                "kind": "text",
                "source_local_id": task["source_local_id"],
                "content": "first summary",
                "is_summary": True,
            }
        ],
    )[0]

    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_outbox_one_summary")
        connection.execute(
            """
            INSERT INTO outbox_items(
                task_id, generation, item_index, kind, content, state,
                idempotency_key, source_local_id, attempts, is_summary,
                created_at, updated_at
            )
            SELECT task_id, generation, item_index + 1, kind, 'confirmed summary',
                   'confirmed', idempotency_key || ':legacy-duplicate',
                   source_local_id, 1, 1, created_at, updated_at
            FROM outbox_items
            WHERE id=?
            """,
            (summary["id"],),
        )
        connection.commit()

    AdapterStore(database).initialize()

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT state, is_summary, error
            FROM outbox_items
            WHERE task_id=? AND generation=?
            ORDER BY item_index
            """,
            (task["id"], task["generation"]),
        ).fetchall()
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(outbox_items)")
        }
        task_row = connection.execute(
            "SELECT final_sent, delivery_suppressed FROM tasks WHERE id=?",
            (task["id"],),
        ).fetchone()

    assert sum(int(row["is_summary"]) for row in rows) == 1
    assert rows[0]["state"] == "suppressed"
    assert rows[0]["error"] == "duplicate summary suppressed during migration"
    assert rows[1]["state"] == "confirmed"
    assert rows[1]["is_summary"] == 1
    assert "idx_outbox_one_summary" in indexes
    assert task_row["final_sent"] == 1
    assert task_row["delivery_suppressed"] == 1


def test_inbound_ledger_deduplicates_request_local_and_server_ids(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    first = store.begin_inbound(
        request_id="request-1",
        request_hash="hash-1",
        room_id=ROOM_ID,
        sender_id="wxid_a",
        source_local_id=100,
        msg_svr_id="9001",
    )
    assert first["created"] is True

    for request_id, local_id, server_id in (
        ("request-1", 100, "9001"),
        ("request-alias", 100, ""),
        ("request-server-alias", 101, "9001"),
    ):
        replay = store.begin_inbound(
            request_id=request_id,
            request_hash="hash-1",
            room_id=ROOM_ID,
            sender_id="wxid_a",
            source_local_id=local_id,
            msg_svr_id=server_id,
        )
        assert replay["created"] is False
        assert replay["request_id"] == "request-1"

    with pytest.raises(ValueError, match="different content"):
        store.begin_inbound(
            request_id="request-spoof",
            request_hash="other-hash",
            room_id=ROOM_ID,
            sender_id="wxid_a",
            source_local_id=100,
            msg_svr_id="",
        )


def test_inbound_ledger_reclaims_only_startup_recovery_leases(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    first = store.begin_inbound(
        request_id="request-crash",
        request_hash="hash-crash",
        room_id=ROOM_ID,
        sender_id="wxid_a",
        source_local_id=102,
        msg_svr_id="9002",
    )
    assert first["created"] is True

    concurrent = store.begin_inbound(
        request_id="request-crash",
        request_hash="hash-crash",
        room_id=ROOM_ID,
        sender_id="wxid_a",
        source_local_id=102,
        msg_svr_id="9002",
    )
    assert concurrent["created"] is False

    assert store.recover_inbound() == 1
    reclaimed = store.begin_inbound(
        request_id="request-crash-alias",
        request_hash="hash-crash",
        room_id=ROOM_ID,
        sender_id="wxid_a",
        source_local_id=102,
        msg_svr_id="9002",
    )
    assert reclaimed["created"] is True
    assert reclaimed["request_id"] == "request-crash"

    second_concurrent = store.begin_inbound(
        request_id="request-crash",
        request_hash="hash-crash",
        room_id=ROOM_ID,
        sender_id="wxid_a",
        source_local_id=102,
        msg_svr_id="9002",
    )
    assert second_concurrent["created"] is False

    assert store.recover_inbound() == 1
    with pytest.raises(ValueError, match="different content"):
        store.begin_inbound(
            request_id="request-crash",
            request_hash="tampered",
            room_id=ROOM_ID,
            sender_id="wxid_a",
            source_local_id=102,
            msg_svr_id="9002",
        )


def test_artifact_id_is_deterministic_and_content_scoped(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "artifact")
    artifact_path = tmp_path / "artifacts" / task["id"] / "result.bin"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"verified")
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    first = store.register_artifact(
        task_id=task["id"],
        generation=task["generation"],
        name=artifact_path.name,
        path=artifact_path,
        mime_type="application/octet-stream",
        size_bytes=artifact_path.stat().st_size,
        sha256=digest,
        max_count=10,
        max_total_bytes=1024,
    )
    second = store.register_artifact(
        task_id=task["id"],
        generation=task["generation"],
        name=artifact_path.name,
        path=artifact_path,
        mime_type="application/octet-stream",
        size_bytes=artifact_path.stat().st_size,
        sha256=digest,
        max_count=10,
        max_total_bytes=1024,
    )
    expected_material = (
        f"{task['id']}\n{task['generation']}\n"
        f"{artifact_path.resolve()}\n{artifact_path.name}\n{digest}"
    )
    assert first["artifact_id"] == (
        "A-" + hashlib.sha256(expected_material.encode()).hexdigest()[:16].upper()
    )
    assert second["artifact_id"] == first["artifact_id"]


def test_outbox_recovery_never_retries_submitted_media_and_caps_text(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "outbox")
    store.complete(task["id"], "succeeded", output="done")
    items = store.prepare_outbox(
        task["id"],
        task["generation"],
        [
            {
                "kind": "image",
                "source_local_id": task["source_local_id"],
                "content": "artifact",
            },
            {
                "kind": "text",
                "source_local_id": task["source_local_id"],
                "content": "summary",
                "is_summary": True,
            },
        ],
    )
    store.mark_outbox_sending(items[0]["id"])
    store.mark_outbox_sending(items[1]["id"])
    assert store.recover_outbox() == 2
    recovered = store.list_outbox(task["id"], task["generation"])
    assert recovered[0]["state"] == "uncertain"
    assert recovered[1]["state"] == "uncertain"

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE outbox_items
            SET state='sending', attempts=?
            WHERE id=?
            """,
            (MAX_DELIVERY_ATTEMPTS, items[1]["id"]),
        )
        connection.commit()
    store.recover_outbox()
    recovered = store.list_outbox(task["id"], task["generation"])
    assert recovered[1]["state"] == "uncertain"


def test_outbox_reconciliation_projects_confirmation_and_errors(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "outbox-reconcile")
    store.complete(task["id"], "succeeded", output="done")
    items = store.prepare_outbox(
        task["id"],
        task["generation"],
        [
            {
                "kind": "image",
                "source_local_id": task["source_local_id"],
                "content": "artifact",
            },
            {
                "kind": "text",
                "source_local_id": task["source_local_id"],
                "content": "summary",
                "is_summary": True,
            },
        ],
    )
    store.mark_outbox_sending(items[0]["id"])
    store.mark_outbox_sending(items[1]["id"])

    assert store.reconcile_outbox_item(
        items[0]["id"],
        "uncertain",
        error="confirmation unavailable",
    )
    assert store.reconcile_outbox_item(
        items[1]["id"],
        "confirmed",
        confirmed_local_id=99,
    )

    current = store.get_task(task["id"])
    assert current["final_sent"] is True
    assert current["delivery_suppressed"] is True
    assert current["delivery_attempts"] == 2
    assert current["delivery_error"] == "confirmation unavailable"


def test_terminal_task_with_existing_question_gets_exactly_one_summary(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "summary-unique")
    store.prepare_outbox(
        task["id"],
        task["generation"],
        [
            {
                "kind": "text",
                "source_local_id": task["source_local_id"],
                "content": "question",
            }
        ],
    )
    store.complete(task["id"], "canceled")

    terminal = store.next_terminal_without_outbox()
    assert terminal["id"] == task["id"]
    store.prepare_outbox(
        task["id"],
        task["generation"],
        [
            {
                "kind": "text",
                "source_local_id": task["source_local_id"],
                "content": "",
                "is_summary": True,
            }
        ],
    )
    store.prepare_outbox(
        task["id"],
        task["generation"],
        [
            {
                "kind": "text",
                "source_local_id": task["source_local_id"],
                "content": "",
                "is_summary": True,
            }
        ],
    )

    outbox = store.list_outbox(task["id"], task["generation"])
    assert [item["is_summary"] for item in outbox] == [0, 1]
    assert store.next_terminal_without_outbox() is None


def test_legacy_terminal_task_is_not_selected_for_outbox_recovery(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "legacy-terminal")
    assert store.complete(task["id"], "succeeded", output="historical result")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE tasks SET outbox_required=0 WHERE id=?",
            (task["id"],),
        )

    assert store.next_terminal_without_outbox() is None
    assert store.prepare_outbox(
        task["id"],
        task["generation"],
        [
            {
                "kind": "text",
                "source_local_id": task["source_local_id"],
                "content": "",
                "is_summary": True,
            }
        ],
    ) == []


def test_new_terminal_task_remains_eligible_for_outbox_recovery(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "new-terminal")
    assert task["outbox_required"] is True
    assert store.complete(task["id"], "succeeded", output="new result")

    terminal = store.next_terminal_without_outbox()

    assert terminal["id"] == task["id"]
    assert terminal["outbox_required"] is True


def test_initialize_terminalizes_legacy_prepared_and_sending_outbox(tmp_path):
    database = tmp_path / "adapter.db"
    store = AdapterStore(database)
    prepared_task = queued_task(store, "legacy-prepared", 11)
    sending_task = queued_task(store, "legacy-sending", 12)
    prepared = store.prepare_outbox(
        prepared_task["id"],
        prepared_task["generation"],
        [
            {
                "kind": "text",
                "source_local_id": prepared_task["source_local_id"],
                "content": "",
                "is_summary": True,
            }
        ],
    )[0]
    sending = store.prepare_outbox(
        sending_task["id"],
        sending_task["generation"],
        [
            {
                "kind": "image",
                "source_local_id": sending_task["source_local_id"],
                "content": "historical artifact",
            }
        ],
    )[0]
    store.mark_outbox_sending(sending["id"])
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE tasks SET outbox_required=0 WHERE id IN (?, ?)",
            (prepared_task["id"], sending_task["id"]),
        )

    restarted = AdapterStore(database)
    restarted.initialize()

    assert restarted.list_outbox(
        prepared_task["id"],
        prepared_task["generation"],
    )[0]["state"] == "suppressed"
    assert restarted.list_outbox(
        sending_task["id"],
        sending_task["generation"],
    )[0]["state"] == "uncertain"
    assert restarted.list_recoverable_outbox() == []
    assert restarted.next_outbox() is None
    assert prepared["id"] != sending["id"]


def test_explicit_retry_reenables_outbox_for_legacy_task(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "legacy-retry")
    assert store.complete(task["id"], "failed", error="historical failure")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE tasks SET outbox_required=0 WHERE id=?",
            (task["id"],),
        )

    retried = store.retry_task(task["id"], ROOM_ID)

    assert retried["outbox_required"] is True
    assert retried["generation"] == task["generation"] + 1
    claimed = store.claim_next()
    assert claimed["id"] == task["id"]
    assert store.complete(
        task["id"],
        "succeeded",
        output="retried result",
        generation=claimed["generation"],
    )
    assert store.next_terminal_without_outbox()["id"] == task["id"]


def test_explicit_revision_reenables_outbox_for_legacy_task(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "legacy-revision")
    assert store.complete(task["id"], "succeeded", output="historical result")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE tasks SET outbox_required=0 WHERE id=?",
            (task["id"],),
        )

    revised = store.revise_task(
        task["id"],
        ROOM_ID,
        "perform the revised task",
        plan={"task_type": "general"},
        delivery_policy="text_only",
    )

    assert revised["outbox_required"] is True
    assert revised["generation"] == task["generation"] + 1
    claimed = store.claim_next()
    assert claimed["id"] == task["id"]
    assert store.complete(
        task["id"],
        "succeeded",
        output="revised result",
        generation=claimed["generation"],
    )
    assert store.next_terminal_without_outbox()["id"] == task["id"]


def test_cancel_does_not_relabel_media_already_sending(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "cancel")
    items = store.prepare_outbox(
        task["id"],
        task["generation"],
        [
            {
                "kind": "image",
                "source_local_id": task["source_local_id"],
                "content": "artifact",
            }
        ],
    )
    store.mark_outbox_sending(items[0]["id"])
    store.cancel_task(task["id"], ROOM_ID)
    assert store.list_outbox(task["id"], task["generation"])[0]["state"] == "sending"


def test_obsolete_generation_cannot_complete_revised_task(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "late-generation")
    old_generation = task["generation"]
    revised = store.revise_task(
        task["id"],
        ROOM_ID,
        "new request",
        plan={"task_type": "general"},
        delivery_policy="text_only",
    )
    assert revised["generation"] == old_generation + 1
    assert revised["status"] == "queued"

    completed = store.complete(
        task["id"],
        "succeeded",
        output="late old result",
        generation=old_generation,
    )
    assert completed is False
    current = store.get_task(task["id"])
    assert current["generation"] == old_generation + 1
    assert current["status"] == "queued"
    assert current["output"] is None


def test_cancel_request_wins_against_late_success_completion(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    task = running_task(store, "cancel-race")
    canceled = store.cancel_task(task["id"], ROOM_ID)
    assert canceled["status"] == "running"
    assert canceled["cancel_requested"] is True

    assert store.complete(
        task["id"],
        "succeeded",
        output="late success that must not be exposed",
        generation=task["generation"],
    )
    current = store.get_task(task["id"])
    assert current["status"] == "canceled"
    assert current["output"] is None
    assert store.complete(
        task["id"],
        "failed",
        error="later terminal overwrite",
        generation=task["generation"],
    ) is False
    assert store.get_task(task["id"])["status"] == "canceled"


def test_memory_ttl_expiry_last_used_and_replacement(tmp_path, monkeypatch):
    store = AdapterStore(tmp_path / "adapter.db")
    preference_task = running_task(store, "memory-pref")
    before = time.time()
    memory = store.update_memory_for_task(
        preference_task["id"],
        action="set",
        key="content style",
        value="concise",
    )[0]
    assert PREFERENCE_MEMORY_TTL_SECONDS - 3 <= memory["expires_at"] - before
    assert memory["last_used_at"] is not None

    store.complete(preference_task["id"], "succeeded", output="ok")
    project_task = running_task(store, "memory-project")
    project = store.update_memory_for_task(
        project_task["id"],
        action="set",
        key="project fact",
        value="cloud only",
    )[0]
    assert PROJECT_MEMORY_TTL_SECONDS - 3 <= project["expires_at"] - time.time()

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE scope_memory SET expires_at=?
            WHERE scope_type='room' AND scope_id=? AND key='project fact'
            """,
            (time.time() - 1, ROOM_ID),
        )
        connection.commit()
    listed = store.list_scope_memory(ROOM_ID, "wxid_other")
    assert [item["key"] for item in listed] == ["content style"]

    store.complete(project_task["id"], "succeeded", output="ok")
    replacement_task = running_task(store, "memory-replace")
    replaced = store.update_memory_for_task(
        replacement_task["id"],
        action="set",
        key="content style",
        value="direct",
    )[0]
    assert replaced["replaces_source_task_id"] == preference_task["id"]


def test_skill_registry_supports_atomic_update_revoke_and_snapshot(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    digest = hashlib.sha256(b"skill-v1").hexdigest()
    registered = store.register_skill(
        name="research",
        version="1.0.0",
        source="registry://research@1.0.0",
        sha256=digest,
        capabilities=["network", "files"],
        audit={"passed": True},
    )
    assert registered["enabled"] is True
    assert store.skill_snapshot() == [
        {
            "name": "research",
            "version": "1.0.0",
            "source": "registry://research@1.0.0",
            "sha256": digest,
            "capabilities": ["network", "files"],
        }
    ]

    revoked = store.revoke_skill("research")
    assert revoked["enabled"] is False
    assert revoked["revoked_at"] is not None
    assert store.skill_snapshot() == []


def test_skill_registry_sync_updates_inventory_and_revokes_missing_entries(
    tmp_path,
):
    store = AdapterStore(tmp_path / "adapter.db")
    first_digest = hashlib.sha256(b"skill-v1").hexdigest()
    second_digest = hashlib.sha256(b"skill-v2").hexdigest()

    first = store.sync_skill_registry(
        [
            {
                "name": "research",
                "version": "1.0.0",
                "source": "registry/research@1.0.0",
                "bundle_sha256": first_digest,
                "capabilities": ["network"],
                "audit": {"passed": True},
            },
            {
                "name": "writing",
                "version": "1.0.0",
                "source": "registry/writing@1.0.0",
                "bundle_sha256": second_digest,
                "capabilities": ["files"],
                "audit": {"passed": True},
            },
        ]
    )
    assert all(item is not None for item in first)

    synced = store.sync_skill_registry(
        [
            {
                "name": "research",
                "version": "1.1.0",
                "source": "registry/research@1.1.0",
                "bundle_sha256": second_digest,
                "capabilities": ["files", "network"],
                "audit": {"passed": True, "revision": 2},
            }
        ]
    )

    assert [item["name"] for item in synced] == ["research", "writing"]
    research = store.get_skill("research")
    writing = store.get_skill("writing")
    assert research["version"] == "1.1.0"
    assert research["capabilities"] == ["files", "network"]
    assert research["enabled"] is True
    assert writing["enabled"] is False
    assert writing["revoked_at"] is not None


def test_skill_registry_sync_validation_is_atomic(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")
    digest = hashlib.sha256(b"trusted").hexdigest()
    store.sync_skill_registry(
        [
            {
                "name": "trusted",
                "version": "1.0.0",
                "source": "registry/trusted@1.0.0",
                "bundle_sha256": digest,
                "capabilities": [],
                "audit": {"passed": True},
            }
        ]
    )

    with pytest.raises(ValueError, match="inventory entry is invalid"):
        store.sync_skill_registry(
            [
                {
                    "name": "new-valid",
                    "version": "1.0.0",
                    "source": "registry/new-valid@1.0.0",
                    "bundle_sha256": hashlib.sha256(b"new").hexdigest(),
                    "capabilities": [],
                    "audit": {"passed": True},
                },
                {
                    "name": "invalid",
                    "version": "1.0.0",
                    "source": "registry/invalid@1.0.0",
                    "bundle_sha256": "not-a-digest",
                    "capabilities": [],
                    "audit": {"passed": True},
                },
            ]
        )

    assert store.get_skill("new-valid") is None
    trusted = store.get_skill("trusted")
    assert trusted["enabled"] is True
    assert trusted["sha256"] == digest

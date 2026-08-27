from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .security import (
    budget_day_bounds,
    contains_sensitive_memory,
    estimated_cost,
    normalized_memory_key,
    normalized_memory_value,
    redact_sensitive_text,
    usage_tokens,
)
from .relationship import (
    MAX_RELATIONSHIP_NOTES,
    RELATIONSHIP_TTL_SECONDS,
    familiarity_for_interactions,
    normalize_relationship_summary,
)


TASK_STATUSES = {"queued", "running", "succeeded", "failed", "canceled"}
TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}
MAX_DELIVERY_ATTEMPTS = 3
PROJECT_MEMORY_TTL_SECONDS = 90 * 24 * 60 * 60
PREFERENCE_MEMORY_TTL_SECONDS = 180 * 24 * 60 * 60
OUTBOX_STATES = {
    "prepared",
    "sending",
    "confirmed",
    "uncertain",
    "suppressed",
    "failed",
}
OUTBOX_TERMINAL_STATES = {"confirmed", "uncertain", "suppressed", "failed"}
RUNNING_HERMES_STATUSES = {
    "started",
    "queued",
    "running",
    "stopping",
    "waiting_for_approval",
}
HERMES_STATUS_MAP = {
    "started": "queued",
    "queued": "queued",
    "running": "running",
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "canceled",
    "canceled": "canceled",
    "stopping": "running",
}


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AdapterStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS request_cache (
                        request_id TEXT PRIMARY KEY,
                        request_hash TEXT NOT NULL,
                        response_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS inbound_ledger (
                        request_id TEXT PRIMARY KEY,
                        request_hash TEXT NOT NULL,
                        room_id TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        source_local_id INTEGER,
                        msg_svr_id TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL
                            CHECK(state IN ('processing', 'completed')),
                        response_json TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_inbound_room_local
                    ON inbound_ledger(room_id, source_local_id)
                    WHERE source_local_id IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_inbound_room_svr
                    ON inbound_ledger(room_id, msg_svr_id)
                    WHERE msg_svr_id <> '';

                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        request_id TEXT NOT NULL UNIQUE,
                        request_hash TEXT NOT NULL,
                        room_id TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('run', 'chat')),
                        prompt TEXT NOT NULL,
                        status TEXT NOT NULL,
                        hermes_run_id TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 3,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        output TEXT,
                        error TEXT,
                        usage_json TEXT,
                        final_sent INTEGER NOT NULL DEFAULT 0,
                        delivery_generation INTEGER NOT NULL DEFAULT 0,
                        delivery_attempts INTEGER NOT NULL DEFAULT 0,
                        delivery_error TEXT,
                        delivery_suppressed INTEGER NOT NULL DEFAULT 0,
                        source_local_id INTEGER,
                        source_msg_svr_id TEXT NOT NULL DEFAULT '',
                        generation INTEGER NOT NULL DEFAULT 1,
                        plan_json TEXT,
                        delivery_policy TEXT NOT NULL DEFAULT 'text_only',
                        internal_state TEXT NOT NULL DEFAULT '',
                        blocked_until REAL,
                        question_count INTEGER NOT NULL DEFAULT 0,
                        skill_snapshot_json TEXT,
                        resource_error TEXT,
                        outbox_required INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        started_at REAL,
                        completed_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_tasks_queue
                    ON tasks(status, created_at);
                    CREATE INDEX IF NOT EXISTS idx_tasks_delivery
                    ON tasks(final_sent, status, completed_at);
                    CREATE INDEX IF NOT EXISTS idx_tasks_room
                    ON tasks(room_id, created_at);

                    CREATE TABLE IF NOT EXISTS task_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        detail TEXT,
                        created_at REAL NOT NULL,
                        FOREIGN KEY(task_id) REFERENCES tasks(id)
                    );

                    CREATE TABLE IF NOT EXISTS usage_ledger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT,
                        session_id TEXT,
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        estimated_cost_usd REAL NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS downloaded_artifacts (
                        task_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        path TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        sha256 TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(task_id, name)
                    );

                    CREATE TABLE IF NOT EXISTS artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        path TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        sha256 TEXT NOT NULL,
                        verified INTEGER NOT NULL DEFAULT 1,
                        role TEXT NOT NULL DEFAULT 'primary',
                        created_at REAL NOT NULL,
                        UNIQUE(task_id, generation, path),
                        FOREIGN KEY(task_id) REFERENCES tasks(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_artifacts_task
                    ON artifacts(task_id, generation, created_at);

                    CREATE TABLE IF NOT EXISTS outbox_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        item_index INTEGER NOT NULL,
                        kind TEXT NOT NULL
                            CHECK(kind IN ('text', 'image', 'video', 'file')),
                        artifact_id TEXT,
                        content TEXT,
                        state TEXT NOT NULL
                            CHECK(state IN (
                                'prepared', 'sending', 'confirmed',
                                'uncertain', 'suppressed', 'failed'
                            )),
                        idempotency_key TEXT NOT NULL UNIQUE,
                        source_local_id INTEGER NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        confirmed_local_id INTEGER,
                        media_fingerprint TEXT,
                        error TEXT,
                        is_summary INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        FOREIGN KEY(task_id) REFERENCES tasks(id),
                        FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id),
                        UNIQUE(task_id, generation, item_index)
                    );
                    CREATE INDEX IF NOT EXISTS idx_outbox_pending
                    ON outbox_items(state, created_at, id);
                    CREATE INDEX IF NOT EXISTS idx_outbox_task
                    ON outbox_items(task_id, generation, item_index);
                    CREATE TABLE IF NOT EXISTS tool_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        run_id TEXT,
                        event_key TEXT NOT NULL DEFAULT '',
                        event_type TEXT NOT NULL,
                        tool_name TEXT,
                        exit_code INTEGER,
                        result_summary TEXT,
                        source TEXT,
                        artifact_id TEXT,
                        created_at REAL NOT NULL,
                        FOREIGN KEY(task_id) REFERENCES tasks(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_tool_events_task
                    ON tool_events(task_id, generation, id);

                    CREATE TABLE IF NOT EXISTS scope_memory (
                        scope_type TEXT NOT NULL
                            CHECK(scope_type IN ('room', 'private')),
                        scope_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        source_task_id TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL,
                        last_used_at REAL,
                        replaces_source_task_id TEXT,
                        PRIMARY KEY(scope_type, scope_id, key)
                    );
                    CREATE INDEX IF NOT EXISTS idx_scope_memory_updated
                    ON scope_memory(scope_type, scope_id, updated_at);

                    CREATE TABLE IF NOT EXISTS relationship_profiles (
                        room_id TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        preferred_name TEXT NOT NULL DEFAULT '',
                        interaction_count INTEGER NOT NULL DEFAULT 0,
                        familiarity INTEGER NOT NULL DEFAULT 0
                            CHECK(familiarity BETWEEN 0 AND 4),
                        reciprocity INTEGER NOT NULL DEFAULT 0
                            CHECK(reciprocity BETWEEN 0 AND 3),
                        banter_style TEXT NOT NULL DEFAULT 'neutral'
                            CHECK(banter_style IN ('neutral', 'soft', 'playful', 'direct')),
                        flirt_opt_out INTEGER NOT NULL DEFAULT 0
                            CHECK(flirt_opt_out IN (0, 1)),
                        last_source_local_id INTEGER,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        PRIMARY KEY(room_id, sender_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_relationship_profiles_expiry
                    ON relationship_profiles(expires_at);

                    CREATE TABLE IF NOT EXISTS relationship_notes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        room_id TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        kind TEXT NOT NULL
                            CHECK(kind IN ('preference', 'inside_joke', 'boundary')),
                        value TEXT NOT NULL,
                        source_local_id INTEGER,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        UNIQUE(room_id, sender_id, kind, value),
                        FOREIGN KEY(room_id, sender_id)
                            REFERENCES relationship_profiles(room_id, sender_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_relationship_notes_profile
                    ON relationship_notes(room_id, sender_id, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_relationship_notes_expiry
                    ON relationship_notes(expires_at);

                    CREATE TABLE IF NOT EXISTS room_session_epochs (
                        room_id TEXT PRIMARY KEY,
                        epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch >= 0),
                        reason TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS group_listener_state (
                        room_id TEXT PRIMARY KEY,
                        last_observed_local_id INTEGER,
                        last_reply_local_id INTEGER,
                        last_reply_at REAL,
                        turns_since_reply INTEGER NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_group_listener_state_updated
                    ON group_listener_state(updated_at);

                    CREATE TABLE IF NOT EXISTS relationship_summary_jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        room_id TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        source_local_id INTEGER,
                        interaction_count INTEGER NOT NULL,
                        trigger TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'dropped')),
                        attempts INTEGER NOT NULL DEFAULT 0,
                        error_type TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_relationship_jobs_pending
                    ON relationship_summary_jobs(status, created_at, id);
                    DROP INDEX IF EXISTS idx_relationship_jobs_active;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_jobs_active
                    ON relationship_summary_jobs(room_id, sender_id)
                    WHERE status='queued';

                    CREATE TABLE IF NOT EXISTS skill_registry (
                        name TEXT PRIMARY KEY,
                        version TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        capabilities_json TEXT,
                        audit_json TEXT,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        revoked_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    """
                )
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
                }
                if "delivery_generation" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE tasks
                        ADD COLUMN delivery_generation INTEGER NOT NULL DEFAULT 0
                        """
                    )
                if "started_at" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE tasks
                        ADD COLUMN started_at REAL
                        """
                    )
                if "delivery_suppressed" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE tasks
                        ADD COLUMN delivery_suppressed INTEGER NOT NULL DEFAULT 0
                        """
                    )
                task_migrations = {
                    "source_msg_svr_id": "TEXT NOT NULL DEFAULT ''",
                    "generation": "INTEGER NOT NULL DEFAULT 1",
                    "plan_json": "TEXT",
                    "delivery_policy": "TEXT NOT NULL DEFAULT 'text_only'",
                    "internal_state": "TEXT NOT NULL DEFAULT ''",
                    "blocked_until": "REAL",
                    "question_count": "INTEGER NOT NULL DEFAULT 0",
                    "skill_snapshot_json": "TEXT",
                    "resource_error": "TEXT",
                    "outbox_required": "INTEGER NOT NULL DEFAULT 0",
                }
                for name, definition in task_migrations.items():
                    if name not in columns:
                        connection.execute(
                            "ALTER TABLE tasks ADD COLUMN %s %s"
                            % (name, definition)
                        )
                legacy_outbox = connection.execute(
                    """
                    SELECT DISTINCT o.task_id, o.generation
                    FROM outbox_items o
                    JOIN tasks t ON t.id=o.task_id
                    WHERE t.outbox_required=0
                      AND o.state IN ('prepared', 'sending')
                    """
                ).fetchall()
                connection.execute(
                    """
                    UPDATE outbox_items
                    SET state=CASE
                            WHEN state='sending' THEN 'uncertain'
                            ELSE 'suppressed'
                        END,
                        error=CASE
                            WHEN state='sending'
                                THEN 'historical delivery submission cannot be reconciled'
                            ELSE 'historical delivery suppressed during Outbox migration'
                        END,
                        updated_at=?
                    WHERE state IN ('prepared', 'sending')
                      AND task_id IN (
                          SELECT id FROM tasks WHERE outbox_required=0
                      )
                    """,
                    (time.time(),),
                )
                for legacy in legacy_outbox:
                    self._sync_delivery_compat(
                        connection,
                        legacy["task_id"],
                        int(legacy["generation"]),
                    )
                memory_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(scope_memory)"
                    ).fetchall()
                }
                memory_migrations = {
                    "expires_at": "REAL",
                    "last_used_at": "REAL",
                    "replaces_source_task_id": "TEXT",
                }
                for name, definition in memory_migrations.items():
                    if name not in memory_columns:
                        connection.execute(
                            "ALTER TABLE scope_memory ADD COLUMN %s %s"
                            % (name, definition)
                        )
                tool_event_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(tool_events)"
                    ).fetchall()
                }
                if "event_key" not in tool_event_columns:
                    connection.execute(
                        """
                        ALTER TABLE tool_events
                        ADD COLUMN event_key TEXT NOT NULL DEFAULT ''
                        """
                    )
                connection.execute("DROP INDEX IF EXISTS idx_tool_events_key")
                connection.execute(
                    """
                    CREATE UNIQUE INDEX idx_tool_events_key
                    ON tool_events(
                        task_id,
                        generation,
                        COALESCE(run_id, ''),
                        event_key
                    )
                    WHERE event_key <> ''
                    """
                )
                duplicate_summaries = connection.execute(
                    """
                    SELECT task_id, generation
                    FROM outbox_items
                    WHERE is_summary=1
                    GROUP BY task_id, generation
                    HAVING COUNT(*) > 1
                    """
                ).fetchall()
                for duplicate in duplicate_summaries:
                    summaries = connection.execute(
                        """
                        SELECT id, state
                        FROM outbox_items
                        WHERE task_id=? AND generation=? AND is_summary=1
                        ORDER BY
                            CASE state
                                WHEN 'confirmed' THEN 0
                                WHEN 'uncertain' THEN 1
                                WHEN 'sending' THEN 2
                                WHEN 'prepared' THEN 3
                                WHEN 'suppressed' THEN 4
                                ELSE 5
                            END,
                            item_index,
                            id
                        """,
                        (duplicate["task_id"], duplicate["generation"]),
                    ).fetchall()
                    keeper_id = int(summaries[0]["id"])
                    connection.execute(
                        """
                        UPDATE outbox_items
                        SET is_summary=0,
                            state=CASE
                                WHEN state='prepared' THEN 'suppressed'
                                ELSE state
                            END,
                            error=CASE
                                WHEN state='prepared'
                                    THEN 'duplicate summary suppressed during migration'
                                ELSE error
                            END,
                            updated_at=?
                        WHERE task_id=? AND generation=?
                          AND is_summary=1 AND id<>?
                        """,
                        (
                            time.time(),
                            duplicate["task_id"],
                            int(duplicate["generation"]),
                            keeper_id,
                        ),
                    )
                    self._sync_delivery_compat(
                        connection,
                        duplicate["task_id"],
                        int(duplicate["generation"]),
                    )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_one_summary
                    ON outbox_items(task_id, generation)
                    WHERE is_summary=1
                    """
                )
                connection.commit()
            self._initialized = True

    @staticmethod
    def _task(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["cancel_requested"] = bool(value["cancel_requested"])
        value["final_sent"] = bool(value["final_sent"])
        value["delivery_suppressed"] = bool(value["delivery_suppressed"])
        value["outbox_required"] = bool(value["outbox_required"])
        value["usage"] = (
            json.loads(value["usage_json"]) if value.get("usage_json") else None
        )
        value["plan"] = (
            json.loads(value["plan_json"]) if value.get("plan_json") else {}
        )
        value["skill_snapshot"] = (
            json.loads(value["skill_snapshot_json"])
            if value.get("skill_snapshot_json")
            else []
        )
        value.pop("usage_json", None)
        value.pop("plan_json", None)
        value.pop("skill_snapshot_json", None)
        return value

    def begin_inbound(
        self,
        *,
        request_id: str,
        request_hash: str,
        room_id: str,
        sender_id: str,
        source_local_id: int | None,
        msg_svr_id: str,
    ) -> dict[str, Any]:
        self.initialize()
        now = time.time()
        server_id = str(msg_svr_id or "").strip()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM inbound_ledger
                WHERE request_id=?
                   OR (? IS NOT NULL AND room_id=? AND source_local_id=?)
                   OR (? <> '' AND room_id=? AND msg_svr_id=?)
                ORDER BY request_id=? DESC
                LIMIT 1
                """,
                (
                    request_id,
                    source_local_id,
                    room_id,
                    source_local_id,
                    server_id,
                    room_id,
                    server_id,
                    request_id,
                ),
            ).fetchone()
            if row is not None:
                if (
                    row["request_hash"] != request_hash
                    or row["room_id"] != room_id
                    or row["sender_id"] != sender_id
                ):
                    connection.rollback()
                    raise ValueError(
                        "inbound message identity was replayed with different content"
                    )
                if row["state"] == "processing" and float(row["updated_at"]) <= 0:
                    cursor = connection.execute(
                        """
                        UPDATE inbound_ledger
                        SET updated_at=?
                        WHERE request_id=? AND state='processing' AND updated_at <= 0
                        """,
                        (now, row["request_id"]),
                    )
                    connection.commit()
                    return {
                        "created": cursor.rowcount == 1,
                        "request_id": row["request_id"],
                        "state": row["state"],
                        "response": None,
                    }
                connection.commit()
                return {
                    "created": False,
                    "request_id": row["request_id"],
                    "state": row["state"],
                    "response": (
                        json.loads(row["response_json"])
                        if row["response_json"]
                        else None
                    ),
                }
            connection.execute(
                """
                INSERT INTO inbound_ledger(
                    request_id, request_hash, room_id, sender_id,
                    source_local_id, msg_svr_id, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?)
                """,
                (
                    request_id,
                    request_hash,
                    room_id,
                    sender_id,
                    source_local_id,
                    server_id,
                    now,
                    now,
                ),
            )
            connection.commit()
        return {
            "created": True,
            "request_id": request_id,
            "state": "processing",
            "response": None,
        }

    def recover_inbound(self) -> int:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_ledger
                SET updated_at=0
                WHERE state='processing'
                """
            )
            connection.commit()
        return int(cursor.rowcount)

    def load_response(self, request_id: str, request_hash: str) -> dict[str, Any] | None:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM request_cache WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ValueError("request_id was replayed with different content")
        value = json.loads(row["response_json"])
        if not isinstance(value, dict):
            raise ValueError("cached response is invalid")
        return value

    def save_response(
        self,
        request_id: str,
        request_hash: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        self.initialize()
        encoded = json_dumps(response)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM request_cache WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    connection.rollback()
                    raise ValueError("request_id was replayed with different content")
                connection.execute(
                    """
                    UPDATE inbound_ledger
                    SET state='completed', response_json=?, updated_at=?
                    WHERE request_id=?
                    """,
                    (row["response_json"], time.time(), request_id),
                )
                connection.commit()
                return json.loads(row["response_json"])
            connection.execute(
                """
                INSERT INTO request_cache(request_id, request_hash, response_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (request_id, request_hash, encoded, time.time()),
            )
            connection.execute(
                """
                UPDATE inbound_ledger
                SET state='completed', response_json=?, updated_at=?
                WHERE request_id=?
                """,
                (encoded, time.time(), request_id),
            )
            connection.commit()
        return response

    def create_task(
        self,
        *,
        request_id: str,
        request_hash: str,
        room_id: str,
        sender_id: str,
        session_id: str,
        kind: str,
        prompt: str,
        max_attempts: int,
        source_local_id: int | None,
        source_msg_svr_id: str = "",
        plan: dict[str, Any] | None = None,
        delivery_policy: str = "text_only",
        skill_snapshot: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        self.initialize()
        if kind not in {"run", "chat"}:
            raise ValueError("invalid task kind")
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM tasks WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    connection.rollback()
                    raise ValueError("request_id was replayed with different content")
                connection.commit()
                return self._task(existing), False
            task_id = "T-" + secrets.token_hex(4).upper()
            connection.execute(
                """
                INSERT INTO tasks (
                    id, request_id, request_hash, room_id, sender_id, session_id,
                    kind, prompt, status, max_attempts, source_local_id,
                    source_msg_svr_id, plan_json, delivery_policy,
                    skill_snapshot_json, outbox_required,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    task_id,
                    request_id,
                    request_hash,
                    room_id,
                    sender_id,
                    session_id,
                    kind,
                    prompt,
                    max(1, int(max_attempts)),
                    source_local_id,
                    str(source_msg_svr_id or "").strip(),
                    json_dumps(plan or {}),
                    str(delivery_policy or "text_only"),
                    json_dumps(skill_snapshot or []),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'queued', NULL, ?)
                """,
                (task_id, now),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            connection.commit()
        return self._task(row), True

    def recover(self) -> int:
        self.initialize()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status='queued',
                    attempts=CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                    error='adapter restarted during uncertain Hermes run creation',
                    updated_at=?
                WHERE status='running' AND hermes_run_id IS NULL
                """,
                (now,),
            )
            connection.commit()
        return int(cursor.rowcount)

    def has_execution_backlog(self) -> bool:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM tasks
                WHERE status='running'
                   OR (status='queued' AND internal_state='')
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def claim_next(self) -> dict[str, Any] | None:
        self.initialize()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            recovering = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status='running' AND hermes_run_id IS NOT NULL
                ORDER BY updated_at, created_at
                LIMIT 1
                """
            ).fetchone()
            if recovering is not None:
                connection.commit()
                return self._task(recovering)
            row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status='queued' AND internal_state=''
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status='running', attempts=attempts+1,
                    started_at=COALESCE(started_at, ?), updated_at=?,
                    error=NULL, resource_error=NULL
                WHERE id=? AND status='queued' AND internal_state=''
                """,
                (now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'running', NULL, ?)
                """,
                (row["id"], now),
            )
            claimed = connection.execute(
                "SELECT * FROM tasks WHERE id=?",
                (row["id"],),
            ).fetchone()
            connection.commit()
        return self._task(claimed)

    def set_run_id(
        self,
        task_id: str,
        run_id: str,
        *,
        generation: int | None = None,
    ) -> bool:
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            query = (
                "UPDATE tasks SET hermes_run_id=?, status='running', updated_at=? "
                "WHERE id=? AND status='running' AND cancel_requested=0 "
                "AND hermes_run_id IS NULL"
            )
            params: list[Any] = [run_id, now, task_id]
            if generation is not None:
                query += " AND generation=?"
                params.append(int(generation))
            cursor = connection.execute(query, params)
            if cursor.rowcount != 1:
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'hermes_run_started', ?, ?)
                """,
                (task_id, run_id, now),
            )
            connection.commit()
        return True

    def complete(
        self,
        task_id: str,
        status: str,
        *,
        output: str | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
        generation: int | None = None,
    ) -> bool:
        if status not in TERMINAL_STATUSES:
            raise ValueError("task status is not terminal")
        now = time.time()
        safe_error = redact_sensitive_text(error, limit=800) if error else None
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = (
                "SELECT status, cancel_requested, generation, resource_error "
                "FROM tasks WHERE id=?"
            )
            params: list[Any] = [task_id]
            if generation is not None:
                query += " AND generation=?"
                params.append(int(generation))
            row = connection.execute(query, params).fetchone()
            if row is None or row["status"] in TERMINAL_STATUSES:
                connection.rollback()
                return False

            effective_status = (
                "canceled"
                if bool(row["cancel_requested"]) and not row["resource_error"]
                else status
            )
            effective_output = None if effective_status == "canceled" else output
            effective_error = None if effective_status == "canceled" else safe_error
            cursor = connection.execute(
                """
                UPDATE tasks SET status=?, output=?, error=?, usage_json=?,
                    updated_at=?, completed_at=?
                WHERE id=? AND generation=? AND status NOT IN
                    ('succeeded', 'failed', 'canceled')
                """,
                (
                    effective_status,
                    effective_output,
                    effective_error,
                    json_dumps(usage) if usage else None,
                    now,
                    now,
                    task_id,
                    int(row["generation"]),
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    task_id,
                    effective_status,
                    (
                        "cancel request won completion race"
                        if effective_status == "canceled" and status != "canceled"
                        else effective_error
                    ),
                    now,
                ),
            )
            connection.commit()
        return True

    def retry_after_failure(
        self,
        task_id: str,
        error: str,
        *,
        generation: int | None = None,
    ) -> str:
        now = time.time()
        safe_error = redact_sensitive_text(error, limit=800)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = "SELECT * FROM tasks WHERE id=?"
            params: list[Any] = [task_id]
            if generation is not None:
                query += " AND generation=?"
                params.append(int(generation))
            row = connection.execute(query, params).fetchone()
            if row is None:
                connection.rollback()
                return "superseded"
            if row["cancel_requested"]:
                status = "canceled"
            elif int(row["attempts"]) < int(row["max_attempts"]):
                status = "queued"
            else:
                status = "failed"
            terminal_at = now if status in TERMINAL_STATUSES else None
            connection.execute(
                """
                UPDATE tasks SET status=?, hermes_run_id=NULL, error=?,
                    updated_at=?, completed_at=?
                WHERE id=?
                """,
                (status, safe_error, now, terminal_at, task_id),
            )
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    task_id,
                    "retry_queued" if status == "queued" else status,
                    safe_error,
                    now,
                ),
            )
            connection.commit()
        return status

    def requeue_uncertain_run_creation(
        self,
        task_id: str,
        error: str,
        *,
        generation: int | None = None,
    ) -> bool:
        now = time.time()
        safe_error = redact_sensitive_text(error, limit=800)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = """
                UPDATE tasks
                SET status='queued',
                    attempts=CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                    error=?, updated_at=?, completed_at=NULL
                WHERE id=? AND status='running' AND hermes_run_id IS NULL
            """
            params: list[Any] = [safe_error, now, task_id]
            if generation is not None:
                query += " AND generation=?"
                params.append(int(generation))
            cursor = connection.execute(query, params)
            if cursor.rowcount != 1:
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'run_creation_uncertain', ?, ?)
                """,
                (task_id, safe_error, now),
            )
            connection.commit()
        return True

    def defer_run_recovery(
        self,
        task_id: str,
        error: str,
        *,
        generation: int | None = None,
    ) -> bool:
        now = time.time()
        safe_error = redact_sensitive_text(error, limit=800)
        with self._lock, closing(self._connect()) as connection:
            query = """
                UPDATE tasks SET status='running', error=?, updated_at=?
                WHERE id=? AND hermes_run_id IS NOT NULL
            """
            params: list[Any] = [safe_error, now, task_id]
            if generation is not None:
                query += " AND generation=?"
                params.append(int(generation))
            cursor = connection.execute(query, params)
            if cursor.rowcount != 1:
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'run_reconnect_pending', ?, ?)
                """,
                (task_id, safe_error, now),
            )
            connection.commit()
        return True

    def get_task(self, task_id: str, room_id: str | None = None) -> dict[str, Any] | None:
        self.initialize()
        query = "SELECT * FROM tasks WHERE id=?"
        params: list[Any] = [task_id.upper()]
        if room_id is not None:
            query += " AND room_id=?"
            params.append(room_id)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(query, params).fetchone()
        return self._task(row)

    def list_tasks(self, room_id: str, limit: int = 10) -> list[dict[str, Any]]:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks WHERE room_id=?
                ORDER BY created_at DESC LIMIT ?
                """,
                (room_id, max(1, min(50, int(limit)))),
            ).fetchall()
        return [self._task(row) for row in rows]

    def cancel_task(self, task_id: str, room_id: str) -> dict[str, Any] | None:
        self.initialize()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=? AND room_id=?",
                (task_id.upper(), room_id),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            if row["status"] == "queued":
                connection.execute(
                    """
                    UPDATE tasks SET status='canceled', cancel_requested=1,
                        delivery_suppressed=1, completed_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (now, now, row["id"]),
                )
            elif row["status"] == "running":
                connection.execute(
                    """
                    UPDATE tasks SET cancel_requested=1, delivery_suppressed=1,
                        updated_at=? WHERE id=?
                    """,
                    (now, row["id"]),
                )
            elif not row["final_sent"]:
                connection.execute(
                    """
                    UPDATE tasks SET cancel_requested=1, delivery_suppressed=1,
                        updated_at=? WHERE id=?
                    """,
                    (now, row["id"]),
                )
            connection.execute(
                """
                UPDATE outbox_items
                SET state='suppressed', error='task canceled',
                    updated_at=?
                WHERE task_id=? AND generation=?
                  AND state='prepared'
                """,
                (now, row["id"], int(row["generation"] or 1)),
            )
            self._sync_delivery_compat(
                connection,
                row["id"],
                int(row["generation"] or 1),
            )
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'cancel_requested', NULL, ?)
                """,
                (row["id"], now),
            )
            updated = connection.execute(
                "SELECT * FROM tasks WHERE id=?",
                (row["id"],),
            ).fetchone()
            connection.commit()
        return self._task(updated)

    def cancel_room_tasks(self, room_id: str) -> list[dict[str, Any]]:
        self.initialize()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE room_id=?
                  AND (
                    status IN ('queued', 'running')
                    OR (final_sent=0 AND delivery_suppressed=0)
                  )
                ORDER BY created_at, id
                """,
                (room_id,),
            ).fetchall()
            for row in rows:
                if row["status"] == "queued":
                    connection.execute(
                        """
                        UPDATE tasks SET status='canceled', cancel_requested=1,
                            delivery_suppressed=1, completed_at=?, updated_at=?
                        WHERE id=?
                        """,
                        (now, now, row["id"]),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE tasks SET cancel_requested=1,
                            delivery_suppressed=1, updated_at=?
                        WHERE id=?
                        """,
                        (now, row["id"]),
                    )
                connection.execute(
                    """
                    INSERT INTO task_events(task_id, event_type, detail, created_at)
                    VALUES (?, 'room_stop_requested', ?, ?)
                    """,
                    (
                        row["id"],
                        "active task canceled and pending delivery suppressed"
                        if row["status"] in {"queued", "running"}
                        else "pending delivery suppressed",
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE outbox_items
                    SET state='suppressed', error='room stop requested',
                        updated_at=?
                    WHERE task_id=? AND generation=?
                      AND state='prepared'
                    """,
                    (now, row["id"], int(row["generation"] or 1)),
                )
                self._sync_delivery_compat(
                    connection,
                    row["id"],
                    int(row["generation"] or 1),
                )
            updated = []
            for row in rows:
                updated_row = connection.execute(
                    "SELECT * FROM tasks WHERE id=?",
                    (row["id"],),
                ).fetchone()
                task = self._task(updated_row)
                task["stop_previous_status"] = row["status"]
                updated.append(task)
            connection.commit()
        return updated

    def retry_task(self, task_id: str, room_id: str) -> dict[str, Any] | None:
        self.initialize()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=? AND room_id=?",
                (task_id.upper(), room_id),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            if row["status"] not in {"failed", "canceled"}:
                connection.commit()
                return self._task(row)
            connection.execute(
                """
                UPDATE outbox_items
                SET state='suppressed',
                    error='superseded by explicit retry', updated_at=?
                WHERE task_id=? AND generation=? AND state='prepared'
                """,
                (now, row["id"], int(row["generation"] or 1)),
            )
            self._sync_delivery_compat(
                connection,
                row["id"],
                int(row["generation"] or 1),
            )
            connection.execute(
                """
                UPDATE tasks SET status='queued', hermes_run_id=NULL, attempts=0,
                    cancel_requested=0, output=NULL, error=NULL, usage_json=NULL,
                    final_sent=0,
                    delivery_generation=delivery_generation+1,
                    generation=generation+1,
                    delivery_attempts=0, delivery_error=NULL,
                    delivery_suppressed=0,
                    internal_state='', blocked_until=NULL,
                    resource_error=NULL,
                    outbox_required=1,
                    started_at=NULL, completed_at=NULL, updated_at=?
                WHERE id=?
                """,
                (now, row["id"]),
            )
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'manual_retry', NULL, ?)
                """,
                (row["id"], now),
            )
            updated = connection.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            connection.commit()
        return self._task(updated)

    def next_terminal_without_outbox(self) -> dict[str, Any] | None:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status IN ('succeeded', 'failed', 'canceled')
                  AND outbox_required=1
                  AND NOT EXISTS (
                      SELECT 1 FROM outbox_items
                      WHERE outbox_items.task_id=tasks.id
                        AND outbox_items.generation=tasks.generation
                        AND outbox_items.is_summary=1
                  )
                ORDER BY completed_at, created_at
                LIMIT 1
                """
            ).fetchone()
        return self._task(row)

    def has_room_activity(self, room_id: str) -> bool:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM tasks
                WHERE room_id=?
                  AND (
                    status IN ('queued', 'running')
                    OR EXISTS (
                        SELECT 1 FROM outbox_items
                        WHERE outbox_items.task_id=tasks.id
                          AND outbox_items.generation=tasks.generation
                          AND outbox_items.state IN ('prepared', 'sending')
                    )
                  )
                LIMIT 1
                """,
                (room_id,),
            ).fetchone()
        return row is not None

    def suppress_room_media(
        self,
        room_id: str,
        source_local_id: int,
        reason: str,
    ) -> int:
        self.initialize()
        now = time.time()
        safe_reason = redact_sensitive_text(reason, limit=300)
        with self._lock, closing(self._connect()) as connection:
            affected = connection.execute(
                """
                SELECT DISTINCT task_id, generation
                FROM outbox_items
                WHERE kind IN ('image', 'video', 'file')
                  AND state='prepared'
                  AND task_id IN (
                      SELECT id FROM tasks
                      WHERE room_id=?
                        AND source_local_id IS NOT NULL
                        AND source_local_id < ?
                  )
                """,
                (room_id, int(source_local_id)),
            ).fetchall()
            cursor = connection.execute(
                """
                UPDATE outbox_items
                SET state='suppressed', error=?, updated_at=?
                WHERE kind IN ('image', 'video', 'file')
                  AND state='prepared'
                  AND task_id IN (
                      SELECT id FROM tasks
                      WHERE room_id=?
                        AND source_local_id IS NOT NULL
                        AND source_local_id < ?
                  )
                """,
                (safe_reason, now, room_id, int(source_local_id)),
            )
            for row in affected:
                self._sync_delivery_compat(
                    connection,
                    row["task_id"],
                    int(row["generation"]),
                )
            connection.commit()
        return int(cursor.rowcount)

    def suppress_task_generation(
        self,
        task_id: str,
        generation: int,
        reason: str,
    ) -> int:
        self.initialize()
        now = time.time()
        safe_reason = redact_sensitive_text(reason, limit=300)
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_items
                SET state='suppressed', error=?, updated_at=?
                WHERE task_id=? AND generation=?
                  AND state='prepared'
                """,
                (safe_reason, now, task_id, int(generation)),
            )
            self._sync_delivery_compat(connection, task_id, int(generation))
            connection.commit()
        return int(cursor.rowcount)

    def revise_task(
        self,
        task_id: str,
        room_id: str,
        prompt: str,
        *,
        plan: dict[str, Any],
        delivery_policy: str,
        supplement: bool = False,
    ) -> dict[str, Any] | None:
        self.initialize()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=? AND room_id=?",
                (task_id.upper(), room_id),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            new_prompt = str(prompt).strip()
            if supplement:
                new_prompt = (
                    str(row["prompt"]).rstrip()
                    + "\n\n用户补充：\n"
                    + new_prompt
                )
            rotate = (
                row["status"] in TERMINAL_STATUSES
                or row["status"] == "running"
                or row["internal_state"] == "blocked_on_input"
            )
            if rotate:
                connection.execute(
                    """
                    UPDATE outbox_items
                    SET state='suppressed',
                        error='superseded by task revision', updated_at=?
                    WHERE task_id=? AND generation=?
                      AND state='prepared'
                    """,
                    (now, row["id"], int(row["generation"] or 1)),
                )
                self._sync_delivery_compat(
                    connection,
                    row["id"],
                    int(row["generation"] or 1),
                )
                connection.execute(
                    """
                    UPDATE tasks
                    SET prompt=?, plan_json=?, delivery_policy=?,
                        status='queued', hermes_run_id=NULL, attempts=0,
                        cancel_requested=0, output=NULL, error=NULL,
                        usage_json=NULL, final_sent=0,
                        generation=generation+1,
                        delivery_generation=delivery_generation+1,
                        delivery_attempts=0, delivery_error=NULL,
                        delivery_suppressed=0, internal_state='',
                        blocked_until=NULL, resource_error=NULL,
                        outbox_required=1,
                        started_at=NULL, completed_at=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (
                        new_prompt,
                        json_dumps(plan),
                        delivery_policy,
                        now,
                        row["id"],
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE tasks
                    SET prompt=?, plan_json=?, delivery_policy=?,
                        internal_state='', blocked_until=NULL,
                        cancel_requested=0, error=NULL,
                        outbox_required=1, updated_at=?
                    WHERE id=?
                    """,
                    (
                        new_prompt,
                        json_dumps(plan),
                        delivery_policy,
                        now,
                        row["id"],
                    ),
                )
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, ?, NULL, ?)
                """,
                (
                    row["id"],
                    "task_supplemented" if supplement else "task_modified",
                    now,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM tasks WHERE id=?",
                (row["id"],),
            ).fetchone()
            connection.commit()
        return self._task(updated)

    def block_on_input(
        self,
        task_id: str,
        question: str,
        *,
        generation: int | None = None,
    ) -> bool:
        self.initialize()
        now = time.time()
        safe_question = redact_sensitive_text(question, limit=800)
        with self._lock, closing(self._connect()) as connection:
            query = """
                UPDATE tasks
                SET status='queued', internal_state='blocked_on_input',
                    hermes_run_id=NULL, output=?, error=NULL,
                    blocked_until=?, question_count=question_count+1,
                    updated_at=?
                WHERE id=?
            """
            params: list[Any] = [
                safe_question,
                now + 86400,
                now,
                task_id,
            ]
            if generation is not None:
                query += " AND generation=?"
                params.append(int(generation))
            cursor = connection.execute(query, params)
            if cursor.rowcount != 1:
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'blocked_on_input', ?, ?)
                """,
                (task_id, safe_question, now),
            )
            connection.commit()
        return True

    def expire_blocked_tasks(self) -> int:
        self.initialize()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, generation, source_local_id, outbox_required
                FROM tasks
                WHERE status='queued'
                  AND internal_state='blocked_on_input'
                  AND blocked_until IS NOT NULL
                  AND blocked_until <= ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status='failed', internal_state='',
                        error='等待补充信息超过 24 小时',
                        completed_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (now, now, row["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO task_events(task_id, event_type, detail, created_at)
                    VALUES (?, 'failed', 'input timeout', ?)
                    """,
                    (row["id"], now),
                )
                connection.execute(
                    """
                    UPDATE outbox_items
                    SET state='suppressed',
                        error='superseded by input timeout',
                        updated_at=?
                    WHERE task_id=? AND generation=? AND state='prepared'
                    """,
                    (now, row["id"], int(row["generation"])),
                )
                summary = connection.execute(
                    """
                    SELECT 1 FROM outbox_items
                    WHERE task_id=? AND generation=? AND is_summary=1
                    """,
                    (row["id"], int(row["generation"])),
                ).fetchone()
                if (
                    bool(row["outbox_required"])
                    and summary is None
                    and row["source_local_id"] is not None
                ):
                    next_index = connection.execute(
                        """
                        SELECT COALESCE(MAX(item_index), 0) + 1 AS value
                        FROM outbox_items
                        WHERE task_id=? AND generation=?
                        """,
                        (row["id"], int(row["generation"])),
                    ).fetchone()["value"]
                    connection.execute(
                        """
                        INSERT INTO outbox_items(
                            task_id, generation, item_index, kind, content,
                            state, idempotency_key, source_local_id,
                            is_summary, created_at, updated_at
                        ) VALUES (?, ?, ?, 'text', '', 'prepared', ?, ?, 1, ?, ?)
                        """,
                        (
                            row["id"],
                            int(row["generation"]),
                            int(next_index),
                            "task:%s:g:%d:item:%d"
                            % (
                                row["id"],
                                int(row["generation"]),
                                int(next_index),
                            ),
                            int(row["source_local_id"]),
                            now,
                            now,
                        ),
                    )
                self._sync_delivery_compat(
                    connection,
                    row["id"],
                    int(row["generation"]),
                )
            connection.commit()
        return len(rows)

    def set_resource_error(self, task_id: str, reason: str) -> None:
        safe_reason = redact_sensitive_text(reason, limit=800)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE tasks
                SET resource_error=?, cancel_requested=1, updated_at=?
                WHERE id=?
                """,
                (safe_reason, time.time(), task_id),
            )
            connection.commit()

    def register_artifact(
        self,
        *,
        task_id: str,
        generation: int,
        name: str,
        path: Path,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        max_count: int,
        max_total_bytes: int,
        role: str = "primary",
        allow_terminal: bool = False,
    ) -> dict[str, Any]:
        self.initialize()
        now = time.time()
        canonical_path = str(Path(path).resolve())
        material = "%s\n%d\n%s\n%s" % (
            task_id,
            int(generation),
            canonical_path + "\n" + str(name),
            sha256,
        )
        artifact_id = "A-" + hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()[:16].upper()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT status, generation, cancel_requested FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                connection.rollback()
                raise KeyError(task_id)
            allowed_statuses = {"running"}
            if allow_terminal:
                allowed_statuses.update(TERMINAL_STATUSES)
            if (
                task["status"] not in allowed_statuses
                or bool(task["cancel_requested"])
            ):
                connection.rollback()
                raise PermissionError(
                    "artifact registration requires a running, non-canceled task"
                )
            if int(task["generation"]) != int(generation):
                connection.rollback()
                raise PermissionError("artifact belongs to an obsolete task generation")
            totals = connection.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(size_bytes), 0) AS total
                FROM artifacts
                WHERE task_id=? AND generation=?
                """,
                (task_id, int(generation)),
            ).fetchone()
            existing = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE task_id=? AND generation=? AND path=?
                """,
                (task_id, int(generation), canonical_path),
            ).fetchone()
            if existing is not None:
                if (
                    existing["sha256"] != sha256
                    or int(existing["size_bytes"]) != int(size_bytes)
                    or existing["mime_type"] != mime_type
                ):
                    connection.rollback()
                    raise ValueError("registered artifact path changed content")
                connection.commit()
                return dict(existing)
            if int(totals["count"]) >= int(max_count):
                connection.rollback()
                raise ValueError("task artifact count limit exceeded")
            if int(totals["total"]) + int(size_bytes) > int(max_total_bytes):
                connection.rollback()
                raise ValueError("task artifact total size limit exceeded")
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, task_id, generation, name, path, mime_type,
                    size_bytes, sha256, verified, role, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    artifact_id,
                    task_id,
                    int(generation),
                    name,
                    canonical_path,
                    mime_type,
                    int(size_bytes),
                    sha256,
                    str(role or "primary")[:32],
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            connection.commit()
        return dict(row)

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?",
                (str(artifact_id or "").strip(),),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_artifact_verified(
        self,
        artifact_id: str,
        verified: bool,
    ) -> bool:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE artifacts SET verified=?
                WHERE artifact_id=?
                """,
                (int(bool(verified)), str(artifact_id or "").strip()),
            )
            connection.commit()
        return cursor.rowcount == 1

    def list_artifacts(
        self,
        task_id: str,
        generation: int | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        query = "SELECT * FROM artifacts WHERE task_id=?"
        params: list[Any] = [task_id]
        if generation is not None:
            query += " AND generation=?"
            params.append(int(generation))
        query += " ORDER BY created_at, artifact_id"
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def add_tool_event(
        self,
        *,
        task_id: str,
        generation: int,
        run_id: str,
        event_key: str = "",
        event_type: str,
        tool_name: str = "",
        exit_code: int | None = None,
        result_summary: str = "",
        source: str = "",
        artifact_id: str = "",
    ) -> bool:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO tool_events(
                    task_id, generation, run_id, event_key, event_type, tool_name,
                    exit_code, result_summary, source, artifact_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    int(generation),
                    str(run_id or "")[:128],
                    str(event_key or "")[:128],
                    str(event_type or "")[:64],
                    str(tool_name or "")[:128],
                    exit_code,
                    redact_sensitive_text(result_summary, limit=500)
                    if result_summary
                    else "",
                    str(source or "")[:2000],
                    str(artifact_id or "")[:64],
                    time.time(),
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def list_tool_events(
        self,
        task_id: str,
        generation: int,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        query = """
            SELECT * FROM tool_events
            WHERE task_id=? AND generation=?
        """
        params: list[Any] = [task_id, int(generation)]
        if run_id is not None:
            query += " AND COALESCE(run_id, '')=?"
            params.append(str(run_id or ""))
        query += " ORDER BY id"
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def tool_call_count(self, task_id: str, generation: int) -> int:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS value FROM tool_events
                WHERE task_id=? AND generation=? AND event_type='tool.started'
                """,
                (task_id, int(generation)),
            ).fetchone()
        return int(row["value"] or 0)

    def prepare_outbox(
        self,
        task_id: str,
        generation: int,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        self.initialize()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            protocol = connection.execute(
                "SELECT outbox_required FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if protocol is None:
                connection.rollback()
                raise KeyError(task_id)
            existing = connection.execute(
                """
                SELECT * FROM outbox_items
                WHERE task_id=? AND generation=?
                ORDER BY item_index
                """,
                (task_id, int(generation)),
            ).fetchall()
            if not bool(protocol["outbox_required"]):
                connection.commit()
                return [dict(row) for row in existing]
            if existing:
                has_summary = any(bool(row["is_summary"]) for row in existing)
                requested_summary = next(
                    (item for item in items if bool(item.get("is_summary"))),
                    None,
                )
                if has_summary or requested_summary is None:
                    self._sync_delivery_compat(connection, task_id, generation)
                    connection.commit()
                    return [dict(row) for row in existing]
                items = [requested_summary]
                start_index = max(int(row["item_index"]) for row in existing) + 1
            else:
                start_index = 1
            for index, item in enumerate(items, start=start_index):
                kind = str(item["kind"])
                if kind not in {"text", "image", "video", "file"}:
                    connection.rollback()
                    raise ValueError("invalid outbox item kind")
                key = "task:%s:g:%d:item:%d" % (
                    task_id,
                    int(generation),
                    index,
                )
                connection.execute(
                    """
                    INSERT INTO outbox_items(
                        task_id, generation, item_index, kind, artifact_id,
                        content, state, idempotency_key, source_local_id,
                        is_summary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        int(generation),
                        index,
                        kind,
                        item.get("artifact_id"),
                        item.get("content"),
                        key,
                        int(item["source_local_id"]),
                        int(bool(item.get("is_summary"))),
                        now,
                        now,
                    ),
                )
            rows = connection.execute(
                """
                SELECT * FROM outbox_items
                WHERE task_id=? AND generation=?
                ORDER BY item_index
                """,
                (task_id, int(generation)),
            ).fetchall()
            self._sync_delivery_compat(connection, task_id, generation)
            connection.commit()
        return [dict(row) for row in rows]

    def list_recoverable_outbox(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT o.*, t.room_id
                FROM outbox_items o
                JOIN tasks t ON t.id=o.task_id
                WHERE o.state='sending'
                  AND t.outbox_required=1
                ORDER BY o.created_at, o.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def reconcile_outbox_item(
        self,
        item_id: int,
        state: str,
        *,
        error: str = "",
        confirmed_local_id: int | None = None,
        media_fingerprint: str = "",
    ) -> bool:
        if state not in {"prepared", "confirmed", "uncertain", "suppressed", "failed"}:
            raise ValueError("invalid recovered outbox state")
        safe_error = redact_sensitive_text(error, limit=800) if error else None
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE outbox_items
                SET state=?, error=?, confirmed_local_id=?,
                    media_fingerprint=?, updated_at=?
                WHERE id=? AND state='sending'
                """,
                (
                    state,
                    safe_error,
                    confirmed_local_id,
                    str(media_fingerprint or "")[:128],
                    now,
                    int(item_id),
                ),
            )
            row = connection.execute(
                "SELECT task_id, generation FROM outbox_items WHERE id=?",
                (int(item_id),),
            ).fetchone()
            if row is not None:
                self._sync_delivery_compat(
                    connection,
                    row["task_id"],
                    row["generation"],
                )
            connection.commit()
        return cursor.rowcount == 1

    def recover_outbox(self) -> int:
        self.initialize()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            uncertain_cursor = connection.execute(
                """
                UPDATE outbox_items
                SET state='uncertain',
                    error='adapter restarted after delivery submission began',
                    updated_at=?
                WHERE state='sending'
                """,
                (now,),
            )
            rows = connection.execute(
                """
                SELECT DISTINCT task_id, generation
                FROM outbox_items
                WHERE state='uncertain' AND updated_at=?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                self._sync_delivery_compat(
                    connection,
                    row["task_id"],
                    row["generation"],
                )
            connection.commit()
        return int(uncertain_cursor.rowcount)

    def next_outbox(self) -> dict[str, Any] | None:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT o.*, a.path AS artifact_path, a.name AS artifact_name,
                       a.mime_type, a.size_bytes, a.sha256,
                       t.room_id, t.status AS task_status,
                       t.completed_at, t.output, t.error AS task_error
                FROM outbox_items o
                JOIN tasks t ON t.id=o.task_id
                LEFT JOIN artifacts a ON a.artifact_id=o.artifact_id
                WHERE o.state='prepared'
                  AND t.outbox_required=1
                  AND (o.kind <> 'text' OR o.attempts < ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM outbox_items prior
                      WHERE prior.task_id=o.task_id
                        AND prior.generation=o.generation
                        AND prior.item_index < o.item_index
                        AND prior.state NOT IN (
                            'confirmed', 'uncertain', 'suppressed', 'failed'
                        )
                  )
                ORDER BY o.created_at, o.id
                LIMIT 1
                """,
                (MAX_DELIVERY_ATTEMPTS,),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_outbox_sending(self, item_id: int) -> dict[str, Any]:
        self.initialize()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM outbox_items WHERE id=?",
                (int(item_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(item_id)
            if row["state"] != "prepared":
                connection.commit()
                return dict(row)
            connection.execute(
                """
                UPDATE outbox_items
                SET state='sending', attempts=attempts+1, updated_at=?
                WHERE id=?
                """,
                (now, int(item_id)),
            )
            updated = connection.execute(
                "SELECT * FROM outbox_items WHERE id=?",
                (int(item_id),),
            ).fetchone()
            self._sync_delivery_compat(
                connection,
                updated["task_id"],
                int(updated["generation"]),
            )
            connection.commit()
        return dict(updated)

    def mark_outbox_state(
        self,
        item_id: int,
        state: str,
        *,
        error: str = "",
        confirmed_local_id: int | None = None,
        media_fingerprint: str = "",
    ) -> None:
        if state not in OUTBOX_STATES:
            raise ValueError("invalid outbox state")
        safe_error = redact_sensitive_text(error, limit=800) if error else None
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE outbox_items
                SET state=?, error=?, confirmed_local_id=?,
                    media_fingerprint=?, updated_at=?
                WHERE id=?
                """,
                (
                    state,
                    safe_error,
                    confirmed_local_id,
                    str(media_fingerprint or "")[:128],
                    now,
                    int(item_id),
                ),
            )
            row = connection.execute(
                "SELECT task_id, generation FROM outbox_items WHERE id=?",
                (int(item_id),),
            ).fetchone()
            if row is not None:
                self._sync_delivery_compat(connection, row["task_id"], row["generation"])
            connection.commit()

    @staticmethod
    def _sync_delivery_compat(
        connection: sqlite3.Connection,
        task_id: str,
        generation: int,
    ) -> None:
        rows = connection.execute(
            """
            SELECT state, attempts, is_summary, error FROM outbox_items
            WHERE task_id=? AND generation=?
            """,
            (task_id, int(generation)),
        ).fetchall()
        if not rows:
            return
        terminal = all(row["state"] in OUTBOX_TERMINAL_STATES for row in rows)
        summary_confirmed = any(
            row["is_summary"] and row["state"] == "confirmed" for row in rows
        )
        suppressed = any(
            row["state"] in {"suppressed", "uncertain"} for row in rows
        )
        attempts = sum(int(row["attempts"] or 0) for row in rows)
        error = next(
            (
                str(row["error"])
                for state in ("uncertain", "failed", "suppressed")
                for row in rows
                if row["state"] == state and row["error"]
            ),
            None,
        )
        connection.execute(
            """
            UPDATE tasks
            SET final_sent=?, delivery_attempts=?,
                delivery_suppressed=?, delivery_error=?, updated_at=?
            WHERE id=?
            """,
            (
                int(summary_confirmed or (terminal and not any(row["is_summary"] for row in rows))),
                attempts,
                int(suppressed),
                error,
                time.time(),
                task_id,
            ),
        )

    def list_outbox(
        self,
        task_id: str,
        generation: int,
    ) -> list[dict[str, Any]]:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM outbox_items
                WHERE task_id=? AND generation=?
                ORDER BY item_index
                """,
                (task_id, int(generation)),
            ).fetchall()
        return [dict(row) for row in rows]

    def outbox_counts(self) -> dict[str, int]:
        self.initialize()
        counts = {state: 0 for state in OUTBOX_STATES}
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM outbox_items GROUP BY state"
            ).fetchall()
        for row in rows:
            counts[row["state"]] = int(row["count"])
        return counts

    def task_counts(self) -> dict[str, int]:
        self.initialize()
        counts = {status: 0 for status in TASK_STATUSES}
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[row["status"]] = int(row["count"])
        return counts

    def next_delivery(self) -> dict[str, Any] | None:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status IN ('succeeded', 'failed', 'canceled')
                  AND final_sent=0
                  AND delivery_suppressed=0
                  AND delivery_attempts < ?
                ORDER BY completed_at, created_at
                LIMIT 1
                """,
                (MAX_DELIVERY_ATTEMPTS,),
            ).fetchone()
        return self._task(row)

    def mark_delivery_success(self, task_id: str) -> None:
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE tasks SET final_sent=1, delivery_error=NULL, updated_at=?
                WHERE id=?
                """,
                (now, task_id),
            )
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'delivered', NULL, ?)
                """,
                (task_id, now),
            )
            connection.commit()

    def mark_delivery_failure(self, task_id: str, error: str) -> None:
        now = time.time()
        safe_error = redact_sensitive_text(error, limit=800)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE tasks SET delivery_attempts=delivery_attempts+1,
                    delivery_error=?, updated_at=? WHERE id=?
                """,
                (safe_error, now, task_id),
            )
            connection.commit()

    def suppress_delivery(self, task_id: str, reason: str) -> None:
        now = time.time()
        safe_reason = redact_sensitive_text(reason, limit=800)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE tasks SET delivery_suppressed=1, delivery_error=?,
                    updated_at=? WHERE id=?
                """,
                (safe_reason, now, task_id),
            )
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'delivery_suppressed', ?, ?)
                """,
                (task_id, safe_reason, now),
            )
            connection.commit()

    def mark_delivery_uncertain(self, task_id: str, error: str) -> None:
        now = time.time()
        safe_error = redact_sensitive_text(error, limit=800)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE tasks SET delivery_attempts=delivery_attempts+1,
                    delivery_error=?, delivery_suppressed=1, updated_at=?
                WHERE id=?
                """,
                (safe_error, now, task_id),
            )
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'media_delivery_uncertain', ?, ?)
                """,
                (task_id, safe_error, now),
            )
            connection.commit()

    def is_delivery_suppressed(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        return bool(
            task
            and (
                task["cancel_requested"]
                or task["delivery_suppressed"]
                or task["final_sent"]
            )
        )

    def is_cancel_requested(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        return bool(task and task["cancel_requested"])

    def record_usage(
        self,
        task_id: str | None,
        session_id: str,
        usage: dict[str, Any] | None,
        input_rate: float,
        output_rate: float,
        *,
        input_text: str | None = None,
        output_text: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        input_tokens, output_tokens, was_estimated = usage_tokens(
            usage,
            input_text=input_text,
            output_text=output_text,
        )
        cost = estimated_cost(
            input_tokens,
            output_tokens,
            input_rate,
            output_rate,
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO usage_ledger(
                    task_id, session_id, input_tokens, output_tokens,
                    estimated_cost_usd, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, session_id, input_tokens, output_tokens, cost, time.time()),
            )
            connection.commit()
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated": was_estimated,
            "estimated_cost_usd": cost,
        }

    def today_usage(
        self,
        timezone_name: str = "UTC",
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        start, end = budget_day_bounds(timezone_name, now=now)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
                FROM usage_ledger
                WHERE created_at >= ? AND created_at < ?
                """,
                (start, end),
            ).fetchone()
        input_tokens = int(row["input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost_usd": float(row["estimated_cost_usd"] or 0),
            "day_start": start,
            "day_end": end,
        }

    def today_cost(
        self,
        timezone_name: str = "UTC",
        *,
        now: float | None = None,
    ) -> float:
        return float(
            self.today_usage(timezone_name, now=now)["estimated_cost_usd"]
        )

    def today_tokens(
        self,
        timezone_name: str = "UTC",
        *,
        now: float | None = None,
    ) -> int:
        return int(self.today_usage(timezone_name, now=now)["total_tokens"])

    @staticmethod
    def _scope(room_id: str | None, sender_id: str) -> tuple[str, str]:
        room = str(room_id or "").strip()
        sender = str(sender_id or "").strip()
        if room and not room.startswith("private:"):
            return "room", room
        if not sender:
            raise ValueError("private memory requires a sender identity")
        return "private", sender

    def list_scope_memory(
        self,
        room_id: str | None,
        sender_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.initialize()
        scope_type, scope_id = self._scope(room_id, sender_id)
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                DELETE FROM scope_memory
                WHERE scope_type=? AND scope_id=?
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (scope_type, scope_id, now),
            )
            rows = connection.execute(
                """
                SELECT key, value, source_task_id, created_at, updated_at,
                       expires_at, last_used_at, replaces_source_task_id
                FROM scope_memory
                WHERE scope_type=? AND scope_id=?
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY updated_at DESC, key
                LIMIT ?
                """,
                (
                    scope_type,
                    scope_id,
                    now,
                    max(1, min(100, int(limit))),
                ),
            ).fetchall()
            if rows:
                connection.executemany(
                    """
                    UPDATE scope_memory SET last_used_at=?
                    WHERE scope_type=? AND scope_id=? AND key=?
                    """,
                    [
                        (now, scope_type, scope_id, row["key"])
                        for row in rows
                    ],
                )
            connection.commit()
        memory = [dict(row) for row in rows]
        for item in memory:
            item["last_used_at"] = now
        return memory

    def memory_for_task(
        self,
        task_id: str,
        *,
        require_running: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if require_running and (
            task["status"] != "running" or task.get("cancel_requested")
        ):
            raise PermissionError("memory tools require a running task")
        return task, self.list_scope_memory(task["room_id"], task["sender_id"])

    def update_memory_for_task(
        self,
        task_id: str,
        *,
        action: str,
        key: str = "",
        value: str = "",
    ) -> list[dict[str, Any]]:
        self.initialize()
        action_name = str(action or "").strip().lower()
        if action_name not in {"set", "delete", "clear"}:
            raise ValueError("memory action must be set, delete, or clear")
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["status"] != "running" or task.get("cancel_requested"):
            raise PermissionError("memory tools require a running task")
        scope_type, scope_id = self._scope(task["room_id"], task["sender_id"])
        memory_key = ""
        memory_value = ""
        if action_name in {"set", "delete"}:
            memory_key = normalized_memory_key(key)
        if action_name == "set":
            memory_value = normalized_memory_value(value)
            if contains_sensitive_memory(memory_key, memory_value):
                raise ValueError("sensitive data cannot be stored in Agent memory")
        now = time.time()
        preference_key = memory_key.casefold()
        preference_markers = (
            "preference",
            "style",
            "tone",
            "habit",
            "format",
            "偏好",
            "风格",
            "语气",
            "习惯",
            "格式",
        )
        ttl_seconds = (
            PREFERENCE_MEMORY_TTL_SECONDS
            if any(marker in preference_key for marker in preference_markers)
            else PROJECT_MEMORY_TTL_SECONDS
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status, cancel_requested FROM tasks WHERE id=?",
                (task["id"],),
            ).fetchone()
            if (
                current is None
                or current["status"] != "running"
                or bool(current["cancel_requested"])
            ):
                connection.rollback()
                raise PermissionError("memory tools require a running task")
            if action_name == "set":
                previous = connection.execute(
                    """
                    SELECT source_task_id FROM scope_memory
                    WHERE scope_type=? AND scope_id=? AND key=?
                    """,
                    (scope_type, scope_id, memory_key),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO scope_memory(
                        scope_type, scope_id, key, value, source_task_id,
                        created_at, updated_at, expires_at, last_used_at,
                        replaces_source_task_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(scope_type, scope_id, key) DO UPDATE SET
                        value=excluded.value,
                        source_task_id=excluded.source_task_id,
                        updated_at=excluded.updated_at,
                        expires_at=excluded.expires_at,
                        replaces_source_task_id=excluded.replaces_source_task_id
                    """,
                    (
                        scope_type,
                        scope_id,
                        memory_key,
                        memory_value,
                        task["id"],
                        now,
                        now,
                        now + ttl_seconds,
                        previous["source_task_id"] if previous else None,
                    ),
                )
            elif action_name == "delete":
                connection.execute(
                    """
                    DELETE FROM scope_memory
                    WHERE scope_type=? AND scope_id=? AND key=?
                    """,
                    (scope_type, scope_id, memory_key),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM scope_memory
                    WHERE scope_type=? AND scope_id=?
                    """,
                    (scope_type, scope_id),
                )
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, 'memory_updated', ?, ?)
                """,
                (task["id"], action_name, now),
            )
            connection.commit()
        return self.list_scope_memory(task["room_id"], task["sender_id"])

    @staticmethod
    def _relationship_profile(
        row: sqlite3.Row | None,
        notes: list[sqlite3.Row] | None = None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        profile = dict(row)
        profile["flirt_opt_out"] = bool(profile.get("flirt_opt_out"))
        profile["notes"] = [dict(note) for note in notes or []]
        return profile

    @staticmethod
    def _relationship_identity(room_id: str, sender_id: str) -> tuple[str, str]:
        room = str(room_id or "").strip()
        sender = str(sender_id or "").strip()
        if not room or not sender:
            raise ValueError("relationship profile requires room and sender identities")
        return room, sender

    @staticmethod
    def _purge_expired_relationships(
        connection: sqlite3.Connection,
        now: float,
    ) -> None:
        connection.execute(
            "DELETE FROM relationship_notes WHERE expires_at <= ?",
            (now,),
        )
        connection.execute(
            "DELETE FROM relationship_profiles WHERE expires_at <= ?",
            (now,),
        )

    @staticmethod
    def _relationship_profile_from_connection(
        connection: sqlite3.Connection,
        room_id: str,
        sender_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT * FROM relationship_profiles
            WHERE room_id=? AND sender_id=?
            """,
            (room_id, sender_id),
        ).fetchone()
        if row is None:
            return None
        notes = connection.execute(
            """
            SELECT kind, value, source_local_id, created_at, updated_at, expires_at
            FROM relationship_notes
            WHERE room_id=? AND sender_id=?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (room_id, sender_id, MAX_RELATIONSHIP_NOTES),
        ).fetchall()
        return AdapterStore._relationship_profile(row, notes)

    def get_relationship_profile(
        self,
        room_id: str,
        sender_id: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        self.initialize()
        room, sender = self._relationship_identity(room_id, sender_id)
        current = time.time() if now is None else float(now)
        with self._lock, closing(self._connect()) as connection:
            self._purge_expired_relationships(connection, current)
            profile = self._relationship_profile_from_connection(
                connection,
                room,
                sender,
            )
            connection.commit()
        return profile

    def record_relationship_interaction(
        self,
        room_id: str,
        sender_id: str,
        *,
        source_local_id: int | None,
        force_summary: bool = False,
        now: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Record a completed chat turn and report whether it merits a summary."""
        self.initialize()
        room, sender = self._relationship_identity(room_id, sender_id)
        current = time.time() if now is None else float(now)
        local_id = (
            int(source_local_id)
            if source_local_id is not None and int(source_local_id) > 0
            else None
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._purge_expired_relationships(connection, current)
            row = connection.execute(
                """
                SELECT interaction_count FROM relationship_profiles
                WHERE room_id=? AND sender_id=?
                """,
                (room, sender),
            ).fetchone()
            interaction_count = int(row["interaction_count"] or 0) + 1 if row else 1
            familiarity = familiarity_for_interactions(interaction_count)
            expires_at = current + RELATIONSHIP_TTL_SECONDS
            if row is None:
                connection.execute(
                    """
                    INSERT INTO relationship_profiles(
                        room_id, sender_id, interaction_count, familiarity,
                        last_source_local_id, created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        room,
                        sender,
                        interaction_count,
                        familiarity,
                        local_id,
                        current,
                        current,
                        expires_at,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE relationship_profiles
                    SET interaction_count=?, familiarity=?, last_source_local_id=?,
                        updated_at=?, expires_at=?
                    WHERE room_id=? AND sender_id=?
                    """,
                    (
                        interaction_count,
                        familiarity,
                        local_id,
                        current,
                        expires_at,
                        room,
                        sender,
                    ),
                )
            profile = self._relationship_profile_from_connection(
                connection,
                room,
                sender,
            )
            connection.commit()
        if profile is None:
            raise RuntimeError("relationship profile was not persisted")
        return profile, bool(force_summary or interaction_count % 3 == 0)

    def set_relationship_flirt_opt_out(
        self,
        room_id: str,
        sender_id: str,
        enabled: bool,
        *,
        source_local_id: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        room, sender = self._relationship_identity(room_id, sender_id)
        current = time.time() if now is None else float(now)
        local_id = (
            int(source_local_id)
            if source_local_id is not None and int(source_local_id) > 0
            else None
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._purge_expired_relationships(connection, current)
            connection.execute(
                """
                INSERT INTO relationship_profiles(
                    room_id, sender_id, flirt_opt_out, last_source_local_id,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room_id, sender_id) DO UPDATE SET
                    flirt_opt_out=excluded.flirt_opt_out,
                    last_source_local_id=excluded.last_source_local_id,
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at
                """,
                (
                    room,
                    sender,
                    int(bool(enabled)),
                    local_id,
                    current,
                    current,
                    current + RELATIONSHIP_TTL_SECONDS,
                ),
            )
            profile = self._relationship_profile_from_connection(
                connection,
                room,
                sender,
            )
            connection.commit()
        if profile is None:
            raise RuntimeError("relationship profile was not persisted")
        return profile

    @staticmethod
    def _group_listener_room_id(room_id: str) -> str:
        room = str(room_id or "").strip()
        if not room:
            raise ValueError("group listener requires a room identity")
        return room

    def get_group_listener_state(self, room_id: str) -> dict[str, Any] | None:
        self.initialize()
        room = self._group_listener_room_id(room_id)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM group_listener_state WHERE room_id=?",
                (room,),
            ).fetchone()
        return dict(row) if row is not None else None

    def observe_group_listener_message(
        self,
        room_id: str,
        source_local_id: int,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Persist one passive inbound turn without retaining its text."""
        self.initialize()
        room = self._group_listener_room_id(room_id)
        local_id = int(source_local_id)
        if local_id <= 0:
            raise ValueError("group listener requires a positive source local ID")
        current = time.time() if now is None else float(now)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM group_listener_state WHERE room_id=?",
                (room,),
            ).fetchone()
            observed = False
            if row is None:
                connection.execute(
                    """
                    INSERT INTO group_listener_state(
                        room_id, last_observed_local_id, turns_since_reply,
                        updated_at
                    ) VALUES (?, ?, 1, ?)
                    """,
                    (room, local_id, current),
                )
                observed = True
            elif local_id > int(row["last_observed_local_id"] or 0):
                connection.execute(
                    """
                    UPDATE group_listener_state
                    SET last_observed_local_id=?,
                        turns_since_reply=turns_since_reply + 1,
                        updated_at=?
                    WHERE room_id=?
                    """,
                    (local_id, current, room),
                )
                observed = True
            stored = connection.execute(
                "SELECT * FROM group_listener_state WHERE room_id=?",
                (room,),
            ).fetchone()
            connection.commit()
        if stored is None:
            raise RuntimeError("group listener state was not persisted")
        result = dict(stored)
        result["observed"] = observed
        return result

    def mark_group_listener_reply(
        self,
        room_id: str,
        source_local_id: int,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        room = self._group_listener_room_id(room_id)
        local_id = int(source_local_id)
        if local_id <= 0:
            raise ValueError("group listener requires a positive source local ID")
        current = time.time() if now is None else float(now)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO group_listener_state(
                    room_id, last_observed_local_id, last_reply_local_id,
                    last_reply_at, turns_since_reply, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(room_id) DO UPDATE SET
                    last_observed_local_id=MAX(
                        group_listener_state.last_observed_local_id,
                        excluded.last_observed_local_id
                    ),
                    last_reply_local_id=CASE
                        WHEN group_listener_state.last_reply_local_id IS NULL
                          OR excluded.last_reply_local_id >= group_listener_state.last_reply_local_id
                        THEN excluded.last_reply_local_id
                        ELSE group_listener_state.last_reply_local_id
                    END,
                    last_reply_at=CASE
                        WHEN group_listener_state.last_reply_local_id IS NULL
                          OR excluded.last_reply_local_id >= group_listener_state.last_reply_local_id
                        THEN excluded.last_reply_at
                        ELSE group_listener_state.last_reply_at
                    END,
                    turns_since_reply=CASE
                        WHEN group_listener_state.last_reply_local_id IS NULL
                          OR excluded.last_reply_local_id >= group_listener_state.last_reply_local_id
                        THEN 0
                        ELSE group_listener_state.turns_since_reply
                    END,
                    updated_at=excluded.updated_at
                """,
                (room, local_id, local_id, current, current),
            )
            stored = connection.execute(
                "SELECT * FROM group_listener_state WHERE room_id=?",
                (room,),
            ).fetchone()
            connection.commit()
        if stored is None:
            raise RuntimeError("group listener reply state was not persisted")
        return dict(stored)

    def group_listener_state_count(self) -> int:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM group_listener_state"
            ).fetchone()
        return int(row["count"] or 0) if row is not None else 0

    def room_session_epoch(self, room_id: str) -> int:
        self.initialize()
        room = str(room_id or "").strip()
        if not room:
            return 0
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT epoch FROM room_session_epochs WHERE room_id=?",
                (room,),
            ).fetchone()
        return max(0, int(row["epoch"] or 0)) if row is not None else 0

    def forget_relationship(
        self,
        room_id: str,
        sender_id: str,
        *,
        now: float | None = None,
    ) -> int:
        """Forget one member and rotate the shared room Session epoch."""
        self.initialize()
        room, sender = self._relationship_identity(room_id, sender_id)
        current = time.time() if now is None else float(now)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM relationship_profiles WHERE room_id=? AND sender_id=?",
                (room, sender),
            )
            connection.execute(
                "DELETE FROM relationship_summary_jobs WHERE room_id=? AND sender_id=?",
                (room, sender),
            )
            connection.execute(
                """
                INSERT INTO room_session_epochs(room_id, epoch, reason, updated_at)
                VALUES (?, 1, 'relationship_forget', ?)
                ON CONFLICT(room_id) DO UPDATE SET
                    epoch=room_session_epochs.epoch + 1,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (room, current),
            )
            epoch_row = connection.execute(
                "SELECT epoch FROM room_session_epochs WHERE room_id=?",
                (room,),
            ).fetchone()
            connection.commit()
        return int(epoch_row["epoch"])

    def enqueue_relationship_summary(
        self,
        room_id: str,
        sender_id: str,
        *,
        source_local_id: int | None,
        interaction_count: int,
        trigger: str,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Queue one summary per member, merging later turns before it starts."""
        self.initialize()
        room, sender = self._relationship_identity(room_id, sender_id)
        current = time.time() if now is None else float(now)
        local_id = (
            int(source_local_id)
            if source_local_id is not None and int(source_local_id) > 0
            else None
        )
        summary_interactions = max(0, int(interaction_count))
        summary_trigger = str(trigger or "interaction")[:64]
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            queued = connection.execute(
                """
                SELECT id FROM relationship_summary_jobs
                WHERE room_id=? AND sender_id=? AND status='queued'
                """,
                (room, sender),
            ).fetchone()
            if queued is not None:
                connection.execute(
                    """
                    UPDATE relationship_summary_jobs
                    SET source_local_id=CASE
                            WHEN ? IS NULL THEN source_local_id
                            WHEN source_local_id IS NULL OR ? >= source_local_id THEN ?
                            ELSE source_local_id
                        END,
                        interaction_count=MAX(interaction_count, ?),
                        trigger=?, updated_at=?
                    WHERE id=? AND status='queued'
                    """,
                    (
                        local_id,
                        local_id,
                        local_id,
                        summary_interactions,
                        summary_trigger,
                        current,
                        int(queued["id"]),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM relationship_summary_jobs WHERE id=?",
                    (int(queued["id"]),),
                ).fetchone()
                connection.commit()
                result = dict(row) if row is not None else None
                if result is not None:
                    result["_coalesced"] = True
                return result
            cursor = connection.execute(
                """
                INSERT INTO relationship_summary_jobs(
                    room_id, sender_id, source_local_id, interaction_count,
                    trigger, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    room,
                    sender,
                    local_id,
                    summary_interactions,
                    summary_trigger,
                    current,
                    current,
                ),
            )
            row = connection.execute(
                "SELECT * FROM relationship_summary_jobs WHERE id=?",
                (cursor.lastrowid,),
            ).fetchone()
            connection.commit()
        result = dict(row) if row is not None else None
        if result is not None:
            result["_coalesced"] = False
        return result

    def claim_relationship_summary(self) -> dict[str, Any] | None:
        self.initialize()
        current = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM relationship_summary_jobs
                WHERE status='queued'
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            cursor = connection.execute(
                """
                UPDATE relationship_summary_jobs
                SET status='running', attempts=attempts + 1, updated_at=?
                WHERE id=? AND status='queued'
                """,
                (current, row["id"]),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            claimed = connection.execute(
                "SELECT * FROM relationship_summary_jobs WHERE id=?",
                (row["id"],),
            ).fetchone()
            connection.commit()
        return dict(claimed) if claimed is not None else None

    def finish_relationship_summary(
        self,
        job_id: int,
        *,
        status: str,
        error_type: str = "",
        now: float | None = None,
    ) -> bool:
        if status not in {"succeeded", "failed", "dropped"}:
            raise ValueError("relationship summary status is invalid")
        self.initialize()
        current = time.time() if now is None else float(now)
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE relationship_summary_jobs
                SET status=?, error_type=?, updated_at=?
                WHERE id=? AND status IN ('queued', 'running')
                """,
                (status, str(error_type or "")[:80], current, int(job_id)),
            )
            connection.commit()
        return cursor.rowcount == 1

    def recover_relationship_summary_jobs(self) -> int:
        """Drop unreplayable jobs because their chat snippets stay only in RAM."""
        self.initialize()
        current = time.time()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE relationship_summary_jobs
                SET status='dropped', error_type='payload_lost_on_restart', updated_at=?
                WHERE status IN ('queued', 'running')
                """,
                (current,),
            )
            connection.commit()
        return int(cursor.rowcount)

    def relationship_summary_counts(self) -> dict[str, int]:
        self.initialize()
        counts = {status: 0 for status in ("queued", "running", "succeeded", "failed", "dropped")}
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM relationship_summary_jobs
                GROUP BY status
                """
            ).fetchall()
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
        return counts

    def apply_relationship_summary(
        self,
        room_id: str,
        sender_id: str,
        summary: dict[str, Any],
        *,
        source_local_id: int | None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        self.initialize()
        room, sender = self._relationship_identity(room_id, sender_id)
        normalized = normalize_relationship_summary(summary)
        current = time.time() if now is None else float(now)
        local_id = (
            int(source_local_id)
            if source_local_id is not None and int(source_local_id) > 0
            else None
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._purge_expired_relationships(connection, current)
            current_profile = connection.execute(
                """
                SELECT * FROM relationship_profiles
                WHERE room_id=? AND sender_id=?
                """,
                (room, sender),
            ).fetchone()
            if current_profile is None:
                connection.commit()
                return None
            preferred_name = normalized["preferred_name"] or str(
                current_profile["preferred_name"] or ""
            )
            banter_style = normalized["banter_style"] or str(
                current_profile["banter_style"] or "neutral"
            )
            reciprocity = max(
                0,
                min(
                    3,
                    int(current_profile["reciprocity"] or 0)
                    + int(normalized["reciprocity_delta"]),
                ),
            )
            connection.execute(
                """
                UPDATE relationship_profiles
                SET preferred_name=?, banter_style=?, reciprocity=?,
                    last_source_local_id=COALESCE(?, last_source_local_id),
                    updated_at=?, expires_at=?
                WHERE room_id=? AND sender_id=?
                """,
                (
                    preferred_name,
                    banter_style,
                    reciprocity,
                    local_id,
                    current,
                    current + RELATIONSHIP_TTL_SECONDS,
                    room,
                    sender,
                ),
            )
            for note in normalized["notes"]:
                connection.execute(
                    """
                    INSERT INTO relationship_notes(
                        room_id, sender_id, kind, value, source_local_id,
                        created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(room_id, sender_id, kind, value) DO UPDATE SET
                        source_local_id=excluded.source_local_id,
                        updated_at=excluded.updated_at,
                        expires_at=excluded.expires_at
                    """,
                    (
                        room,
                        sender,
                        note["kind"],
                        note["value"],
                        local_id,
                        current,
                        current,
                        current + RELATIONSHIP_TTL_SECONDS,
                    ),
                )
            overflow = connection.execute(
                """
                SELECT id FROM relationship_notes
                WHERE room_id=? AND sender_id=?
                ORDER BY updated_at DESC, id DESC
                LIMIT -1 OFFSET ?
                """,
                (room, sender, MAX_RELATIONSHIP_NOTES),
            ).fetchall()
            if overflow:
                placeholders = ",".join("?" for _ in overflow)
                connection.execute(
                    "DELETE FROM relationship_notes WHERE id IN (%s)" % placeholders,
                    tuple(int(row["id"]) for row in overflow),
                )
            profile = self._relationship_profile_from_connection(
                connection,
                room,
                sender,
            )
            connection.commit()
        return profile

    def register_skill(
        self,
        *,
        name: str,
        version: str,
        source: str,
        sha256: str,
        capabilities: list[str] | dict[str, Any] | None = None,
        audit: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        self.initialize()
        skill_name = str(name or "").strip()
        digest = str(sha256 or "").strip().lower()
        if not skill_name:
            raise ValueError("skill name is required")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("skill sha256 is invalid")
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO skill_registry(
                    name, version, source, sha256, capabilities_json,
                    audit_json, enabled, revoked_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    version=excluded.version,
                    source=excluded.source,
                    sha256=excluded.sha256,
                    capabilities_json=excluded.capabilities_json,
                    audit_json=excluded.audit_json,
                    enabled=excluded.enabled,
                    revoked_at=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    skill_name,
                    str(version or ""),
                    str(source or ""),
                    digest,
                    json_dumps(capabilities or []),
                    json_dumps(audit or {}),
                    int(bool(enabled)),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM skill_registry WHERE name=?",
                (skill_name,),
            ).fetchone()
            connection.commit()
        return self._skill(row)

    @staticmethod
    def _skill(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        value["capabilities"] = json.loads(value.pop("capabilities_json") or "[]")
        value["audit"] = json.loads(value.pop("audit_json") or "{}")
        return value

    def get_skill(self, name: str) -> dict[str, Any] | None:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM skill_registry WHERE name=?",
                (str(name or "").strip(),),
            ).fetchone()
        return self._skill(row)

    def list_skills(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        self.initialize()
        query = "SELECT * FROM skill_registry"
        if enabled_only:
            query += " WHERE enabled=1 AND revoked_at IS NULL"
        query += " ORDER BY name"
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query).fetchall()
        return [self._skill(row) for row in rows]

    def revoke_skill(self, name: str) -> dict[str, Any] | None:
        self.initialize()
        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE skill_registry
                SET enabled=0, revoked_at=?, updated_at=?
                WHERE name=?
                """,
                (now, now, str(name or "").strip()),
            )
            if cursor.rowcount != 1:
                connection.commit()
                return None
            row = connection.execute(
                "SELECT * FROM skill_registry WHERE name=?",
                (str(name or "").strip(),),
            ).fetchone()
            connection.commit()
        return self._skill(row)

    def sync_skill_registry(
        self,
        inventory: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        self.initialize()
        normalized: dict[str, dict[str, Any]] = {}
        for item in inventory:
            if not isinstance(item, dict):
                raise ValueError("skill inventory entry is invalid")
            name = str(item.get("name") or "").strip()
            digest = str(
                item.get("sha256") or item.get("bundle_sha256") or ""
            ).strip().lower()
            capabilities = item.get("capabilities")
            audit = item.get("audit")
            if (
                not name
                or name in normalized
                or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
                or not isinstance(capabilities, list)
                or not isinstance(audit, dict)
            ):
                raise ValueError("skill inventory entry is invalid")
            normalized[name] = {
                "name": name,
                "version": str(item.get("version") or ""),
                "source": str(item.get("source") or ""),
                "sha256": digest,
                "capabilities": capabilities,
                "audit": audit,
            }

        now = time.time()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM skill_registry"
                ).fetchall()
            }
            for item in normalized.values():
                connection.execute(
                    """
                    INSERT INTO skill_registry(
                        name, version, source, sha256, capabilities_json,
                        audit_json, enabled, revoked_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        version=excluded.version,
                        source=excluded.source,
                        sha256=excluded.sha256,
                        capabilities_json=excluded.capabilities_json,
                        audit_json=excluded.audit_json,
                        enabled=1,
                        revoked_at=NULL,
                        updated_at=excluded.updated_at
                    """,
                    (
                        item["name"],
                        item["version"],
                        item["source"],
                        item["sha256"],
                        json_dumps(item["capabilities"]),
                        json_dumps(item["audit"]),
                        now,
                        now,
                    ),
                )
            missing = sorted(existing - set(normalized))
            if missing:
                placeholders = ",".join("?" for _ in missing)
                connection.execute(
                    """
                    UPDATE skill_registry
                    SET enabled=0, revoked_at=COALESCE(revoked_at, ?), updated_at=?
                    WHERE name IN (%s)
                    """
                    % placeholders,
                    (now, now, *missing),
                )
            rows = connection.execute(
                "SELECT * FROM skill_registry ORDER BY name"
            ).fetchall()
            connection.commit()
        return [self._skill(row) for row in rows]

    def skill_snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "name": skill["name"],
                "version": skill["version"],
                "source": skill["source"],
                "sha256": skill["sha256"],
                "capabilities": skill["capabilities"],
            }
            for skill in self.list_skills(enabled_only=True)
        ]

    def add_task_event(
        self,
        task_id: str,
        event_type: str,
        detail: str | None = None,
    ) -> None:
        self.initialize()
        safe_detail = (
            redact_sensitive_text(detail, limit=300) if detail else None
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO task_events(task_id, event_type, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, str(event_type)[:64], safe_detail, time.time()),
            )
            connection.commit()

    def list_task_events(self, task_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_events
                WHERE task_id=?
                ORDER BY id
                """,
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_downloaded_artifact(
        self,
        task_id: str,
        name: str,
        path: Path,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        *,
        max_total_bytes: int,
    ) -> dict[str, Any]:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT status, cancel_requested FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                connection.rollback()
                raise KeyError(task_id)
            if task["status"] != "running" or bool(task["cancel_requested"]):
                connection.rollback()
                raise PermissionError("download recording requires a running task")
            existing = connection.execute(
                """
                SELECT * FROM downloaded_artifacts
                WHERE task_id=? AND name=?
                """,
                (task_id, name),
            ).fetchone()
            if existing is not None:
                if (
                    existing["path"] != str(path)
                    or existing["mime_type"] != mime_type
                    or int(existing["size_bytes"]) != int(size_bytes)
                    or existing["sha256"] != sha256
                ):
                    connection.rollback()
                    raise ValueError(
                        "download name already belongs to different content"
                    )
                connection.commit()
                return dict(existing)
            total = connection.execute(
                """
                SELECT COALESCE(SUM(size_bytes), 0) AS value
                FROM downloaded_artifacts WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
            if int(total["value"] or 0) + int(size_bytes) > int(max_total_bytes):
                connection.rollback()
                raise ValueError("task download byte limit exceeded")
            connection.execute(
                """
                INSERT INTO downloaded_artifacts(
                    task_id, name, path, mime_type, size_bytes, sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, name, str(path), mime_type, size_bytes, sha256, time.time()),
            )
            row = connection.execute(
                """
                SELECT * FROM downloaded_artifacts
                WHERE task_id=? AND name=?
                """,
                (task_id, name),
            ).fetchone()
            connection.commit()
        return dict(row)

    def downloaded_bytes(self, task_id: str) -> int:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(size_bytes), 0) AS value
                FROM downloaded_artifacts WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
        return int(row["value"] or 0)

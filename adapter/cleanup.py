from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any


TASK_DIR_RE = re.compile(r"^T-[A-F0-9]{8}$")
HERMES_RUNTIME_PATTERNS = (
    (".hermes/sessions", "request_dump_*.json"),
    (".hermes/logs", "*.log.*"),
    (".npm/_logs", "*-debug-*.log"),
)


def cleanup_artifacts(
    root: Path,
    cutoff: float,
    *,
    failures: list[dict[str, str]] | None = None,
) -> int:
    root = root.resolve(strict=True)
    removed = 0
    for child in root.iterdir():
        if not TASK_DIR_RE.fullmatch(child.name):
            continue
        if child.is_symlink():
            raise RuntimeError("artifact cleanup refuses symbolic links")
        if not child.is_dir() or child.stat().st_mtime >= cutoff:
            continue
        resolved = child.resolve(strict=True)
        if resolved.parent != root:
            raise RuntimeError("artifact cleanup path escaped its root")
        try:
            shutil.rmtree(resolved)
            removed += 1
        except OSError as exc:
            if failures is None:
                raise
            failures.append(
                {
                    "task_id": child.name,
                    "error_type": type(exc).__name__,
                }
            )
    return removed


def cleanup_hermes_runtime_files(home: Path, cutoff: float) -> dict[str, int]:
    if home.is_symlink():
        raise RuntimeError("Hermes home cleanup refuses symbolic links")
    home = home.resolve(strict=True)
    counts: dict[str, int] = {}
    for relative_dir, pattern in HERMES_RUNTIME_PATTERNS:
        base = home / relative_dir
        key = relative_dir.replace("/", "_")
        counts[key] = 0
        if not base.exists():
            continue
        if base.is_symlink():
            raise RuntimeError("Hermes runtime cleanup refuses symbolic links")
        resolved_base = base.resolve(strict=True)
        if home not in resolved_base.parents:
            raise RuntimeError("Hermes runtime cleanup path escaped its home")
        for candidate in base.glob(pattern):
            if candidate.is_symlink():
                raise RuntimeError(
                    "Hermes runtime cleanup refuses symbolic links"
                )
            if not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            if resolved.parent != resolved_base:
                raise RuntimeError(
                    "Hermes runtime cleanup path escaped its directory"
                )
            if resolved.stat().st_mtime < cutoff:
                resolved.unlink()
                counts[key] += 1
    return counts


def cleanup_database(path: Path, cutoff: float) -> dict[str, int]:
    path = path.resolve(strict=True)
    counts: dict[str, int] = {}
    with sqlite3.connect(str(path), timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        expired_task_ids = [
            row[0]
            for row in connection.execute(
                """
                SELECT id FROM tasks
                WHERE status IN ('succeeded', 'failed', 'canceled')
                  AND completed_at IS NOT NULL
                  AND completed_at < ?
                """,
                (cutoff,),
            ).fetchall()
        ]
        if expired_task_ids:
            placeholders = ",".join("?" for _ in expired_task_ids)
            for table in (
                "outbox_items",
                "artifacts",
                "tool_events",
                "task_events",
                "downloaded_artifacts",
            ):
                cursor = connection.execute(
                    "DELETE FROM %s WHERE task_id IN (%s)"
                    % (table, placeholders),
                    expired_task_ids,
                )
                counts[table] = int(cursor.rowcount)
            cursor = connection.execute(
                "DELETE FROM tasks WHERE id IN (%s)" % placeholders,
                expired_task_ids,
            )
            counts["tasks"] = int(cursor.rowcount)
        else:
            for table in (
                "outbox_items",
                "artifacts",
                "tool_events",
                "task_events",
                "downloaded_artifacts",
                "tasks",
            ):
                counts[table] = 0

        for table, timestamp in (
            ("request_cache", "created_at"),
            ("inbound_ledger", "created_at"),
            ("usage_ledger", "created_at"),
            ("task_events", "created_at"),
            ("tool_events", "created_at"),
            ("downloaded_artifacts", "created_at"),
        ):
            cursor = connection.execute(
                "DELETE FROM %s WHERE %s < ?" % (table, timestamp),
                (cutoff,),
            )
            counts[table] = counts.get(table, 0) + int(cursor.rowcount)
        cursor = connection.execute(
            """
            DELETE FROM scope_memory
            WHERE expires_at IS NOT NULL AND expires_at <= ?
            """,
            (time.time(),),
        )
        counts["scope_memory"] = int(cursor.rowcount)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            connection.rollback()
            raise RuntimeError("database cleanup would leave foreign key violations")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return counts


def write_cleanup_status(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("cleanup status path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("cleanup status path must not be a symbolic link")
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--artifact-days", type=int, default=7)
    parser.add_argument("--record-days", type=int, default=30)
    args = parser.parse_args()
    started_at = time.time()
    artifact_cutoff = started_at - max(1, args.artifact_days) * 86400
    record_cutoff = started_at - max(1, args.record_days) * 86400
    errors: list[dict[str, str]] = []
    artifact_failures: list[dict[str, str]] = []
    artifact_count = 0
    database_counts: dict[str, int] = {}
    runtime_counts: dict[str, int] = {}

    try:
        artifact_count = cleanup_artifacts(
            args.artifact_root,
            artifact_cutoff,
            failures=artifact_failures,
        )
        errors.extend(
            {"stage": "artifacts", **failure}
            for failure in artifact_failures
        )
    except Exception as exc:  # noqa: BLE001 - later stages must still run
        errors.append(
            {"stage": "artifacts", "error_type": type(exc).__name__}
        )

    try:
        database_counts = cleanup_database(args.database, record_cutoff)
    except Exception as exc:  # noqa: BLE001 - runtime cleanup remains independent
        errors.append(
            {"stage": "database", "error_type": type(exc).__name__}
        )

    try:
        runtime_counts = cleanup_hermes_runtime_files(
            args.hermes_home,
            record_cutoff,
        )
    except Exception as exc:  # noqa: BLE001 - status must capture the failure
        errors.append(
            {"stage": "runtime", "error_type": type(exc).__name__}
        )

    status = {
        "schema_version": 1,
        "ok": not errors,
        "started_at": started_at,
        "completed_at": time.time(),
        "artifact_directories_removed": artifact_count,
        "database_rows_removed": database_counts,
        "runtime_files_removed": runtime_counts,
        "errors": errors,
    }
    if args.status_file is not None:
        try:
            write_cleanup_status(args.status_file, status)
        except Exception as exc:  # noqa: BLE001 - report without hiding prior work
            status["ok"] = False
            status["errors"].append(
                {"stage": "status", "error_type": type(exc).__name__}
            )
    print(json.dumps(status, sort_keys=True))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

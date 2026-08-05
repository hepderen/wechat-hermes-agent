from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.media import validate_media_path
from app.policy import stable_session_id
from app.store import AdapterStore


TASK_ID_RE = re.compile(r"^T-[A-F0-9]{8}$")
ROOM_ID = "00000000000@chatroom"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def require_same_artifact(expected, actual) -> None:
    require(actual.path == expected.path, "MCP returned a different artifact path")
    require(actual.mime_type == expected.mime_type, "MCP returned a different MIME")
    require(actual.size_bytes == expected.size_bytes, "MCP returned a different size")
    require(actual.sha256 == expected.sha256, "MCP returned a different SHA-256")


def load_mcp_server():
    original_cwd = Path.cwd()
    restore_cwd = (
        original_cwd
        if os.access(original_cwd, os.X_OK)
        else PROJECT_ROOT
    )
    try:
        os.chdir(PROJECT_ROOT)
        return importlib.import_module("mcp_server")
    finally:
        os.chdir(restore_cwd)


def remove_test_task(path: Path, artifact_root: Path) -> None:
    resolved_root = artifact_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    require(
        resolved.parent == resolved_root and TASK_ID_RE.fullmatch(resolved.name) is not None,
        "refusing to remove a path outside the generated task directory",
    )
    shutil.rmtree(resolved)


def test_store_recovery(state_root: Path) -> dict[str, object]:
    test_root = Path(
        tempfile.mkdtemp(prefix=".readiness-store-", dir=str(state_root))
    )
    try:
        store = AdapterStore(test_root / "adapter.db")
        task, created = store.create_task(
            request_id="readiness-" + secrets.token_hex(8),
            request_hash=secrets.token_hex(32),
            room_id=ROOM_ID,
            sender_id="wxid_readiness",
            session_id=stable_session_id(ROOM_ID, "wxid_readiness"),
            kind="run",
            prompt="readiness test",
            max_attempts=3,
            source_local_id=None,
        )
        require(created, "test task was not created")

        claimed = store.claim_next()
        require(claimed is not None and claimed["id"] == task["id"], "claim failed")
        require(store.recover() == 1, "run without a Hermes ID was not recovered")
        require(store.get_task(task["id"])["status"] == "queued", "recovery did not queue")

        claimed = store.claim_next()
        require(claimed is not None, "recovered task could not be claimed")
        store.set_run_id(task["id"], "run_readiness")
        require(store.recover() == 0, "live Hermes run should remain running")

        canceled = store.cancel_task(task["id"], ROOM_ID)
        require(canceled is not None and canceled["cancel_requested"], "cancel was not recorded")
        store.complete(task["id"], "canceled")
        retried = store.retry_task(task["id"], ROOM_ID)
        require(retried is not None and retried["status"] == "queued", "retry did not queue")
        require(retried["attempts"] == 0, "retry did not reset attempts")
        require(retried["hermes_run_id"] is None, "retry kept the old Hermes run ID")
        require(retried["delivery_generation"] == 1, "retry did not rotate delivery IDs")
        return {
            "task_id": task["id"],
            "recovered": True,
            "cancel_retry": True,
        }
    finally:
        shutil.rmtree(test_root)


def test_real_mp4(artifact_root: Path) -> dict[str, object]:
    mcp_server = load_mcp_server()
    artifact_root = artifact_root.resolve(strict=True)
    task_id = "T-" + secrets.token_hex(4).upper()
    other_task_id = "T-" + secrets.token_hex(4).upper()
    task_root = artifact_root / task_id
    other_root = artifact_root / other_task_id
    require(not task_root.exists() and not other_root.exists(), "test task already exists")
    task_root.mkdir(mode=0o700)
    other_root.mkdir(mode=0o700)

    try:
        video = task_root / "readiness.mp4"
        subprocess.run(
            [
                "/usr/bin/ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x568:r=24:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-y",
                str(video),
            ],
            check=True,
            timeout=60,
        )
        probe = subprocess.run(
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration,size",
                "-of",
                "json",
                str(video),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        probe_data = json.loads(probe.stdout)["format"]

        artifact = validate_media_path(
            str(video),
            artifact_root,
            task_id,
            max_bytes=20 * 1024 * 1024,
        )
        require(artifact.mime_type == "video/mp4", "adapter MIME check failed")

        original_root = mcp_server.ARTIFACT_ROOT
        try:
            mcp_server.ARTIFACT_ROOT = artifact_root
            validated = mcp_server.validate_cloud_artifact(task_id, str(video))
            require_same_artifact(artifact, validated)

            outside = other_root / "outside.mp4"
            shutil.copyfile(video, outside)
            try:
                mcp_server.validate_cloud_artifact(task_id, str(outside))
            except ValueError:
                pass
            else:
                raise RuntimeError("MCP accepted an artifact owned by another task")

            fake = task_root / "fake.mp4"
            fake.write_bytes(b"\x89PNG\r\n\x1a\nnot-an-mp4")
            try:
                mcp_server.validate_cloud_artifact(task_id, str(fake))
            except ValueError:
                pass
            else:
                raise RuntimeError("MCP accepted a forged MP4 MIME signature")
        finally:
            mcp_server.ARTIFACT_ROOT = original_root

        return {
            "task_id": task_id,
            "mime_type": artifact.mime_type,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "format": probe_data.get("format_name"),
            "duration": probe_data.get("duration"),
            "task_scope_rejected": True,
            "forged_mime_rejected": True,
        }
    finally:
        if task_root.exists():
            remove_test_task(task_root, artifact_root)
        if other_root.exists():
            remove_test_task(other_root, artifact_root)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic cloud-only production readiness checks."
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path("/var/lib/wechat-hermes"),
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("/var/lib/wechat-hermes/artifacts"),
    )
    args = parser.parse_args()

    state_root = args.state_root.resolve(strict=True)
    artifact_root = args.artifact_root.resolve(strict=True)
    result = {
        "status": "ok",
        "checks": {
            "store": test_store_recovery(state_root),
            "media": test_real_mp4(artifact_root),
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

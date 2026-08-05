from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


SELECTED_SKILLS = (
    {
        "name": "humanizer",
        "category": "creative",
        "install_path": "creative/humanizer",
        "source": "builtin",
        "expected_version": "2.5.1",
    },
    {
        "name": "creative-ideation",
        "category": "creative",
        "install_path": "creative/creative-ideation",
        "source": "official",
        "expected_version": "2.1.0",
    },
    {
        "name": "youtube-content",
        "category": "media",
        "install_path": "media/youtube-content",
        "source": "builtin",
        "expected_version": None,
    },
    {
        "name": "kanban-video-orchestrator",
        "category": "creative",
        "install_path": "creative/kanban-video-orchestrator",
        "source": "official",
        "expected_version": "1.0.0",
    },
    {
        "name": "arxiv",
        "category": "research",
        "install_path": "research/arxiv",
        "source": "builtin",
        "expected_version": "1.0.0",
    },
    {
        "name": "hermes-agent-skill-authoring",
        "category": "software-development",
        "install_path": "software-development/hermes-agent-skill-authoring",
        "source": "builtin",
        "expected_version": "1.1.0",
    },
    {
        "name": "douyin-video-production",
        "category": "media",
        "install_path": "media/douyin-video-production",
        "source": "local",
        "expected_version": "1.2.0",
    },
    {
        "name": "wechat-group-operations",
        "category": "productivity",
        "install_path": "productivity/wechat-group-operations",
        "source": "local",
        "expected_version": "1.2.0",
    },
)

SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_SCAN_VERDICTS = {
    "clean",
    "pass",
    "passed",
    "safe",
    "trusted",
    "trusted-builtin",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise RuntimeError("%s has no YAML frontmatter" % path)
    end = text.find("\n---", 4)
    if end < 0:
        raise RuntimeError("%s has unterminated YAML frontmatter" % path)
    value = yaml.safe_load(text[4:end]) or {}
    if not isinstance(value, dict):
        raise RuntimeError("%s frontmatter is not an object" % path)
    return value


def frontmatter_version(value: dict[str, Any]) -> str | None:
    version = value.get("version")
    if version is None:
        metadata = value.get("metadata")
        if isinstance(metadata, dict):
            hermes = metadata.get("hermes")
            if isinstance(hermes, dict):
                version = hermes.get("version")
    return str(version) if version is not None else None


def inventory(path: Path) -> tuple[dict[str, str], str]:
    files: dict[str, str] = {}
    bundle = hashlib.sha256()
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        if "__pycache__" in file_path.parts or file_path.name.endswith(".pyc"):
            continue
        relative = file_path.relative_to(path).as_posix()
        digest = sha256_file(file_path)
        files[relative] = digest
        bundle.update(relative.encode("utf-8"))
        bundle.update(b"\0")
        bundle.update(file_path.read_bytes())
        bundle.update(b"\0")
    return files, bundle.hexdigest()


def dynamic_inventory(path: Path) -> tuple[dict[str, str], str]:
    files: dict[str, str] = {}
    bundle = hashlib.sha256()
    for file_path in sorted(path.rglob("*")):
        if file_path.is_symlink():
            raise RuntimeError("dynamic Skill contains a symbolic link")
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(path).as_posix()
        digest = sha256_file(file_path)
        files[relative] = digest
        bundle.update(relative.encode("utf-8"))
        bundle.update(b"\0")
        bundle.update(bytes.fromhex(digest))
        bundle.update(b"\0")
    return files, bundle.hexdigest()


def command_output(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def skill_root(skills_root: Path, install_path: str) -> Path:
    if not install_path or Path(install_path).is_absolute():
        raise RuntimeError("Skill has an invalid install_path")
    root = (skills_root / install_path).resolve(strict=True)
    if root == skills_root or not root.is_relative_to(skills_root):
        raise RuntimeError("skill path escaped the configured skills root")
    if not root.is_dir():
        raise RuntimeError("Skill install_path is not a directory")
    return root


def discover_dynamic_skills(
    skills_root: Path,
    hub_installed: dict[str, Any],
    previous_dynamic: list[Any],
) -> list[dict[str, Any]]:
    selected_names = {str(item["name"]) for item in SELECTED_SKILLS}
    previous_by_name: dict[str, dict[str, Any]] = {}
    for item in previous_dynamic:
        if not isinstance(item, dict):
            raise RuntimeError("existing dynamic Skill lock is invalid")
        name = item.get("name")
        if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
            raise RuntimeError("existing dynamic Skill lock has an unsafe name")
        previous_by_name[name] = item

    dynamic: list[dict[str, Any]] = []
    for name, entry in hub_installed.items():
        if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
            raise RuntimeError("Skill hub lock contains an unsafe metadata name")
        if not isinstance(entry, dict):
            raise RuntimeError("Skill hub lock contains invalid metadata")
        if name in selected_names:
            continue

        previous = previous_by_name.get(name, {})
        install_path = str(
            entry.get("install_path") or previous.get("install_path") or ""
        ).strip()
        root = skill_root(skills_root, install_path)
        metadata = parse_frontmatter(root / "SKILL.md")
        if metadata.get("name") != name:
            raise RuntimeError("dynamic Skill name mismatch for %s" % install_path)

        provenance = entry.get("scan_provenance")
        verdict = entry.get("scan_verdict")
        if not verdict and isinstance(provenance, dict):
            verdict = provenance.get("verdict")
        if not verdict:
            verdict = previous.get("scan_verdict")
        verdict = str(verdict or "").strip().lower()
        if verdict not in SAFE_SCAN_VERDICTS:
            raise RuntimeError("dynamic Skill has no safe scan verdict: %s" % name)

        content_hash = str(
            entry.get("content_hash") or previous.get("content_hash") or ""
        ).strip()
        if len(content_hash) < 16:
            raise RuntimeError(
                "dynamic Skill has no verifiable content hash: %s" % name
            )
        if not bool(previous.get("pinned") or entry.get("pinned")):
            raise RuntimeError(
                "dynamic Skill has no audited pin metadata: %s" % name
            )

        files, bundle_hash = dynamic_inventory(root)
        if previous and previous.get("bundle_sha256") != bundle_hash:
            raise RuntimeError(
                "dynamic Skill content changed since it was pinned: %s" % name
            )
        dynamic.append(
            {
                "name": name,
                "install_path": root.relative_to(skills_root).as_posix(),
                "content_hash": content_hash,
                "bundle_sha256": bundle_hash,
                "file_count": len(files),
                "scan_verdict": verdict,
                "pinned": True,
            }
        )
    return sorted(dynamic, key=lambda item: item["name"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-repo", type=Path, required=True)
    parser.add_argument("--skills-root", type=Path, required=True)
    parser.add_argument("--hub-lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    hermes_repo = args.hermes_repo.resolve(strict=True)
    skills_root = args.skills_root.resolve(strict=True)
    hub_lock = json.loads(args.hub_lock.read_text(encoding="utf-8"))
    if not isinstance(hub_lock, dict):
        raise RuntimeError("Skill hub lock is invalid")
    hub_installed = hub_lock.get("installed")
    if not isinstance(hub_installed, dict):
        raise RuntimeError("Skill hub lock has no installed inventory")
    previous_dynamic: list[Any] = []
    if args.output.exists():
        previous_lock = json.loads(args.output.read_text(encoding="utf-8"))
        if not isinstance(previous_lock, dict):
            raise RuntimeError("existing Skills integrity lock is invalid")
        raw_dynamic = previous_lock.get("dynamic_skills", [])
        if not isinstance(raw_dynamic, list):
            raise RuntimeError("existing dynamic Skills lock is invalid")
        previous_dynamic = raw_dynamic
    revision = command_output("git", "-C", str(hermes_repo), "rev-parse", "HEAD")
    version = command_output(
        str(hermes_repo / "venv" / "bin" / "python"),
        "-c",
        "from hermes_cli import __version__; print(__version__)",
    )

    locked_skills: list[dict[str, Any]] = []
    for selected in SELECTED_SKILLS:
        selected_root = skill_root(skills_root, selected["install_path"])
        metadata = parse_frontmatter(selected_root / "SKILL.md")
        if metadata.get("name") != selected["name"]:
            raise RuntimeError("skill name mismatch for %s" % selected["install_path"])
        actual_version = frontmatter_version(metadata)
        if actual_version != selected["expected_version"]:
            raise RuntimeError(
                "version mismatch for %s: expected %r, got %r"
                % (selected["name"], selected["expected_version"], actual_version)
            )
        files, bundle_hash = inventory(selected_root)
        hub_entry = hub_installed.get(selected["name"]) or {}
        scan = hub_entry.get("scan_provenance")
        if selected["source"] == "local":
            scan = {
                "verdict": "safe",
                "method": "quick_validate.py plus no-executable-file review",
                "executable_files": [
                    name
                    for name in files
                    if Path(name).suffix.lower() in {".py", ".ps1", ".sh", ".bat", ".cmd"}
                ],
            }
            if scan["executable_files"]:
                raise RuntimeError(
                    "custom skill contains executable files: %s"
                    % ", ".join(scan["executable_files"])
                )
        elif selected["source"] == "builtin":
            scan = {
                "verdict": "trusted-builtin",
                "pinned_by": "Hermes revision and per-file SHA-256",
            }
        locked_skills.append(
            {
                **selected,
                "version": actual_version,
                "files": files,
                "bundle_sha256": bundle_hash,
                "hub_content_hash": hub_entry.get("content_hash"),
                "scan": scan,
            }
        )

    lock = {
        "lock_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hermes": {
            "version": version,
            "revision": revision,
            "repository": str(hermes_repo),
        },
        "skills_root": str(skills_root),
        "skills": locked_skills,
        "dynamic_skills": discover_dynamic_skills(
            skills_root,
            hub_installed,
            previous_dynamic,
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

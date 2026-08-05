from __future__ import annotations

import json

import pytest

from app.skill_install import _inventory as installer_inventory
from scripts.generate_skills_lock import (
    discover_dynamic_skills,
    dynamic_inventory,
)


def make_dynamic_skill(tmp_path):
    skills_root = tmp_path / "skills"
    root = skills_root / "creative" / "dynamic-example"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        """---
name: dynamic-example
version: 1.0.0
---

# Dynamic example
""",
        encoding="utf-8",
    )
    _files, bundle_hash = dynamic_inventory(root)
    hub = {
        "dynamic-example": {
            "install_path": "creative/dynamic-example",
            "content_hash": "0123456789abcdef0123456789abcdef",
            "scan_verdict": "safe",
            "scan_provenance": {"verdict": "safe", "scanner": "fixture"},
        }
    }
    previous = [
        {
            "name": "dynamic-example",
            "install_path": "creative/dynamic-example",
            "content_hash": "0123456789abcdef0123456789abcdef",
            "bundle_sha256": bundle_hash,
            "file_count": 1,
            "scan_verdict": "safe",
            "pinned": True,
        }
    ]
    return skills_root, hub, previous


def test_dynamic_skills_are_preserved_and_revalidated(tmp_path):
    skills_root, hub, previous = make_dynamic_skill(tmp_path)
    root = skills_root / "creative" / "dynamic-example"
    assert dynamic_inventory(root) == installer_inventory(root)

    dynamic = discover_dynamic_skills(skills_root, hub, previous)
    assert dynamic == previous

    skill_file = skills_root / "creative" / "dynamic-example" / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8") + "\nchanged\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="content changed"):
        discover_dynamic_skills(skills_root, hub, previous)


def test_dynamic_skill_discovery_rejects_unsafe_hub_names(tmp_path):
    skills_root, _hub, _previous = make_dynamic_skill(tmp_path)
    with pytest.raises(RuntimeError, match="unsafe metadata name"):
        discover_dynamic_skills(
            skills_root,
            {"--force": {}},
            [],
        )

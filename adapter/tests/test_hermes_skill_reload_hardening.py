from pathlib import Path

import pytest

from deploy.harden_hermes_skill_reload import (
    RELATIVE_PATH,
    REPLACEMENTS,
    harden,
)


def write_fixture(root: Path) -> Path:
    path = root / RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            replacement.old
            for replacement in REPLACEMENTS
            for _ in range(replacement.expected_count)
        ),
        encoding="utf-8",
    )
    return path


def test_skill_reload_hardening_is_complete_and_idempotent(tmp_path):
    path = write_fixture(tmp_path)

    first = harden(tmp_path, compile_file=False)
    second = harden(tmp_path, compile_file=False)

    assert first == path
    assert second == path
    source = path.read_text(encoding="utf-8")
    for replacement in REPLACEMENTS:
        assert source.count(replacement.new) == replacement.expected_count
        assert source.count(replacement.old) == (
            replacement.expected_count
            * replacement.new.count(replacement.old)
        )
    assert 'self._app.router.add_post("/v1/skills/reload"' in source
    assert "skills_reload_busy" in source
    assert "actual_root != expected_root" in source
    assert "clear_skills_system_prompt_cache(clear_snapshot=True)" in source
    assert "_SKILLS_CACHE.clear()" in source
    assert "reload_bundles()" in source


def test_skill_reload_hardening_stops_on_source_drift(tmp_path):
    path = write_fixture(tmp_path)
    path.write_text("upstream changed the API implementation\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected Hermes Skill API source"):
        harden(tmp_path, compile_file=False)


def test_skill_reload_hardening_rejects_mixed_patched_source(tmp_path):
    path = write_fixture(tmp_path)
    harden(tmp_path, compile_file=False)
    path.write_text(
        path.read_text(encoding="utf-8") + REPLACEMENTS[1].old,
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unexpected Hermes Skill API source"):
        harden(tmp_path, compile_file=False)

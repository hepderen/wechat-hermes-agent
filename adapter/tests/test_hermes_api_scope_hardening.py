from pathlib import Path

import pytest

from deploy.harden_hermes_api_scopes import (
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


def test_api_scope_hardening_is_complete_and_idempotent(tmp_path):
    path = write_fixture(tmp_path)

    first = harden(tmp_path, compile_file=False)
    second = harden(tmp_path, compile_file=False)

    assert first == path
    assert second == path
    source = path.read_text(encoding="utf-8")
    for replacement in REPLACEMENTS:
        assert replacement.old not in source
        assert source.count(replacement.new) == replacement.expected_count
    assert "disable_tools must be a boolean" in source
    assert "enabled_toolsets_override=[] if disable_tools else None" in source


def test_api_scope_hardening_stops_on_source_drift(tmp_path):
    path = write_fixture(tmp_path)
    path.write_text("upstream changed the API implementation\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected Hermes API source"):
        harden(tmp_path, compile_file=False)

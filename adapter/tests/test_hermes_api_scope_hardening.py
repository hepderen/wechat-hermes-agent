import textwrap
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
    assert "invalid_enabled_toolsets" in source
    assert "unavailable_toolsets" in source
    assert "enabled_toolsets_override=enabled_toolsets" in source


def test_api_scope_hardening_generated_source_compiles(tmp_path):
    path = write_fixture(tmp_path)

    # The compact fixture above intentionally contains source fragments. Pull
    # the generated multiline call out and compile it in the same function
    # shape as the real Hermes implementation.
    harden(tmp_path, compile_file=False)
    source = path.read_text(encoding="utf-8")
    replacement = next(
        item
        for item in REPLACEMENTS
        if "enabled_toolsets_override=enabled_toolsets_override" in item.new
    )
    call = textwrap.dedent(replacement.new)
    harness = (
        "def _run():\n"
        "    agent = self._create_agent(\n"
        + textwrap.indent(call, "    ")
        + "    if agent_ref is not None:\n"
        + "        agent_ref[0] = agent\n"
    )
    compile(harness, str(path), "exec")

    # A second pass must remain both idempotent and syntactically valid. This
    # catches whitespace-only drift in multiline replacement blocks.
    harden(tmp_path, compile_file=False)
    source_again = path.read_text(encoding="utf-8")
    assert source_again == source
    compile(harness, str(path), "exec")


def test_api_scope_hardening_compile_failure_does_not_mutate_source(tmp_path):
    path = write_fixture(tmp_path)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(IndentationError):
        harden(tmp_path)

    assert path.read_text(encoding="utf-8") == before


def test_api_scope_hardening_stops_on_source_drift(tmp_path):
    path = write_fixture(tmp_path)
    path.write_text("upstream changed the API implementation\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected Hermes API source"):
        harden(tmp_path, compile_file=False)

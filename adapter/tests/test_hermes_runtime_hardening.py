from pathlib import Path

import pytest

from deploy.harden_hermes_logging import REPLACEMENTS, harden


def write_fixture(root: Path) -> None:
    grouped: dict[str, list[str]] = {}
    for replacement in REPLACEMENTS:
        grouped.setdefault(replacement.relative_path, []).extend(
            [replacement.old] * replacement.expected_count
        )
    for relative_path, snippets in grouped.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(snippets), encoding="utf-8")


def test_hardening_is_complete_and_idempotent(tmp_path):
    write_fixture(tmp_path)

    first = harden(tmp_path, compile_files=False)
    second = harden(tmp_path, compile_files=False)

    assert first == second
    for replacement in REPLACEMENTS:
        text = (tmp_path / replacement.relative_path).read_text(
            encoding="utf-8"
        )
        assert replacement.old not in text
        assert text.count(replacement.new) == replacement.expected_count

    redact_source = (tmp_path / "agent/redact.py").read_text(
        encoding="utf-8"
    )
    assert "[REDACTED_SECRET]" in redact_source
    assert "[REDACTED_URL]" in redact_source


def test_hardening_stops_on_upstream_source_drift(tmp_path):
    write_fixture(tmp_path)
    target = tmp_path / REPLACEMENTS[0].relative_path
    target.write_text("upstream changed this logging call\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected Hermes source"):
        harden(tmp_path, compile_files=False)


def test_endpoint_replacements_match_the_runtime_unicode_character():
    endpoint_replacements = [
        replacement
        for replacement in REPLACEMENTS
        if replacement.relative_path == "agent/conversation_loop.py"
    ]

    assert endpoint_replacements
    assert all(
        "\U0001f310" in replacement.old
        for replacement in endpoint_replacements
    )
    assert all(
        "\\U0001f310" not in replacement.old
        for replacement in endpoint_replacements
    )


def test_tool_failure_logs_do_not_include_results_or_exception_text(tmp_path):
    write_fixture(tmp_path)
    harden(tmp_path, compile_files=False)

    source = (tmp_path / "agent/tool_executor.py").read_text(
        encoding="utf-8"
    )
    assert "output omitted from logs" in source
    assert "details omitted" in source
    assert "result[:200]" not in source
    assert "tool_error, exc_info=True" not in source

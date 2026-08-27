import json
import urllib.parse
from pathlib import Path

import pytest

from deploy.harden_hermes_run_evidence import (
    CALLBACK_OLD,
    CODE_RESULT_OLD,
    EVIDENCE_HELPERS,
    INDENTED_CODE_RESULT_OLD,
    REPLACEMENTS,
    WEB_EXTRACT_FAILURE_OLD,
    harden,
)


def write_fixture(root: Path) -> None:
    api_path = root / "gateway/platforms/api_server.py"
    api_path.parent.mkdir(parents=True, exist_ok=True)
    api_path.write_text(
        (
            "import json\n"
            "import uuid\n"
            "from pathlib import Path\n"
            "from typing import Any, Dict\n"
            "\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "\n"
            "def _hermes_version() -> str:\n"
            "    return 'fixture'\n"
            "\n"
            "class Fixture:\n"
            "    def callback(self):\n"
            + CALLBACK_OLD
        ),
        encoding="utf-8",
    )
    code_path = root / "tools/code_execution_tool.py"
    code_path.parent.mkdir(parents=True, exist_ok=True)
    code_path.write_text(
        CODE_RESULT_OLD + "\n" + INDENTED_CODE_RESULT_OLD,
        encoding="utf-8",
    )
    display_path = root / "agent/display.py"
    display_path.parent.mkdir(parents=True, exist_ok=True)
    display_path.write_text(
        (
            "import json\n"
            "def safe_json_loads(value):\n"
            "    return json.loads(value)\n"
            "def detect(tool_name, result):\n"
            + WEB_EXTRACT_FAILURE_OLD
            + "    return False, ''\n"
        ),
        encoding="utf-8",
    )


def helper() -> object:
    namespace = {
        "Any": object,
        "Dict": dict,
        "json": json,
        "urllib": urllib,
        "redact_sensitive_text": (
            lambda value, force=False: (
                "[REDACTED]" if "secret-token" in value else value
            )
        ),
    }
    exec(EVIDENCE_HELPERS, namespace)
    return namespace["_hermes_safe_tool_evidence"]


def test_hardening_is_complete_idempotent_and_removes_tool_preview(tmp_path):
    write_fixture(tmp_path)

    first = harden(tmp_path, compile_files=False)
    second = harden(tmp_path, compile_files=False)

    assert first == second
    for replacement in REPLACEMENTS:
        source = (tmp_path / replacement.relative_path).read_text(
            encoding="utf-8"
        )
        assert replacement.old not in source
        assert source.count(replacement.new) == replacement.expected_count
    api_source = (
        tmp_path / "gateway/platforms/api_server.py"
    ).read_text(encoding="utf-8")
    assert '"preview": preview' not in api_source
    assert '"event": "tool.failed" if failed else "tool.completed"' in api_source
    assert "kwargs.get(\"result\")" in api_source
    assert api_source.count('"exit_code": exit_code') == 1

    code_source = (
        tmp_path / "tools/code_execution_tool.py"
    ).read_text(encoding="utf-8")
    assert code_source.count('"exit_code": exit_code') == 2


def test_hardening_stops_on_upstream_source_drift(tmp_path):
    write_fixture(tmp_path)
    target = tmp_path / REPLACEMENTS[2].relative_path
    target.write_text("upstream changed the run callback\n", encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match="unexpected Hermes run evidence source",
    ):
        harden(tmp_path, compile_files=False)


def test_command_evidence_requires_structured_integer_exit_code():
    evidence = helper()

    assert evidence("terminal", '{"exit_code": 0, "output": "private"}') == {
        "exit_code": 0
    }
    assert evidence("execute_code", {"exit_code": 7}) == {"exit_code": 7}
    assert evidence("terminal", "exit_code=0") == {}
    assert evidence("terminal", '{"exit_code": true}') == {}
    assert evidence("terminal", '{"output": "exit_code: 0"}') == {}


def test_research_evidence_only_uses_structured_sanitized_urls():
    evidence = helper()
    result = {
        "data": {
            "web": [
                {
                    "url": (
                        "https://user:password@example.com/path"
                        "?token=private#fragment"
                    ),
                    "description": "https://ignored.example/private",
                },
                {"href": "http://second.example/article"},
                {"url": "https://example.com/secret-token"},
            ]
        }
    }

    assert evidence("web_search", json.dumps(result)) == {
        "source": (
            "http://second.example/article,"
            "https://example.com/path"
        )
    }
    assert evidence(
        "web_search",
        '{"content":"https://ignored.example/not-a-source"}',
    ) == {}
    assert evidence("terminal", json.dumps(result)) == {}


def test_extract_evidence_records_only_pages_with_successful_content():
    evidence = helper()
    result = {
        "results": [
            {
                "url": "https://good.example/article?tracking=1",
                "content": "verified page text " * 12,
                "error": None,
            },
            {
                "url": "https://failed.example/article",
                "content": "",
                "error": "timeout",
            },
            {
                "url": "https://empty.example/article",
                "content": "",
                "error": None,
            },
            {
                "url": "https://thin.example/article",
                "content": "navigation only",
                "error": None,
            },
        ]
    }

    assert evidence("web_extract", json.dumps(result)) == {
        "source": "https://good.example/article"
    }


def test_web_extract_failure_detection_accepts_partial_evidence(tmp_path):
    write_fixture(tmp_path)
    harden(tmp_path, compile_files=False)
    namespace = {}
    exec(
        (tmp_path / "agent/display.py").read_text(encoding="utf-8"),
        namespace,
    )
    detect = namespace["detect"]

    partial = {
        "results": [
            {"url": "https://failed.example", "content": "", "error": "timeout"},
            {
                "url": "https://good.example",
                "content": "verified weather evidence " * 8,
                "error": None,
            },
        ]
    }
    assert detect("web_extract", json.dumps(partial)) == (False, "")


def test_web_extract_failure_detection_rejects_thin_or_failed_batches(tmp_path):
    write_fixture(tmp_path)
    harden(tmp_path, compile_files=False)
    namespace = {}
    exec(
        (tmp_path / "agent/display.py").read_text(encoding="utf-8"),
        namespace,
    )
    detect = namespace["detect"]

    thin = {"results": [{"content": "navigation only", "error": None}]}
    failed = {"results": [{"content": "", "error": "timeout"}]}
    assert detect("web_extract", json.dumps(thin)) == (
        True,
        " [no extractable content]",
    )
    assert detect("web_extract", json.dumps(failed)) == (
        True,
        " [no extractable content]",
    )


def test_browser_evidence_is_categorical_and_excludes_page_content():
    evidence = helper()
    result = {
        "success": True,
        "url": "https://user:pass@example.com/page?q=private#fragment",
        "snapshot": "private page text",
        "typed": "private input",
    }

    normalized = evidence("browser_snapshot", json.dumps(result))

    assert normalized == {
        "summary": "snapshot_captured",
        "source": "https://example.com/page",
    }
    assert "private" not in json.dumps(normalized)
    assert evidence(
        "browser_click",
        '{"success": false, "error": "private failure"}',
    ) == {}
    assert evidence("fake_browser", json.dumps(result)) == {}

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Replacement:
    relative_path: str
    old: str
    new: str
    expected_count: int = 1


EVIDENCE_HELPERS = r'''_HERMES_COMMAND_EVIDENCE_TOOLS = frozenset({
    "terminal",
    "execute_code",
})
_HERMES_RESEARCH_EVIDENCE_TOOLS = frozenset({
    "web_search",
    "web_extract",
    "x_search",
})
_HERMES_BROWSER_EVIDENCE_SUMMARIES = {
    "browser_navigate": "navigation_completed",
    "browser_snapshot": "snapshot_captured",
    "browser_click": "click_completed",
    "browser_type": "typing_completed",
    "browser_scroll": "scroll_completed",
    "browser_back": "history_navigation_completed",
    "browser_press": "key_press_completed",
    "browser_get_images": "image_inventory_completed",
    "browser_vision": "visual_inspection_completed",
    "browser_console": "console_action_completed",
    "browser_cdp": "cdp_action_completed",
    "browser_dialog": "dialog_action_completed",
}


def _hermes_structured_tool_result(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if (
        not stripped
        or len(stripped) > 2_000_000
        or stripped[0] not in "[{"
    ):
        return None
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _hermes_safe_source_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw or len(raw) > 8192:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if scheme not in {"http", "https"} or not hostname:
        return ""
    if any(character.isspace() for character in hostname):
        return ""
    path = parsed.path or ""
    if redact_sensitive_text(path, force=True) != path:
        return ""
    host = hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    return urllib.parse.urlunsplit((scheme, host, path, "", ""))


def _hermes_research_sources(value: Any) -> list[str]:
    pending = [value]
    sources: list[str] = []
    seen: set[str] = set()
    visited = 0
    while pending and visited < 2000 and len(sources) < 10:
        current = pending.pop()
        visited += 1
        if isinstance(current, dict):
            for key, child in current.items():
                normalized_key = str(key).strip().lower()
                if normalized_key in {
                    "url",
                    "href",
                    "source_url",
                }:
                    safe = _hermes_safe_source_url(child)
                    if safe and safe not in seen:
                        seen.add(safe)
                        sources.append(safe)
                elif normalized_key in {"urls", "sources"} and isinstance(
                    child, list
                ):
                    for item in child:
                        safe = _hermes_safe_source_url(item)
                        if safe and safe not in seen:
                            seen.add(safe)
                            sources.append(safe)
                if isinstance(child, (dict, list)):
                    pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)
    return sources[:10]


def _hermes_join_sources(values: list[str], limit: int = 500) -> str:
    selected: list[str] = []
    length = 0
    for value in values:
        additional = len(value) + (1 if selected else 0)
        if length + additional > limit:
            break
        selected.append(value)
        length += additional
    return ",".join(selected)


def _hermes_safe_tool_evidence(tool_name: Any, result: Any) -> Dict[str, Any]:
    normalized_tool = str(tool_name or "").strip().lower()
    structured = _hermes_structured_tool_result(result)
    if normalized_tool in _HERMES_COMMAND_EVIDENCE_TOOLS:
        if not isinstance(structured, dict):
            return {}
        exit_code = structured.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            return {}
        return {"exit_code": exit_code}

    if normalized_tool in _HERMES_RESEARCH_EVIDENCE_TOOLS:
        sources = _hermes_research_sources(structured)
        source = _hermes_join_sources(sources)
        return {"source": source} if source else {}

    summary = _HERMES_BROWSER_EVIDENCE_SUMMARIES.get(normalized_tool)
    if summary is None or not isinstance(structured, dict):
        return {}
    if structured.get("success") is not True:
        return {}
    evidence: Dict[str, Any] = {"summary": summary}
    source = _hermes_safe_source_url(structured.get("url"))
    if source:
        evidence["source"] = source
    return evidence


'''

CALLBACK_OLD = '''        def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
            ts = time.time()
            if event_type == "tool.started":
                _push({
                    "event": "tool.started",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "preview": preview,
                })
            elif event_type == "tool.completed":
                _push({
                    "event": "tool.completed",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "duration": round(kwargs.get("duration", 0), 3),
                    "error": kwargs.get("is_error", False),
                })
            elif event_type == "reasoning.available":
                _push({
                    "event": "reasoning.available",
                    "run_id": run_id,
                    "timestamp": ts,
                    "text": preview or "",
                })
            # _thinking and subagent_progress are intentionally not forwarded
'''

CALLBACK_NEW = '''        def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
            ts = time.time()
            if event_type == "tool.started":
                _push({
                    "event": "tool.started",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                })
            elif event_type in {"tool.completed", "tool.failed"}:
                failed = (
                    event_type == "tool.failed"
                    or bool(kwargs.get("is_error", False))
                )
                try:
                    duration = round(float(kwargs.get("duration", 0)), 3)
                except (TypeError, ValueError):
                    duration = 0.0
                event = {
                    "event": "tool.failed" if failed else "tool.completed",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "duration": duration,
                    "error": failed,
                }
                event.update(
                    _hermes_safe_tool_evidence(
                        tool_name,
                        kwargs.get("result"),
                    )
                )
                if failed and "summary" not in event:
                    event["summary"] = "tool_execution_failed"
                _push(event)
            elif event_type == "reasoning.available":
                _push({
                    "event": "reasoning.available",
                    "run_id": run_id,
                    "timestamp": ts,
                    "text": preview or "",
                })
            # _thinking and subagent_progress are intentionally not forwarded
'''

CODE_RESULT_OLD = '''    result: Dict[str, Any] = {
        "status": status,
        "output": stdout_text,
        "tool_calls_made": tool_call_counter[0],
        "duration_seconds": duration,
    }
'''

CODE_RESULT_NEW = '''    result: Dict[str, Any] = {
        "status": status,
        "output": stdout_text,
        "exit_code": exit_code,
        "tool_calls_made": tool_call_counter[0],
        "duration_seconds": duration,
    }
'''

INDENTED_CODE_RESULT_OLD = '''        result: Dict[str, Any] = {
            "status": status,
            "output": stdout_text,
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }
'''

INDENTED_CODE_RESULT_NEW = '''        result: Dict[str, Any] = {
            "status": status,
            "output": stdout_text,
            "exit_code": exit_code,
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }
'''

REPLACEMENTS = (
    Replacement(
        "gateway/platforms/api_server.py",
        "import uuid\nfrom pathlib import Path\n",
        "import uuid\nimport urllib.parse\nfrom pathlib import Path\n",
    ),
    Replacement(
        "gateway/platforms/api_server.py",
        (
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "\n"
            "def _hermes_version() -> str:\n"
        ),
        (
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "\n"
            + EVIDENCE_HELPERS
            + "def _hermes_version() -> str:\n"
        ),
    ),
    Replacement(
        "gateway/platforms/api_server.py",
        CALLBACK_OLD,
        CALLBACK_NEW,
    ),
    Replacement(
        "tools/code_execution_tool.py",
        CODE_RESULT_OLD,
        CODE_RESULT_NEW,
    ),
    Replacement(
        "tools/code_execution_tool.py",
        INDENTED_CODE_RESULT_OLD,
        INDENTED_CODE_RESULT_NEW,
    ),
)


def apply_replacement(root: Path, replacement: Replacement) -> Path:
    path = root / replacement.relative_path
    text = path.read_text(encoding="utf-8")
    old_count = text.count(replacement.old)
    new_count = text.count(replacement.new)
    if old_count == replacement.expected_count:
        path.write_text(
            text.replace(replacement.old, replacement.new),
            encoding="utf-8",
        )
        return path
    if old_count == 0 and new_count == replacement.expected_count:
        return path
    raise RuntimeError(
        f"unexpected Hermes run evidence source at {path}: "
        f"old={old_count}, new={new_count}, "
        f"expected={replacement.expected_count}"
    )


def harden(root: Path, *, compile_files: bool = True) -> list[Path]:
    changed_paths = {
        apply_replacement(root, replacement) for replacement in REPLACEMENTS
    }
    paths = sorted(changed_paths)
    if compile_files:
        for path in paths:
            compile(
                path.read_text(encoding="utf-8"),
                str(path),
                "exec",
            )
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add privacy-preserving run evidence to Hermes SSE."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/opt/hermes-runtime"),
    )
    args = parser.parse_args()
    for path in harden(args.root.resolve(strict=True)):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

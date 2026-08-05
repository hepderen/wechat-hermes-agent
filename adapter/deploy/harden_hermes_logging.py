from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Replacement:
    relative_path: str
    old: str
    new: str
    expected_count: int


REPLACEMENTS = (
    Replacement(
        "agent/tool_executor.py",
        (
            'logger.error("_invoke_tool raised for %s: %s", '
            "function_name, tool_error, exc_info=True)"
        ),
        (
            'logger.error("_invoke_tool raised for %s (%s); '
            'details omitted", function_name, type(tool_error).__name__)'
        ),
        1,
    ),
    Replacement(
        "agent/tool_executor.py",
        (
            'logger.info("tool %s failed (%.2fs): %s", '
            "function_name, duration, result[:200])"
        ),
        (
            'logger.info("tool %s failed (%.2fs); output omitted from '
            'logs", function_name, duration)'
        ),
        1,
    ),
    Replacement(
        "agent/tool_executor.py",
        (
            'logger.error("context_engine.handle_tool_call raised for '
            '%s: %s", function_name, tool_error, exc_info=True)'
        ),
        (
            'logger.error("context_engine.handle_tool_call raised for '
            '%s (%s); details omitted", function_name, '
            "type(tool_error).__name__)"
        ),
        1,
    ),
    Replacement(
        "agent/tool_executor.py",
        (
            'logger.error("memory_manager.handle_tool_call raised for '
            '%s: %s", function_name, tool_error, exc_info=True)'
        ),
        (
            'logger.error("memory_manager.handle_tool_call raised for '
            '%s (%s); details omitted", function_name, '
            "type(tool_error).__name__)"
        ),
        1,
    ),
    Replacement(
        "agent/tool_executor.py",
        (
            'logger.error("handle_function_call raised for %s: %s", '
            "function_name, tool_error, exc_info=True)"
        ),
        (
            'logger.error("handle_function_call raised for %s (%s); '
            'details omitted", function_name, type(tool_error).__name__)'
        ),
        2,
    ),
    Replacement(
        "agent/tool_executor.py",
        (
            'logger.warning("Tool %s returned error (%.2fs): %s", '
            "function_name, tool_duration, result_preview)"
        ),
        (
            'logger.warning("Tool %s returned error (%.2fs); '
            'output omitted from logs", function_name, tool_duration)'
        ),
        2,
    ),
    Replacement(
        "run_agent.py",
        """        provider = getattr(self, "provider", "unknown")
        base_url = getattr(self, "base_url", "unknown")
        model = getattr(self, "model", "unknown")
        return (
            f"thread={self._thread_identity()} provider={provider} "
            f"base_url={base_url} model={model}"
        )
""",
        """        provider = getattr(self, "provider", "unknown")
        model = getattr(self, "model", "unknown")
        return (
            f"thread={self._thread_identity()} provider={provider} "
            f"model={model}"
        )
""",
        1,
    ),
    Replacement(
        "agent/conversation_loop.py",
        (
            'agent._buffer_vprint(f"   \U0001f310 Endpoint: {_base}")'
        ),
        (
            'agent._buffer_vprint(f"   \U0001f310 Endpoint: [redacted]")'
        ),
        1,
    ),
    Replacement(
        "agent/conversation_loop.py",
        (
            'agent._vprint(f"{agent.log_prefix}   \U0001f310 '
            'Endpoint: {_base}", force=True)'
        ),
        (
            'agent._vprint(f"{agent.log_prefix}   \U0001f310 '
            'Endpoint: [redacted]", force=True)'
        ),
        1,
    ),
    Replacement(
        "agent/redact.py",
        "        return redact_sensitive_text(original)\n",
        """        redacted = redact_sensitive_text(original, force=True)
        redacted = re.sub(
            (
                r"\\b(?:sk-|ghp_|github_pat_|xox[baprs]-|AIza|"
                r"pplx-|gsk_|xai-)[A-Za-z0-9_-]{1,12}"
                r"\\.{3}[A-Za-z0-9_-]{2,12}\\b"
            ),
            "[REDACTED_SECRET]",
            redacted,
        )
        redacted = re.sub(
            r"(?i)(base_url=)\\S+",
            r"\\1[REDACTED_URL]",
            redacted,
        )
        return re.sub(
            r"(?i)(Endpoint:\\s*)\\S+",
            r"\\1[REDACTED_URL]",
            redacted,
        )
""",
        1,
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
        f"unexpected Hermes source at {path}: "
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
        description="Apply idempotent Hermes production log redaction."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/opt/hermes-runtime"),
    )
    args = parser.parse_args()
    paths = harden(args.root.resolve(strict=True))
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Replacement:
    old: str
    new: str
    expected_count: int = 1


RELATIVE_PATH = "gateway/platforms/api_server.py"
REPLACEMENTS = (
    Replacement(
        """        gateway_session_key: Optional[str] = None,
        route: Optional[Dict[str, Any]] = None,
    ) -> Any:
""",
        """        gateway_session_key: Optional[str] = None,
        route: Optional[Dict[str, Any]] = None,
        enabled_toolsets_override: Optional[List[str]] = None,
    ) -> Any:
""",
    ),
    Replacement(
        """        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))

        max_iterations = _current_max_iterations()
""",
        """        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))
        if enabled_toolsets_override is not None:
            enabled_toolsets = list(enabled_toolsets_override)

        max_iterations = _current_max_iterations()
""",
    ),
    Replacement(
        """        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)
        history = self._conversation_history_for_session(session_id)
""",
        """        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)
        disable_tools = body.get("disable_tools", False)
        if not isinstance(disable_tools, bool):
            return web.json_response(_openai_error("disable_tools must be a boolean", code="invalid_disable_tools"), status=400)
        history = self._conversation_history_for_session(session_id)
""",
    ),
    Replacement(
        """        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)

        loop = asyncio.get_running_loop()
""",
        """        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)
        disable_tools = body.get("disable_tools", False)
        if not isinstance(disable_tools, bool):
            return web.json_response(_openai_error("disable_tools must be a boolean", code="invalid_disable_tools"), status=400)

        loop = asyncio.get_running_loop()
""",
    ),
    Replacement(
        """            ephemeral_system_prompt=system_prompt,
            session_id=session_id,
            gateway_session_key=gateway_session_key,
        )
""",
        """            ephemeral_system_prompt=system_prompt,
            session_id=session_id,
            gateway_session_key=gateway_session_key,
            enabled_toolsets_override=[] if disable_tools else None,
        )
""",
    ),
    Replacement(
        """                    tool_progress_callback=_tool_progress,
                    gateway_session_key=gateway_session_key,
                )
""",
        """                    tool_progress_callback=_tool_progress,
                    gateway_session_key=gateway_session_key,
                    enabled_toolsets_override=[] if disable_tools else None,
                )
""",
    ),
    Replacement(
        """        agent_ref: Optional[list] = None,
        gateway_session_key: Optional[str] = None,
        route: Optional[Dict[str, Any]] = None,
    ) -> tuple:
""",
        """        agent_ref: Optional[list] = None,
        gateway_session_key: Optional[str] = None,
        route: Optional[Dict[str, Any]] = None,
        enabled_toolsets_override: Optional[List[str]] = None,
    ) -> tuple:
""",
    ),
    Replacement(
        """                    tool_complete_callback=tool_complete_callback,
                    gateway_session_key=gateway_session_key,
                    route=route,
                )
""",
        """                    tool_complete_callback=tool_complete_callback,
                    gateway_session_key=gateway_session_key,
                    route=route,
                    enabled_toolsets_override=enabled_toolsets_override,
                )
""",
    ),
    Replacement(
        """        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_input = body.get("input")
""",
        """        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        enabled_toolsets = body.get("enabled_toolsets")
        if enabled_toolsets is not None:
            if (
                not isinstance(enabled_toolsets, list)
                or len(enabled_toolsets) > 32
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in enabled_toolsets
                )
            ):
                return web.json_response(
                    _openai_error(
                        "enabled_toolsets must be an array of non-empty strings",
                        code="invalid_enabled_toolsets",
                    ),
                    status=400,
                )
            enabled_toolsets = list(dict.fromkeys(
                item.strip() for item in enabled_toolsets
            ))
            from gateway.run import _load_gateway_config
            from hermes_cli.tools_config import _get_platform_tools
            configured_toolsets = set(_get_platform_tools(
                _load_gateway_config(), "api_server"
            ))
            unavailable_toolsets = sorted(
                set(enabled_toolsets) - configured_toolsets
            )
            if unavailable_toolsets:
                return web.json_response(
                    _openai_error(
                        "enabled_toolsets contains unavailable toolsets: "
                        + ",".join(unavailable_toolsets),
                        code="unavailable_toolsets",
                    ),
                    status=400,
                )

        raw_input = body.get("input")
""",
    ),
    Replacement(
        """                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_text_cb,
                    tool_progress_callback=event_cb,
                    gateway_session_key=gateway_session_key,
                    route=route,
                )
""",
        """                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_text_cb,
                    tool_progress_callback=event_cb,
                    gateway_session_key=gateway_session_key,
                    route=route,
                    enabled_toolsets_override=enabled_toolsets,
                )
""",
    ),
)


def _apply_replacement(text: str, replacement: Replacement, path: Path) -> str:
    old_count = text.count(replacement.old)
    new_count = text.count(replacement.new)
    if old_count == replacement.expected_count:
        return text.replace(replacement.old, replacement.new)
    if old_count == 0 and new_count == replacement.expected_count:
        return text
    raise RuntimeError(
        f"unexpected Hermes API source at {path}: "
        f"old={old_count}, new={new_count}, "
        f"expected={replacement.expected_count}"
    )


def apply_replacement(path: Path, replacement: Replacement) -> None:
    """Apply one replacement for callers that need the primitive directly."""
    source = path.read_text(encoding="utf-8")
    path.write_text(
        _apply_replacement(source, replacement, path),
        encoding="utf-8",
    )


def harden(root: Path, *, compile_file: bool = True) -> Path:
    path = root / RELATIVE_PATH
    source = path.read_text(encoding="utf-8")
    updated = source
    for replacement in REPLACEMENTS:
        updated = _apply_replacement(updated, replacement, path)
    if compile_file:
        compile(updated, str(path), "exec")
    if updated != source:
        path.write_text(updated, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add request-scoped tool isolation to Hermes API calls."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/opt/hermes-runtime"),
    )
    args = parser.parse_args()
    print(harden(args.root.resolve(strict=True)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

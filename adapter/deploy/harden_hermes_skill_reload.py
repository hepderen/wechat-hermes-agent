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
HANDLER = '''    async def _handle_skills_reload(
        self,
        request: "web.Request",
    ) -> "web.Response":
        """Reload the exact trusted Skill release selected by the Adapter."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        if self._inflight_agent_runs or any(
            not task.done() for task in self._active_run_tasks.values()
        ):
            return web.json_response(
                _openai_error(
                    "Skill reload requires an idle Hermes runtime",
                    code="skills_reload_busy",
                ),
                status=409,
            )

        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                _openai_error(
                    "Invalid JSON in request body",
                    code="invalid_request_error",
                ),
                status=400,
            )
        expected_root = body.get("expected_skills_root")
        if (
            not isinstance(expected_root, str)
            or not expected_root.startswith("/")
            or len(expected_root) > 4096
            or "\\x00" in expected_root
        ):
            return web.json_response(
                _openai_error(
                    "expected_skills_root must be an absolute path",
                    code="invalid_skills_root",
                ),
                status=400,
            )

        try:
            from agent.prompt_builder import clear_skills_system_prompt_cache
            from agent.skill_bundles import reload_bundles
            from agent.skill_commands import reload_skills
            from tools.skills_tool import (
                _SKILLS_CACHE,
                _find_all_skills,
                _skills_dir,
                _sort_skills,
            )

            actual_root = str(_skills_dir().resolve(strict=True))
            if actual_root != expected_root:
                return web.json_response(
                    _openai_error(
                        "Hermes Skill root does not match the activated release",
                        code="skills_root_mismatch",
                    ),
                    status=409,
                )

            reloaded = self._hermes_skill_runtime_root != actual_root
            if reloaded:
                clear_skills_system_prompt_cache(clear_snapshot=True)
                _SKILLS_CACHE.clear()
                reload_skills()
                reload_bundles()
                self._hermes_skill_runtime_root = actual_root
            skills = _sort_skills(_find_all_skills(skip_disabled=False))
        except Exception:
            logger.exception("POST /v1/skills/reload failed")
            return web.json_response(
                _openai_error(
                    "Failed to reload Skills",
                    err_type="server_error",
                    code="skills_reload_failed",
                ),
                status=500,
            )

        return web.json_response({
            "object": "hermes.skills.reload",
            "skills_root": actual_root,
            "reloaded": reloaded,
            "count": len(skills),
        })

'''

REPLACEMENTS = (
    Replacement(
        """        self._inflight_agent_runs: int = 0

    def _readiness_work_counts(self) -> tuple[int, int, int]:
""",
        """        self._inflight_agent_runs: int = 0
        self._hermes_skill_runtime_root: str = ""

    def _readiness_work_counts(self) -> tuple[int, int, int]:
""",
    ),
    Replacement(
        """    async def _handle_toolsets(self, request: "web.Request") -> "web.Response":
""",
        HANDLER
        + """    async def _handle_toolsets(self, request: "web.Request") -> "web.Response":
""",
    ),
    Replacement(
        """            self._app.router.add_get("/v1/skills", self._handle_skills)
            self._app.router.add_get("/v1/toolsets", self._handle_toolsets)
""",
        """            self._app.router.add_get("/v1/skills", self._handle_skills)
            self._app.router.add_post("/v1/skills/reload", self._handle_skills_reload)
            self._app.router.add_get("/v1/toolsets", self._handle_toolsets)
""",
    ),
)


def apply_replacement(path: Path, replacement: Replacement) -> None:
    text = path.read_text(encoding="utf-8")
    old_count = text.count(replacement.old)
    new_count = text.count(replacement.new)
    embedded_old_count = (
        new_count * replacement.new.count(replacement.old)
    )
    if (
        new_count == replacement.expected_count
        and old_count == embedded_old_count
    ):
        return
    if old_count == replacement.expected_count:
        path.write_text(
            text.replace(replacement.old, replacement.new),
            encoding="utf-8",
        )
        return
    raise RuntimeError(
        f"unexpected Hermes Skill API source at {path}: "
        f"old={old_count}, new={new_count}, "
        f"expected={replacement.expected_count}"
    )


def harden(root: Path, *, compile_file: bool = True) -> Path:
    path = root / RELATIVE_PATH
    for replacement in REPLACEMENTS:
        apply_replacement(path, replacement)
    if compile_file:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add authenticated trusted Skill reload to Hermes."
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

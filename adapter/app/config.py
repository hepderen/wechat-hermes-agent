from __future__ import annotations

import os
from math import isfinite
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    bridge_token: str
    internal_token: str
    hermes_base_url: str
    hermes_api_key: str
    chat_api_url: str
    allowed_room_ids: frozenset[str]
    bot_wxid: str
    database_path: Path
    artifact_root: Path
    artifact_public_base_url: str
    max_artifact_bytes: int
    max_image_bytes: int
    max_task_seconds: int
    max_task_attempts: int
    daily_cost_limit_usd: float
    daily_token_limit: int
    budget_timezone: str
    input_token_cost_per_million: float
    output_token_cost_per_million: float
    wechat_session_generation: str
    hermes_cli_path: Path
    hermes_home: Path
    skill_install_timeout_seconds: int
    allow_private_chat: bool
    worker_poll_seconds: float
    sync_chat_timeout_seconds: float = 8.0
    max_tool_calls: int = 80
    max_artifact_count: int = 10
    max_artifact_total_bytes: int = 500 * 1024 * 1024
    max_download_bytes: int = 1024 * 1024 * 1024
    max_delivery_media_items: int = 3
    artifact_retention_days: int = 7
    audit_retention_days: int = 30
    chat_api_token: str = ""
    cleanup_status_path: Path = Path(
        "/var/lib/wechat-hermes/adapter-data/cleanup-status.json"
    )
    cleanup_max_age_seconds: int = 172800

    def validate_startup(self) -> None:
        credentials = {
            "BRIDGE_TOKEN": self.bridge_token,
            "HERMES_WECHAT_INTERNAL_TOKEN": self.internal_token,
            "WECHAT_CHAT_API_TOKEN": self.chat_api_token,
            "HERMES_API_KEY": self.hermes_api_key,
        }
        for name, value in credentials.items():
            if not str(value or "").strip():
                raise ValueError("%s must be configured" % name)
        credential_values = [str(value).strip() for value in credentials.values()]
        if len(set(credential_values)) != len(credential_values):
            raise ValueError("production credentials must be pairwise distinct")

        for name, value in (
            ("HERMES_BASE_URL", self.hermes_base_url),
            ("WECHAT_CHAT_API_URL", self.chat_api_url),
            (
                "HERMES_WECHAT_ARTIFACT_BASE_URL",
                self.artifact_public_base_url,
            ),
        ):
            parsed = urlparse(str(value or ""))
            if (
                parsed.scheme != "http"
                or parsed.hostname
                not in {"127.0.0.1", "::1", "localhost"}
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("%s must be an HTTP loopback URL" % name)

        if not self.allowed_room_ids:
            raise ValueError("ALLOWED_WECHAT_ROOM_IDS must not be empty")
        if not self.bot_wxid.strip():
            raise ValueError("WECHAT_BOT_WXID must be configured")
        try:
            ZoneInfo(self.budget_timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError(
                "HERMES_WECHAT_BUDGET_TIMEZONE is invalid"
            ) from exc

        positive_limits = {
            "max_artifact_bytes": self.max_artifact_bytes,
            "max_image_bytes": self.max_image_bytes,
            "max_task_seconds": self.max_task_seconds,
            "max_task_attempts": self.max_task_attempts,
            "daily_cost_limit_usd": self.daily_cost_limit_usd,
            "daily_token_limit": self.daily_token_limit,
            "worker_poll_seconds": self.worker_poll_seconds,
            "sync_chat_timeout_seconds": self.sync_chat_timeout_seconds,
            "max_tool_calls": self.max_tool_calls,
            "max_artifact_count": self.max_artifact_count,
            "max_artifact_total_bytes": self.max_artifact_total_bytes,
            "max_download_bytes": self.max_download_bytes,
            "max_delivery_media_items": self.max_delivery_media_items,
            "artifact_retention_days": self.artifact_retention_days,
            "audit_retention_days": self.audit_retention_days,
            "cleanup_max_age_seconds": self.cleanup_max_age_seconds,
            "skill_install_timeout_seconds": (
                self.skill_install_timeout_seconds
            ),
        }
        for name, value in positive_limits.items():
            if not isfinite(float(value)) or float(value) <= 0:
                raise ValueError("%s must be positive" % name)
        for name, value in (
            (
                "input_token_cost_per_million",
                self.input_token_cost_per_million,
            ),
            (
                "output_token_cost_per_million",
                self.output_token_cost_per_million,
            ),
        ):
            if not isfinite(float(value)) or float(value) < 0:
                raise ValueError("%s must not be negative" % name)

        database = Path(self.database_path).expanduser()
        artifacts = Path(self.artifact_root).expanduser()
        cleanup_status = Path(self.cleanup_status_path).expanduser()
        if not database.is_absolute():
            raise ValueError("HERMES_WECHAT_DB_PATH must be absolute")
        if not artifacts.is_absolute():
            raise ValueError(
                "HERMES_WECHAT_ARTIFACT_ROOT must be absolute"
            )
        if not cleanup_status.is_absolute():
            raise ValueError(
                "HERMES_WECHAT_CLEANUP_STATUS_PATH must be absolute"
            )
        database = database.resolve(strict=False)
        artifacts = artifacts.resolve(strict=False)
        if database.exists() and database.is_dir():
            raise ValueError("HERMES_WECHAT_DB_PATH must be a file")
        if artifacts.exists() and not artifacts.is_dir():
            raise ValueError(
                "HERMES_WECHAT_ARTIFACT_ROOT must be a directory"
            )
        if database == artifacts or artifacts in database.parents:
            raise ValueError(
                "adapter database must be outside the artifact directory"
            )

    @classmethod
    def from_env(cls) -> "Settings":
        rooms = frozenset(
            value.strip()
            for value in os.getenv("ALLOWED_WECHAT_ROOM_IDS", "").split(",")
            if value.strip()
        )
        return cls(
            bridge_token=os.getenv("BRIDGE_TOKEN", ""),
            internal_token=os.getenv("HERMES_WECHAT_INTERNAL_TOKEN", ""),
            hermes_base_url=os.getenv(
                "HERMES_BASE_URL", "http://127.0.0.1:8642"
            ).rstrip("/"),
            hermes_api_key=os.getenv("HERMES_API_KEY", ""),
            chat_api_url=os.getenv(
                "WECHAT_CHAT_API_URL", "http://127.0.0.1:8765"
            ).rstrip("/"),
            allowed_room_ids=rooms,
            bot_wxid=os.getenv("WECHAT_BOT_WXID", ""),
            database_path=Path(
                os.getenv(
                    "HERMES_WECHAT_DB_PATH",
                    "/var/lib/wechat-hermes/adapter-data/adapter.db",
                )
            ),
            artifact_root=Path(
                os.getenv(
                    "HERMES_WECHAT_ARTIFACT_ROOT",
                    "/var/lib/wechat-hermes/artifacts",
                )
            ),
            artifact_public_base_url=os.getenv(
                "HERMES_WECHAT_ARTIFACT_BASE_URL",
                "http://127.0.0.1:8000",
            ).rstrip("/"),
            max_artifact_bytes=int(
                os.getenv("HERMES_WECHAT_MAX_ARTIFACT_BYTES", str(1024 * 1024 * 1024))
            ),
            max_image_bytes=int(
                os.getenv("HERMES_WECHAT_MAX_IMAGE_BYTES", str(20 * 1024 * 1024))
            ),
            max_task_seconds=int(
                os.getenv("HERMES_WECHAT_MAX_TASK_SECONDS", "1800")
            ),
            max_task_attempts=max(
                1, int(os.getenv("HERMES_WECHAT_MAX_TASK_ATTEMPTS", "3"))
            ),
            daily_cost_limit_usd=float(
                os.getenv("HERMES_WECHAT_DAILY_COST_LIMIT_USD", "20")
            ),
            daily_token_limit=max(
                0, int(os.getenv("HERMES_WECHAT_DAILY_TOKEN_LIMIT", "10000000"))
            ),
            budget_timezone=os.getenv(
                "HERMES_WECHAT_BUDGET_TIMEZONE",
                "Asia/Shanghai",
            ).strip()
            or "Asia/Shanghai",
            input_token_cost_per_million=float(
                os.getenv("HERMES_INPUT_TOKEN_COST_PER_MILLION", "3")
            ),
            output_token_cost_per_million=float(
                os.getenv("HERMES_OUTPUT_TOKEN_COST_PER_MILLION", "15")
            ),
            wechat_session_generation=(
                os.getenv("HERMES_WECHAT_SESSION_GENERATION", "1").strip()
                or "1"
            ),
            hermes_cli_path=Path(
                os.getenv(
                    "HERMES_CLI_PATH",
                    "/opt/hermes-runtime/venv/bin/hermes",
                )
            ),
            hermes_home=Path(
                os.getenv(
                    "HERMES_HOME",
                    "/var/lib/wechat-hermes/workspace/home",
                )
            ),
            skill_install_timeout_seconds=max(
                30,
                int(
                    os.getenv(
                        "HERMES_WECHAT_SKILL_INSTALL_TIMEOUT_SECONDS",
                        "300",
                    )
                ),
            ),
            allow_private_chat=env_bool("ALLOW_PRIVATE_WECHAT_CHAT", False),
            worker_poll_seconds=max(
                0.2, float(os.getenv("HERMES_WECHAT_WORKER_POLL_SECONDS", "1"))
            ),
            sync_chat_timeout_seconds=max(
                1.0,
                float(os.getenv("HERMES_WECHAT_SYNC_TIMEOUT_SECONDS", "8")),
            ),
            max_tool_calls=max(
                1,
                int(os.getenv("HERMES_WECHAT_MAX_TOOL_CALLS", "80")),
            ),
            max_artifact_count=max(
                1,
                int(os.getenv("HERMES_WECHAT_MAX_ARTIFACT_COUNT", "10")),
            ),
            max_artifact_total_bytes=max(
                1,
                int(
                    os.getenv(
                        "HERMES_WECHAT_MAX_ARTIFACT_TOTAL_BYTES",
                        str(500 * 1024 * 1024),
                    )
                ),
            ),
            max_download_bytes=max(
                1,
                int(
                    os.getenv(
                        "HERMES_WECHAT_MAX_DOWNLOAD_BYTES",
                        str(1024 * 1024 * 1024),
                    )
                ),
            ),
            max_delivery_media_items=min(
                3,
                max(
                    1,
                    int(
                        os.getenv(
                            "HERMES_WECHAT_MAX_DELIVERY_MEDIA_ITEMS",
                            "3",
                        )
                    ),
                ),
            ),
            artifact_retention_days=max(
                1,
                int(os.getenv("HERMES_WECHAT_ARTIFACT_RETENTION_DAYS", "7")),
            ),
            audit_retention_days=max(
                1,
                int(os.getenv("HERMES_WECHAT_AUDIT_RETENTION_DAYS", "30")),
            ),
            chat_api_token=os.getenv("WECHAT_CHAT_API_TOKEN", ""),
            cleanup_status_path=Path(
                os.getenv(
                    "HERMES_WECHAT_CLEANUP_STATUS_PATH",
                    "/var/lib/wechat-hermes/adapter-data/cleanup-status.json",
                )
            ),
            cleanup_max_age_seconds=max(
                3600,
                int(
                    os.getenv(
                        "HERMES_WECHAT_CLEANUP_MAX_AGE_SECONDS",
                        "172800",
                    )
                ),
            ),
        )

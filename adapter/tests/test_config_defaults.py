from pathlib import Path

from app.config import Settings
from scripts.live_fake_stack import adapter_environment


def test_production_resource_defaults_match_v2_plan(monkeypatch):
    monkeypatch.delenv("HERMES_WECHAT_MAX_TASK_SECONDS", raising=False)
    monkeypatch.delenv("HERMES_WECHAT_DAILY_COST_LIMIT_USD", raising=False)
    monkeypatch.delenv("HERMES_WECHAT_CHAT_ONLY", raising=False)
    monkeypatch.delenv("HERMES_WECHAT_GROUP_LISTENER_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_IDLE_SECONDS", raising=False)
    monkeypatch.delenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_MIN_INTERACTIONS", raising=False)
    monkeypatch.delenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_MAX_PER_MEMBER_DAY", raising=False)
    monkeypatch.delenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_MAX_PER_ROOM_DAY", raising=False)
    monkeypatch.delenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_TIMEOUT_SECONDS", raising=False)

    settings = Settings.from_env()

    assert settings.max_task_seconds == 1800
    assert settings.daily_cost_limit_usd == 20
    assert settings.database_path == Path(
        "/var/lib/wechat-hermes/adapter-data/adapter.db"
    )
    assert settings.cleanup_status_path == Path(
        "/var/lib/wechat-hermes/adapter-data/cleanup-status.json"
    )
    assert settings.cleanup_max_age_seconds == 172800
    assert settings.delivery_reconcile_attempts == 5
    assert settings.delivery_reconcile_delay_seconds == 0.75
    assert settings.relationship_memory_enabled is True
    assert settings.relationship_summary_timeout_seconds == 5
    assert settings.relationship_proactive_enabled is True
    assert settings.relationship_proactive_idle_seconds == 5400
    assert settings.relationship_proactive_min_interactions == 3
    assert settings.relationship_proactive_max_per_member_day == 1
    assert settings.relationship_proactive_max_per_room_day == 2
    assert settings.relationship_proactive_timeout_seconds == 6
    assert settings.chat_only_mode is False
    assert settings.group_listener_enabled is True
    assert settings.group_listener_min_reply_gap_seconds == 12
    assert settings.group_listener_min_turns_between_replies == 2
    assert settings.group_listener_names == ("小格", "Hermes")


def test_chat_only_mode_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("HERMES_WECHAT_CHAT_ONLY", "true")
    assert Settings.from_env().chat_only_mode is True


def test_relationship_memory_settings_are_read_from_environment(monkeypatch):
    monkeypatch.setenv("HERMES_WECHAT_RELATIONSHIP_MEMORY_ENABLED", "false")
    monkeypatch.setenv("HERMES_WECHAT_RELATIONSHIP_SUMMARY_TIMEOUT_SECONDS", "3.5")
    settings = Settings.from_env()
    assert settings.relationship_memory_enabled is False
    assert settings.relationship_summary_timeout_seconds == 3.5


def test_relationship_proactive_settings_are_read_and_bounded(monkeypatch):
    monkeypatch.setenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_ENABLED", "false")
    monkeypatch.setenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_IDLE_SECONDS", "999999")
    monkeypatch.setenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_MIN_INTERACTIONS", "0")
    monkeypatch.setenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_MAX_PER_MEMBER_DAY", "99")
    monkeypatch.setenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_MAX_PER_ROOM_DAY", "99")
    monkeypatch.setenv("HERMES_WECHAT_RELATIONSHIP_PROACTIVE_TIMEOUT_SECONDS", "0")

    settings = Settings.from_env()

    assert settings.relationship_proactive_enabled is False
    assert settings.relationship_proactive_idle_seconds == 86400
    assert settings.relationship_proactive_min_interactions == 1
    assert settings.relationship_proactive_max_per_member_day == 5
    assert settings.relationship_proactive_max_per_room_day == 10
    assert settings.relationship_proactive_timeout_seconds == 1


def test_group_listener_settings_are_read_and_bounded_from_environment(monkeypatch):
    monkeypatch.setenv("HERMES_WECHAT_GROUP_LISTENER_ENABLED", "false")
    monkeypatch.setenv(
        "HERMES_WECHAT_GROUP_LISTENER_MIN_REPLY_GAP_SECONDS",
        "9999",
    )
    monkeypatch.setenv(
        "HERMES_WECHAT_GROUP_LISTENER_MIN_TURNS_BETWEEN_REPLIES",
        "999",
    )
    monkeypatch.setenv(
        "HERMES_WECHAT_GROUP_LISTENER_NAMES",
        " 小格 , 阿格 ,",
    )
    settings = Settings.from_env()
    assert settings.group_listener_enabled is False
    assert settings.group_listener_min_reply_gap_seconds == 600
    assert settings.group_listener_min_turns_between_replies == 100
    assert settings.group_listener_names == ("小格", "阿格")


def test_delivery_media_limit_is_clamped_to_three(monkeypatch):
    monkeypatch.setenv("HERMES_WECHAT_MAX_DELIVERY_MEDIA_ITEMS", "99")
    assert Settings.from_env().max_delivery_media_items == 3


def test_delivery_reconciliation_limits_are_clamped(monkeypatch):
    monkeypatch.setenv("HERMES_WECHAT_DELIVERY_RECONCILE_ATTEMPTS", "99")
    monkeypatch.setenv(
        "HERMES_WECHAT_DELIVERY_RECONCILE_DELAY_SECONDS",
        "0",
    )
    settings = Settings.from_env()
    assert settings.delivery_reconcile_attempts == 10
    assert settings.delivery_reconcile_delay_seconds == 0.05


def test_fake_stack_uses_platform_absolute_cleanup_status_path(tmp_path):
    database = tmp_path / "data" / "adapter.db"
    artifacts = tmp_path / "artifacts"
    home = tmp_path / "home"
    database.parent.mkdir()
    artifacts.mkdir()
    environment = adapter_environment(
        database,
        artifacts,
        "http://127.0.0.1:18642",
        "http://127.0.0.1:18765",
        18000,
        home,
    )

    cleanup_status = Path(
        environment["HERMES_WECHAT_CLEANUP_STATUS_PATH"]
    )
    assert cleanup_status.is_absolute()
    assert cleanup_status.parent == database.parent
    assert environment["HERMES_WECHAT_RELATIONSHIP_MEMORY_ENABLED"] == "false"

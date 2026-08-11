from pathlib import Path

from app.config import Settings
from scripts.live_fake_stack import adapter_environment


def test_production_resource_defaults_match_v2_plan(monkeypatch):
    monkeypatch.delenv("HERMES_WECHAT_MAX_TASK_SECONDS", raising=False)
    monkeypatch.delenv("HERMES_WECHAT_DAILY_COST_LIMIT_USD", raising=False)

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

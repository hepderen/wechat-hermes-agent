from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import build_runtime, create_app
from tests.test_adapter import make_runtime, make_settings


def test_build_runtime_uses_distinct_chat_api_token(tmp_path):
    settings = make_settings(tmp_path)
    runtime = build_runtime(settings)
    assert runtime.chat_api.auth_token == settings.chat_api_token
    assert runtime.chat_api.auth_token != settings.internal_token


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"bridge_token": ""}, "BRIDGE_TOKEN"),
        ({"internal_token": ""}, "HERMES_WECHAT_INTERNAL_TOKEN"),
        ({"chat_api_token": ""}, "WECHAT_CHAT_API_TOKEN"),
        ({"hermes_api_key": ""}, "HERMES_API_KEY"),
        (
            {"chat_api_token": "internal-secret"},
            "pairwise distinct",
        ),
        (
            {"bridge_token": "hermes-secret"},
            "pairwise distinct",
        ),
        (
            {"internal_token": "bridge-secret"},
            "pairwise distinct",
        ),
        (
            {"hermes_api_key": "chat-api-secret"},
            "pairwise distinct",
        ),
        (
            {"hermes_base_url": "http://10.0.0.2:8642"},
            "loopback",
        ),
        (
            {"chat_api_url": "https://127.0.0.1:8765"},
            "loopback",
        ),
        (
            {"artifact_public_base_url": "http://example.com"},
            "loopback",
        ),
        ({"allowed_room_ids": frozenset()}, "must not be empty"),
        ({"bot_wxid": ""}, "WECHAT_BOT_WXID"),
        ({"budget_timezone": "Mars/Olympus"}, "invalid"),
        ({"max_tool_calls": 0}, "positive"),
    ],
)
def test_startup_validation_fails_closed(tmp_path, changes, message):
    settings = replace(make_settings(tmp_path), **changes)
    with pytest.raises(ValueError, match=message):
        settings.validate_startup()


def test_startup_validation_rejects_unsafe_paths(tmp_path):
    settings = make_settings(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        replace(settings, database_path=settings.database_path.name).validate_startup()
    with pytest.raises(ValueError, match="outside"):
        replace(
            settings,
            database_path=settings.artifact_root / "adapter.db",
        ).validate_startup()


def test_second_adapter_stays_degraded_and_does_not_recover(tmp_path):
    first = make_runtime(tmp_path)
    second = make_runtime(tmp_path)
    with TestClient(create_app(first, start_worker=False)) as first_client:
        health = first_client.get("/health").json()
        assert health["ready"] is True
        assert health["persona"]["integrity"] is True
        assert health["persona"]["version"].startswith("sophia@1.0.0+")
        assert "wechat_hermes_persona_skill_integrity 1" in (
            first_client.get("/metrics").text
        )
        with TestClient(create_app(second, start_worker=False)) as second_client:
            health = second_client.get("/health").json()
            assert health["status"] == "degraded"
            assert health["ready"] is False
            assert health["degraded_reason"] == "process_lock_unavailable"
            assert second.store._initialized is False

    third = make_runtime(tmp_path)
    with TestClient(create_app(third, start_worker=False)) as third_client:
        assert third_client.get("/health").json()["ready"] is True


def test_failed_cleanup_status_degrades_health_and_metrics(tmp_path):
    runtime = make_runtime(
        tmp_path,
        cleanup_status_path=tmp_path / "cleanup-status.json",
    )
    runtime.settings.cleanup_status_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ok": False,
                "completed_at": time.time(),
                "errors": [
                    {"stage": "artifacts", "error_type": "PermissionError"}
                ],
            }
        ),
        encoding="utf-8",
    )

    with TestClient(create_app(runtime, start_worker=False)) as client:
        health = client.get("/health").json()
        assert health["ready"] is False
        assert health["degraded_reason"] == "cleanup_failed"
        assert health["cleanup"]["status"] == "failed"
        metrics = client.get("/metrics").text
        assert "wechat_hermes_ready 0" in metrics
        assert "wechat_hermes_cleanup_healthy 0" in metrics


def test_persona_integrity_degrades_health(monkeypatch, tmp_path):
    import app.main as main_module

    monkeypatch.setattr(main_module, "PERSONA_SKILL_INTEGRITY_OK", False)
    runtime = make_runtime(tmp_path)
    with TestClient(create_app(runtime, start_worker=False)) as client:
        health = client.get("/health").json()
        assert health["ready"] is False
        assert health["degraded_reason"] == "persona_skill_integrity"
        assert health["persona"]["integrity"] is False


def test_missing_cleanup_status_uses_bounded_startup_grace(tmp_path):
    runtime = make_runtime(
        tmp_path,
        cleanup_status_path=tmp_path / "missing-cleanup-status.json",
        cleanup_max_age_seconds=3600,
    )
    runtime.started_at = time.time() - 3601

    with TestClient(create_app(runtime, start_worker=False)) as client:
        health = client.get("/health").json()
        assert health["ready"] is False
        assert health["degraded_reason"] == "cleanup_missing"

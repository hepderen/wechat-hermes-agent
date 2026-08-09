from __future__ import annotations

import datetime as dt
import json
import os
import stat
from pathlib import Path

import httpx
import pytest
import yaml

from scripts.rotate_hermes_model import (
    RotationError,
    RotationSecret,
    apply_rotation,
    file_sha256,
    load_rotation_secret,
    model_ids_compatible,
    preflight_provider,
    redact,
    resolve_model_id,
)


TEST_KEY = "provider-test-key-0123456789abcdef"


def write_secret(path: Path, *, model: str = "5.6sol") -> None:
    path.write_text(
        json.dumps({"api_key": TEST_KEY, "model": model}),
        encoding="utf-8",
    )
    path.chmod(0o600)


def write_config(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "custom",
                    "base_url": "https://provider.example/v1",
                    "default": "old-model",
                    "api_key": "old-provider-key-0123456789",
                    "context_length": 128000,
                },
                "approvals": {"mode": "off"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_model_alias_resolves_to_unique_provider_id():
    assert resolve_model_id(
        "5.6sol",
        ["gpt-5.6", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"],
    ) == "gpt-5.6-sol"


def test_model_alias_rejects_ambiguous_provider_ids():
    with pytest.raises(RotationError, match="ambiguous"):
        resolve_model_id(
            "5.6sol",
            ["gpt-5.6-sol", "openai-5.6-sol"],
        )


def test_returned_model_accepts_versioned_id_but_rejects_other_route():
    assert model_ids_compatible("gpt-5.6-sol", "gpt-5.6-sol-2026-08-01")
    assert not model_ids_compatible("gpt-5.6-sol", "gpt-5.6-terra")


def test_rotation_secret_requires_private_regular_file(tmp_path):
    secret_path = tmp_path / "secret.json"
    write_secret(secret_path)
    loaded = load_rotation_secret(secret_path)
    assert loaded == RotationSecret(TEST_KEY, "5.6sol")

    if os.name == "posix":
        secret_path.chmod(0o640)
        with pytest.raises(RotationError, match="group or others"):
            load_rotation_secret(secret_path)


def test_rotation_secret_rejects_symlink(tmp_path):
    secret_path = tmp_path / "secret.json"
    link_path = tmp_path / "secret-link.json"
    write_secret(secret_path)
    try:
        link_path.symlink_to(secret_path)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable in this environment")
    with pytest.raises(RotationError, match="symlink"):
        load_rotation_secret(link_path)


def test_provider_preflight_resolves_alias_without_exposing_content():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + TEST_KEY
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "gpt-5.6"},
                        {"id": "gpt-5.6-sol"},
                    ]
                },
            )
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-5.6-sol"
        return httpx.Response(
            200,
            json={
                "model": "gpt-5.6-sol",
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"total_tokens": 42},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = preflight_provider(
            "https://provider.example/v1",
            RotationSecret(TEST_KEY, "5.6sol"),
            client=client,
        )

    assert result.resolved_model == "gpt-5.6-sol"
    assert result.model_listed is True
    assert result.chat_status == 200
    assert result.content_nonempty is True
    assert result.total_tokens == 42
    assert len(requests) == 2
    assert "OK" not in json.dumps(result.__dict__)


def test_provider_preflight_stops_before_chat_for_unknown_model():
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={"data": [{"id": "gpt-5.6-sol"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RotationError, match="not advertised"):
            preflight_provider(
                "https://provider.example/v1",
                RotationSecret(TEST_KEY, "missing-model"),
                client=client,
            )
    assert methods == ["GET"]


def test_provider_preflight_rejects_silent_model_reroute():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-5.6-sol"}]})
        return httpx.Response(
            200,
            json={
                "model": "gpt-5.6-terra",
                "choices": [{"message": {"content": "OK"}}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RotationError, match="another model"):
            preflight_provider(
                "https://provider.example/v1",
                RotationSecret(TEST_KEY, "gpt-5.6-sol"),
                client=client,
            )


def test_apply_rotation_backs_up_and_atomically_updates_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    backup_root = tmp_path / "backups"
    write_config(config_path)
    before = file_sha256(config_path)
    secret = RotationSecret(TEST_KEY, "5.6sol")

    result = apply_rotation(
        config_path,
        backup_root,
        secret,
        "gpt-5.6-sol",
        expected_sha256=before,
        now=dt.datetime(2026, 8, 9, 14, 44, 48, tzinfo=dt.timezone.utc),
    )

    assert result["applied"] is True
    assert result["changed"] is True
    assert result["model"] == "gpt-5.6-sol"
    assert TEST_KEY not in json.dumps(result)
    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert updated["model"]["default"] == "gpt-5.6-sol"
    assert updated["model"]["api_key"] == TEST_KEY
    assert updated["approvals"]["mode"] == "off"

    backup_dir = Path(str(result["backup_dir"]))
    backup = yaml.safe_load(
        (backup_dir / "config.yaml.before").read_text(encoding="utf-8")
    )
    assert backup["model"]["api_key"] == "old-provider-key-0123456789"
    manifest_text = (backup_dir / "manifest.json").read_text(encoding="utf-8")
    assert TEST_KEY not in manifest_text
    assert "old-provider-key" not in manifest_text
    assert json.loads(manifest_text)["state"] == "applied"
    if os.name == "posix":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        assert stat.S_IMODE((backup_dir / "manifest.json").stat().st_mode) == 0o600


def test_apply_rotation_rejects_stale_hash_without_changing_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    backup_root = tmp_path / "backups"
    write_config(config_path)
    original = config_path.read_bytes()

    with pytest.raises(RotationError, match="changed after"):
        apply_rotation(
            config_path,
            backup_root,
            RotationSecret(TEST_KEY, "5.6sol"),
            "gpt-5.6-sol",
            expected_sha256="0" * 64,
        )

    assert config_path.read_bytes() == original
    assert not backup_root.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_apply_rotation_rejects_writable_backup_root(tmp_path):
    config_path = tmp_path / "config.yaml"
    backup_root = tmp_path / "backups"
    write_config(config_path)
    backup_root.mkdir(mode=0o777)
    backup_root.chmod(0o777)

    with pytest.raises(RotationError, match="group/world writable"):
        apply_rotation(
            config_path,
            backup_root,
            RotationSecret(TEST_KEY, "5.6sol"),
            "gpt-5.6-sol",
            expected_sha256=file_sha256(config_path),
        )


def test_redaction_removes_provider_keys():
    value = "request failed for sk-abcdefghijklmnop and " + TEST_KEY
    cleaned = redact(value, TEST_KEY)
    assert "sk-abcdefghijklmnop" not in cleaned
    assert TEST_KEY not in cleaned
    assert cleaned.count("[REDACTED]") == 2

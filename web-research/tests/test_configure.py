from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "configure_hermes_web.py"


def load_module():
    spec = importlib.util.spec_from_file_location("configure_hermes_web_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_enable_is_idempotent_and_preserves_other_config():
    module = load_module()
    config = {
        "model": {"provider": "existing"},
        "plugins": {
            "enabled": ["other", "wechat-cloud-web", "wechat-cloud-web"],
            "disabled": ["wechat-cloud-web", "disabled-other"],
        },
    }

    first = module.enable(config)
    second = module.enable(first)
    module.validate(second)

    assert second["model"] == {"provider": "existing"}
    assert second["plugins"]["enabled"] == ["other", "wechat-cloud-web"]
    assert second["plugins"]["disabled"] == ["disabled-other"]
    assert second["web"]["search_backend"] == "wechat-cloud"
    assert second["web"]["extract_backend"] == "wechat-cloud"


def test_atomic_write_round_trips_yaml(tmp_path, monkeypatch):
    module = load_module()
    path = tmp_path / "config.yaml"
    path.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    monkeypatch.setattr(module.os, "chown", lambda *_args: None, raising=False)

    config = module.enable(module._load(path))
    module.atomic_write(path, config)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))

    module.validate(loaded)
    assert not list(tmp_path.glob("*.tmp"))


def test_validate_rejects_tool_override():
    module = load_module()
    config = module.enable({})
    config["plugins"]["entries"]["wechat-cloud-web"]["allow_tool_override"] = True

    try:
        module.validate(config)
    except ValueError as exc:
        assert "override" in str(exc)
    else:
        raise AssertionError("unsafe tool override was accepted")

#!/usr/bin/env python3
"""Atomically enable or validate the WeChat cloud web provider in Hermes."""

from __future__ import annotations

import argparse
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict

import yaml


PLUGIN_NAME = "wechat-cloud-web"
PROVIDER_NAME = "wechat-cloud"


def _load(path: Path) -> Dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Hermes config root must be a mapping")
    return value


def enable(config: Dict[str, Any]) -> Dict[str, Any]:
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("plugins must be a mapping")

    enabled = plugins.setdefault("enabled", [])
    disabled = plugins.setdefault("disabled", [])
    entries = plugins.setdefault("entries", {})
    if not isinstance(enabled, list) or not isinstance(disabled, list):
        raise ValueError("plugin enabled and disabled values must be lists")
    if not isinstance(entries, dict):
        raise ValueError("plugin entries must be a mapping")

    if PLUGIN_NAME not in enabled:
        enabled.append(PLUGIN_NAME)
    plugins["enabled"] = list(dict.fromkeys(str(item) for item in enabled))
    plugins["disabled"] = [item for item in disabled if str(item) != PLUGIN_NAME]
    entry = entries.setdefault(PLUGIN_NAME, {})
    if not isinstance(entry, dict):
        raise ValueError("wechat-cloud-web plugin entry must be a mapping")
    entry["allow_tool_override"] = False

    web = config.setdefault("web", {})
    if not isinstance(web, dict):
        raise ValueError("web must be a mapping")
    web["search_backend"] = PROVIDER_NAME
    web["extract_backend"] = PROVIDER_NAME
    web.setdefault("extract_char_limit", 20_000)
    return config


def validate(config: Dict[str, Any]) -> None:
    plugins = config.get("plugins")
    web = config.get("web")
    if not isinstance(plugins, dict) or not isinstance(web, dict):
        raise ValueError("Hermes web provider is not configured")
    if PLUGIN_NAME not in plugins.get("enabled", []):
        raise ValueError("wechat-cloud-web is not enabled")
    if PLUGIN_NAME in plugins.get("disabled", []):
        raise ValueError("wechat-cloud-web is also disabled")
    entry = plugins.get("entries", {}).get(PLUGIN_NAME, {})
    if entry.get("allow_tool_override") is not False:
        raise ValueError("wechat-cloud-web tool override must remain disabled")
    if web.get("search_backend") != PROVIDER_NAME:
        raise ValueError("wechat-cloud is not the search backend")
    if web.get("extract_backend") != PROVIDER_NAME:
        raise ValueError("wechat-cloud is not the extract backend")


def atomic_write(path: Path, config: Dict[str, Any]) -> None:
    original = path.stat()
    payload = yaml.safe_dump(
        config,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(original.st_mode))
        if hasattr(os, "chown"):
            os.chown(temporary, original.st_uid, original.st_gid)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("enable", "validate"))
    parser.add_argument("config", type=Path)
    args = parser.parse_args()

    config = _load(args.config)
    if args.action == "enable":
        enable(config)
        validate(config)
        atomic_write(args.config, config)
    else:
        validate(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

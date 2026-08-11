from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_searxng_is_json_only_and_uses_bounded_engines():
    settings = yaml.safe_load((ROOT / "searxng" / "settings.yml").read_text(encoding="utf-8"))
    engines = settings["use_default_settings"]["engines"]["keep_only"]
    assert "bing" in engines
    assert "baidu" in engines
    enabled = {item["name"]: item["disabled"] for item in settings["engines"]}
    assert enabled == {
        "360search": False,
        "bing": False,
        "bing news": False,
        "baidu": False,
        "quark": False,
        "sogou": False,
    }
    assert settings["search"]["formats"] == ["json"]
    assert settings["server"]["limiter"] is False
    assert settings["server"]["image_proxy"] is False


def test_container_is_loopback_hardened_and_digest_pinned():
    service = (ROOT / "deploy" / "wechat-searxng.service").read_text(encoding="utf-8")
    assert "127.0.0.1:8651:8080" in service
    assert "--read-only" in service
    assert "--cap-drop ALL" in service
    assert "--security-opt no-new-privileges:true" in service
    assert "--memory 384m" in service
    assert "@sha256:268fdb05" in service
    assert ":latest" not in service


def test_plugin_does_not_log_queries_or_page_bodies():
    source = (ROOT / "hermes-plugin" / "provider.py").read_text(encoding="utf-8")
    assert 'query_hash=%s' in source
    assert 'query=%s' not in source
    assert 'content=%s' not in source
    assert 'document=%s' not in source


def test_domestic_fallbacks_are_fixed_https_endpoints_and_cache_is_hash_keyed():
    source = (ROOT / "hermes-plugin" / "provider.py").read_text(encoding="utf-8")
    environment = (ROOT / "deploy" / "hermes-web.env").read_text(encoding="utf-8")
    assert '"https://m.sogou.com/web/searchList.jsp"' in source
    assert '"https://m.so.com/s"' in source
    assert '"https://m.baidu.com/s"' in source
    assert '"https://global.bing.com/news/search"' in source
    assert '"https://www.leiphone.com/feed"' in source
    assert '"https://www.qbitai.com/feed"' in source
    assert '"https://www.infoq.cn/feed"' in source
    assert "follow_redirects=False" in source
    assert "cache_key TEXT PRIMARY KEY" in source
    assert "WECHAT_WEB_DOMESTIC_FALLBACK_ENABLED=true" in environment
    assert "WECHAT_WEB_DOMESTIC_MERGE_ENABLED=true" in environment
    assert "WECHAT_WEB_EXTRACT_WORKERS=3" in environment
    assert "WECHAT_WEB_BING_NEWS_RSS_ENABLED=true" in environment
    assert "WECHAT_WEB_SEARCH_STALE_IF_ERROR_SECONDS=86400" in environment
    assert "WECHAT_WEB_TRUSTED_FEED_WORKERS=9" in environment


def test_extractor_dependencies_are_version_and_hash_locked():
    lock = (ROOT / "requirements-extract.lock").read_text(encoding="utf-8")
    assert "trafilatura==2.0.0" in lock
    assert lock.count("--hash=sha256:") == 12


def test_probe_is_read_only_and_does_not_call_wechat():
    for name in (
        "probe_provider.py",
        "probe_hermes_tools.py",
        "probe_gateway_run.py",
        "probe_adapter_search.py",
        "stress_provider.py",
    ):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert ":8765" not in source
        assert "/groups/" not in source
        assert "CHAT_API" not in source
        assert "write_text" not in source
        assert "unlink(" not in source
    provider_probe = (ROOT / "scripts" / "probe_provider.py").read_text(encoding="utf-8")
    assert "http://169.254.169.254" in provider_probe
    assert "https://www.python.org/" in provider_probe
    assert "expected_hosts" in provider_probe
    stress_probe = (ROOT / "scripts" / "stress_provider.py").read_text(
        encoding="utf-8"
    )
    assert "reset_source_circuits(module)" in stress_probe


def test_candidate_gateway_is_loopback_and_uses_private_vendor():
    source = (ROOT / "scripts" / "run_candidate_gateway.sh").read_text(encoding="utf-8")
    assert "API_SERVER_HOST=127.0.0.1" in source
    assert 'export PYTHONPATH="$plugin_vendor"' in source
    assert 'WECHAT_WEB_SEARCH_URL="$search_url"' in source
    assert "set API_SERVER_KEY for the candidate Gateway" in source
    assert "candidate_api_key=$API_SERVER_KEY" in source
    assert 'export API_SERVER_KEY="$candidate_api_key"' in source
    assert "runuser -m -u wechat-hermes-runner" in source
    assert 'API_SERVER_KEY="$api_key"' not in source
    assert "PORT API_KEY" not in source
    assert "wechat-chat-api" not in source
    assert "systemctl" not in source


def test_candidate_builder_vendors_only_hash_locked_dependencies():
    source = (ROOT / "scripts" / "build_candidate.sh").read_text(
        encoding="utf-8"
    )
    assert "/var/lib/wechat-hermes/candidates/web-research" in source
    assert "--require-hashes" in source
    assert "--no-deps" in source
    assert 'test -d "$vendor/trafilatura"' in source
    assert "/opt/hermes-runtime/venv/bin/python" in source
    assert "gateway-home" in source
    assert "configure_hermes_web.py" in source
    assert ":8765" not in source
    assert "/groups/" not in source


def test_adapter_search_probe_delivers_only_to_the_fake_chat_api():
    source = (ROOT / "scripts" / "probe_adapter_search.py").read_text(
        encoding="utf-8"
    )
    assert "fake.ChatHandler" in source
    assert "fake_media_deliveries" in source
    assert "http://127.0.0.1:8642" in source
    assert '"--message"' in source
    assert '"--profile"' in source
    assert '"china"' in source
    assert '"twitter"' in source
    assert '"compare"' in source
    assert '"verify"' in source
    assert '"dual"' in source
    assert 'requirements.get("dual_region")' in source
    assert 'started_tools.count("web_search") > 4' in source
    assert 'name.startswith("browser_")' in source
    assert '"--request-id"' in source
    assert ":8765" not in source
    assert "/groups/" not in source


def test_production_release_is_versioned_health_checked_and_rollback_safe():
    install = (ROOT / "scripts" / "install_production.sh").read_text(encoding="utf-8")
    rollback = (ROOT / "scripts" / "rollback_production.sh").read_text(encoding="utf-8")
    health = (ROOT / "scripts" / "healthcheck_production.sh").read_text(encoding="utf-8")
    dropin = (ROOT / "deploy" / "hermes-worker-web.conf").read_text(encoding="utf-8")
    combined = install + rollback + health

    assert "MANIFEST.sha256" in install
    assert "rollback/READY" in rollback
    assert "on_error" in install
    assert "set WECHAT_PID to the active WeChat process ID" in combined
    assert "127.0.0.1:8642/health" in combined
    assert "127.0.0.1:8651/search" in combined
    assert "PYTHONPATH=/var/lib/wechat-hermes" in dropin
    assert ":8765" not in combined
    assert "send-state" not in combined
    assert "db-state" not in combined
    assert "bot.db" not in combined
    assert "rm -rf" not in combined
    assert "kill " not in combined

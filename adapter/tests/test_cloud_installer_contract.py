import re
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1] / "deploy" / "install_cloud.sh"
).read_text(encoding="utf-8")


def test_cloud_browser_is_pinned_and_has_a_verified_mirror_fallback():
    assert (
        "CHROME_FOR_TESTING_VERSION=${CHROME_FOR_TESTING_VERSION:-"
        "150.0.7871.115}"
    ) in SCRIPT
    match = re.search(
        r"CHROME_FOR_TESTING_SHA256=\$\{CHROME_FOR_TESTING_SHA256:-([0-9a-f]{64})\}",
        SCRIPT,
    )
    assert match is not None
    assert match.group(1) == (
        "1be2db033133c5e2dd1a4e8664bf67b"
        "19a61bcf6ed28d2b00f433b3f0b4f9585"
    )
    assert "https://storage.googleapis.com/chrome-for-testing-public/" in SCRIPT
    assert "https://npmmirror.com/mirrors/chrome-for-testing/" in SCRIPT
    assert "verify_cloud_browser_archive" in SCRIPT
    assert "unzip -tq" in SCRIPT
    assert "apt-cache show libasound2t64" in SCRIPT
    assert "libgtk-3-0t64" in SCRIPT


def test_browser_only_mode_preserves_existing_service_credentials():
    assert 'if [[ "${1:-}" == "--browser-only" ]]' in SCRIPT
    browser_only = SCRIPT.split(
        'if [[ "${1:-}" == "--browser-only" ]]', 1
    )[1].split("fi", 1)[0]
    assert "write_environment" not in browser_only
    assert "update_cloud_browser_environment" in browser_only


def test_hermes_receives_the_pinned_browser_executable():
    assert "AGENT_BROWSER_EXECUTABLE_PATH" in SCRIPT
    assert (
        '"$STATE_ROOT/chrome-for-testing/$CHROME_FOR_TESTING_VERSION/'
        'chrome-linux64/chrome"'
    ) in SCRIPT
    assert "os.environ['CHROME_FOR_TESTING_VERSION']" in SCRIPT
    assert "agent-browser install --with-deps" not in SCRIPT


def test_hermes_runtime_logging_is_hardened_during_install():
    assert "harden_hermes_logging()" in SCRIPT
    runtime_install = SCRIPT.index("  install_hermes_runtime\n")
    hardening = SCRIPT.index("  harden_hermes_logging\n")
    home_install = SCRIPT.index("  install_hermes_home\n")

    assert runtime_install < hardening < home_install
    assert "deploy/harden_hermes_logging.py" in SCRIPT


def test_hermes_session_chat_scope_is_hardened_during_install():
    assert "harden_hermes_api_scopes()" in SCRIPT
    runtime_install = SCRIPT.index("  install_hermes_runtime\n")
    api_hardening = SCRIPT.index("  harden_hermes_api_scopes\n")
    home_install = SCRIPT.index("  install_hermes_home\n")

    assert runtime_install < api_hardening < home_install
    assert "deploy/harden_hermes_api_scopes.py" in SCRIPT


def test_hermes_run_evidence_is_hardened_before_home_install():
    assert "harden_hermes_run_evidence()" in SCRIPT
    runtime_install = SCRIPT.index("  install_hermes_runtime\n")
    evidence_hardening = SCRIPT.index("  harden_hermes_run_evidence\n")
    home_install = SCRIPT.index("  install_hermes_home\n")

    assert runtime_install < evidence_hardening < home_install
    assert "deploy/harden_hermes_run_evidence.py" in SCRIPT


def test_hermes_skill_reload_is_hardened_before_home_install():
    assert "harden_hermes_skill_reload()" in SCRIPT
    runtime_install = SCRIPT.index("  install_hermes_runtime\n")
    reload_hardening = SCRIPT.index("  harden_hermes_skill_reload\n")
    home_install = SCRIPT.index("  install_hermes_home\n")

    assert runtime_install < reload_hardening < home_install
    assert "deploy/harden_hermes_skill_reload.py" in SCRIPT


def test_production_ports_memory_and_approvals_match_cloud_policy():
    assert "127.0.0.1:18000" not in SCRIPT
    assert '"HERMES_WECHAT_PORT": "8000"' in SCRIPT
    assert 'approvals["mode"] = "off"' in SCRIPT
    assert 'approvals["cron_mode"] = "off"' in SCRIPT
    assert '["memory_enabled"] = False' in SCRIPT
    assert 'disabled_toolsets.append("memory")' in SCRIPT
    assert '"ALLOW_PRIVATE_WECHAT_CHAT": "false"' in SCRIPT
    assert '"HERMES_WECHAT_SESSION_GENERATION": "3"' in SCRIPT
    assert 'config.setdefault("model", {})["context_length"] = 128000' in SCRIPT
    assert 'compression["threshold"] = 0.75' in SCRIPT
    assert 'compression["target_ratio"] = 0.20' in SCRIPT
    assert 'compression["protect_first_n"] = 0' in SCRIPT
    assert 'compression["protect_last_n"] = 16' in SCRIPT
    assert 'compression["in_place"] = True' in SCRIPT


def test_outbound_control_database_uses_isolated_state_path():
    root = Path(__file__).resolve().parents[2]
    service = (root / "chat-api" / "wechat-chat-api.service").read_text(
        encoding="utf-8"
    )
    example = (root / "chat-api" / "config.example.json").read_text(
        encoding="utf-8"
    )

    expected = "/var/lib/wechat-hermes/outbound-control.db"
    assert f"OUTBOUND_CONTROL_DB=$STATE_ROOT/outbound-control.db" in SCRIPT
    assert 'config["outbound_control_db"] = ' in SCRIPT
    assert expected in SCRIPT
    assert expected in example
    assert "ReadWritePaths=/var/lib/wechat-hermes" in service
    for protected in (
        "adapter-data",
        "artifacts",
        "browser-downloads",
        "candidates",
        "chrome-for-testing",
        "home",
        "skill-trust",
        "workspace",
    ):
        assert f"/var/lib/wechat-hermes/{protected}" in service
    assert "source.backup(destination)" in SCRIPT
    assert 'install -d -o root -g "$RUNTIME_GROUP" -m 1750 "$STATE_ROOT"' in SCRIPT
    assert 'chmod 1750 "$STATE_ROOT"' in SCRIPT
    assert 'setfacl -m u:ubuntu:-wx "$STATE_ROOT"' in SCRIPT


def test_structured_bridge_uses_scoped_tokens_without_loading_ocr_module():
    root = Path(__file__).resolve().parents[2]
    service = (root / "chat-api" / "linux-wechat-bridge.service").read_text(
        encoding="utf-8"
    )
    bridge = (root / "chat-api" / "db_bridge.py").read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/wechat-hermes/bridge.env" in service
    assert 'os.environ.get("WECHAT_CHAT_API_TOKEN")' in bridge
    assert 'os.environ.get("BRIDGE_TOKEN")' in bridge
    assert 'headers["Authorization"] = "Bearer " + CHAT_API_TOKEN' in bridge
    assert "import bridge" not in bridge
    assert "OCR_FALLBACK" not in bridge


def test_cleanup_and_log_rotation_cover_hermes_runtime_records():
    root = Path(__file__).resolve().parents[1]
    cleanup_service = (
        root / "deploy" / "wechat-hermes-cleanup.service"
    ).read_text(encoding="utf-8")
    logrotate = (root / "deploy" / "wechat-hermes.logrotate").read_text(
        encoding="utf-8"
    )

    assert "--hermes-home ${HERMES_HOME}" in cleanup_service
    assert "/etc/logrotate.d/wechat-hermes" in SCRIPT
    assert "rotate 30" in logrotate
    assert "copytruncate" in logrotate
    assert (
        "create 0600 wechat-hermes-runner wechat-hermes-runtime"
        in logrotate
    )


def test_budget_defaults_are_nonzero_and_calendar_scoped():
    assert '"HERMES_WECHAT_DAILY_TOKEN_LIMIT": "10000000"' in SCRIPT
    assert '"HERMES_WECHAT_BUDGET_TIMEZONE": "Asia/Shanghai"' in SCRIPT
    assert '"HERMES_INPUT_TOKEN_COST_PER_MILLION": "3"' in SCRIPT
    assert '"HERMES_OUTPUT_TOKEN_COST_PER_MILLION": "15"' in SCRIPT


def test_environment_examples_match_production_generation_and_budget():
    root = Path(__file__).resolve().parents[1]
    for relative_path in ("deploy/adapter.env.example",):
        example = (root / relative_path).read_text(encoding="utf-8")
        assert "HERMES_WECHAT_SESSION_GENERATION=3" in example
        assert "HERMES_WECHAT_DAILY_TOKEN_LIMIT=10000000" in example
        assert "HERMES_INPUT_TOKEN_COST_PER_MILLION=3" in example
        assert "HERMES_OUTPUT_TOKEN_COST_PER_MILLION=15" in example
        assert "WECHAT_CHAT_API_TOKEN=" in example
        assert (
            "HERMES_WECHAT_DB_PATH="
            "/var/lib/wechat-hermes/adapter-data/adapter.db"
        ) in example
        assert "HERMES_HOME=/var/lib/wechat-hermes/workspace/home" in example
        assert (
            "HERMES_SKILL_TRUST_ROOT=/var/lib/wechat-hermes/skill-trust"
            in example
        )


def test_release_permissions_preserve_runtime_and_skill_executables():
    assert (
        'find "$release_root/.venv" -type f -exec chmod go-w,u+rw {} +'
        in SCRIPT
    )
    assert not re.search(
        r"chmod[^\n]*\"\$release_root/\.venv/bin/python\"",
        SCRIPT,
    )
    assert (
        'find "$pending_release" -type f -perm /0111 '
        "-exec chmod 0550 {} +"
    ) in SCRIPT
    assert (
        'find "$pending_release" -type f ! -perm /0111 '
        "-exec chmod 0440 {} +"
    ) in SCRIPT


def test_skill_sandbox_is_installed_before_skill_runtime_use():
    sandbox_install = SCRIPT.index("  install_skill_sandbox_dependency\n")
    hermes_home_install = SCRIPT.index("  install_hermes_home\n")

    assert sandbox_install < hermes_home_install
    assert "apt-get install -y bubblewrap" in SCRIPT
    assert '"HERMES_SKILL_SANDBOX": "/usr/bin/bwrap"' in SCRIPT


def test_skill_sandbox_allows_only_bwrap_user_namespaces_on_restricted_ubuntu():
    profile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "wechat-hermes-bwrap.apparmor"
    ).read_text(encoding="utf-8")

    assert "profile wechat-hermes-bwrap /usr/bin/bwrap" in profile
    assert "flags=(unconfined)" in profile
    assert "userns," in profile
    assert "kernel.apparmor_restrict_unprivileged_userns" in SCRIPT
    assert "apparmor_parser -r /etc/apparmor.d/wechat-hermes-bwrap" in SCRIPT
    assert 'sudo -u "$ADAPTER_USER" bwrap' in SCRIPT
    assert "unprivileged Bubblewrap Skill sandbox self-test failed" in SCRIPT


def test_deployment_uses_four_separate_credentials_and_scoped_environments():
    for name in (
        "BRIDGE_TOKEN",
        "HERMES_WECHAT_INTERNAL_TOKEN",
        "WECHAT_CHAT_API_TOKEN",
        "HERMES_API_KEY",
    ):
        assert name in SCRIPT
    assert "production credentials are not pairwise distinct" in SCRIPT
    assert 'hermes.pop(forbidden, None)' in SCRIPT
    assert '"BRIDGE_TOKEN",' in SCRIPT
    assert '"WECHAT_CHAT_API_TOKEN",' in SCRIPT
    assert 'chat_api.pop(forbidden, None)' in SCRIPT
    assert 'bridge.pop(forbidden, None)' in SCRIPT
    assert 'write_env(bridge_path, bridge)' in SCRIPT


def test_deployment_uses_distinct_service_accounts_and_no_legacy_user_alias():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "deploy" / "hermes-worker.service").read_text(
        encoding="utf-8"
    )
    adapter = (
        root / "deploy" / "wechat-hermes-adapter.service"
    ).read_text(encoding="utf-8")

    assert "ADAPTER_USER=wechat-hermes" in SCRIPT
    assert "RUNTIME_USER=wechat-hermes-runner" in SCRIPT
    assert "RUNTIME_GROUP=wechat-hermes-runtime" in SCRIPT
    assert "SERVICE_USER" not in SCRIPT
    assert "User=wechat-hermes-runner" in worker
    assert "User=wechat-hermes" in adapter
    assert "UMask=0007" in worker


def test_systemd_units_have_hard_resource_limits():
    root = Path(__file__).resolve().parents[2]
    units = (
        root / "adapter" / "deploy" / "hermes-worker.service",
        root / "adapter" / "deploy" / "wechat-hermes-adapter.service",
        root / "adapter" / "deploy" / "wechat-hermes-cleanup.service",
        root / "chat-api" / "wechat-chat-api.service",
        root / "chat-api" / "linux-wechat-bridge.service",
    )
    for path in units:
        content = path.read_text(encoding="utf-8")
        assert re.search(r"^MemoryMax=\S+$", content, re.MULTILINE)
        assert re.search(r"^CPUQuota=\d+%$", content, re.MULTILINE)
        assert re.search(r"^TasksMax=\d+$", content, re.MULTILINE)

def test_release_layout_and_protected_baselines_are_required():
    assert 'SOURCE_ROOT="$RELEASE_ROOT/adapter"' in SCRIPT
    assert (
        "CHAT_API_SOURCE_ROOT=${CHAT_API_SOURCE_ROOT:-"
        "$RELEASE_ROOT/chat-api}"
    ) in SCRIPT
    for name in (
        "EXPECTED_DB_STATE_SHA256",
        "EXPECTED_SEND_STATE_SHA256",
        "EXPECTED_BOT_DB_SHA256",
        "EXPECTED_DB_STATE_INODE",
        "EXPECTED_SEND_STATE_INODE",
        "EXPECTED_BOT_DB_INODE",
    ):
        assert name in SCRIPT
    assert '"$BRIDGE_ROOT/db-state.json"' in SCRIPT
    assert '"/home/ubuntu/.cache/wechat-chat-api/send-state.json"' in SCRIPT
    assert '"/opt/wechat-ai-bot/data/bot.db"' in SCRIPT


def test_versioned_runtime_and_skill_links_handle_legacy_directories():
    assert 'if [[ -d "$ADAPTER_ROOT" && ! -L "$ADAPTER_ROOT" ]]' in SCRIPT
    assert 'if [[ -d "$HERMES_ROOT" && ! -L "$HERMES_ROOT" ]]' in SCRIPT
    assert (
        'if [[ -d "$HERMES_PYTHON_ROOT" && ! -L "$HERMES_PYTHON_ROOT" ]]'
        in SCRIPT
    )
    assert 'mv -Tf -- "$next_link" "$ADAPTER_ROOT"' in SCRIPT
    assert 'mv -Tf -- "$next_root_link" "$HERMES_ROOT"' in SCRIPT
    assert 'mv -Tf -- "$next_python_link" "$HERMES_PYTHON_ROOT"' in SCRIPT
    assert 'mv -Tf -- "$next_link" "$SKILL_TRUST_ROOT/current"' in SCRIPT
    assert 'local active_link="$SKILL_TRUST_ROOT/active"' in SCRIPT
    assert '[[ "$active_target" == releases/* ]]' in SCRIPT
    assert 'fail "Hermes Skill active pointer is not a symlink"' in SCRIPT
    assert 'ln -s "releases/$release_name" "$active_link"' in SCRIPT
    assert (
        'ln -sfn "$SKILL_TRUST_ROOT/active/skills" "$hermes_home/skills"'
        in SCRIPT
    )
    assert (
        '"$SKILL_TRUST_ROOT/active/skills-lock.json"'
        in SCRIPT
    )
    assert (
        '"$SKILL_TRUST_ROOT/current/skills" "$hermes_home/skills"'
        not in SCRIPT
    )
    assert (
        'find "$pending_release" -type d -exec chmod 0550 {} +'
        in SCRIPT
    )


def test_hermes_startup_and_install_require_runtime_readable_skills():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "deploy" / "hermes-worker.service").read_text(
        encoding="utf-8"
    )
    for relative_path in (
        "skills-lock.json",
        "skills/creative/creative-ideation/SKILL.md",
        "skills/media/douyin-video-production/SKILL.md",
        "skills/personality/wechat-hermes-persona/SKILL.md",
        "skills/productivity/wechat-group-operations/SKILL.md",
    ):
        absolute = "/var/lib/wechat-hermes/skill-trust/active/" + relative_path
        assert f"ExecStartPre=/usr/bin/test -r {absolute}" in worker
        assert f'"$SKILL_TRUST_ROOT/active/{relative_path}"' in SCRIPT
    assert SCRIPT.count('sudo -u "$RUNTIME_USER" test -r') >= 5


def test_mcp_dependencies_keep_fastapi_starlette_compatible():
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements-mcp.txt").read_text(encoding="utf-8")

    assert "mcp==1.26.0" in requirements
    assert "starlette==0.47.3" in requirements


def test_cleanup_status_and_upgrade_permissions_are_installed():
    assert "HERMES_WECHAT_CLEANUP_STATUS_PATH" in SCRIPT
    assert "HERMES_WECHAT_CLEANUP_MAX_AGE_SECONDS" in SCRIPT
    assert "normalize_existing_artifact_permissions" in SCRIPT
    assert '-exec chmod g+rwx,o-rwx {} +' in SCRIPT


def test_cleanup_runtime_directories_are_group_traversable():
    assert "ensure_cleanup_runtime_directories" in SCRIPT
    for path in (
        '"$hermes_home/logs"',
        '"$hermes_home/sessions"',
        '"$home/.npm/_logs"',
    ):
        assert SCRIPT.count(path) >= 2
    assert (
        'install -d -o "$RUNTIME_USER" -g "$RUNTIME_GROUP" -m 2770'
        in SCRIPT
    )
    assert "cleanup runtime directory must not be a symbolic link" in SCRIPT

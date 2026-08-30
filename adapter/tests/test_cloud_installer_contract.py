import re
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1] / "deploy" / "install_cloud.sh"
).read_text(encoding="utf-8")
PERSONA_ROLLBACK = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "rollback_persona.sh"
).read_text(encoding="utf-8")
SSHD_HARDENING = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "sshd-wechat-hermes.conf"
).read_text(encoding="utf-8")
CCV3_ADAPTER_RELEASE = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "deploy_ccv3_adapter_release.sh"
).read_text(encoding="utf-8")
BRIDGE_RELEASE = (
    Path(__file__).resolve().parents[2]
    / "chat-api"
    / "deploy"
    / "deploy_bridge_release.sh"
).read_text(encoding="utf-8")
CHAT_API_RELEASE = (
    Path(__file__).resolve().parents[2]
    / "chat-api"
    / "deploy"
    / "deploy_chat_api_release.sh"
).read_text(encoding="utf-8")


def test_sshd_hardening_disables_password_and_root_login():
    assert "PubkeyAuthentication yes" in SSHD_HARDENING
    assert "PasswordAuthentication no" in SSHD_HARDENING
    assert "KbdInteractiveAuthentication no" in SSHD_HARDENING
    assert "AuthenticationMethods publickey" in SSHD_HARDENING
    assert "PermitRootLogin no" in SSHD_HARDENING
    assert "AllowUsers ubuntu" in SSHD_HARDENING
    assert "LoginGraceTime 30" in SSHD_HARDENING
    assert "MaxAuthTries 3" in SSHD_HARDENING
    assert "MaxStartups 10:30:30" in SSHD_HARDENING


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


def test_hermes_home_mode_is_hardened_during_install():
    assert "harden_hermes_home_mode()" in SCRIPT
    runtime_install = SCRIPT.index("  install_hermes_runtime\n")
    hardening = SCRIPT.index("  harden_hermes_home_mode\n")
    home_install = SCRIPT.index("  install_hermes_home\n")

    assert runtime_install < hardening < home_install
    assert "deploy/harden_hermes_home_mode.py" in SCRIPT


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


def test_hermes_skills_are_disabled_during_home_install():
    assert 'disabled_toolsets.append("skills")' in SCRIPT
    assert 'skills["external_dirs"] = []' in SCRIPT
    assert 'install -d -o root -g "$RUNTIME_GROUP" -m 0550' in SCRIPT
    assert 'chmod 0550 "$skills_path"' in SCRIPT
    assert 'chmod g-s "$skills_path"' in SCRIPT
    assert "harden_hermes_skill_reload" not in SCRIPT
    assert "remove_legacy_skill_sandbox" in SCRIPT
    assert 'rm -f -- "$profile"' in SCRIPT


def test_production_ports_memory_and_approvals_match_cloud_policy():
    assert "127.0.0.1:18000" not in SCRIPT
    assert '"HERMES_WECHAT_PORT": "8000"' in SCRIPT
    assert 'approvals["mode"] = "off"' in SCRIPT
    assert 'approvals["cron_mode"] = "off"' in SCRIPT
    assert '["memory_enabled"] = False' in SCRIPT
    assert 'disabled_toolsets.append("memory")' in SCRIPT
    assert 'disabled_toolsets.append("skills")' in SCRIPT
    assert '"ALLOW_PRIVATE_WECHAT_CHAT": "false"' in SCRIPT
    assert '"HERMES_WECHAT_SESSION_GENERATION": "11"' in SCRIPT
    assert '"HERMES_WECHAT_CHAT_ONLY": "true"' in SCRIPT
    assert '"HERMES_WECHAT_GROUP_LISTENER_ENABLED": "true"' in SCRIPT
    assert '"HERMES_WECHAT_GROUP_LISTENER_MIN_REPLY_GAP_SECONDS": "12"' in SCRIPT
    assert '"HERMES_WECHAT_GROUP_LISTENER_MIN_TURNS_BETWEEN_REPLIES": "3"' in SCRIPT
    assert '"HERMES_WECHAT_RELATIONSHIP_MEMORY_ENABLED": "false"' not in SCRIPT
    assert '"HERMES_WECHAT_RELATIONSHIP_PROACTIVE_ENABLED": "false"' not in SCRIPT
    assert 'key.startswith("HERMES_WECHAT_RELATIONSHIP_")' in SCRIPT
    assert '"HERMES_HOME_MODE": "2770"' in SCRIPT
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


def test_chat_api_restart_is_isolated_and_fatal_stacks_are_enabled():
    root = Path(__file__).resolve().parents[2]
    adapter_unit = (
        root / "adapter" / "deploy" / "wechat-hermes-adapter.service"
    ).read_text(encoding="utf-8")
    bridge_unit = (root / "chat-api" / "linux-wechat-bridge.service").read_text(
        encoding="utf-8"
    )
    chat_unit = (root / "chat-api" / "wechat-chat-api.service").read_text(
        encoding="utf-8"
    )
    chat_source = (root / "chat-api" / "chat_api.py").read_text(
        encoding="utf-8"
    )

    assert "Wants=network-online.target wechat-chat-api.service" in adapter_unit
    assert "Requires=hermes-worker.service\n" in adapter_unit
    assert "Requires=hermes-worker.service wechat-chat-api.service" not in adapter_unit
    assert "Wants=network-online.target wechat-chat-api.service" in bridge_unit
    assert "Requires=wechat-chat-api.service" not in bridge_unit
    assert "Environment=PYTHONFAULTHANDLER=1" in chat_unit
    assert "faulthandler.enable(all_threads=True)" in chat_source


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
        assert "HERMES_WECHAT_SESSION_GENERATION=11" in example
        assert "HERMES_WECHAT_CHAT_ONLY=true" in example
        assert "HERMES_WECHAT_GROUP_LISTENER_ENABLED=true" in example
        assert "HERMES_WECHAT_GROUP_LISTENER_MIN_REPLY_GAP_SECONDS=12" in example
        assert "HERMES_WECHAT_GROUP_LISTENER_MIN_TURNS_BETWEEN_REPLIES=3" in example
        assert "HERMES_WECHAT_DAILY_TOKEN_LIMIT=10000000" in example
        assert "HERMES_WECHAT_DELIVERY_RECONCILE_ATTEMPTS=5" in example
        assert "HERMES_WECHAT_DELIVERY_RECONCILE_DELAY_SECONDS=0.75" in example
        assert "Legacy per-member relationship variables are intentionally omitted" in example
        assert "HERMES_WECHAT_RELATIONSHIP_MEMORY_ENABLED=" not in example
        assert "HERMES_WECHAT_RELATIONSHIP_PROACTIVE_ENABLED=" not in example
        assert "HERMES_INPUT_TOKEN_COST_PER_MILLION=3" in example
        assert "HERMES_OUTPUT_TOKEN_COST_PER_MILLION=15" in example
        assert "WECHAT_CHAT_API_TOKEN=" in example
        assert (
            "HERMES_WECHAT_DB_PATH="
            "/var/lib/wechat-hermes/adapter-data/adapter.db"
        ) in example
        assert "HERMES_HOME=/var/lib/wechat-hermes/workspace/home" in example
        assert "HERMES_SKILL_TRUST_ROOT=" not in example
        assert "HERMES_SKILL_SANDBOX=" not in example

    hermes_example = (root / "deploy/hermes.env.example").read_text(
        encoding="utf-8"
    )
    assert "HERMES_HOME_MODE=2770" in hermes_example
    assert '"HERMES_WECHAT_DELIVERY_RECONCILE_ATTEMPTS": "5"' in SCRIPT


def test_environment_writer_removes_legacy_skill_runtime_variables():
    for name in (
        "HERMES_CLI_PATH",
        "HERMES_SKILL_TRUST_ROOT",
        "HERMES_SKILL_SANDBOX",
        "HERMES_WECHAT_SKILL_INSTALL_TIMEOUT_SECONDS",
    ):
        assert f'"{name}",' in SCRIPT
    assert "environment.pop(obsolete, None)" in SCRIPT


def test_persona_bundles_are_pinned_and_checked_before_release_install():
    assert 'local ccv3_root="$SOURCE_ROOT/third_party/character-card-spec-v3"' in SCRIPT
    assert 'local card_root="$SOURCE_ROOT/personas"' in SCRIPT
    assert 'local sophia_root="$SOURCE_ROOT/skills/sophia"' in SCRIPT
    assert "https://github.com/kwaroran/character-card-spec-v3" in SCRIPT
    assert "f3a86af019fbd99f788f7a1155f399655b34ab35" in SCRIPT
    assert "3c472a16eeda5d018837e90d30fce2816b0982f07f4dba14c8fcc89aa11fe76c" in SCRIPT
    assert "9805dc6bf59dcf8d9eaedc8987f2798dc434bc3c8e6dafbbf23eb2147d74db95" in SCRIPT
    assert "7d55fb9df10760e689346335b64fc0699e2977a83dc2be3ee6a93972cc015ffa" in SCRIPT
    assert "https://github.com/sharbelxyz/sophia" in SCRIPT
    assert "f2cd448553d61aa3c2ea774dc7e2296f09d4b584" in SCRIPT
    assert "356bd853722504cafec04988555ca36933ef926b2146d0b9df0f72ad48579301" in SCRIPT
    assert "assert_persona_skill_bundle" in SCRIPT
    assert "fixed character card source lock mismatch" in SCRIPT
    assert "--exclude 'skills/humanizer-zh-next'" in SCRIPT
    assert 'data.replace(b"\\r\\n", b"\\n").replace(b"\\r", b"\\n")' in SCRIPT


def test_ccv3_adapter_only_release_keeps_the_runtime_pinned_and_reversible():
    assert "EXPECTED_SOURCE_COMMIT" in CCV3_ADAPTER_RELEASE
    assert 'git -C "$SOURCE_ROOT" rev-parse HEAD' in CCV3_ADAPTER_RELEASE
    assert "assert_ccv3_persona_resources" in CCV3_ADAPTER_RELEASE
    assert "sophia@1.0.0+ccv3-xiaoge@1.1.1" in CCV3_ADAPTER_RELEASE
    assert "skills/humanizer-zh-next" in CCV3_ADAPTER_RELEASE
    assert '"HERMES_WECHAT_SESSION_GENERATION": "11"' in CCV3_ADAPTER_RELEASE
    assert "Legacy relationship environment values are ignored" in CCV3_ADAPTER_RELEASE
    assert "restoring previous Adapter release" in CCV3_ADAPTER_RELEASE
    assert "systemctl restart wechat-hermes-adapter.service" in CCV3_ADAPTER_RELEASE
    assert "systemctl restart hermes-worker.service" not in CCV3_ADAPTER_RELEASE
    assert "systemctl restart wechat-chat-api.service" not in CCV3_ADAPTER_RELEASE
    for protected in (
        "/home/ubuntu/linux-wechat-bot/db-state.json",
        "/home/ubuntu/.cache/wechat-chat-api/send-state.json",
        "/opt/wechat-ai-bot/data/bot.db",
    ):
        assert protected in CCV3_ADAPTER_RELEASE


def test_bridge_only_release_requires_a_ready_ccv3_adapter_and_preserves_state():
    assert "EXPECTED_SOURCE_COMMIT" in BRIDGE_RELEASE
    assert '"$SOURCE_ROOT/chat-api/db_bridge.py"' in BRIDGE_RELEASE
    assert "sophia@1.0.0+ccv3-xiaoge@1.1.1" in BRIDGE_RELEASE
    assert "Adapter is not CCV3-ready" in BRIDGE_RELEASE
    assert '"HERMES_WECHAT_GROUP_LISTENER_ENABLED"' in BRIDGE_RELEASE
    assert "restoring previous Bridge release" in BRIDGE_RELEASE
    assert "systemctl restart linux-wechat-bridge.service" in BRIDGE_RELEASE
    assert "systemctl restart wechat-chat-api.service" not in BRIDGE_RELEASE
    assert "systemctl restart hermes-worker.service" not in BRIDGE_RELEASE


def test_chat_api_release_is_reversible_and_checks_plain_text_delivery():
    assert "EXPECTED_SOURCE_COMMIT" in CHAT_API_RELEASE
    assert '"$SOURCE_ROOT/chat-api/chat_api.py"' in CHAT_API_RELEASE
    assert "assert_plain_text_protocol" in CHAT_API_RELEASE
    assert '"wire_text = text"' in CHAT_API_RELEASE
    assert "restoring previous Chat API source" in CHAT_API_RELEASE
    assert "systemctl restart wechat-chat-api.service" in CHAT_API_RELEASE
    assert "systemctl restart linux-wechat-bridge.service" not in CHAT_API_RELEASE
    assert "systemctl restart wechat-hermes-adapter.service" not in CHAT_API_RELEASE
    assert "systemctl restart hermes-worker.service" not in CHAT_API_RELEASE
    for protected in (
        "/home/ubuntu/linux-wechat-bot/db-state.json",
        "/home/ubuntu/.cache/wechat-chat-api/send-state.json",
        "/opt/wechat-ai-bot/data/bot.db",
    ):
        assert protected in CHAT_API_RELEASE


def test_persona_rollback_rotates_sessions_without_deleting_relationship_data():
    assert 'PREVIOUS_RELEASE_ID=${1:-}' in PERSONA_ROLLBACK
    assert 'EXPECTED_WECHAT_PID=${EXPECTED_WECHAT_PID:-}' in PERSONA_ROLLBACK
    assert '"HERMES_WECHAT_SESSION_GENERATION": "12"' in PERSONA_ROLLBACK
    assert "systemctl restart wechat-hermes-adapter.service" in PERSONA_ROLLBACK
    assert "wait_for_adapter_ready" in PERSONA_ROLLBACK
    assert "http://127.0.0.1:8000/health" in PERSONA_ROLLBACK
    assert "restoring prior Adapter after failed rollback" in PERSONA_ROLLBACK
    assert 'mv -f -- "$env_backup" "$ADAPTER_ENV"' in PERSONA_ROLLBACK
    assert "pgrep -x wechat" in PERSONA_ROLLBACK
    assert "room-scoped context only" in PERSONA_ROLLBACK
    assert "DELETE FROM relationship" not in PERSONA_ROLLBACK
    assert "wechat-hermes-adapter-releases" in PERSONA_ROLLBACK


def test_release_permissions_preserve_runtime_executables():
    assert (
        'find "$release_root/.venv" -type f -exec chmod go-w,u+rw {} +'
        in SCRIPT
    )
    assert not re.search(
        r"chmod[^\n]*\"\$release_root/\.venv/bin/python\"",
        SCRIPT,
    )
    assert "pending_release" not in SCRIPT
    assert "install_skill_sandbox_dependency" not in SCRIPT


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


def test_versioned_runtime_links_and_empty_skill_home_handle_legacy_directories():
    assert 'if [[ -d "$ADAPTER_ROOT" && ! -L "$ADAPTER_ROOT" ]]' in SCRIPT
    assert 'if [[ -d "$HERMES_ROOT" && ! -L "$HERMES_ROOT" ]]' in SCRIPT
    assert (
        'if [[ -d "$HERMES_PYTHON_ROOT" && ! -L "$HERMES_PYTHON_ROOT" ]]'
        in SCRIPT
    )
    assert 'mv -Tf -- "$next_link" "$ADAPTER_ROOT"' in SCRIPT
    assert 'mv -Tf -- "$next_root_link" "$HERMES_ROOT"' in SCRIPT
    assert 'mv -Tf -- "$next_python_link" "$HERMES_PYTHON_ROOT"' in SCRIPT
    assert 'local skills_path="$hermes_home/skills"' in SCRIPT
    assert 'unlink -- "$skills_path"' in SCRIPT
    assert 'mv -- "$skills_path" "$skills_backup"' in SCRIPT
    assert 'unlink -- "$lock_path"' in SCRIPT
    assert 'install -d -o root -g "$RUNTIME_GROUP" -m 0550' in SCRIPT
    assert "SKILL_TRUST_ROOT=$STATE_ROOT" not in SCRIPT
    assert '"$SKILL_TRUST_ROOT/' not in SCRIPT


def test_hermes_startup_requires_config_and_empty_skill_directory():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "deploy" / "hermes-worker.service").read_text(
        encoding="utf-8"
    )
    assert (
        "ExecStartPre=/usr/bin/test -r "
        "/var/lib/wechat-hermes/workspace/home/.hermes/config.yaml"
    ) in worker
    assert (
        "ExecStartPre=/usr/bin/test -d "
        "/var/lib/wechat-hermes/workspace/home/.hermes/skills"
    ) in worker
    assert "/var/lib/wechat-hermes/skill-trust" in worker
    assert "/var/lib/wechat-hermes/skill-trust/active" not in worker
    assert (
        "ReadOnlyPaths="
        "/var/lib/wechat-hermes/workspace/home/.hermes/skills"
    ) in worker


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
    assert '"HERMES_HOME_MODE": "2770"' in SCRIPT
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

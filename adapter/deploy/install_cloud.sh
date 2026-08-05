#!/usr/bin/env bash
set -euo pipefail

RELEASE_ROOT=${RELEASE_ROOT:-/tmp/wechat-hermes-release}
if [[ -z "${SOURCE_ROOT:-}" ]]; then
  if [[ -d "$RELEASE_ROOT/adapter" ]]; then
    SOURCE_ROOT="$RELEASE_ROOT/adapter"
  else
    SOURCE_ROOT="$RELEASE_ROOT"
  fi
fi
CHAT_API_SOURCE_ROOT=${CHAT_API_SOURCE_ROOT:-$RELEASE_ROOT/chat-api}
HERMES_SOURCE=${HERMES_SOURCE:-/home/ubuntu/.hermes/hermes-agent}
HERMES_HOME_SOURCE=${HERMES_HOME_SOURCE:-/home/ubuntu/.hermes}
ADAPTER_ROOT=/opt/wechat-hermes-adapter
ADAPTER_RELEASES_ROOT=/opt/wechat-hermes-adapter-releases
HERMES_ROOT=/opt/hermes-runtime
HERMES_PYTHON_ROOT=/opt/hermes-python
HERMES_RELEASES_ROOT=/opt/hermes-runtime-releases
HERMES_PYTHON_RELEASES_ROOT=/opt/hermes-python-releases
STATE_ROOT=/var/lib/wechat-hermes
CONFIG_ROOT=/etc/wechat-hermes
CHAT_API_ROOT=/home/ubuntu/wechat-chat-api
BRIDGE_ROOT=/home/ubuntu/linux-wechat-bot
ADAPTER_USER=wechat-hermes
RUNTIME_USER=wechat-hermes-runner
RUNTIME_GROUP=wechat-hermes-runtime
ADAPTER_HOME=$STATE_ROOT/home
RUNTIME_HOME=$STATE_ROOT/workspace/home
ADAPTER_DATA_ROOT=$STATE_ROOT/adapter-data
SKILL_TRUST_ROOT=$STATE_ROOT/skill-trust
OUTBOUND_CONTROL_DB=$STATE_ROOT/outbound-control.db
RELEASE_ID=${RELEASE_ID:-$(date -u +%Y%m%d%H%M%S)}
MAINTENANCE_MODE=${MAINTENANCE_MODE:-0}
PIP_INDEX_URL=${PIP_INDEX_URL:-http://mirrors.tencentyun.com/pypi/simple}
PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST:-mirrors.tencentyun.com}
EXPECTED_WECHAT_PID=${EXPECTED_WECHAT_PID:-}
ALLOWED_ROOM_ID=${ALLOWED_ROOM_ID:-}
BOT_WXID=${BOT_WXID:-}
EXPECTED_DB_STATE_SHA256=${EXPECTED_DB_STATE_SHA256:-}
EXPECTED_SEND_STATE_SHA256=${EXPECTED_SEND_STATE_SHA256:-}
EXPECTED_BOT_DB_SHA256=${EXPECTED_BOT_DB_SHA256:-}
EXPECTED_DB_STATE_INODE=${EXPECTED_DB_STATE_INODE:-}
EXPECTED_SEND_STATE_INODE=${EXPECTED_SEND_STATE_INODE:-}
EXPECTED_BOT_DB_INODE=${EXPECTED_BOT_DB_INODE:-}
CHROME_FOR_TESTING_VERSION=${CHROME_FOR_TESTING_VERSION:-150.0.7871.115}
CHROME_FOR_TESTING_SIZE=${CHROME_FOR_TESTING_SIZE:-187332810}
CHROME_FOR_TESTING_SHA256=${CHROME_FOR_TESTING_SHA256:-1be2db033133c5e2dd1a4e8664bf67b19a61bcf6ed28d2b00f433b3f0b4f9585}
CHROME_FOR_TESTING_URL=${CHROME_FOR_TESTING_URL:-https://storage.googleapis.com/chrome-for-testing-public/$CHROME_FOR_TESTING_VERSION/linux64/chrome-linux64.zip}
CHROME_FOR_TESTING_MIRROR_URL=${CHROME_FOR_TESTING_MIRROR_URL:-https://npmmirror.com/mirrors/chrome-for-testing/$CHROME_FOR_TESTING_VERSION/linux64/chrome-linux64.zip}

fail() {
  printf 'install_cloud: %s\n' "$*" >&2
  exit 1
}

require_root() {
  [[ $(id -u) -eq 0 ]] || fail "must run as root"
}

require_deployment_identity() {
  [[ "$EXPECTED_WECHAT_PID" =~ ^[0-9]+$ ]] ||
    fail "EXPECTED_WECHAT_PID must be the active WeChat process ID"
  [[ "$ALLOWED_ROOM_ID" == *@chatroom ]] ||
    fail "ALLOWED_ROOM_ID must be a configured WeChat room ID"
  [[ "$BOT_WXID" == wxid_* ]] ||
    fail "BOT_WXID must be the bot account wxid"
}

assert_exact_path() {
  local expected=$1
  local actual
  actual=$(readlink -m -- "$2")
  [[ "$actual" == "$expected" ]] || fail "unsafe path: $actual"
}

assert_protected_file() {
  local path=$1
  local expected_hash=$2
  local expected_inode=$3
  local label=$4
  local actual_hash
  local actual_inode

  [[ "$expected_hash" =~ ^[0-9a-f]{64}$ ]] ||
    fail "$label baseline hash was not supplied"
  [[ -f "$path" ]] || fail "$label is missing"
  actual_hash=$(sha256sum "$path" | cut -d' ' -f1)
  [[ "$actual_hash" == "$expected_hash" ]] ||
    fail "$label changed before deployment"
  if [[ -n "$expected_inode" ]]; then
    actual_inode=$(stat -c '%d:%i' "$path")
    [[ "$actual_inode" == "$expected_inode" ]] ||
      fail "$label inode changed before deployment"
  fi
}

assert_baseline() {
  local pid
  pid=$(pgrep -x wechat || true)
  [[ "$pid" == "$EXPECTED_WECHAT_PID" ]] || {
    fail "WeChat PID changed: expected $EXPECTED_WECHAT_PID, got ${pid:-none}"
  }
  if [[ "$MAINTENANCE_MODE" != "1" ]]; then
    systemctl is-active --quiet linux-wechat-bridge.service ||
      fail "bridge service is not active"
    if ! systemctl is-active --quiet wechat-ai-bot.service; then
      systemctl is-active --quiet wechat-hermes-adapter.service ||
        fail "neither legacy nor Hermes Adapter service is active"
      systemctl is-active --quiet hermes-worker.service ||
        fail "Hermes Worker service is not active"
    fi
  fi
  systemctl is-active --quiet wechat-chat-api.service ||
    fail "Chat API service is not active"
  assert_protected_file \
    "$BRIDGE_ROOT/db-state.json" \
    "$EXPECTED_DB_STATE_SHA256" \
    "$EXPECTED_DB_STATE_INODE" \
    "db-state.json"
  assert_protected_file \
    "/home/ubuntu/.cache/wechat-chat-api/send-state.json" \
    "$EXPECTED_SEND_STATE_SHA256" \
    "$EXPECTED_SEND_STATE_INODE" \
    "send-state.json"
  assert_protected_file \
    "/opt/wechat-ai-bot/data/bot.db" \
    "$EXPECTED_BOT_DB_SHA256" \
    "$EXPECTED_BOT_DB_INODE" \
    "bot.db"
  if ! systemctl is-active --quiet hermes-worker.service &&
    ss -ltn | grep -Eq '127\.0\.0\.1:8642\b'; then
    fail "Hermes port 8642 is already in use"
  fi
  [[ -f "$SOURCE_ROOT/requirements.txt" ]] ||
    fail "release source is missing at $SOURCE_ROOT"
  [[ -f "$CHAT_API_SOURCE_ROOT/chat_api.py" ]] ||
    fail "Chat API release source is missing at $CHAT_API_SOURCE_ROOT"
  [[ -d "$HERMES_SOURCE/.git" && -x "$HERMES_SOURCE/venv/bin/hermes" ]] ||
    fail "Hermes source runtime is incomplete"
}

ensure_accounts_and_directories() {
  if ! getent group "$RUNTIME_GROUP" >/dev/null; then
    groupadd --system "$RUNTIME_GROUP"
  fi
  if ! getent passwd "$ADAPTER_USER" >/dev/null; then
    useradd \
      --system \
      --home-dir "$ADAPTER_HOME" \
      --shell /usr/sbin/nologin \
      "$ADAPTER_USER"
  fi
  usermod --append --groups "$RUNTIME_GROUP" "$ADAPTER_USER"
  if ! getent passwd "$RUNTIME_USER" >/dev/null; then
    useradd \
      --system \
      --gid "$RUNTIME_GROUP" \
      --home-dir "$RUNTIME_HOME" \
      --shell /usr/sbin/nologin \
      "$RUNTIME_USER"
  fi

  install -d -o root -g "$RUNTIME_GROUP" -m 1750 "$STATE_ROOT"
  install -d -o "$ADAPTER_USER" -g "$ADAPTER_USER" -m 0700 \
    "$ADAPTER_HOME" \
    "$ADAPTER_DATA_ROOT"
  install -d -o "$RUNTIME_USER" -g "$RUNTIME_GROUP" -m 0750 \
    "$STATE_ROOT/workspace"
  install -d -o "$RUNTIME_USER" -g "$RUNTIME_GROUP" -m 2770 \
    "$RUNTIME_HOME"
  install -d -o "$ADAPTER_USER" -g "$RUNTIME_GROUP" -m 2770 \
    "$STATE_ROOT/artifacts"
  install -d -o "$ADAPTER_USER" -g "$RUNTIME_GROUP" -m 2750 \
    "$SKILL_TRUST_ROOT" \
    "$SKILL_TRUST_ROOT/releases"
  install -d -o "$ADAPTER_USER" -g "$ADAPTER_USER" -m 0700 \
    "$SKILL_TRUST_ROOT/staging"
  install -d -o root -g root -m 0700 "$CONFIG_ROOT"
  install -d -o root -g root -m 0755 \
    "$ADAPTER_RELEASES_ROOT" \
    "$HERMES_RELEASES_ROOT" \
    "$HERMES_PYTHON_RELEASES_ROOT"
}

normalize_existing_artifact_permissions() {
  local task_dir
  local task_name
  for task_dir in "$STATE_ROOT"/artifacts/T-*; do
    [[ -d "$task_dir" && ! -L "$task_dir" ]] || continue
    task_name=$(basename -- "$task_dir")
    [[ "$task_name" =~ ^T-[A-F0-9]{8}$ ]] || continue
    find "$task_dir" -xdev -type d \
      -exec chgrp "$RUNTIME_GROUP" {} + \
      -exec chmod g+rwx,o-rwx {} +
    find "$task_dir" -xdev -type f \
      -exec chgrp "$RUNTIME_GROUP" {} + \
      -exec chmod g+rw,o-rwx {} +
  done
}

ensure_chat_api_control_store() {
  local legacy=/home/ubuntu/.cache/wechat-chat-api/outbound-control.db
  local journal=$OUTBOUND_CONTROL_DB-journal

  if ! command -v setfacl >/dev/null 2>&1; then
    env DEBIAN_FRONTEND=noninteractive apt-get update
    env DEBIAN_FRONTEND=noninteractive apt-get install -y acl
  fi
  command -v setfacl >/dev/null 2>&1 ||
    fail "setfacl is required for the isolated outbound control database"

  # SQLite must create and maintain its journal in the database directory.
  # The directory remains non-listable to ubuntu, and its sticky bit plus
  # systemd path masks protect every sibling component.
  chmod 1750 "$STATE_ROOT"
  setfacl -m u:ubuntu:-wx "$STATE_ROOT"
  if [[ ! -f "$OUTBOUND_CONTROL_DB" ]]; then
    OUTBOUND_CONTROL_DB="$OUTBOUND_CONTROL_DB" LEGACY_CONTROL_DB="$legacy" \
      python3 - <<'PY'
import os
import sqlite3
from pathlib import Path

target = Path(os.environ["OUTBOUND_CONTROL_DB"])
legacy = Path(os.environ["LEGACY_CONTROL_DB"])
temporary = target.with_name(target.name + ".next")
temporary.unlink(missing_ok=True)
source = sqlite3.connect(str(legacy if legacy.is_file() else ":memory:"))
destination = sqlite3.connect(str(temporary))
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
temporary.replace(target)
PY
  fi
  touch "$journal"
  chown ubuntu:ubuntu "$OUTBOUND_CONTROL_DB" "$journal"
  chmod 0600 "$OUTBOUND_CONTROL_DB" "$journal"
}

install_adapter() {
  local release_root="$ADAPTER_RELEASES_ROOT/$RELEASE_ID"
  local next_link="$ADAPTER_RELEASES_ROOT/.current-$RELEASE_ID"
  local previous_root

  assert_exact_path "$ADAPTER_RELEASES_ROOT/$RELEASE_ID" "$release_root"
  [[ ! -e "$release_root" ]] || fail "Adapter release already exists: $release_root"
  install -d -o root -g root -m 0755 "$release_root"

  rsync -a \
    --exclude '.venv' \
    --exclude '.pytest_cache' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    "$SOURCE_ROOT/" "$release_root/"

  python3 -m venv "$release_root/.venv"
  "$release_root/.venv/bin/python" -m pip install \
    --disable-pip-version-check \
    --index-url "$PIP_INDEX_URL" \
    --trusted-host "$PIP_TRUSTED_HOST" \
    -r "$release_root/requirements.txt" \
    -r "$release_root/requirements-mcp.txt"

  "$release_root/.venv/bin/python" -m compileall -q \
    "$release_root/app" \
    "$release_root/mcp_server.py" \
    "$release_root/cleanup.py"

  find "$release_root" -path "$release_root/.venv" -prune -o \
    -type d -exec chmod 0755 {} +
  find "$release_root" -path "$release_root/.venv" -prune -o \
    -type f -exec chmod 0644 {} +
  find "$release_root/.venv" -type d -exec chmod 0755 {} +
  find "$release_root/.venv" -type f -exec chmod go-w,u+rw {} +
  chmod 0755 "$release_root/deploy/install_cloud.sh"
  chown -R root:root "$release_root"

  ln -s "$release_root" "$next_link"
  if [[ -d "$ADAPTER_ROOT" && ! -L "$ADAPTER_ROOT" ]]; then
    previous_root="$ADAPTER_RELEASES_ROOT/legacy-$RELEASE_ID"
    mv -- "$ADAPTER_ROOT" "$previous_root"
  fi
  mv -Tf -- "$next_link" "$ADAPTER_ROOT"
}

install_hermes_runtime() {
  local resolved_python
  local python_source
  local release_root="$HERMES_RELEASES_ROOT/$RELEASE_ID"
  local release_python="$HERMES_PYTHON_RELEASES_ROOT/$RELEASE_ID"
  local next_root="$HERMES_RELEASES_ROOT/.next-$RELEASE_ID"
  local next_python="$HERMES_PYTHON_RELEASES_ROOT/.next-$RELEASE_ID"
  local next_root_link="$HERMES_RELEASES_ROOT/.current-$RELEASE_ID"
  local next_python_link="$HERMES_PYTHON_RELEASES_ROOT/.current-$RELEASE_ID"
  local previous_root
  local previous_python

  resolved_python=$(readlink -f "$HERMES_SOURCE/venv/bin/python")
  python_source=$(dirname "$(dirname "$resolved_python")")
  [[ -x "$python_source/bin/python3.11" ]] ||
    fail "Hermes Python runtime was not found"

  assert_exact_path "$HERMES_RELEASES_ROOT/.next-$RELEASE_ID" "$next_root"
  assert_exact_path "$HERMES_PYTHON_RELEASES_ROOT/.next-$RELEASE_ID" "$next_python"
  assert_exact_path "$HERMES_RELEASES_ROOT/$RELEASE_ID" "$release_root"
  assert_exact_path "$HERMES_PYTHON_RELEASES_ROOT/$RELEASE_ID" "$release_python"
  [[ ! -e "$release_root" ]] || fail "Hermes release already exists: $release_root"
  [[ ! -e "$release_python" ]] ||
    fail "Hermes Python release already exists: $release_python"
  [[ ! -e "$next_root" ]] || fail "$next_root already exists"
  [[ ! -e "$next_python" ]] || fail "$next_python already exists"

  cp -aL -- "$python_source" "$next_python"
  rsync -a "$HERMES_SOURCE/" "$next_root/"

  rm -- "$next_root/venv/bin/python"
  ln -s /opt/hermes-python/bin/python3.11 "$next_root/venv/bin/python"
  ln -sfn python "$next_root/venv/bin/python3"
  ln -sfn python "$next_root/venv/bin/python3.11"

  python3 - "$next_root" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
replacements = {
    b"/home/ubuntu/.hermes/hermes-agent": b"/opt/hermes-runtime",
    b"/home/ubuntu/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu":
        b"/opt/hermes-python",
    b"/home/ubuntu/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu":
        b"/opt/hermes-python",
}
for base in (root / "venv" / "bin", root / "venv" / "lib"):
    for path in base.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        updated = data
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        if updated != data:
            path.write_bytes(updated)
PY

  mv -- "$next_python" "$release_python"
  mv -- "$next_root" "$release_root"

  ln -s "$release_python" "$next_python_link"
  ln -s "$release_root" "$next_root_link"
  if [[ -d "$HERMES_PYTHON_ROOT" && ! -L "$HERMES_PYTHON_ROOT" ]]; then
    previous_python="$HERMES_PYTHON_RELEASES_ROOT/legacy-$RELEASE_ID"
    mv -- "$HERMES_PYTHON_ROOT" "$previous_python"
  fi
  mv -Tf -- "$next_python_link" "$HERMES_PYTHON_ROOT"
  if [[ -d "$HERMES_ROOT" && ! -L "$HERMES_ROOT" ]]; then
    previous_root="$HERMES_RELEASES_ROOT/legacy-$RELEASE_ID"
    mv -- "$HERMES_ROOT" "$previous_root"
  fi
  mv -Tf -- "$next_root_link" "$HERMES_ROOT"

  "$HERMES_ROOT/venv/bin/python" -m pip install \
    --disable-pip-version-check \
    --force-reinstall \
    --no-cache-dir \
    --index-url "$PIP_INDEX_URL" \
    --trusted-host "$PIP_TRUSTED_HOST" \
    charset-normalizer==3.4.4

  chown -R root:root "$HERMES_ROOT" "$HERMES_PYTHON_ROOT"
  find "$HERMES_ROOT" -type d -exec chmod a+rX {} +
  find "$HERMES_PYTHON_ROOT" -type d -exec chmod a+rX {} +

  sudo -u "$RUNTIME_USER" \
    env HOME="$RUNTIME_HOME" \
    "$HERMES_ROOT/venv/bin/python" -c \
    'import hermes_cli; print(hermes_cli.__version__)'
}

migrate_adapter_database() {
  local target="$ADAPTER_DATA_ROOT/adapter.db"
  local source=/var/lib/wechat-hermes/adapter.db

  if [[ -f "$CONFIG_ROOT/adapter.env" ]]; then
    source=$(
      awk -F= '
        $1 == "HERMES_WECHAT_DB_PATH" {
          print substr($0, index($0, "=") + 1)
          exit
        }
      ' "$CONFIG_ROOT/adapter.env"
    )
    source=${source:-/var/lib/wechat-hermes/adapter.db}
  fi
  [[ "$source" == "$target" || ! -e "$target" ]] ||
    fail "Adapter database target already exists with a different source"
  if [[ "$source" != "$target" && -f "$source" ]]; then
    python3 - "$source" "$target" <<'PY'
from pathlib import Path
import sqlite3
import sys

source = Path(sys.argv[1]).resolve(strict=True)
target = Path(sys.argv[2]).resolve(strict=False)
target.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(str(source), timeout=30) as src:
    src.execute("PRAGMA busy_timeout=30000")
    with sqlite3.connect(str(target), timeout=30) as dst:
        src.backup(dst)
        violations = dst.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SystemExit("Adapter database backup failed foreign-key validation")
PY
  fi
  if [[ -f "$target" ]]; then
    chown "$ADAPTER_USER:$ADAPTER_USER" "$target"
    chmod 0600 "$target"
  fi
}

install_chat_api_components() {
  local source
  local destination
  local backup

  for source in chat_api.py; do
    destination="$CHAT_API_ROOT/$source"
    backup="$destination.previous-$RELEASE_ID"
    install -o ubuntu -g ubuntu -m 0644 \
      "$CHAT_API_SOURCE_ROOT/$source" "$destination.next-$RELEASE_ID"
    if [[ -f "$destination" ]]; then
      cp -a -- "$destination" "$backup"
    fi
    mv -f -- "$destination.next-$RELEASE_ID" "$destination"
  done

  for source in db_bridge.py; do
    destination="$BRIDGE_ROOT/$source"
    backup="$destination.previous-$RELEASE_ID"
    install -o ubuntu -g ubuntu -m 0644 \
      "$CHAT_API_SOURCE_ROOT/$source" "$destination.next-$RELEASE_ID"
    if [[ -f "$destination" ]]; then
      cp -a -- "$destination" "$backup"
    fi
    mv -f -- "$destination.next-$RELEASE_ID" "$destination"
  done

  python3 -m py_compile \
    "$CHAT_API_ROOT/chat_api.py" \
    "$BRIDGE_ROOT/db_bridge.py"
  python3 - "$CHAT_API_ROOT/config.json" <<'PY'
from pathlib import Path
import json
import sys

path = Path(sys.argv[1])
config = json.loads(path.read_text(encoding="utf-8"))
config["outbound_control_db"] = "/var/lib/wechat-hermes/outbound-control.db"
config["outbound_auth_token_env"] = "WECHAT_CHAT_API_TOKEN"
temporary = path.with_name(path.name + ".next")
temporary.write_text(
    json.dumps(config, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
temporary.chmod(0o600)
temporary.replace(path)
PY
  chown ubuntu:ubuntu "$CHAT_API_ROOT/config.json"
  chmod 0600 "$CHAT_API_ROOT/config.json"
}

install_skill_sandbox_dependency() {
  if command -v bwrap >/dev/null 2>&1; then
    return
  fi
  env DEBIAN_FRONTEND=noninteractive apt-get update
  env DEBIAN_FRONTEND=noninteractive apt-get install -y bubblewrap
  command -v bwrap >/dev/null 2>&1 ||
    fail "bubblewrap installation did not provide bwrap"
}

harden_hermes_logging() {
  python3 \
    "$ADAPTER_ROOT/deploy/harden_hermes_logging.py" \
    --root "$HERMES_ROOT"
}

harden_hermes_api_scopes() {
  python3 \
    "$ADAPTER_ROOT/deploy/harden_hermes_api_scopes.py" \
    --root "$HERMES_ROOT"
}

harden_hermes_run_evidence() {
  python3 \
    "$ADAPTER_ROOT/deploy/harden_hermes_run_evidence.py" \
    --root "$HERMES_ROOT"
}

harden_hermes_skill_reload() {
  python3 \
    "$ADAPTER_ROOT/deploy/harden_hermes_skill_reload.py" \
    --root "$HERMES_ROOT"
}

install_hermes_home() {
  local home="$RUNTIME_HOME"
  local hermes_home="$home/.hermes"
  local stage_root="$SKILL_TRUST_ROOT/staging/bootstrap-$RELEASE_ID"
  local stage_home="$stage_root/home"
  local stage_hermes="$stage_home/.hermes"
  local stage_skills="$stage_hermes/skills"
  local source_skills
  local config_source
  local release_name="release-$RELEASE_ID"
  local pending_release="$SKILL_TRUST_ROOT/releases/.pending-$release_name"
  local release="$SKILL_TRUST_ROOT/releases/$release_name"
  local next_link="$SKILL_TRUST_ROOT/.current-$RELEASE_ID"
  local active_link="$SKILL_TRUST_ROOT/active"
  local active_target

  assert_exact_path \
    "$SKILL_TRUST_ROOT/staging/bootstrap-$RELEASE_ID" \
    "$stage_root"
  assert_exact_path \
    "$SKILL_TRUST_ROOT/releases/.pending-$release_name" \
    "$pending_release"
  [[ ! -e "$stage_root" ]] || fail "Hermes home stage already exists"
  [[ ! -e "$pending_release" && ! -e "$release" ]] ||
    fail "Hermes Skill release already exists"

  install -d -o "$RUNTIME_USER" -g "$RUNTIME_GROUP" -m 2770 "$hermes_home"
  install -d -o "$ADAPTER_USER" -g "$ADAPTER_USER" -m 0700 \
    "$stage_root" \
    "$stage_home" \
    "$stage_hermes" \
    "$stage_skills"

  if [[ -d "$SKILL_TRUST_ROOT/current/skills" ]]; then
    source_skills="$SKILL_TRUST_ROOT/current/skills"
  elif [[ -d "$ADAPTER_HOME/.hermes/skills" ]]; then
    source_skills="$ADAPTER_HOME/.hermes/skills"
  else
    source_skills="$HERMES_HOME_SOURCE/skills"
  fi
  rsync -aL "$source_skills/" "$stage_skills/"

  install -d -o "$ADAPTER_USER" -g "$ADAPTER_USER" -m 0755 \
    "$stage_skills/media" \
    "$stage_skills/productivity"
  rsync -a --delete \
    "$ADAPTER_ROOT/skills/douyin-video-production/" \
    "$stage_skills/media/douyin-video-production/"
  rsync -a --delete \
    "$ADAPTER_ROOT/skills/wechat-group-operations/" \
    "$stage_skills/productivity/wechat-group-operations/"

  if [[ -f "$hermes_home/config.yaml" ]]; then
    config_source="$hermes_home/config.yaml"
  elif [[ -f "$ADAPTER_HOME/.hermes/config.yaml" ]]; then
    config_source="$ADAPTER_HOME/.hermes/config.yaml"
  else
    config_source="$HERMES_HOME_SOURCE/config.yaml"
  fi
  python3 - "$config_source" "$stage_hermes/config.yaml" <<'PY'
from pathlib import Path
import sys
import yaml

source = Path(sys.argv[1])
target = Path(sys.argv[2])
config = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
approvals = config.setdefault("approvals", {})
approvals["mode"] = "off"
approvals["cron_mode"] = "off"
config.setdefault("memory", {})["memory_enabled"] = False
config.setdefault("model", {})["context_length"] = 128000
compression = config.setdefault("compression", {})
compression["threshold"] = 0.75
compression["target_ratio"] = 0.20
compression["protect_first_n"] = 0
compression["protect_last_n"] = 16
compression["in_place"] = True
agent = config.setdefault("agent", {})
disabled_toolsets = agent.get("disabled_toolsets")
if not isinstance(disabled_toolsets, list):
    disabled_toolsets = []
if "memory" not in disabled_toolsets:
    disabled_toolsets.append("memory")
agent["disabled_toolsets"] = disabled_toolsets
config.setdefault("gateway", {}).setdefault("api_server", {})[
    "max_concurrent_runs"
] = 1
config.setdefault("mcp_servers", {})["wechat-production"] = {
    "command": "/opt/wechat-hermes-adapter/.venv/bin/python",
    "args": ["/opt/wechat-hermes-adapter/mcp_server.py"],
    "env": {
        "HERMES_WECHAT_INTERNAL_TOKEN": "${HERMES_WECHAT_INTERNAL_TOKEN}",
        "HERMES_WECHAT_ADAPTER_URL": "${HERMES_WECHAT_ADAPTER_URL}",
        "HERMES_WECHAT_ARTIFACT_ROOT": "${HERMES_WECHAT_ARTIFACT_ROOT}",
        "HERMES_WECHAT_MAX_ARTIFACT_BYTES": (
            "${HERMES_WECHAT_MAX_ARTIFACT_BYTES}"
        ),
        "HERMES_WECHAT_MAX_IMAGE_BYTES": "${HERMES_WECHAT_MAX_IMAGE_BYTES}",
        "HERMES_WECHAT_MAX_HTTP_FETCH_BYTES": (
            "${HERMES_WECHAT_MAX_HTTP_FETCH_BYTES}"
        ),
        "HERMES_WECHAT_MAX_TEXT_ARTIFACT_BYTES": (
            "${HERMES_WECHAT_MAX_TEXT_ARTIFACT_BYTES}"
        ),
        "HERMES_WECHAT_MAX_ARCHIVE_FILES": (
            "${HERMES_WECHAT_MAX_ARCHIVE_FILES}"
        ),
        "HERMES_WECHAT_MAX_ARCHIVE_SOURCE_BYTES": (
            "${HERMES_WECHAT_MAX_ARCHIVE_SOURCE_BYTES}"
        ),
    },
    "enabled": True,
}
target.write_text(
    yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
    encoding="utf-8",
)
PY

  if rg -n -i -F \
    -e 'jianying' \
    -e '剪映' \
    -e 'workstation' \
    -e '18766' \
    -e 'powershell' \
    -e 'c:\' \
    -e 'd:\' \
    -e 'e:\' \
    "$stage_skills/media/douyin-video-production" \
    "$stage_skills/productivity/wechat-group-operations"; then
    fail "custom Skill cloud-only scan failed"
  fi

  chown -R "$ADAPTER_USER:$ADAPTER_USER" "$stage_root"
  sudo -u "$ADAPTER_USER" env HOME="$stage_home" \
    git config --global --add safe.directory "$HERMES_ROOT"
  sudo -u "$ADAPTER_USER" env \
    HOME="$stage_home" \
    HERMES_HOME="$stage_hermes" \
    "$HERMES_ROOT/venv/bin/hermes" curator pin wechat-group-operations
  sudo -u "$ADAPTER_USER" env \
    HOME="$stage_home" \
    HERMES_HOME="$stage_hermes" \
    "$HERMES_ROOT/venv/bin/hermes" curator pin douyin-video-production

  "$HERMES_ROOT/venv/bin/python" \
    "$ADAPTER_ROOT/scripts/generate_skills_lock.py" \
    --hermes-repo "$HERMES_ROOT" \
    --skills-root "$stage_skills" \
    --hub-lock "$stage_skills/.hub/lock.json" \
    --output "$stage_hermes/skills-lock.json"

  install -d -o "$ADAPTER_USER" -g "$RUNTIME_GROUP" -m 0750 \
    "$pending_release"
  mv -- "$stage_skills" "$pending_release/skills"
  mv -- "$stage_hermes/skills-lock.json" "$pending_release/skills-lock.json"
  chown -R "$ADAPTER_USER:$RUNTIME_GROUP" "$pending_release"
  find "$pending_release" -type d -exec chmod 0550 {} +
  find "$pending_release" -type f -perm /0111 -exec chmod 0550 {} +
  find "$pending_release" -type f ! -perm /0111 -exec chmod 0440 {} +
  mv -- "$pending_release" "$release"

  ln -s "releases/$release_name" "$next_link"
  mv -Tf -- "$next_link" "$SKILL_TRUST_ROOT/current"

  if [[ -L "$active_link" ]]; then
    active_target=$(readlink "$active_link")
    [[ "$active_target" == releases/* ]] ||
      fail "Hermes Skill active pointer is invalid"
    [[ -d "$SKILL_TRUST_ROOT/$active_target/skills" ]] ||
      fail "Hermes Skill active release is missing"
    [[ -f "$SKILL_TRUST_ROOT/$active_target/skills-lock.json" ]] ||
      fail "Hermes Skill active release lock is missing"
  elif [[ -e "$active_link" ]]; then
    fail "Hermes Skill active pointer is not a symlink"
  else
    ln -s "releases/$release_name" "$active_link"
  fi

  for name in skills skills-lock.json; do
    if [[ -e "$hermes_home/$name" && ! -L "$hermes_home/$name" ]]; then
      mv -- "$hermes_home/$name" \
        "$hermes_home/$name.previous-$RELEASE_ID"
    fi
  done
  ln -sfn "$SKILL_TRUST_ROOT/active/skills" "$hermes_home/skills"
  ln -sfn \
    "$SKILL_TRUST_ROOT/active/skills-lock.json" \
    "$hermes_home/skills-lock.json"
  sudo -u "$RUNTIME_USER" test -r \
    "$SKILL_TRUST_ROOT/active/skills-lock.json" ||
    fail "Hermes runtime cannot read the active Skill lock"
  sudo -u "$RUNTIME_USER" test -r \
    "$SKILL_TRUST_ROOT/active/skills/creative/creative-ideation/SKILL.md" ||
    fail "Hermes runtime cannot read creative-ideation"
  sudo -u "$RUNTIME_USER" test -r \
    "$SKILL_TRUST_ROOT/active/skills/media/douyin-video-production/SKILL.md" ||
    fail "Hermes runtime cannot read douyin-video-production"
  sudo -u "$RUNTIME_USER" test -r \
    "$SKILL_TRUST_ROOT/active/skills/productivity/wechat-group-operations/SKILL.md" ||
    fail "Hermes runtime cannot read wechat-group-operations"
  install -o "$RUNTIME_USER" -g "$RUNTIME_GROUP" -m 0640 \
    "$stage_hermes/config.yaml" "$hermes_home/config.yaml"
  chown "$RUNTIME_USER:$RUNTIME_GROUP" "$hermes_home"
  chmod 2770 "$hermes_home"
  rm -rf -- "$stage_root"

  sudo -u "$RUNTIME_USER" env HOME="$home" \
    git config --global --add safe.directory "$HERMES_ROOT"

  for directory in "$hermes_home/logs" "$hermes_home/sessions"; do
    if [[ -d "$directory" ]]; then
      find "$directory" -type f -exec chmod 0600 {} +
    fi
  done
}

cloud_browser_executable() {
  printf '%s\n' \
    "$STATE_ROOT/chrome-for-testing/$CHROME_FOR_TESTING_VERSION/chrome-linux64/chrome"
}

verify_cloud_browser_archive() {
  local archive=$1
  local actual_size
  local actual_sha256

  [[ -f "$archive" ]] || return 1
  actual_size=$(stat -c '%s' "$archive")
  [[ "$actual_size" == "$CHROME_FOR_TESTING_SIZE" ]] || return 1
  actual_sha256=$(sha256sum "$archive" | cut -d' ' -f1)
  [[ "$actual_sha256" == "$CHROME_FOR_TESTING_SHA256" ]] || return 1
  unzip -tq "$archive" >/dev/null
}

download_cloud_browser_archive() {
  local archive=$1
  local url

  if verify_cloud_browser_archive "$archive"; then
    return
  fi

  for url in \
    "$CHROME_FOR_TESTING_URL" \
    "$CHROME_FOR_TESTING_MIRROR_URL"; do
    if curl \
      --fail \
      --location \
      --continue-at - \
      --retry 5 \
      --retry-delay 3 \
      --retry-all-errors \
      --connect-timeout 20 \
      --output "$archive" \
      "$url"; then
      if verify_cloud_browser_archive "$archive"; then
        return
      fi
      mv -- "$archive" "$archive.rejected.$(date +%s)"
    fi
  done

  fail "Chrome for Testing download or integrity verification failed"
}

install_cloud_browser_dependencies() {
  local -a time_abi_packages

  env DEBIAN_FRONTEND=noninteractive apt-get update
  if apt-cache show libasound2t64 >/dev/null 2>&1; then
    time_abi_packages=(
      libasound2t64
      libatk-bridge2.0-0t64
      libatk1.0-0t64
      libcups2t64
      libglib2.0-0t64
      libgtk-3-0t64
    )
  else
    time_abi_packages=(
      libasound2
      libatk-bridge2.0-0
      libatk1.0-0
      libcups2
      libglib2.0-0
      libgtk-3-0
    )
  fi

  env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates \
    curl \
    fonts-liberation \
    fonts-noto-color-emoji \
    libcairo2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libu2f-udev \
    libvulkan1 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    unzip \
    xdg-utils \
    "${time_abi_packages[@]}"
}

install_cloud_browser() {
  local home="$RUNTIME_HOME"
  local hermes_home="$home/.hermes"
  local node_bin="$hermes_home/node/bin"
  local browser_bin="$hermes_home/node_modules/.bin/agent-browser"
  local browser_cache="$home/.cache/ms-playwright"
  local browser_root="$STATE_ROOT/chrome-for-testing"
  local download_root="$STATE_ROOT/browser-downloads"
  local archive="$download_root/chrome-linux64-$CHROME_FOR_TESTING_VERSION.zip.part"
  local install_root="$browser_root/$CHROME_FOR_TESTING_VERSION"
  local next_root="$browser_root/.next-$CHROME_FOR_TESTING_VERSION"
  local browser_executable

  browser_executable=$(cloud_browser_executable)

  sudo -u "$RUNTIME_USER" env \
    HOME="$home" \
    HERMES_HOME="$hermes_home" \
    bash -c \
    "source '$HERMES_ROOT/scripts/lib/node-bootstrap.sh'; ensure_node"

  sudo -u "$RUNTIME_USER" env \
    HOME="$home" \
    HERMES_HOME="$hermes_home" \
    PATH="$node_bin:/usr/local/bin:/usr/bin:/bin" \
    "$node_bin/npm" install \
    --prefix "$hermes_home" \
    --save-exact \
    --no-audit \
    --no-fund \
    agent-browser@0.26.0

  install_cloud_browser_dependencies
  install -d -o "$RUNTIME_USER" -g "$RUNTIME_GROUP" -m 0700 \
    "$browser_cache" \
    "$browser_root" \
    "$download_root"

  if [[ ! -x "$browser_executable" ]] ||
    ! "$browser_executable" --version |
      grep -Fq "$CHROME_FOR_TESTING_VERSION"; then
    download_cloud_browser_archive "$archive"
    assert_exact_path \
      "$STATE_ROOT/chrome-for-testing/.next-$CHROME_FOR_TESTING_VERSION" \
      "$next_root"
    if [[ -e "$next_root" ]]; then
      rm -rf -- "$next_root"
    fi
    install -d -o "$RUNTIME_USER" -g "$RUNTIME_GROUP" -m 0700 "$next_root"
    unzip -q "$archive" -d "$next_root"
    [[ -x "$next_root/chrome-linux64/chrome" ]] ||
      fail "Chrome executable is missing from the verified archive"
    "$next_root/chrome-linux64/chrome" --version |
      grep -Fq "$CHROME_FOR_TESTING_VERSION" ||
      fail "Chrome executable version does not match the pinned version"
    chown -R "$RUNTIME_USER:$RUNTIME_GROUP" "$next_root"
    if [[ -e "$install_root" ]]; then
      mv -- "$install_root" "$install_root.replaced.$(date +%s)"
    fi
    mv -- "$next_root" "$install_root"
  fi

  chown -R "$RUNTIME_USER:$RUNTIME_GROUP" \
    "$hermes_home/node" \
    "$hermes_home/node_modules" \
    "$home/.cache" \
    "$browser_root" \
    "$download_root"

  sudo -u "$RUNTIME_USER" env \
    HOME="$home" \
    HERMES_HOME="$hermes_home" \
    PATH="$node_bin:/usr/local/bin:/usr/bin:/bin" \
    PLAYWRIGHT_BROWSERS_PATH="$browser_cache" \
    AGENT_BROWSER_EXECUTABLE_PATH="$browser_executable" \
    "$browser_bin" --version
  sudo -u "$RUNTIME_USER" "$browser_executable" --version
}

update_cloud_browser_environment() {
  local environment_file="$CONFIG_ROOT/hermes.env"
  local browser_executable

  browser_executable=$(cloud_browser_executable)
  [[ -f "$environment_file" ]] ||
    fail "Hermes environment file is missing: $environment_file"
  python3 - \
    "$environment_file" \
    "PLAYWRIGHT_BROWSERS_PATH=$RUNTIME_HOME/.cache/ms-playwright" \
    "AGENT_BROWSER_EXECUTABLE_PATH=$browser_executable" \
    "AGENT_BROWSER_ARGS=--no-sandbox,--disable-dev-shm-usage" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
updates = dict(item.split("=", 1) for item in sys.argv[2:])
lines = path.read_text(encoding="utf-8").splitlines()
updated = []
seen = set()
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in updates:
        if key not in seen:
            updated.append(f"{key}={updates[key]}")
            seen.add(key)
        continue
    updated.append(line)
for key, value in updates.items():
    if key not in seen:
        updated.append(f"{key}={value}")
path.write_text("\n".join(updated) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
}

write_environment() {
  ALLOWED_ROOM_ID="$ALLOWED_ROOM_ID" \
    BOT_WXID="$BOT_WXID" \
    CHROME_FOR_TESTING_VERSION="$CHROME_FOR_TESTING_VERSION" \
    python3 - "$CONFIG_ROOT" <<'PY'
from pathlib import Path
import json
import os
import secrets
import sys

root = Path(sys.argv[1])


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key:
            values[key] = value
    return values


def choose_distinct(
    existing: str,
    *,
    excluded: set[str],
) -> str:
    value = str(existing or "").strip()
    if len(value) >= 32 and value not in excluded:
        return value
    while True:
        value = secrets.token_urlsafe(48)
        if value not in excluded:
            return value


def write_env(path: Path, values: dict[str, str]) -> None:
    temporary = path.with_name(path.name + ".next")
    temporary.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


legacy_bridge_config = Path("/home/ubuntu/linux-wechat-bot/config.json")
bridge_config = (
    json.loads(legacy_bridge_config.read_text(encoding="utf-8"))
    if legacy_bridge_config.is_file()
    else {}
)

adapter_path = root / "adapter.env"
hermes_path = root / "hermes.env"
chat_api_path = root / "chat-api.env"
bridge_path = root / "bridge.env"
adapter = read_env(adapter_path)
hermes = read_env(hermes_path)
chat_api = read_env(chat_api_path)
bridge = read_env(bridge_path)

legacy_bridge_token = str(bridge_config.get("bridge_token") or "").strip()
environment_bridge_token = str(bridge.get("BRIDGE_TOKEN") or "").strip()
if (
    legacy_bridge_token
    and environment_bridge_token
    and legacy_bridge_token != environment_bridge_token
):
    raise SystemExit("bridge token differs between legacy config and bridge.env")
bridge_token = legacy_bridge_token or environment_bridge_token
if len(bridge_token) < 32:
    raise SystemExit("existing bridge token is missing or too short")
api_key = choose_distinct(
    adapter.get("HERMES_API_KEY") or hermes.get("API_SERVER_KEY", ""),
    excluded={bridge_token},
)
internal_token = choose_distinct(
    adapter.get("HERMES_WECHAT_INTERNAL_TOKEN")
    or hermes.get("HERMES_WECHAT_INTERNAL_TOKEN", ""),
    excluded={bridge_token, api_key},
)
chat_api_token = choose_distinct(
    adapter.get("WECHAT_CHAT_API_TOKEN")
    or chat_api.get("WECHAT_CHAT_API_TOKEN", ""),
    excluded={bridge_token, api_key, internal_token},
)

adapter.update({
    "BRIDGE_TOKEN": bridge_token,
    "HERMES_WECHAT_INTERNAL_TOKEN": internal_token,
    "WECHAT_CHAT_API_TOKEN": chat_api_token,
    "HERMES_BASE_URL": "http://127.0.0.1:8642",
    "HERMES_API_KEY": api_key,
    "WECHAT_CHAT_API_URL": "http://127.0.0.1:8765",
    "ALLOWED_WECHAT_ROOM_IDS": os.environ["ALLOWED_ROOM_ID"],
    "WECHAT_BOT_WXID": os.environ["BOT_WXID"],
    "HERMES_WECHAT_DB_PATH": "/var/lib/wechat-hermes/adapter-data/adapter.db",
    "HERMES_WECHAT_ARTIFACT_ROOT": "/var/lib/wechat-hermes/artifacts",
    "HERMES_WECHAT_ARTIFACT_BASE_URL": "http://127.0.0.1:8000",
    "HERMES_WECHAT_PORT": "8000",
    "HERMES_WECHAT_MAX_ARTIFACT_BYTES": "1073741824",
    "HERMES_WECHAT_MAX_IMAGE_BYTES": "20971520",
    "HERMES_WECHAT_MAX_TASK_SECONDS": "1800",
    "HERMES_WECHAT_MAX_TASK_ATTEMPTS": "3",
    "HERMES_WECHAT_DAILY_COST_LIMIT_USD": "20",
    "HERMES_WECHAT_DAILY_TOKEN_LIMIT": "10000000",
    "HERMES_WECHAT_BUDGET_TIMEZONE": "Asia/Shanghai",
    "HERMES_INPUT_TOKEN_COST_PER_MILLION": "3",
    "HERMES_OUTPUT_TOKEN_COST_PER_MILLION": "15",
    "HERMES_WECHAT_SESSION_GENERATION": "2",
    "HERMES_CLI_PATH": "/opt/hermes-runtime/venv/bin/hermes",
    "HERMES_HOME": "/var/lib/wechat-hermes/workspace/home",
    "HERMES_SKILL_TRUST_ROOT": "/var/lib/wechat-hermes/skill-trust",
    "HERMES_SKILL_SANDBOX": "/usr/bin/bwrap",
    "HERMES_WECHAT_SKILL_INSTALL_TIMEOUT_SECONDS": "300",
    "ALLOW_PRIVATE_WECHAT_CHAT": "false",
    "HERMES_WECHAT_WORKER_POLL_SECONDS": "1",
    "HERMES_WECHAT_ADAPTER_URL": "http://127.0.0.1:8000",
})
for key, value in {
    "HERMES_WECHAT_SYNC_TIMEOUT_SECONDS": "8",
    "HERMES_WECHAT_MAX_TOOL_CALLS": "80",
    "HERMES_WECHAT_MAX_ARTIFACT_COUNT": "10",
    "HERMES_WECHAT_MAX_ARTIFACT_TOTAL_BYTES": "524288000",
    "HERMES_WECHAT_MAX_DOWNLOAD_BYTES": "1073741824",
    "HERMES_WECHAT_MAX_DELIVERY_MEDIA_ITEMS": "3",
    "HERMES_WECHAT_ARTIFACT_RETENTION_DAYS": "7",
    "HERMES_WECHAT_AUDIT_RETENTION_DAYS": "30",
    "HERMES_WECHAT_CLEANUP_STATUS_PATH": (
        "/var/lib/wechat-hermes/adapter-data/cleanup-status.json"
    ),
    "HERMES_WECHAT_CLEANUP_MAX_AGE_SECONDS": "172800",
}.items():
    adapter.setdefault(key, value)

hermes.update({
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": "127.0.0.1",
    "API_SERVER_PORT": "8642",
    "API_SERVER_KEY": api_key,
    "HERMES_WECHAT_INTERNAL_TOKEN": internal_token,
    "HERMES_WECHAT_ADAPTER_URL": "http://127.0.0.1:8000",
    "HERMES_WECHAT_ARTIFACT_ROOT": "/var/lib/wechat-hermes/artifacts",
    "HERMES_WECHAT_MAX_ARTIFACT_BYTES": "1073741824",
    "HERMES_WECHAT_MAX_IMAGE_BYTES": "20971520",
    "HERMES_WECHAT_MAX_HTTP_FETCH_BYTES": "26214400",
    "HERMES_WECHAT_MAX_TEXT_ARTIFACT_BYTES": "4194304",
    "HERMES_WECHAT_MAX_ARCHIVE_FILES": "5000",
    "HERMES_WECHAT_MAX_ARCHIVE_SOURCE_BYTES": "524288000",
    "PLAYWRIGHT_BROWSERS_PATH": (
        "/var/lib/wechat-hermes/workspace/home/.cache/ms-playwright"
    ),
    "AGENT_BROWSER_EXECUTABLE_PATH": (
        "/var/lib/wechat-hermes/chrome-for-testing/"
        f"{os.environ['CHROME_FOR_TESTING_VERSION']}/chrome-linux64/chrome"
    ),
    "AGENT_BROWSER_ARGS": "--no-sandbox,--disable-dev-shm-usage",
})
for forbidden in (
    "BRIDGE_TOKEN",
    "WECHAT_CHAT_API_TOKEN",
    "HERMES_API_KEY",
):
    hermes.pop(forbidden, None)

chat_api["WECHAT_CHAT_API_TOKEN"] = chat_api_token
for forbidden in (
    "BRIDGE_TOKEN",
    "HERMES_WECHAT_INTERNAL_TOKEN",
    "HERMES_API_KEY",
    "API_SERVER_KEY",
):
    chat_api.pop(forbidden, None)

bridge.update({
    "BRIDGE_TOKEN": bridge_token,
    "WECHAT_CHAT_API_TOKEN": chat_api_token,
    "HERMES_WECHAT_ADAPTER_URL": "http://127.0.0.1:8000",
    "HERMES_WECHAT_ADAPTER_TIMEOUT_SECONDS": "210",
    "WECHAT_BOT_WXID": os.environ["BOT_WXID"],
})
for forbidden in (
    "HERMES_WECHAT_INTERNAL_TOKEN",
    "HERMES_API_KEY",
    "API_SERVER_KEY",
):
    bridge.pop(forbidden, None)

credentials = {
    bridge_token,
    internal_token,
    chat_api_token,
    api_key,
}
if len(credentials) != 4:
    raise SystemExit("production credentials are not pairwise distinct")

write_env(adapter_path, adapter)
write_env(hermes_path, hermes)
write_env(chat_api_path, chat_api)
write_env(bridge_path, bridge)
PY
}

install_services() {
  install -o root -g root -m 0644 \
    "$ADAPTER_ROOT/deploy/hermes-worker.service" \
    /etc/systemd/system/hermes-worker.service
  install -o root -g root -m 0644 \
    "$ADAPTER_ROOT/deploy/wechat-hermes-adapter.service" \
    /etc/systemd/system/wechat-hermes-adapter.service
  install -o root -g root -m 0644 \
    "$ADAPTER_ROOT/deploy/wechat-hermes-cleanup.service" \
    /etc/systemd/system/wechat-hermes-cleanup.service
  install -o root -g root -m 0644 \
    "$ADAPTER_ROOT/deploy/wechat-hermes-cleanup.timer" \
    /etc/systemd/system/wechat-hermes-cleanup.timer
  install -o root -g root -m 0644 \
    "$ADAPTER_ROOT/deploy/wechat-hermes.logrotate" \
    /etc/logrotate.d/wechat-hermes
  install -o root -g root -m 0644 \
    "$CHAT_API_SOURCE_ROOT/wechat-chat-api.service" \
    /etc/systemd/system/wechat-chat-api.service
  install -o root -g root -m 0644 \
    "$CHAT_API_SOURCE_ROOT/linux-wechat-bridge.service" \
    /etc/systemd/system/linux-wechat-bridge.service
  systemctl daemon-reload
}

main() {
  require_root
  if [[ "${1:-}" == "--browser-only" ]]; then
    [[ $# -eq 1 ]] || fail "usage: install_cloud.sh --browser-only"
    ensure_accounts_and_directories
    install_cloud_browser
    update_cloud_browser_environment
    printf 'cloud browser installation completed; services were not restarted\n'
    return
  fi
  [[ $# -eq 0 ]] || fail "unknown install option: $1"
  require_deployment_identity
  assert_baseline
  ensure_accounts_and_directories
  normalize_existing_artifact_permissions
  ensure_chat_api_control_store
  migrate_adapter_database
  install_adapter
  install_skill_sandbox_dependency
  install_hermes_runtime
  harden_hermes_logging
  harden_hermes_api_scopes
  harden_hermes_run_evidence
  harden_hermes_skill_reload
  install_hermes_home
  install_cloud_browser
  write_environment
  install_chat_api_components
  install_services
  printf 'cloud installation completed; services were not started\n'
}

main "$@"

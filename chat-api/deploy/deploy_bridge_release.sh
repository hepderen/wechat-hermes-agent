#!/usr/bin/env bash
set -euo pipefail

# Upgrade only the structured Bridge after its paired Adapter release is ready.
: "${SOURCE_ROOT:?SOURCE_ROOT is required}"
: "${RELEASE_ID:?RELEASE_ID is required}"
: "${EXPECTED_SOURCE_COMMIT:?EXPECTED_SOURCE_COMMIT is required}"
: "${EXPECTED_WECHAT_PID:?EXPECTED_WECHAT_PID is required}"
: "${EXPECTED_DB_STATE_SHA256:?EXPECTED_DB_STATE_SHA256 is required}"
: "${EXPECTED_SEND_STATE_SHA256:?EXPECTED_SEND_STATE_SHA256 is required}"
: "${EXPECTED_BOT_DB_SHA256:?EXPECTED_BOT_DB_SHA256 is required}"
: "${EXPECTED_DB_STATE_INODE:?EXPECTED_DB_STATE_INODE is required}"
: "${EXPECTED_SEND_STATE_INODE:?EXPECTED_SEND_STATE_INODE is required}"
: "${EXPECTED_BOT_DB_INODE:?EXPECTED_BOT_DB_INODE is required}"

BRIDGE_ROOT=/home/ubuntu/linux-wechat-bot
BRIDGE_FILE=$BRIDGE_ROOT/db_bridge.py
BRIDGE_ENV=/etc/wechat-hermes/bridge.env
CACHE_ROOT=/var/tmp/wechat-hermes-bridge-pyc-$RELEASE_ID

fail() {
  printf 'deploy_bridge_release: %s\n' "$*" >&2
  exit 1
}

assert_protected_file() {
  local path=$1
  local expected_hash=$2
  local expected_inode=$3
  local label=$4
  local actual_hash
  local actual_inode

  [[ -f "$path" ]] || fail "$label is missing"
  actual_hash=$(sha256sum "$path" | cut -d' ' -f1)
  [[ "$actual_hash" == "$expected_hash" ]] ||
    fail "$label changed before deployment"
  actual_inode=$(stat -c '%d:%i' "$path")
  [[ "$actual_inode" == "$expected_inode" ]] ||
    fail "$label inode changed before deployment"
}

assert_baseline() {
  local pid

  [[ $(id -u) -eq 0 ]] || fail "must run as root"
  [[ "$RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] ||
    fail "invalid release ID"
  [[ "$EXPECTED_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] ||
    fail "invalid expected source commit"
  pid=$(pgrep -x wechat || true)
  [[ "$pid" == "$EXPECTED_WECHAT_PID" ]] ||
    fail "WeChat PID changed: expected $EXPECTED_WECHAT_PID, got ${pid:-none}"
  systemctl is-active --quiet linux-wechat-bridge.service ||
    fail "Bridge is not active"
  systemctl is-active --quiet wechat-chat-api.service ||
    fail "Chat API is not active"
  systemctl is-active --quiet wechat-hermes-adapter.service ||
    fail "Adapter is not active"
  assert_protected_file \
    "$BRIDGE_ROOT/db-state.json" \
    "$EXPECTED_DB_STATE_SHA256" \
    "$EXPECTED_DB_STATE_INODE" \
    db-state.json
  assert_protected_file \
    /home/ubuntu/.cache/wechat-chat-api/send-state.json \
    "$EXPECTED_SEND_STATE_SHA256" \
    "$EXPECTED_SEND_STATE_INODE" \
    send-state.json
  assert_protected_file \
    /opt/wechat-ai-bot/data/bot.db \
    "$EXPECTED_BOT_DB_SHA256" \
    "$EXPECTED_BOT_DB_INODE" \
    bot.db
}

assert_source_tree() {
  local actual_commit

  [[ -d "$SOURCE_ROOT" && ! -L "$SOURCE_ROOT" ]] ||
    fail "source root is invalid"
  [[ -d "$SOURCE_ROOT/.git" && ! -L "$SOURCE_ROOT/.git" ]] ||
    fail "source root must be a Git checkout"
  [[ -f "$SOURCE_ROOT/chat-api/db_bridge.py" &&
    ! -L "$SOURCE_ROOT/chat-api/db_bridge.py" ]] ||
    fail "candidate Bridge source is invalid"
  actual_commit=$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null || true)
  [[ "$actual_commit" == "$EXPECTED_SOURCE_COMMIT" ]] ||
    fail "source checkout does not match the expected commit"
  [[ -z "$(git -C "$SOURCE_ROOT" status --porcelain)" ]] ||
    fail "source checkout is dirty"
}

adapter_health_is_ready() {
  curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8000/health |
    python3 -c '
import json
import sys

payload = json.load(sys.stdin)
persona = payload.get("persona") or {}
if payload.get("ready") is not True or payload.get("degraded") is True:
    raise SystemExit(1)
if persona.get("version") != "sophia@1.0.0+ccv3-xiaoge@1.1.1":
    raise SystemExit(1)
if persona.get("integrity") is not True:
    raise SystemExit(1)
'
}

assert_baseline
assert_source_tree
adapter_health_is_ready || fail "Adapter is not CCV3-ready"

source_file="$SOURCE_ROOT/chat-api/db_bridge.py"
install -d -o root -g root -m 0700 "$CACHE_ROOT"
PYTHONPYCACHEPREFIX="$CACHE_ROOT" python3 -m py_compile "$source_file"

bridge_backup="$BRIDGE_FILE.previous-$RELEASE_ID"
env_backup="$BRIDGE_ENV.previous-$RELEASE_ID"
next_file="$BRIDGE_FILE.next-$RELEASE_ID"
[[ -f "$BRIDGE_FILE" && ! -L "$BRIDGE_FILE" ]] ||
  fail "active Bridge script is invalid"
[[ -f "$BRIDGE_ENV" && ! -L "$BRIDGE_ENV" ]] ||
  fail "Bridge environment is invalid"
[[ ! -e "$bridge_backup" && ! -L "$bridge_backup" ]] ||
  fail "Bridge source backup already exists"
[[ ! -e "$env_backup" && ! -L "$env_backup" ]] ||
  fail "Bridge environment backup already exists"
[[ ! -e "$next_file" && ! -L "$next_file" ]] ||
  fail "next Bridge source already exists"
cp --preserve=mode,ownership -- "$BRIDGE_FILE" "$bridge_backup"
cp --preserve=mode,ownership -- "$BRIDGE_ENV" "$env_backup"
install -o ubuntu -g ubuntu -m 0644 "$source_file" "$next_file"
PYTHONPYCACHEPREFIX="$CACHE_ROOT" python3 -m py_compile "$next_file"

deployment_succeeded=0
restore_after_failure() {
  local status=$?

  trap - EXIT
  if [[ "$deployment_succeeded" != "1" ]]; then
    set +e
    printf 'deploy_bridge_release: restoring previous Bridge release\n' >&2
    cp --preserve=mode,ownership -- "$bridge_backup" "$BRIDGE_FILE"
    cp --preserve=mode,ownership -- "$env_backup" "$BRIDGE_ENV"
    rm -f -- "$next_file"
    systemctl restart linux-wechat-bridge.service >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap restore_after_failure EXIT

python3 - "$BRIDGE_ENV" <<'PY'
from pathlib import Path
import os
import stat
import sys

raw_path = Path(sys.argv[1])
if raw_path.is_symlink():
    raise SystemExit("Bridge environment must not be a symbolic link")
path = raw_path.resolve(strict=True)
metadata = path.stat()
if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
    raise SystemExit("Bridge environment must be root-private 0600")

key = "HERMES_WECHAT_GROUP_LISTENER_ENABLED"
value = "true"
seen = False
rewritten = []
for line in path.read_text(encoding="utf-8").splitlines():
    name, separator, _old = line.partition("=")
    if separator and name == key:
        rewritten.append(key + "=" + value)
        seen = True
    else:
        rewritten.append(line)
if not seen:
    rewritten.append(key + "=" + value)

temporary = path.with_name(path.name + ".next-%d" % os.getpid())
try:
    temporary.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
finally:
    temporary.unlink(missing_ok=True)
PY

mv -- "$next_file" "$BRIDGE_FILE"
systemctl restart linux-wechat-bridge.service
sleep 2
systemctl is-active --quiet linux-wechat-bridge.service ||
  fail "Bridge did not restart"
bridge_pid=$(systemctl show --property=MainPID --value linux-wechat-bridge.service)
[[ "$bridge_pid" =~ ^[1-9][0-9]*$ ]] || fail "Bridge has no main PID"
tr '\0' '\n' < "/proc/$bridge_pid/environ" |
  grep -Fx 'HERMES_WECHAT_GROUP_LISTENER_ENABLED=true' >/dev/null ||
  fail "Bridge listener environment was not loaded"
adapter_health_is_ready || fail "Adapter became unavailable after Bridge restart"
assert_baseline

deployment_succeeded=1
trap - EXIT
printf 'Bridge release %s is active with the structured group listener enabled\n' \
  "$RELEASE_ID"

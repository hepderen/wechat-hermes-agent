#!/usr/bin/env bash
set -euo pipefail

# Upgrade only Chat API's local implementation. The surrounding WeChat
# process, Bridge cursor, and durable delivery state stay in place.
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

CHAT_API_ROOT=/home/ubuntu/wechat-chat-api
CHAT_API_FILE=$CHAT_API_ROOT/chat_api.py
CACHE_ROOT=/var/tmp/wechat-hermes-chat-api-pyc-$RELEASE_ID

fail() {
  printf 'deploy_chat_api_release: %s\n' "$*" >&2
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
  systemctl is-active --quiet wechat-chat-api.service ||
    fail "Chat API is not active"
  systemctl is-active --quiet linux-wechat-bridge.service ||
    fail "Bridge is not active"
  systemctl is-active --quiet wechat-hermes-adapter.service ||
    fail "Adapter is not active"
  assert_protected_file \
    /home/ubuntu/linux-wechat-bot/db-state.json \
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
  [[ -f "$SOURCE_ROOT/chat-api/chat_api.py" &&
    ! -L "$SOURCE_ROOT/chat-api/chat_api.py" ]] ||
    fail "candidate Chat API source is invalid"
  actual_commit=$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null || true)
  [[ "$actual_commit" == "$EXPECTED_SOURCE_COMMIT" ]] ||
    fail "source checkout does not match the expected commit"
  [[ -z "$(git -C "$SOURCE_ROOT" status --porcelain)" ]] ||
    fail "source checkout is dirty"
}

assert_plain_text_protocol() {
  local source_file=$1
  local python_bin=$2

  "$python_bin" - "$source_file" <<'PY'
import importlib.util
from pathlib import Path
import sys

source = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("release_chat_api", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

sample = "x\u2063\u200b\u200c"
if module.outbound_message_text(sample) != "x":
    raise SystemExit("candidate still exposes delivery marker characters")
if module.outbound_message_text("x\U0001f469\u200d\U0001f4bb") != "x\U0001f469\u200d\U0001f4bb":
    raise SystemExit("candidate damages composite emoji")
if "wire_text = text" not in source.read_text(encoding="utf-8"):
    raise SystemExit("candidate does not use plain text for new deliveries")
PY
}

chat_api_is_ready() {
  curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8765/health |
    python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if payload.get("ok") is not True:
    raise SystemExit(1)
if payload.get("ready") is not True or payload.get("degraded") is True:
    raise SystemExit(1)
'
}

assert_baseline
assert_source_tree
[[ -x "$CHAT_API_ROOT/.venv/bin/python" ]] ||
  fail "Chat API virtual environment is missing"
[[ -f "$CHAT_API_FILE" && ! -L "$CHAT_API_FILE" ]] ||
  fail "active Chat API script is invalid"
install -d -o root -g root -m 0700 "$CACHE_ROOT"
PYTHONPYCACHEPREFIX="$CACHE_ROOT" "$CHAT_API_ROOT/.venv/bin/python" \
  -m py_compile "$SOURCE_ROOT/chat-api/chat_api.py"
assert_plain_text_protocol \
  "$SOURCE_ROOT/chat-api/chat_api.py" "$CHAT_API_ROOT/.venv/bin/python"

backup="$CHAT_API_FILE.previous-$RELEASE_ID"
next_file="$CHAT_API_FILE.next-$RELEASE_ID"
[[ ! -e "$backup" && ! -L "$backup" ]] ||
  fail "Chat API source backup already exists"
[[ ! -e "$next_file" && ! -L "$next_file" ]] ||
  fail "next Chat API source already exists"
cp --preserve=mode,ownership -- "$CHAT_API_FILE" "$backup"
install -o ubuntu -g ubuntu -m 0644 \
  "$SOURCE_ROOT/chat-api/chat_api.py" "$next_file"
PYTHONPYCACHEPREFIX="$CACHE_ROOT" "$CHAT_API_ROOT/.venv/bin/python" \
  -m py_compile "$next_file"

deployment_succeeded=0
restore_after_failure() {
  local status=$?

  trap - EXIT
  if [[ "$deployment_succeeded" != "1" ]]; then
    set +e
    printf 'deploy_chat_api_release: restoring previous Chat API source\n' >&2
    cp --preserve=mode,ownership -- "$backup" "$CHAT_API_FILE"
    rm -f -- "$next_file"
    systemctl restart wechat-chat-api.service >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap restore_after_failure EXIT

mv -- "$next_file" "$CHAT_API_FILE"
cmp --silent "$SOURCE_ROOT/chat-api/chat_api.py" "$CHAT_API_FILE" ||
  fail "active Chat API source differs from the verified candidate"
systemctl restart wechat-chat-api.service
systemctl is-active --quiet wechat-chat-api.service ||
  fail "Chat API did not restart"
for _attempt in $(seq 1 20); do
  if chat_api_is_ready >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
chat_api_is_ready || fail "Chat API did not become ready"
active_pid=$(systemctl show --property=MainPID --value wechat-chat-api.service)
[[ "$active_pid" =~ ^[1-9][0-9]*$ ]] || fail "Chat API has no main PID"
tr '\0' ' ' < "/proc/$active_pid/cmdline" |
  grep -Fq "$CHAT_API_FILE" || fail "Chat API process does not use the active script"
assert_plain_text_protocol "$CHAT_API_FILE" "$CHAT_API_ROOT/.venv/bin/python"
assert_baseline

deployment_succeeded=1
trap - EXIT
printf 'Chat API release %s is active with plain-text outbound delivery\n' \
  "$RELEASE_ID"

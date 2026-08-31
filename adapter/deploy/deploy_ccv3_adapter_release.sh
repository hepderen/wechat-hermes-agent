#!/usr/bin/env bash
set -euo pipefail

# Upgrade only the Adapter release. The caller supplies a clean, pinned Git
# checkout so a live WeChat process and its state are never part of the update.
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

ADAPTER_RELEASES_ROOT=/opt/wechat-hermes-adapter-releases
ADAPTER_ROOT=/opt/wechat-hermes-adapter
ADAPTER_ENV=/etc/wechat-hermes/adapter.env
PIP_INDEX_URL=${PIP_INDEX_URL:-http://mirrors.tencentyun.com/pypi/simple}
PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST:-mirrors.tencentyun.com}

fail() {
  printf 'deploy_sunxiaochuan_adapter_release: %s\n' "$*" >&2
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
  systemctl is-active --quiet wechat-hermes-adapter.service ||
    fail "Adapter is not active"
  systemctl is-active --quiet hermes-worker.service ||
    fail "Hermes Worker is not active"
  systemctl is-active --quiet wechat-chat-api.service ||
    fail "Chat API is not active"
  systemctl is-active --quiet linux-wechat-bridge.service ||
    fail "Bridge is not active"
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
  local checkout_root
  local source_root

  [[ -d "$SOURCE_ROOT" && ! -L "$SOURCE_ROOT" ]] ||
    fail "source root is invalid"
  source_root=$(realpath -e "$SOURCE_ROOT")
  checkout_root=$(git -C "$source_root" rev-parse --show-toplevel 2>/dev/null || true)
  [[ -n "$checkout_root" && "$source_root" == "$checkout_root/adapter" ]] ||
    fail "source root must be the adapter directory in a Git checkout"
  actual_commit=$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null || true)
  [[ "$actual_commit" == "$EXPECTED_SOURCE_COMMIT" ]] ||
    fail "source checkout does not match the expected commit"
  [[ -z "$(git -C "$SOURCE_ROOT" status --porcelain)" ]] ||
    fail "source checkout is dirty"
  [[ -z "$(find "$SOURCE_ROOT" -type l -print -quit)" ]] ||
    fail "source checkout contains symbolic links"
}

assert_sunxiaochuan_persona_resources() {
  local root=$1
  local python_bin=$2

PYTHONPATH="$root" "$python_bin" - <<'PY'
from app.persona import (
    PERSONA_SKILL_BUNDLES,
    PERSONA_SKILL_INTEGRITY_OK,
    PERSONA_SKILL_PROMPT,
    PERSONA_SKILL_SHA256,
    PERSONA_VERSION,
    SUNXIAOCHUAN_RUNTIME_PATH,
    SUNXIAOCHUAN_RUNTIME_SHA256,
    sunxiaochuan_runtime_integrity,
    SUNXIAOCHUAN_SECTION_PATH,
    sunxiaochuan_section_integrity,
    weirdotv_source_archive_integrity,
)

if PERSONA_VERSION != "weirdotv@1.0.0+sunxiaochuan@3.0.0":
    raise SystemExit("unexpected WeirdoTV persona version")
if (
    not weirdotv_source_archive_integrity()
    or not sunxiaochuan_section_integrity()
    or not sunxiaochuan_runtime_integrity()
):
    raise SystemExit("pinned persona source archive integrity failed")
if not PERSONA_SKILL_INTEGRITY_OK:
    raise SystemExit("WeirdoTV persona integrity failed")
if not SUNXIAOCHUAN_RUNTIME_PATH.is_file():
    raise SystemExit("Sun Xiaochuan runtime bundle is missing")
if len(PERSONA_SKILL_PROMPT) < 1200:
    raise SystemExit("Sun Xiaochuan runtime bundle is unexpectedly thin")
if PERSONA_SKILL_SHA256 != SUNXIAOCHUAN_RUNTIME_SHA256:
    raise SystemExit("Sun Xiaochuan runtime hash metadata is inconsistent")
bundles = {str(item.get("name") or "") for item in PERSONA_SKILL_BUNDLES}
if bundles != {"weirdo-tv-sunxiaochuan"}:
    raise SystemExit("unexpected runtime persona bundle set")
PY
}

adapter_health_is_ready() {
  curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8000/health |
    python3 -c '
import json
import sys

payload = json.load(sys.stdin)
persona = payload.get("persona") or {}
group_listener = payload.get("group_listener") or {}
if payload.get("ready") is not True or payload.get("degraded") is True:
    raise SystemExit(1)
if persona.get("version") != "weirdotv@1.0.0+sunxiaochuan@3.0.0":
    raise SystemExit(1)
if persona.get("integrity") is not True:
    raise SystemExit(1)
skills = {
    str(item.get("name") or ""): item
    for item in list(persona.get("skills") or [])
    if isinstance(item, dict)
}
bundle = skills.get("weirdo-tv-sunxiaochuan") or {}
if set(skills) != {"weirdo-tv-sunxiaochuan"}:
    raise SystemExit(1)
if bundle.get("runtime_file") != "sunxiaochuan.runtime.md":
    raise SystemExit(1)
if bundle.get("loaded_sections") != [
    "Sun Xiaochuan section",
    "Slang Corpus",
    "single-person source rules (adapted)",
    "Xiaoge group-chat expression rules",
]:
    raise SystemExit(1)
if group_listener.get("enabled") is not True:
    raise SystemExit(1)
'
}

wait_for_adapter_ready() {
  local attempt
  for ((attempt = 1; attempt <= 20; attempt++)); do
    if adapter_health_is_ready >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

assert_baseline
assert_source_tree
[[ -f "$SOURCE_ROOT/requirements.txt" ]] || fail "source requirements are missing"
assert_sunxiaochuan_persona_resources "$SOURCE_ROOT" python3

release="$ADAPTER_RELEASES_ROOT/$RELEASE_ID"
[[ ! -e "$release" && ! -L "$release" ]] ||
  fail "release already exists: $release"
install -d -o root -g root -m 0755 "$ADAPTER_RELEASES_ROOT"
install -d -o root -g root -m 0755 "$release"
rsync -a \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '.pytest_cache' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'app/ccv3.py' \
  --exclude 'personas/sunxiaochuan.card.json' \
  --exclude 'third_party/character-card-spec-v3' \
  --exclude 'skills/sophia' \
  --exclude 'skills/humanizer-zh-next' \
  "$SOURCE_ROOT/" "$release/"

[[ ! -e "$release/skills/humanizer-zh-next" ]] ||
  fail "Humanizer must not be present in the WeirdoTV runtime release"
[[ ! -e "$release/skills/sophia" ]] ||
  fail "Sophia must not be present in the WeirdoTV runtime release"
[[ ! -e "$release/app/ccv3.py" ]] ||
  fail "CCV3 runtime loader must not be present in the Sun Xiaochuan release"
[[ ! -e "$release/personas/sunxiaochuan.card.json" ]] ||
  fail "legacy character card must not be present in the Sun Xiaochuan release"
[[ ! -e "$release/third_party/character-card-spec-v3" ]] ||
  fail "CCV3 archive must not be present in the Sun Xiaochuan runtime release"
python3 -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip install \
  --disable-pip-version-check \
  --no-cache-dir \
  --index-url "$PIP_INDEX_URL" \
  --trusted-host "$PIP_TRUSTED_HOST" \
  -r "$release/requirements.txt"
"$release/.venv/bin/python" -m compileall -q \
  "$release/app" \
  "$release/cleanup.py"
"$release/.venv/bin/uvicorn" --version >/dev/null
assert_sunxiaochuan_persona_resources "$release" "$release/.venv/bin/python"

find "$release" -path "$release/.venv" -prune -o -type d -exec chmod 0755 {} +
find "$release" -path "$release/.venv" -prune -o -type f -exec chmod 0644 {} +
find "$release/.venv" -type d -exec chmod 0755 {} +
find "$release/.venv" -type f -exec chmod go-w,u+rw {} +
chmod 0755 "$release/deploy/deploy_ccv3_adapter_release.sh"
chown -R root:root "$release"

current=$(readlink -f -- "$ADAPTER_ROOT" 2>/dev/null || true)
[[ -L "$ADAPTER_ROOT" && -n "$current" &&
  "$current" == "$ADAPTER_RELEASES_ROOT/"* ]] ||
  fail "active Adapter release is invalid"
[[ "$current" != "$release" ]] || fail "candidate is already active"
[[ -x "$release/.venv/bin/uvicorn" && -f "$release/app/main.py" ]] ||
  fail "candidate Adapter release is incomplete"

[[ -f "$ADAPTER_ENV" && ! -L "$ADAPTER_ENV" ]] ||
  fail "adapter environment is invalid"
env_backup="$ADAPTER_ENV.previous-$RELEASE_ID"
[[ ! -e "$env_backup" && ! -L "$env_backup" ]] ||
  fail "environment backup already exists"
cp --preserve=mode,ownership -- "$ADAPTER_ENV" "$env_backup"

deployment_succeeded=0
restore_after_failure() {
  local status=$?
  local restore_link="$ADAPTER_RELEASES_ROOT/.restore-$RELEASE_ID-$$"

  trap - EXIT
  if [[ "$deployment_succeeded" != "1" ]]; then
    set +e
    printf 'deploy_sunxiaochuan_adapter_release: restoring previous Adapter release\n' >&2
    cp --preserve=mode,ownership -- "$env_backup" "$ADAPTER_ENV"
    if [[ ! -e "$restore_link" && ! -L "$restore_link" ]]; then
      ln -s -- "$current" "$restore_link" &&
        mv -Tf -- "$restore_link" "$ADAPTER_ROOT"
    fi
    systemctl restart wechat-hermes-adapter.service >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap restore_after_failure EXIT

python3 - "$ADAPTER_ENV" <<'PY'
from pathlib import Path
import os
import stat
import sys

raw_path = Path(sys.argv[1])
if raw_path.is_symlink():
    raise SystemExit("adapter environment must not be a symbolic link")
path = raw_path.resolve(strict=True)
metadata = path.stat()
if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
    raise SystemExit("adapter environment must be root-private 0600")

# Retired relationship environment values are removed from the active
# environment. Historical SQLite data remains untouched.
updates = {
    "HERMES_WECHAT_SESSION_GENERATION": "16",
    "HERMES_WECHAT_CHAT_ONLY": "true",
    "HERMES_WECHAT_GROUP_LISTENER_ENABLED": "true",
    "HERMES_WECHAT_GROUP_LISTENER_MIN_REPLY_GAP_SECONDS": "12",
    "HERMES_WECHAT_GROUP_LISTENER_MIN_TURNS_BETWEEN_REPLIES": "3",
    "HERMES_WECHAT_GROUP_LISTENER_NAMES": "小格,Hermes",
}
lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
rewritten = []
for line in lines:
    key, separator, _value = line.partition("=")
    if separator and key.startswith("HERMES_WECHAT_RELATIONSHIP_"):
        continue
    if separator and key in updates:
        rewritten.append(key + "=" + updates[key])
        seen.add(key)
    else:
        rewritten.append(line)
for key, value in updates.items():
    if key not in seen:
        rewritten.append(key + "=" + value)

temporary = path.with_name(path.name + ".next-%d" % os.getpid())
try:
    temporary.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
finally:
    temporary.unlink(missing_ok=True)
PY

next_link="$ADAPTER_RELEASES_ROOT/.current-$RELEASE_ID"
[[ ! -e "$next_link" && ! -L "$next_link" ]] ||
  fail "next Adapter link already exists"
ln -s -- "$release" "$next_link"
mv -Tf -- "$next_link" "$ADAPTER_ROOT"

systemctl restart wechat-hermes-adapter.service
systemctl is-active --quiet wechat-hermes-adapter.service ||
  fail "Adapter did not restart"
wait_for_adapter_ready || fail "Adapter did not become Sun Xiaochuan-ready"
assert_baseline

deployment_succeeded=1
trap - EXIT
printf 'Sun Xiaochuan Adapter release %s is active; previous environment saved at %s\n' \
  "$RELEASE_ID" "$env_backup"

#!/usr/bin/env bash
set -euo pipefail

PREVIOUS_RELEASE_ID=${1:-}
EXPECTED_WECHAT_PID=${EXPECTED_WECHAT_PID:-}
ADAPTER_RELEASES_ROOT=/opt/wechat-hermes-adapter-releases
ADAPTER_ROOT=/opt/wechat-hermes-adapter
ADAPTER_ENV=/etc/wechat-hermes/adapter.env

fail() {
  printf 'rollback_persona: %s\n' "$*" >&2
  exit 1
}

adapter_health_is_ready() {
  curl --fail --silent --show-error --max-time 3 \
    http://127.0.0.1:8000/health |
    python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if payload.get("ready") is not True or payload.get("degraded") is True:
    raise SystemExit(1)
'
}

wait_for_adapter_ready() {
  local attempt
  for ((attempt = 1; attempt <= 15; attempt++)); do
    if adapter_health_is_ready >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

rollback_mutated=0
rollback_succeeded=0
env_backup=
current=

restore_after_failure() {
  local status=$?
  local restore_link

  trap - EXIT
  if [[ "$rollback_succeeded" != "1" && "$rollback_mutated" == "1" ]]; then
    set +e
    printf 'rollback_persona: restoring prior Adapter after failed rollback\n' >&2
    if [[ -n "$env_backup" && -f "$env_backup" ]]; then
      mv -f -- "$env_backup" "$ADAPTER_ENV"
    fi
    if [[ -n "$current" ]]; then
      restore_link="$ADAPTER_RELEASES_ROOT/.persona-restore-${PREVIOUS_RELEASE_ID}-$$"
      rm -f -- "$restore_link"
      ln -s -- "$current" "$restore_link" && mv -Tf -- "$restore_link" "$ADAPTER_ROOT"
    fi
    systemctl restart wechat-hermes-adapter.service >/dev/null 2>&1 || true
  fi
  if [[ -n "$env_backup" && -f "$env_backup" ]]; then
    rm -f -- "$env_backup"
  fi
  exit "$status"
}

trap restore_after_failure EXIT

[[ $(id -u) -eq 0 ]] || fail "must run as root"
[[ "$PREVIOUS_RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] ||
  fail "usage: rollback_persona.sh PREVIOUS_RELEASE_ID"
[[ "$EXPECTED_WECHAT_PID" =~ ^[0-9]+$ ]] ||
  fail "EXPECTED_WECHAT_PID must be configured"
[[ -f "$ADAPTER_ENV" ]] || fail "missing $ADAPTER_ENV"

wechat_pid=$(pgrep -x wechat || true)
[[ "$wechat_pid" == "$EXPECTED_WECHAT_PID" ]] ||
  fail "WeChat PID changed before persona rollback"

release="$ADAPTER_RELEASES_ROOT/$PREVIOUS_RELEASE_ID"
[[ -d "$release" && ! -L "$release" ]] ||
  fail "previous Adapter release is missing or invalid"
release=$(readlink -f -- "$release")
root=$(readlink -f -- "$ADAPTER_RELEASES_ROOT")
[[ "$release" == "$root/"* ]] || fail "previous release escapes release root"
[[ -x "$release/.venv/bin/uvicorn" ]] ||
  fail "previous Adapter release runtime is incomplete"
[[ -f "$release/app/main.py" ]] ||
  fail "previous Adapter release source is incomplete"

current=$(readlink -f -- "$ADAPTER_ROOT" 2>/dev/null || true)
[[ -n "$current" && "$release" != "$current" ]] ||
  fail "previous Adapter release must differ from the active release"

env_backup=$(mktemp "${ADAPTER_ENV}.persona-rollback.XXXXXX")
cp --preserve=mode,ownership -- "$ADAPTER_ENV" "$env_backup"
rollback_mutated=1

python3 - "$ADAPTER_ENV" <<'PY'
from pathlib import Path
import os
import sys

raw_path = Path(sys.argv[1])
if raw_path.is_symlink():
    raise SystemExit("adapter environment must not be a symbolic link")
path = raw_path.resolve(strict=True)
metadata = path.stat()
if metadata.st_uid != 0 or metadata.st_mode & 0o077:
    raise SystemExit("adapter environment must be a non-symlink root-private file")

updates = {
    "HERMES_WECHAT_SESSION_GENERATION": "12",
}
lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
rewritten = []
for line in lines:
    key, separator, _value = line.partition("=")
    if separator and key in updates:
        rewritten.append(key + "=" + updates[key])
        seen.add(key)
    else:
        rewritten.append(line)
for key, value in updates.items():
    if key not in seen:
        rewritten.append(key + "=" + value)

temporary = path.with_name(path.name + ".persona-rollback-%d" % os.getpid())
try:
    temporary.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
finally:
    temporary.unlink(missing_ok=True)
PY

next_link="$ADAPTER_RELEASES_ROOT/.persona-rollback-$PREVIOUS_RELEASE_ID"
rm -f -- "$next_link"
ln -s -- "$release" "$next_link"
mv -Tf -- "$next_link" "$ADAPTER_ROOT"

systemctl restart wechat-hermes-adapter.service
systemctl is-active --quiet wechat-hermes-adapter.service ||
  fail "Adapter did not restart after persona rollback"
ss -ltn | grep -q '127\.0\.0\.1:8000 ' ||
  fail "Adapter is not listening on 127.0.0.1:8000 after persona rollback"
wait_for_adapter_ready ||
  fail "Adapter did not become ready after persona rollback"

wechat_pid=$(pgrep -x wechat || true)
[[ "$wechat_pid" == "$EXPECTED_WECHAT_PID" ]] ||
  fail "WeChat PID changed during persona rollback"

rollback_succeeded=1
rm -f -- "$env_backup"
trap - EXIT
printf 'previous Adapter persona restored with generation 12; room-scoped context only\n'

#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR=${1:-}
EXPECTED_WECHAT_PID=${EXPECTED_WECHAT_PID:-}

fail() {
  printf 'rollback_to_legacy: %s\n' "$*" >&2
  exit 1
}

[[ $(id -u) -eq 0 ]] || fail "must run as root"
[[ -n "$BACKUP_DIR" ]] || fail "usage: rollback_to_legacy.sh BACKUP_DIR"
[[ "$EXPECTED_WECHAT_PID" =~ ^[0-9]+$ ]] ||
  fail "EXPECTED_WECHAT_PID must be configured"
[[ -f "$BACKUP_DIR/adapter.env" ]] ||
  fail "missing adapter.env backup in $BACKUP_DIR"
[[ -f "$BACKUP_DIR/hermes.env" ]] ||
  fail "missing hermes.env backup in $BACKUP_DIR"

wechat_pid=$(pgrep -x wechat || true)
[[ "$wechat_pid" == "$EXPECTED_WECHAT_PID" ]] ||
  fail "WeChat PID changed before rollback"

systemctl disable --now wechat-hermes-cleanup.timer
systemctl disable --now wechat-hermes-adapter.service
systemctl disable --now hermes-worker.service

install -o root -g root -m 0600 \
  "$BACKUP_DIR/adapter.env" /etc/wechat-hermes/adapter.env
install -o root -g root -m 0600 \
  "$BACKUP_DIR/hermes.env" /etc/wechat-hermes/hermes.env

systemctl enable --now wechat-ai-bot.service
systemctl is-active --quiet wechat-ai-bot.service ||
  fail "legacy AI service did not start"
ss -ltn | grep -q '127\.0\.0\.1:8000 ' ||
  fail "legacy AI service is not listening on 127.0.0.1:8000"

wechat_pid=$(pgrep -x wechat || true)
[[ "$wechat_pid" == "$EXPECTED_WECHAT_PID" ]] ||
  fail "WeChat PID changed during rollback"

printf 'legacy AI restored on 127.0.0.1:8000; WeChat was not restarted\n'

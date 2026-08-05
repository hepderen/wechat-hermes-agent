#!/bin/bash
set -euo pipefail

umask 077

BASE=/var/lib/wechat-hermes/releases/web-research
HERMES_HOME=/var/lib/wechat-hermes/workspace/home/.hermes
PLUGIN_DIR="$HERMES_HOME/plugins/wechat-cloud-web"
CONFIG="$HERMES_HOME/config.yaml"
WEB_ENV=/etc/wechat-hermes/hermes-web.env
DROPIN=/etc/systemd/system/hermes-worker.service.d/20-web-research.conf
SEARX_UNIT=/etc/systemd/system/wechat-searxng.service
SEARX_SETTINGS=/etc/wechat-hermes/searxng/settings.yml
SEARX_ENV=/etc/wechat-hermes/searxng.env
WECHAT_PID=${WECHAT_PID:?set WECHAT_PID to the active WeChat process ID}

if [ "${EUID}" -ne 0 ]; then
    echo "rollback must run as root" >&2
    exit 2
fi
if [ "$#" -ne 1 ]; then
    echo "usage: $0 RELEASE_ID_OR_PATH" >&2
    exit 2
fi

case "$1" in
    /*) release_root=$(readlink -f -- "$1") ;;
    *) release_root=$(readlink -f -- "$BASE/$1") ;;
esac
case "$release_root" in
    "$BASE"/*) ;;
    *) echo "release path is outside $BASE" >&2; exit 2 ;;
esac

rollback="$release_root/rollback"
test -f "$rollback/READY"
test -d "/proc/$WECHAT_PID"

removed="$rollback/removed-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$removed"

restore_file() {
    local backup=$1
    local missing=$2
    local target=$3
    local label=$4
    if [ -f "$backup" ]; then
        install -d -m 0755 "$(dirname "$target")"
        cp -a -- "$backup" "$target"
    elif [ -f "$missing" ] && [ -e "$target" ]; then
        mv -- "$target" "$removed/$label"
    fi
}

systemctl disable --now wechat-searxng.service >/dev/null 2>&1 || true

if [ -e "$PLUGIN_DIR" ]; then
    mv -- "$PLUGIN_DIR" "$removed/plugin.failed"
fi
if [ -d "$rollback/plugin.previous" ]; then
    install -d -o wechat-hermes-runner -g wechat-hermes-runtime -m 0750 \
        "$(dirname "$PLUGIN_DIR")"
    mv -- "$rollback/plugin.previous" "$PLUGIN_DIR"
elif [ ! -f "$rollback/plugin.missing" ]; then
    echo "rollback plugin snapshot is incomplete" >&2
    exit 1
fi

install -o wechat-hermes-runner -g wechat-hermes-runtime -m 0640 \
    "$rollback/config.yaml" "$CONFIG"
restore_file "$rollback/hermes-web.env" "$rollback/hermes-web.env.missing" \
    "$WEB_ENV" hermes-web.env
restore_file "$rollback/hermes-dropin.conf" "$rollback/hermes-dropin.conf.missing" \
    "$DROPIN" hermes-dropin.conf
restore_file "$rollback/searxng.service" "$rollback/searxng.service.missing" \
    "$SEARX_UNIT" wechat-searxng.service
restore_file "$rollback/searxng-settings.yml" "$rollback/searxng-settings.yml.missing" \
    "$SEARX_SETTINGS" searxng-settings.yml
restore_file "$rollback/searxng.env" "$rollback/searxng.env.missing" \
    "$SEARX_ENV" searxng.env

systemctl daemon-reload
if grep -qx enabled "$rollback/searx-enabled"; then
    systemctl enable wechat-searxng.service >/dev/null
else
    systemctl disable wechat-searxng.service >/dev/null 2>&1 || true
fi
if grep -qx active "$rollback/searx-active"; then
    systemctl start wechat-searxng.service
else
    systemctl stop wechat-searxng.service >/dev/null 2>&1 || true
fi

systemctl restart hermes-worker.service
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:8642/health >/dev/null; then
        break
    fi
    sleep 1
done
curl -fsS --max-time 3 http://127.0.0.1:8642/health >/dev/null
test -d "/proc/$WECHAT_PID"

if [ -L "$BASE/current" ]; then
    mv -- "$BASE/current" "$removed/current-link"
fi
if [ -s "$rollback/previous-current-link" ]; then
    ln -s "$(cat "$rollback/previous-current-link")" "$BASE/current"
fi

date -u +%Y-%m-%dT%H:%M:%SZ >"$release_root/ROLLED_BACK"
echo "rollback complete release=$(basename "$release_root") wechat_pid=$WECHAT_PID"

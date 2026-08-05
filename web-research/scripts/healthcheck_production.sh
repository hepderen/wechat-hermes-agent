#!/bin/bash
set -euo pipefail

umask 077

BASE=/var/lib/wechat-hermes/releases/web-research
HERMES_HOME=/var/lib/wechat-hermes/workspace/home/.hermes
CONFIG="$HERMES_HOME/config.yaml"
PLUGIN_DIR="$HERMES_HOME/plugins/wechat-cloud-web"
WECHAT_PID=${WECHAT_PID:?set WECHAT_PID to the active WeChat process ID}

if [ "${EUID}" -ne 0 ]; then
    echo "health check must run as root" >&2
    exit 2
fi

release_root=${1:-$BASE/current}
release_root=$(readlink -f -- "$release_root")
case "$release_root" in
    "$BASE"/*) ;;
    *) echo "release path is outside $BASE" >&2; exit 2 ;;
esac

test -d "/proc/$WECHAT_PID"
test -f "$PLUGIN_DIR/provider.py"
test -f "$PLUGIN_DIR/plugin.yaml"

assert_loopback_listener() {
    local port=$1
    local listeners
    listeners=$(ss -H -ltn "sport = :$port")
    test -n "$listeners"
    while read -r address; do
        case "$address" in
            127.0.0.1:"$port") ;;
            *) echo "port $port is not loopback-only: $address" >&2; exit 1 ;;
        esac
    done < <(printf '%s\n' "$listeners" | awk '{print $4}')
}

systemctl is-active --quiet hermes-worker.service
systemctl is-active --quiet wechat-searxng.service
assert_loopback_listener 8642
assert_loopback_listener 8651

curl -fsS --max-time 3 http://127.0.0.1:8642/health \
    | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "ok"'
curl -fsS --max-time 8 -H 'X-Real-IP: 127.0.0.1' \
    --get --data-urlencode 'q=health check' \
    --data 'format=json' --data 'language=en-US' \
    http://127.0.0.1:8651/search \
    | python3 -c 'import json,sys; assert isinstance(json.load(sys.stdin).get("results"), list)'

python3 "$release_root/scripts/configure_hermes_web.py" validate "$CONFIG"

set -a
. /etc/wechat-hermes/hermes.env
. /etc/wechat-hermes/hermes-web.env
set +a
probe_output=$(
    runuser -u wechat-hermes-runner -- env \
        HOME=/var/lib/wechat-hermes/workspace/home \
        HERMES_HOME="$HERMES_HOME" \
        PYTHONPATH="$PLUGIN_DIR/vendor" \
        /opt/hermes-runtime/venv/bin/python \
        "$release_root/scripts/probe_hermes_tools.py"
)
printf '%s' "$probe_output" \
    | python3 -c 'import json,sys; assert json.load(sys.stdin)["ok"] is True'

cache="$HERMES_HOME/cache/web-search.sqlite3"
if [ -e "$cache" ]; then
    test "$(stat -c %a "$cache")" = 600
    test "$(stat -c %U "$cache")" = wechat-hermes-runner
    runuser -u wechat-hermes-runner -- python3 - "$cache" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
connection = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
PY
fi

printf '{"ok":true,"release":"%s","wechat_pid":%s,"ports":[8642,8651]}\n' \
    "$(basename "$release_root")" "$WECHAT_PID"

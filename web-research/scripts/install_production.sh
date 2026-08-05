#!/bin/bash
set -euo pipefail

umask 077

BASE=/var/lib/wechat-hermes/releases/web-research
HERMES_HOME=/var/lib/wechat-hermes/workspace/home/.hermes
PLUGIN_ROOT="$HERMES_HOME/plugins"
PLUGIN_DIR="$PLUGIN_ROOT/wechat-cloud-web"
CONFIG="$HERMES_HOME/config.yaml"
WEB_ENV=/etc/wechat-hermes/hermes-web.env
DROPIN=/etc/systemd/system/hermes-worker.service.d/20-web-research.conf
SEARX_UNIT=/etc/systemd/system/wechat-searxng.service
SEARX_SETTINGS=/etc/wechat-hermes/searxng/settings.yml
SEARX_ENV=/etc/wechat-hermes/searxng.env
WECHAT_PID=${WECHAT_PID:?set WECHAT_PID to the active WeChat process ID}

if [ "${EUID}" -ne 0 ]; then
    echo "installation must run as root" >&2
    exit 2
fi
if [ "$#" -ne 2 ]; then
    echo "usage: $0 CANDIDATE_ROOT RELEASE_ID" >&2
    exit 2
fi

candidate_root=$(readlink -f -- "$1")
release_id=$2
case "$candidate_root" in
    /var/lib/wechat-hermes/candidates/web-research/*) ;;
    *) echo "candidate root is outside the approved directory" >&2; exit 2 ;;
esac
case "$release_id" in
    *[!A-Za-z0-9._-]*|'') echo "invalid release id" >&2; exit 2 ;;
esac

release_root="$BASE/$release_id"
rollback="$release_root/rollback"
test ! -e "$release_root"
test -d "/proc/$WECHAT_PID"
test -f "$candidate_root/hermes-plugin/provider.py"
test -f "$candidate_root/hermes-plugin/plugin.yaml"
test -d "$candidate_root/hermes-plugin/vendor/trafilatura"
test -f "$candidate_root/deploy/wechat-searxng.service"
test -f "$candidate_root/deploy/hermes-worker-web.conf"
test -f "$candidate_root/scripts/rollback_production.sh"
cmp -s "$candidate_root/hermes-plugin/provider.py" \
    "$candidate_root/gateway-home/.hermes/plugins/wechat-cloud-web/provider.py"

install -d -o root -g wechat-hermes-runtime -m 0750 "$BASE"
install -d -o root -g wechat-hermes-runtime -m 0750 "$release_root"
install -d -o root -g root -m 0700 "$rollback"

for name in README.md requirements-extract.lock requirements-extract.txt; do
    cp -a -- "$candidate_root/$name" "$release_root/$name"
done
for name in hermes-plugin deploy scripts searxng; do
    cp -a -- "$candidate_root/$name" "$release_root/$name"
done
chown -R root:wechat-hermes-runtime "$release_root"
chmod 0700 "$rollback"

(
    cd "$release_root"
    find . -path ./rollback -prune -o -type f ! -name MANIFEST.sha256 -print0 \
        | sort -z | xargs -0 sha256sum >MANIFEST.sha256
)
chown root:wechat-hermes-runtime "$release_root/MANIFEST.sha256"
chmod 0640 "$release_root/MANIFEST.sha256"

backup_file() {
    local source=$1
    local destination=$2
    if [ -f "$source" ]; then
        cp -a -- "$source" "$rollback/$destination"
    else
        touch "$rollback/$destination.missing"
    fi
}

cp -a -- "$CONFIG" "$rollback/config.yaml"
backup_file "$WEB_ENV" hermes-web.env
backup_file "$DROPIN" hermes-dropin.conf
backup_file "$SEARX_UNIT" searxng.service
backup_file "$SEARX_SETTINGS" searxng-settings.yml
backup_file "$SEARX_ENV" searxng.env
if systemctl is-enabled --quiet wechat-searxng.service 2>/dev/null; then
    echo enabled >"$rollback/searx-enabled"
else
    echo disabled >"$rollback/searx-enabled"
fi
if systemctl is-active --quiet wechat-searxng.service 2>/dev/null; then
    echo active >"$rollback/searx-active"
else
    echo inactive >"$rollback/searx-active"
fi
if [ -L "$BASE/current" ]; then
    readlink "$BASE/current" >"$rollback/previous-current-link"
else
    : >"$rollback/previous-current-link"
fi
touch "$rollback/READY"

changes_started=0
on_error() {
    local status=$?
    trap - ERR
    if [ "$changes_started" -eq 1 ]; then
        WECHAT_PID="$WECHAT_PID" "$release_root/scripts/rollback_production.sh" \
            "$release_root" || true
    fi
    exit "$status"
}
trap on_error ERR

install -d -o wechat-hermes-runner -g wechat-hermes-runtime -m 0750 "$PLUGIN_ROOT"
stage="$PLUGIN_ROOT/.wechat-cloud-web.stage-$release_id"
test ! -e "$stage"
cp -a -- "$release_root/hermes-plugin" "$stage"
chown -R wechat-hermes-runner:wechat-hermes-runtime "$stage"
find "$stage" -type d -exec chmod 0750 {} +
find "$stage" -type f -exec chmod 0640 {} +
changes_started=1
if [ -e "$PLUGIN_DIR" ]; then
    mv -- "$PLUGIN_DIR" "$rollback/plugin.previous"
else
    touch "$rollback/plugin.missing"
fi
mv -- "$stage" "$PLUGIN_DIR"

install -o root -g root -m 0600 "$release_root/deploy/hermes-web.env" "$WEB_ENV"
install -d -o root -g root -m 0755 "$(dirname "$DROPIN")"
install -o root -g root -m 0644 "$release_root/deploy/hermes-worker-web.conf" "$DROPIN"
install -d -o root -g root -m 0755 "$(dirname "$SEARX_SETTINGS")"
install -o root -g root -m 0644 "$release_root/searxng/settings.yml" "$SEARX_SETTINGS"
if [ ! -f "$SEARX_ENV" ]; then
    secret=$(openssl rand -hex 32)
    temporary_env="$rollback/searxng.env.generated"
    printf 'SEARXNG_SECRET=%s\n' "$secret" >"$temporary_env"
    install -o root -g root -m 0600 "$temporary_env" "$SEARX_ENV"
fi
install -o root -g root -m 0644 "$release_root/deploy/wechat-searxng.service" \
    "$SEARX_UNIT"

python3 "$release_root/scripts/configure_hermes_web.py" enable "$CONFIG"
python3 "$release_root/scripts/configure_hermes_web.py" validate "$CONFIG"

systemctl daemon-reload
systemctl enable --now wechat-searxng.service
for _ in $(seq 1 45); do
    if curl -fsS --max-time 2 -H 'X-Real-IP: 127.0.0.1' \
        --get --data-urlencode 'q=health check' \
        --data 'format=json' http://127.0.0.1:8651/search \
        >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -fsS --max-time 5 -H 'X-Real-IP: 127.0.0.1' \
    --get --data-urlencode 'q=health check' \
    --data 'format=json' http://127.0.0.1:8651/search >/dev/null

systemctl restart hermes-worker.service
for _ in $(seq 1 45); do
    if curl -fsS --max-time 2 http://127.0.0.1:8642/health \
        >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -fsS --max-time 3 http://127.0.0.1:8642/health >/dev/null

if [ -L "$BASE/current" ]; then
    mv -- "$BASE/current" "$rollback/current-link.replaced"
elif [ -e "$BASE/current" ]; then
    echo "$BASE/current exists and is not a symlink" >&2
    false
fi
ln -s "$release_root" "$BASE/current"

"$release_root/scripts/healthcheck_production.sh" "$release_root"
date -u +%Y-%m-%dT%H:%M:%SZ >"$release_root/ACTIVATED"
trap - ERR
echo "activated release=$release_id wechat_pid=$WECHAT_PID"

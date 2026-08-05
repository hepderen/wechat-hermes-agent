#!/bin/sh
set -eu

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
    echo "usage: $0 CANDIDATE_ROOT PORT API_KEY [SEARCH_URL]" >&2
    exit 2
fi

candidate_root=$1
port=$2
api_key=$3
search_url=${4:-http://127.0.0.1:18651}
gateway_home="$candidate_root/gateway-home"
plugin_vendor="$gateway_home/.hermes/plugins/wechat-cloud-web/vendor"

test -d "$gateway_home/.hermes"
test -d "$plugin_vendor"

set -a
. /etc/wechat-hermes/hermes.env
. "$candidate_root/deploy/hermes-web.env"
set +a

exec runuser -u wechat-hermes-runner -- env \
    HOME="$gateway_home" \
    HERMES_HOME="$gateway_home/.hermes" \
    PYTHONPATH="$plugin_vendor" \
    API_SERVER_ENABLED=true \
    API_SERVER_HOST=127.0.0.1 \
    API_SERVER_PORT="$port" \
    API_SERVER_KEY="$api_key" \
    WECHAT_WEB_SEARCH_URL="$search_url" \
    /opt/hermes-runtime/venv/bin/hermes gateway run --replace --accept-hooks

#!/bin/sh
set -eu

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    echo "usage: $0 CANDIDATE_ROOT PORT [SEARCH_URL]" >&2
    exit 2
fi

candidate_root=$1
port=$2
search_url=${3:-http://127.0.0.1:18651}
gateway_home="$candidate_root/gateway-home"
plugin_vendor="$gateway_home/.hermes/plugins/wechat-cloud-web/vendor"
: "${API_SERVER_KEY:?set API_SERVER_KEY for the candidate Gateway}"

test -d "$gateway_home/.hermes"
test -d "$plugin_vendor"

set -a
. /etc/wechat-hermes/hermes.env
. "$candidate_root/deploy/hermes-web.env"
set +a

export HOME="$gateway_home"
export HERMES_HOME="$gateway_home/.hermes"
export PYTHONPATH="$plugin_vendor"
export API_SERVER_ENABLED=true
export API_SERVER_HOST=127.0.0.1
export API_SERVER_PORT="$port"
export WECHAT_WEB_SEARCH_URL="$search_url"

exec runuser -m -u wechat-hermes-runner -- \
    /opt/hermes-runtime/venv/bin/hermes gateway run --replace --accept-hooks

#!/usr/bin/env bash
set -euo pipefail

umask 077

if [[ $(id -u) -ne 0 ]]; then
  printf 'build_candidate: must run as root\n' >&2
  exit 2
fi
if [[ $# -ne 2 ]]; then
  printf 'usage: %s SOURCE_ROOT RELEASE_ID\n' "$0" >&2
  exit 2
fi

source_root=$(readlink -f -- "$1")
release_id=$2
case "$release_id" in
  *[!A-Za-z0-9._-]*|'')
    printf 'build_candidate: invalid release id\n' >&2
    exit 2
    ;;
esac

candidate_base=/var/lib/wechat-hermes/candidates/web-research
candidate_root="$candidate_base/$release_id"
if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin=$PYTHON_BIN
elif [[ -x /opt/hermes-runtime/venv/bin/python ]]; then
  python_bin=/opt/hermes-runtime/venv/bin/python
else
  python_bin=python3
fi
hermes_config=${HERMES_CONFIG_SOURCE:-/var/lib/wechat-hermes/workspace/home/.hermes/config.yaml}

test -f "$source_root/hermes-plugin/provider.py"
test -f "$source_root/hermes-plugin/plugin.yaml"
test -f "$source_root/requirements-extract.lock"
test -f "$source_root/scripts/configure_hermes_web.py"
test -f "$hermes_config"
test ! -e "$candidate_root"

install -d -o root -g wechat-hermes-runtime -m 0750 \
  /var/lib/wechat-hermes/candidates \
  "$candidate_base" \
  "$candidate_root"

for name in README.md requirements-extract.lock requirements-extract.txt; do
  cp -a -- "$source_root/$name" "$candidate_root/$name"
done
for name in hermes-plugin deploy scripts searxng tests; do
  cp -a -- "$source_root/$name" "$candidate_root/$name"
done

vendor="$candidate_root/hermes-plugin/vendor"
install -d -o root -g wechat-hermes-runtime -m 0750 "$vendor"
"$python_bin" -m pip install \
  --disable-pip-version-check \
  --require-hashes \
  --no-deps \
  --no-compile \
  --target "$vendor" \
  --requirement "$candidate_root/requirements-extract.lock"
test -d "$vendor/trafilatura"

gateway_home="$candidate_root/gateway-home"
gateway_plugins="$gateway_home/.hermes/plugins"
install -d -o wechat-hermes-runner -g wechat-hermes-runtime -m 0750 \
  "$gateway_plugins"
cp -a -- "$candidate_root/hermes-plugin" \
  "$gateway_plugins/wechat-cloud-web"
install -o wechat-hermes-runner -g wechat-hermes-runtime -m 0640 \
  "$hermes_config" "$gateway_home/.hermes/config.yaml"
"$python_bin" "$candidate_root/scripts/configure_hermes_web.py" \
  enable "$gateway_home/.hermes/config.yaml"

chown -R root:wechat-hermes-runtime "$candidate_root"
chown -R wechat-hermes-runner:wechat-hermes-runtime "$gateway_home"
find "$candidate_root" -type d -exec chmod 0750 {} +
find "$candidate_root" -type f -exec chmod 0640 {} +
find "$candidate_root/scripts" -type f -name '*.sh' -exec chmod 0750 {} +

(
  cd "$candidate_root"
  find . -type f ! -name MANIFEST.sha256 -print0 \
    | sort -z \
    | xargs -0 sha256sum >MANIFEST.sha256
)
chown root:wechat-hermes-runtime "$candidate_root/MANIFEST.sha256"
chmod 0640 "$candidate_root/MANIFEST.sha256"

printf '%s\n' "$candidate_root"

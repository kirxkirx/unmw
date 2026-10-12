#!/usr/bin/env bash
# Install into a NEW, caller-owned directory, never overwrite the system RAR.
set -euo pipefail
if [[ -n ${GATEWAY_INTERFACE:-}${REQUEST_METHOD:-} ]]; then
 echo 'ERROR: not a CGI' >&2
 exit 1
fi
[[ $# == 1 ]] || exit 2
test_tools=$1
mkdir -m 700 -- "$test_tools"
test_tools=$(cd -- "$test_tools" && pwd)
# shellcheck source=tests/dependencies.env
source "$(dirname -- "${BASH_SOURCE[0]}")/dependencies.env"
curl --fail --location --connect-timeout 10 --max-time 120 --retry 2 \
 "$UNMW_TEST_RAR_URL" -o "$test_tools/rar.tar.gz"
printf '%s  %s\n' "$UNMW_TEST_RAR_SHA256" "$test_tools/rar.tar.gz" | sha256sum --check --strict
tar -xzf "$test_tools/rar.tar.gz" -C "$test_tools" rar/rar rar/unrar rar/default.sfx
"$test_tools/rar/rar"

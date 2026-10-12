#!/usr/bin/env bash
# CI only: sudo unshare --net -- bash tests/offline_namespace.sh USER PYTHON ARTIFACTS BACKEND RAR_DIR
set -euo pipefail
if [[ -n ${GATEWAY_INTERFACE:-}${REQUEST_METHOD:-} ]]; then
 echo 'ERROR: not a CGI' >&2
 exit 1
fi
[[ $EUID == 0 && $# == 5 ]] || exit 2
# Never change networking in the host namespace, even if invoked incorrectly.
[[ $(readlink /proc/self/ns/net) != "$(readlink /proc/1/ns/net)" ]] || {
 echo 'Refusing to run in the host network namespace' >&2
 exit 1
}
ip link set lo up
test_user=$1
test_python=$2
test_artifacts=$3
test_backend=$4
test_rar_dir=$5
[[ $(id -u "$test_user") != 0 ]] || exit 2
test_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# No host credentials, notification hooks, proxy settings or production config.
exec runuser -u "$test_user" -- env -i \
 PATH="$(dirname -- "$test_python"):$test_rar_dir:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
 USER="$test_user" LOGNAME="$test_user" LANG=C.UTF-8 \
 bash "$test_repo/tests/run_ci_tests.sh" "$test_python" "$test_artifacts" "$test_backend"

#!/usr/bin/env bash
# Run as an unprivileged user; CI invokes this inside an offline namespace.
set -euo pipefail
if [[ -n ${GATEWAY_INTERFACE:-}${REQUEST_METHOD:-} ]]; then
 echo 'ERROR: not a CGI' >&2
 exit 1
fi
if [[ $EUID == 0 || $# -lt 3 ]]; then
 echo 'Usage (non-root): bash tests/run_ci_tests.sh PYTHON NEW_ARTIFACT_DIR present|absent [pytest args]' >&2
 exit 1
fi
test_python=$1
test_artifacts=$2
test_backend=$3
shift 3
case "$test_backend" in present|absent) ;; *) exit 2 ;; esac
test_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$test_repo"
# Do not let pytest --basetemp replace an existing directory.
mkdir -m 700 -- "$test_artifacts"
test_artifacts=$(cd -- "$test_artifacts" && pwd)
mkdir "$test_artifacts/pytest"
export PATH="$(dirname -- "$test_python"):$PATH"
export PYTHONDONTWRITEBYTECODE=1
export UNMW_TEST_STRICT=1 UNMW_TEST_RARFILE="$test_backend"
export COVERAGE_RCFILE="$test_repo/.coveragerc" COVERAGE_FILE="$test_artifacts/.coverage"
export UNMW_COVERAGE_SOURCE="$test_repo,$test_artifacts/pytest"
export UNMW_COVERAGE_REPORT_SOURCE="$test_repo"
finish() {
 local result=$?
 trap - EXIT
 "$test_python" -m coverage combine --keep || result=1
 "$test_python" -m coverage report > "$test_artifacts/coverage.txt" || result=1
 "$test_python" -m coverage xml -o "$test_artifacts/coverage.xml" || result=1
 "$test_python" -m coverage html -d "$test_artifacts/coverage-html" || result=1
 # A report with no branches or no CGI data is not a successful coverage run.
 "$test_python" "$test_repo/tests/check_coverage.py" || result=1
 cat "$test_artifacts/coverage.txt"
 exit "$result"
}
trap finish EXIT
"$test_python" -m pytest --version
"$test_python" -m pip freeze > "$test_artifacts/python-packages.txt"
"$test_python" -m coverage run -m pytest -p no:cacheprovider \
 --basetemp="$test_artifacts/pytest" --junitxml="$test_artifacts/junit.xml" "$@" \
 2>&1 | tee "$test_artifacts/pytest.log"

#!/usr/bin/env bash

# This script checks if all GitHub Actions tests are passing for the latest commit,
# and if so, pulls the latest version. Designed to run manually or from cron.
#
# Usage:
#   ./git_unmw_automated_update.sh
#
# This script must NOT be run through CGI. It is blocked by .htaccess (Apache)
# and by custom_http_server.py (only upload.py is treated as CGI).

# Guard: refuse to run if invoked as CGI
if [ -n "$GATEWAY_INTERFACE" ] || [ -n "$REQUEST_METHOD" ]; then
    echo "Content-Type: text/plain"
    echo ""
    echo "ERROR: this script must not be run as CGI"
    exit 1
fi

#################################
# Set the safe locale that should be available on any POSIX system
LC_ALL=C
LANGUAGE=C
export LANGUAGE LC_ALL
#################################

# Configuration
GITHUB_REPO_OWNER="kirxkirx"
GITHUB_REPO_NAME="unmw"

# Exit codes
EXIT_SUCCESS=0
EXIT_ALREADY_UPTODATE=0
EXIT_ERROR=1

#################################
# Helper functions
#################################

command_exists() {
    command -v "$1" &> /dev/null
    return $?
}

#################################
# Pre-flight checks
#################################

for cmd in git grep sed; do
    if ! command_exists "$cmd"; then
        echo "ERROR: required command '$cmd' is not installed" >&2
        exit $EXIT_ERROR
    fi
done

if ! command_exists curl && ! command_exists wget; then
    echo "ERROR: neither curl nor wget is installed" >&2
    exit $EXIT_ERROR
fi

# Check that no other instances of this script are running (using lock file)
LOCKFILE="/tmp/git_unmw_automated_update.lock"
if [ -f "$LOCKFILE" ]; then
    LOCK_PID=$(cat "$LOCKFILE" 2>/dev/null)
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "Another instance of this script is already running (PID $LOCK_PID), exiting"
        exit $EXIT_ALREADY_UPTODATE
    fi
    # Stale lock file — remove it
    rm -f "$LOCKFILE"
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

#################################
# Main logic
#################################

# Change to the directory containing this script (the unmw repo root)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit $EXIT_ERROR

# Check if this is a git repository
if [ ! -d .git ]; then
    echo "ERROR: $SCRIPT_DIR is not a git repository" >&2
    exit $EXIT_ERROR
fi

# Determine the default branch (main or master)
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
if [ -z "$DEFAULT_BRANCH" ]; then
    # Fallback: try main, then master
    if git show-ref --verify --quiet refs/remotes/origin/main 2>/dev/null; then
        DEFAULT_BRANCH="main"
    elif git show-ref --verify --quiet refs/remotes/origin/master 2>/dev/null; then
        DEFAULT_BRANCH="master"
    else
        echo "ERROR: cannot determine default branch" >&2
        exit $EXIT_ERROR
    fi
fi

# Get current local commit
CURRENT_LOCAL_COMMIT=$(git rev-parse HEAD 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$CURRENT_LOCAL_COMMIT" ]; then
    echo "ERROR: cannot get current commit hash" >&2
    exit $EXIT_ERROR
fi

# Fetch latest info from remote
git fetch origin "$DEFAULT_BRANCH" &>/dev/null
if [ $? -ne 0 ]; then
    echo "ERROR: git fetch failed" >&2
    exit $EXIT_ERROR
fi

# Get remote commit
REMOTE_COMMIT=$(git rev-parse "origin/$DEFAULT_BRANCH" 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$REMOTE_COMMIT" ]; then
    echo "ERROR: cannot get remote commit hash" >&2
    exit $EXIT_ERROR
fi

# Check if already up to date
if [ "$CURRENT_LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then
    echo "Already up to date"
    exit $EXIT_ALREADY_UPTODATE
fi

echo "New version available: $REMOTE_COMMIT"
echo "Checking GitHub Actions status for this commit..."

# Check GitHub Actions status using the Check Runs API.
# The legacy /commits/{sha}/status endpoint is only populated by third-party
# CI services (Travis, CircleCI, ...) that POST commit statuses; GitHub Actions
# itself reports only via the Checks API, so on a GitHub-Actions-only repo the
# legacy endpoint returns an empty list with the default state "pending" and
# the script would never proceed.
CHECK_RUNS_URL="https://api.github.com/repos/${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}/commits/${REMOTE_COMMIT}/check-runs?per_page=100"

if command_exists curl; then
    STATUS_RESPONSE=$(curl --silent --show-error --fail "$CHECK_RUNS_URL" 2>/dev/null)
elif command_exists wget; then
    STATUS_RESPONSE=$(wget -q -O - "$CHECK_RUNS_URL" 2>/dev/null)
fi

if [ $? -ne 0 ] || [ -z "$STATUS_RESPONSE" ]; then
    echo "ERROR: failed to fetch check-runs from GitHub API" >&2
    exit $EXIT_ERROR
fi

# Total number of check runs reported for this commit
TOTAL_COUNT=$(echo "$STATUS_RESPONSE" | grep -o '"total_count"[[:space:]]*:[[:space:]]*[0-9]*' | head -n 1 | grep -o '[0-9]*$')
if [ -z "$TOTAL_COUNT" ]; then
    TOTAL_COUNT=0
fi

if [ "$TOTAL_COUNT" -eq 0 ]; then
    echo "No GitHub Actions check runs reported yet for commit $REMOTE_COMMIT"
    echo "Tests have probably not been scheduled yet. Will try again later"
    exit $EXIT_ERROR
fi

# Extract all check-run statuses (queued / in_progress / completed)
STATUSES=$(echo "$STATUS_RESPONSE" | grep -o '"status"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/"status"[[:space:]]*:[[:space:]]*"\([^"]*\)"/\1/')
# Extract all check-run conclusions (success / failure / cancelled / timed_out / action_required / skipped / neutral / stale)
CONCLUSIONS=$(echo "$STATUS_RESPONSE" | grep -o '"conclusion"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/"conclusion"[[:space:]]*:[[:space:]]*"\([^"]*\)"/\1/')

# Are all check runs completed?
N_NOT_COMPLETED=$(echo "$STATUSES" | grep -v '^completed$' | grep -v '^$' | wc -l)
if [ "$N_NOT_COMPLETED" -gt 0 ]; then
    echo "$N_NOT_COMPLETED of $TOTAL_COUNT GitHub Actions check runs are still running for commit $REMOTE_COMMIT"
    echo "Will try again later"
    exit $EXIT_ERROR
fi

# All check runs are completed; verify every conclusion is acceptable
N_BAD=$(echo "$CONCLUSIONS" | grep -v -E '^(success|skipped|neutral)$' | grep -v '^$' | wc -l)
if [ "$N_BAD" -gt 0 ]; then
    BAD_LIST=$(echo "$CONCLUSIONS" | grep -v -E '^(success|skipped|neutral)$' | sort -u | tr '\n' ' ')
    echo "ERROR: $N_BAD of $TOTAL_COUNT GitHub Actions check runs failed for commit $REMOTE_COMMIT (conclusions: $BAD_LIST)" >&2
    echo "Will not update to this version" >&2
    exit $EXIT_ERROR
fi

echo "All $TOTAL_COUNT GitHub Actions check runs passed. Proceeding with update."

# Discard any local changes to tracked files before pulling.
# Policy (intentional): the production tree must track upstream only - local
# hotfixes are NOT preserved. All fixes belong upstream. The previous
# 'git stash' silently accumulated them in an ever-growing stash stack;
# instead we discard them outright, but LOG what was discarded first so the
# cron mail / update log records it (never a silent loss). Untracked files
# (local_config.sh, logs, uploads) are left untouched by reset --hard.
LOCAL_TRACKED_CHANGES=$(git status --porcelain --untracked-files=no)
if [ -n "$LOCAL_TRACKED_CHANGES" ]; then
    echo "WARNING: discarding local changes to tracked files (production tracks upstream only - put fixes upstream):"
    echo "$LOCAL_TRACKED_CHANGES"
    git reset --hard --quiet
fi

# Pull the latest version
echo "Pulling latest version..."
git pull origin "$DEFAULT_BRANCH"
if [ $? -ne 0 ]; then
    echo "ERROR: git pull failed" >&2
    exit $EXIT_ERROR
fi

echo "unmw successfully updated"

# Refresh the per-host static web interface pages in htdocs.
# generate_htdocs.sh is a no-op (exit 0) unless HTDOCS_DIR is set in
# local_config.sh, so hosts that copy move_to_htdocs/ manually are
# unaffected. Non-fatal: the code update itself already succeeded.
# The -x check also covers updating from a version predating the script.
if [ -x ./generate_htdocs.sh ]; then
    ./generate_htdocs.sh || echo "WARNING: generate_htdocs.sh failed - the web interface pages in htdocs may be stale" >&2
fi

exit $EXIT_SUCCESS

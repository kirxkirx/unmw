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

# Check combined status using GitHub's commit status API
STATUS_URL="https://api.github.com/repos/${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}/commits/${REMOTE_COMMIT}/status"

if command_exists curl; then
    STATUS_RESPONSE=$(curl --silent --show-error --fail "$STATUS_URL" 2>/dev/null)
elif command_exists wget; then
    STATUS_RESPONSE=$(wget -q -O - "$STATUS_URL" 2>/dev/null)
fi

if [ $? -ne 0 ] || [ -z "$STATUS_RESPONSE" ]; then
    echo "ERROR: failed to fetch commit status from GitHub API" >&2
    exit $EXIT_ERROR
fi

# Extract the state field
STATE=$(echo "$STATUS_RESPONSE" | grep -o '"state"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/"state"[[:space:]]*:[[:space:]]*"\([^"]*\)"/\1/' | head -n 1)

echo "Commit status: $STATE"

if [ "$STATE" != "success" ]; then
    if [ "$STATE" = "pending" ]; then
        echo "Tests are still running for commit $REMOTE_COMMIT"
        echo "Will try again later"
        exit $EXIT_ERROR
    else
        echo "ERROR: tests did not pass for commit $REMOTE_COMMIT (state: $STATE)" >&2
        echo "Will not update to this version" >&2
        exit $EXIT_ERROR
    fi
fi

echo "All tests passed. Proceeding with update."

# Stash any local changes to tracked files
git stash --quiet 2>/dev/null

# Pull the latest version
echo "Pulling latest version..."
git pull origin "$DEFAULT_BRANCH"
if [ $? -ne 0 ]; then
    echo "ERROR: git pull failed" >&2
    exit $EXIT_ERROR
fi

echo "unmw successfully updated"
exit $EXIT_SUCCESS

#!/usr/bin/env bash
#
# Housekeeping for the web-served per-request result directories in uploads/.
#
# The coordinate-search, forced-photometry and archival-photometry pages
# leave their per-request output directories in place so result URLs keep
# working; nothing deletes them automatically. Run this script (manually or
# from cron) when disk space matters:
#
#   ./archive_phot_prune.sh 90            # delete result dirs older than 90 days
#   ./archive_phot_prune.sh 90 --dry-run  # only show what would be deleted
#
# What it removes (only directly under uploads/):
#   archive_phot_*          archival photometry job dirs (persistent results);
#                           jobs still queued or running are NEVER touched
#   .archive_phot_tmp_*     half-created job dirs left by crashed submissions
#   forced_phot_*           coord_forced_photometry.py per-request output
#   coord_search_*          coord_search.py per-request output
# and, regardless of the <days> argument, disposable VaST working copies
# orphaned by a crash (vast_archive_phot_* and vast_forced_phot_* older than
# one day whose owning process -- the pid embedded in the directory name --
# is no longer alive). These are big (a full VaST tree each), so crashes
# should not be allowed to accumulate them.
#
# Cron example (/etc/crontab syntax, running daily as the web server user):
#   41 4 * * * apache cd /data/cgi-bin/unmw && ./archive_phot_prune.sh 90 >> uploads/archive_phot_prune.log 2>&1

set -u

# This script is NOT a CGI entry point and must never be reachable over the
# web: it deletes data irreversibly, and Apache passes a query string that
# contains no '=' straight through as command-line arguments (RFC 3875), so a
# bare GET would supply the <days> argument it needs. The .htaccess rules are
# ignored unless the vhost sets AllowOverride, so this guard - not .htaccess -
# is the authoritative protection. Note the ${VAR:-} form: set -u is in effect.
if [ -n "${GATEWAY_INTERFACE:-}" ] || [ -n "${REQUEST_METHOD:-}" ]; then
    printf 'Content-Type: text/plain\n\nERROR: this script must not be run as CGI\n'
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)" || exit 1
# In production uploads/ is a SYMLINK to the data root; resolve it to the
# physical path (pwd -P) rather than passing the symlink to find, which
# (in its default -P mode) would not descend into the target and would
# silently prune nothing. Plain pwd keeps the symlink component.
UPLOADS="$(cd "$SCRIPT_DIR/uploads" 2>/dev/null && pwd -P)"

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
 echo "Usage: $0 <days> [--dry-run]" >&2
 echo "Deletes per-request result directories under $SCRIPT_DIR/uploads older than <days> days." >&2
 exit 1
fi

DAYS="$1"
case "$DAYS" in
 ''|*[!0-9]*)
  echo "ERROR: <days> must be a whole number, got: $DAYS" >&2
  exit 1
  ;;
esac
if [ "$DAYS" -lt 1 ]; then
 echo "ERROR: <days> must be at least 1" >&2
 exit 1
fi

DRY_RUN=0
if [ $# -eq 2 ]; then
 if [ "$2" = "--dry-run" ]; then
  DRY_RUN=1
 else
  echo "ERROR: unknown option: $2" >&2
  exit 1
 fi
fi

if [ ! -d "$UPLOADS" ]; then
 echo "ERROR: uploads directory not found: $UPLOADS" >&2
 exit 1
fi

remove_dir() {
 # remove_dir <dir> <reason>
 if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY RUN: would remove $1 ($2)"
 else
  echo "removing $1 ($2)"
  rm -rf -- "$1"
 fi
}

# ---- Old per-request result directories. ----
# archive_phot_* jobs that are still queued or running are never touched,
# whatever their age (an uncapped job may legitimately run for a long time
# and a queued one may legitimately wait behind it).
find "$UPLOADS" -maxdepth 1 -type d \
 \( -name 'archive_phot_*' -o -name '.archive_phot_tmp_*' \
    -o -name 'forced_phot_*' -o -name 'coord_search_*' \) \
 -mtime +"$DAYS" -print0 2>/dev/null |
 while IFS= read -r -d '' DIR; do
  BASE="$(basename "$DIR")"
  case "$BASE" in
   archive_phot_*)
    STATE_FILE="$DIR/state.json"
    if [ -f "$STATE_FILE" ] &&
       grep -Eq '"state":[[:space:]]*"(queued|running)"' "$STATE_FILE"; then
     echo "skipping $DIR (job is queued or running)"
     continue
    fi
    remove_dir "$DIR" "result dir older than $DAYS days"
    ;;
   *)
    remove_dir "$DIR" "result dir older than $DAYS days"
    ;;
  esac
 done

# ---- Orphaned VaST working copies. ----
# Normal operation deletes these when the request/job finishes; one older
# than a day whose owning process is gone is crash debris. The owning pid is
# embedded in the directory name right after the prefix
# (vast_archive_phot_<pid><random> / vast_forced_phot_<pid><random> /
# vast_monitoring_<pid><random>).
# Note: the persistent per-source monitoring registry uploads/monitoring/
# is deliberately OUT OF SCOPE of this script - only the prefix-matched
# names below are ever touched.
find "$UPLOADS" -maxdepth 1 -type d \
 \( -name 'vast_archive_phot_*' -o -name 'vast_forced_phot_*' \
    -o -name 'vast_monitoring_*' \) \
 -mtime +1 -print0 2>/dev/null |
 while IFS= read -r -d '' DIR; do
  BASE="$(basename "$DIR")"
  REST="${BASE#vast_archive_phot_}"
  REST="${REST#vast_forced_phot_}"
  REST="${REST#vast_monitoring_}"
  PID="${REST%%[!0-9]*}"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
   echo "skipping $DIR (owner pid $PID is alive)"
   continue
  fi
  remove_dir "$DIR" "orphaned VaST working copy, owner pid ${PID:-unknown} not running"
 done

echo "archive_phot_prune.sh finished (older than $DAYS days, dry run: $DRY_RUN)"

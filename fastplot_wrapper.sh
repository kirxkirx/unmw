#!/usr/bin/env bash

# Fastplot wrapper script
# Creates a disposable VaST copy, runs fastplot.sh, moves output to serving directory.
# Uses flock for concurrency control (compatible with the CGI's fcntl.flock).
#
# Arguments: $1 = candidate URL, $2 = candidate ID
# Exit codes: 0 = success, 1 = error

# shellcheck disable=SC2086

LC_ALL=C
LANGUAGE=C
export LANGUAGE LC_ALL

# --- Configuration ---

SCRIPTDIR=$(dirname "$0")
# Resolve to absolute path portably
if command -v realpath >/dev/null 2>&1 ; then
 SCRIPTDIR=$(realpath "$SCRIPTDIR")
elif command -v readlink >/dev/null 2>&1 && readlink -f "$SCRIPTDIR" >/dev/null 2>&1 ; then
 SCRIPTDIR=$(readlink -f "$SCRIPTDIR")
else
 # Last resort: use cd + pwd
 SCRIPTDIR=$(cd "$SCRIPTDIR" && pwd)
fi

# Source configuration
if [ -s "$SCRIPTDIR/local_config.sh" ]; then
 # shellcheck source=/dev/null
 source "$SCRIPTDIR/local_config.sh"
fi

# --- Argument Validation ---

CANDIDATE_URL="$1"
CANDIDATE_ID="$2"

if [ -z "$CANDIDATE_URL" ] || [ -z "$CANDIDATE_ID" ]; then
 echo "Usage: $0 <candidate_url> <candidate_id>"
 exit 1
fi

# Validate CANDIDATE_ID contains only safe characters
if ! echo "$CANDIDATE_ID" | grep -qE '^[a-zA-Z0-9_.-]+$'; then
 echo "ERROR: Invalid candidate ID: $CANDIDATE_ID"
 exit 1
fi

# --- Resolve Paths ---

# Resolve VAST_REFERENCE_COPY to absolute path
if [ -z "$VAST_REFERENCE_COPY" ]; then
 echo "ERROR: VAST_REFERENCE_COPY is not set"
 exit 1
fi
if command -v realpath >/dev/null 2>&1 ; then
 RESOLVED_VAST_REFERENCE_COPY=$(realpath "$VAST_REFERENCE_COPY")
elif command -v readlink >/dev/null 2>&1 && readlink -f "$VAST_REFERENCE_COPY" >/dev/null 2>&1 ; then
 RESOLVED_VAST_REFERENCE_COPY=$(readlink -f "$VAST_REFERENCE_COPY")
else
 RESOLVED_VAST_REFERENCE_COPY=$(cd "$VAST_REFERENCE_COPY" && pwd)
fi

if [ ! -d "$RESOLVED_VAST_REFERENCE_COPY" ]; then
 echo "ERROR: VAST_REFERENCE_COPY directory does not exist: $RESOLVED_VAST_REFERENCE_COPY"
 exit 1
fi

if [ -z "$DATA_PROCESSING_ROOT" ]; then
 echo "ERROR: DATA_PROCESSING_ROOT is not set"
 exit 1
fi

# --- Setup Output Directory ---

FASTPLOT_OUTPUT_DIR="$DATA_PROCESSING_ROOT/fastplot"
mkdir -p "$FASTPLOT_OUTPUT_DIR"

# --- Acquire Lock ---

LOCK_FILE="$FASTPLOT_OUTPUT_DIR/.fastplot.lock"

# Open the lock file on fd 200
exec 200>"$LOCK_FILE"

LOCK_ACQUIRED=0

# Try flock command first (Linux, FreeBSD 13+)
if command -v flock >/dev/null 2>&1 ; then
 if flock -n 200 ; then
  LOCK_ACQUIRED=1
 fi
fi

# Portable fallback using Python fcntl (macOS, older FreeBSD)
# Python 3 is a hard prerequisite - the CGI script requires it
if [ "$LOCK_ACQUIRED" -ne 1 ] ; then
 if python3 -c "
import fcntl, sys
fd = int(sys.argv[1])
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except (IOError, OSError):
    sys.exit(1)
" 200 2>/dev/null ; then
  LOCK_ACQUIRED=1
 fi
fi

if [ "$LOCK_ACQUIRED" -ne 1 ] ; then
 echo "ERROR: Cannot acquire lock."
 echo "Either another fastplot job is running, or neither flock"
 echo "nor python3 fcntl is available. Refusing to run."
 exit 1
fi

echo "Lock acquired"

# Write candidate ID to lock file so CGI can identify the current job
echo "$CANDIDATE_ID" >&200

# --- Clean Up Stale Working Directories ---
# Safe because we hold the lock - no other fastplot is running
echo "Cleaning up stale working directories..."
for STALE_DIR in "$DATA_PROCESSING_ROOT"/vast_fastplot_* ; do
 if [ -d "$STALE_DIR" ]; then
  echo "Removing stale directory: $STALE_DIR"
  rm -rf "$STALE_DIR"
 fi
done

# --- Check Disk Space ---
# Require at least 5 GB free before proceeding
MIN_FREE_KB=5242880
FREE_KB=$(df -k "$DATA_PROCESSING_ROOT" 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "$FREE_KB" ] && [ "$FREE_KB" -lt "$MIN_FREE_KB" ] 2>/dev/null; then
 FREE_GB=$(echo "$FREE_KB" | awk '{printf "%.1f", $1/1024/1024}')
 echo "ERROR: Insufficient disk space: ${FREE_GB} GB free, need at least 5 GB"
 exit 1
fi

# --- Create Disposable VaST Copy ---

FASTPLOT_VAST_WORKDIR="$DATA_PROCESSING_ROOT/vast_fastplot_${CANDIDATE_ID}_$$"

echo "Creating disposable VaST copy at $FASTPLOT_VAST_WORKDIR"
echo "Source: $RESOLVED_VAST_REFERENCE_COPY"

# rsync excluding large/unnecessary items (mirroring autoprocess.sh)
rsync -a --whole-file --no-times --omit-dir-times \
 --exclude 'astorb.dat' --exclude 'lib/catalogs' \
 --exclude 'src' --exclude '.git' --exclude '.github' \
 "$RESOLVED_VAST_REFERENCE_COPY/" "$FASTPLOT_VAST_WORKDIR"
if [ $? -ne 0 ]; then
 echo "ERROR: rsync failed"
 rm -rf "$FASTPLOT_VAST_WORKDIR"
 exit 1
fi

# Create symlinks for large excluded items
if [ -f "$RESOLVED_VAST_REFERENCE_COPY/astorb.dat" ]; then
 ln -s "$RESOLVED_VAST_REFERENCE_COPY/astorb.dat" "$FASTPLOT_VAST_WORKDIR/astorb.dat"
fi
if [ -d "$RESOLVED_VAST_REFERENCE_COPY/lib/catalogs" ]; then
 cd "$FASTPLOT_VAST_WORKDIR/lib/" || exit 1
 ln -s "$RESOLVED_VAST_REFERENCE_COPY/lib/catalogs" .
 cd "$DATA_PROCESSING_ROOT" || exit 1
fi

# --- Run Fastplot ---

echo "Running fastplot.sh with URL: $CANDIDATE_URL"
echo "Working directory: $FASTPLOT_VAST_WORKDIR"

"$FASTPLOT_VAST_WORKDIR/util/transients/fastplot.sh" "$CANDIDATE_URL"
FASTPLOT_EXIT_CODE=$?

echo "fastplot.sh exit code: $FASTPLOT_EXIT_CODE"

if [ $FASTPLOT_EXIT_CODE -ne 0 ]; then
 echo "ERROR: fastplot.sh failed with exit code $FASTPLOT_EXIT_CODE"
 rm -rf "$FASTPLOT_VAST_WORKDIR"
 exit 1
fi

# --- Move Output ---

# Find the output archive in the disposable VaST directory
# fastplot.sh creates: fastplot__${CAMERA_NAME}__${TRANSIENT_ID}.tar.bz2
OUTPUT_ARCHIVE=""
for CANDIDATE_ARCHIVE in "$FASTPLOT_VAST_WORKDIR"/fastplot__*__"${CANDIDATE_ID}".tar.bz2 ; do
 if [ -f "$CANDIDATE_ARCHIVE" ]; then
  OUTPUT_ARCHIVE="$CANDIDATE_ARCHIVE"
  break
 fi
done

if [ -z "$OUTPUT_ARCHIVE" ] || [ ! -f "$OUTPUT_ARCHIVE" ]; then
 echo "ERROR: Cannot find output archive for candidate $CANDIDATE_ID"
 echo "Expected pattern: $FASTPLOT_VAST_WORKDIR/fastplot__*__${CANDIDATE_ID}.tar.bz2"
 ls -la "$FASTPLOT_VAST_WORKDIR"/fastplot__* 2>/dev/null
 rm -rf "$FASTPLOT_VAST_WORKDIR"
 exit 1
fi

ARCHIVE_BASENAME=$(basename "$OUTPUT_ARCHIVE")
echo "Moving $ARCHIVE_BASENAME to $FASTPLOT_OUTPUT_DIR/"

mv "$OUTPUT_ARCHIVE" "$FASTPLOT_OUTPUT_DIR/$ARCHIVE_BASENAME"
if [ $? -ne 0 ]; then
 echo "ERROR: Failed to move archive to output directory"
 rm -rf "$FASTPLOT_VAST_WORKDIR"
 exit 1
fi

# --- Cleanup ---

echo "Removing disposable VaST directory: $FASTPLOT_VAST_WORKDIR"
rm -rf "$FASTPLOT_VAST_WORKDIR"

echo "=== Fastplot completed successfully ==="
echo "Output: $FASTPLOT_OUTPUT_DIR/$ARCHIVE_BASENAME"

# Lock is released automatically when this script exits (fd 200 closes)

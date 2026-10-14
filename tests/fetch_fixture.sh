#!/usr/bin/env bash
# Download one pinned test fixture from the first mirror that serves it.
#
#   bash tests/fetch_fixture.sh DEST_FILE SHA256 URL [URL...]
#
# Same approach as download_test_data_file() in VaST's util/examples/test_vast.sh
# (curl --fail, a few attempts per URL, then the next mirror, nothing left
# behind), plus a SHA256 pin and resumed transfers. '--fail' makes an HTTP error
# page a failed download instead of a saved file: on 2026-10-13 the reverse
# proxy in front of tau.kirx.net answered every CI job with "503 Service
# Unavailable", the error page was kept as the archive and the second mirror
# was never tried because only the unpacking failed later.
# A failed or stalled transfer is retried up to UNMW_FETCH_ATTEMPTS times per
# mirror (default 3) after UNMW_FETCH_RETRY_PAUSE seconds (default 10),
# resuming the partial file; a mirror that does not honour the resume request
# is restarted from scratch. A complete file with the wrong checksum is deleted
# and the next mirror is tried at once. Nothing is ever resumed across mirrors
# or from an earlier run, and no file is left behind on failure.
set -euo pipefail
if [[ -n ${GATEWAY_INTERFACE:-}${REQUEST_METHOD:-} ]]; then
 echo 'ERROR: not a CGI' >&2
 exit 1
fi
attempts=${UNMW_FETCH_ATTEMPTS:-3}
pause=${UNMW_FETCH_RETRY_PAUSE:-10}
if [[ $# -lt 3 || ! $2 =~ ^[0-9a-f]{64}$ || ! $attempts =~ ^[1-9][0-9]*$ || ! $pause =~ ^[0-9]+$ ]]; then
 echo 'Usage: [UNMW_FETCH_ATTEMPTS=N] [UNMW_FETCH_RETRY_PAUSE=SECONDS] bash tests/fetch_fixture.sh DEST_FILE SHA256 URL [URL...]' >&2
 exit 2
fi
dest=$1
expected=$2
shift 2
# However this script ends, a failure leaves no file behind.
trap 'rm -f -- "$dest"' EXIT
for url in "$@"; do
 # Never resume a partial file left by a different mirror or an earlier run.
 rm -f -- "$dest"
 for (( attempt = 1; attempt <= attempts; attempt++ )); do
  if (( attempt > 1 )); then
   sleep "$pause"
  fi
  status=0
  curl --fail --location --ipv4 --silent --show-error --connect-timeout 10 --max-time 600 \
   --speed-limit 1024 --speed-time 60 --continue-at - --output "$dest" "$url" || status=$?
  if (( status == 0 )); then
   if printf '%s  %s\n' "$expected" "$dest" | sha256sum --check --strict --status; then
    trap - EXIT
    echo "Downloaded $dest from $url (SHA256 verified)"
    exit 0
   fi
   # A complete answer with the wrong content: show what it was, next mirror.
   echo "Rejected $url: $(stat -c %s -- "$dest") bytes, SHA256 $(sha256sum -- "$dest" | cut -c1-64) is not $expected" >&2
   echo " $(file -b -- "$dest" 2>/dev/null || true): $(head -c 300 -- "$dest" | tr -c '\40-\176' ' ')" >&2
   continue 2
  fi
  echo "Download attempt $attempt of $attempts from $url failed (curl exit $status)" >&2
  if (( status == 33 )); then
   # The server did not honour the resume request: start this mirror over.
   rm -f -- "$dest"
  fi
 done
 echo "Giving up on $url" >&2
done
echo "ERROR: no mirror served $dest with SHA256 $expected" >&2
exit 1

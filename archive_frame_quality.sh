#!/usr/bin/env bash
#
# archive_frame_quality.sh - judge the archived frames that sort_workdir_images.sh placed in
# <workdir>/good_wcs_fd_images/img_<S>/ and move the poor ones out of the archive:
#
#   KEEP    clearly good: the frame stays where it is
#   REVIEW  borderline:   moved to <workdir>/frame_quality/review/<reason>/
#   REJECT  clearly poor: moved to <workdir>/frame_quality/rejected/<reason>/
#
# The review and rejected folders hold the frames directly (no subdirectories) plus good/ and
# bad/, so that a folder can be gone through with
#   for i in *.fits ;do echo $i ; ~/vast_test/vast/pgfv -- "$i" &>/dev/null && mv -v -- "$i" good/ && continue ; mv -v -- "$i" bad/ ;done
# A frame put into good/ returns to its good_wcs_fd_images/img_<S>/ with the next --execute run
# and is never judged again; a frame in bad/ stays there to be deleted by hand. This script
# never deletes anything.
#
# Every frame is measured once by VaST's util/archive_frame_quality (one SExtractor pass, the
# local Tycho-2 catalog, the Sun and Moon positions): the line of results goes to
# frame_quality/measurements.txt. The verdicts are worked out anew in every run from all the
# measurements, because they compare each frame with the other frames of its camera and field:
#
#  1. twilight: the Sun above TWILIGHT_SUN_ALT_DEG (-18 deg)                    -> REJECT
#  2. moon:     moonlight brightens the sky at the field by more than
#               MOON_BRIGHTENING_MAG (0.5 mag; Krisciunas & Schaefer 1991)       -> REJECT
#  3. no_stars, obstructed, focus, trailed: no Tycho-2 zero point; a cell of the frame that
#               lost more than half of the Tycho-2 stars it normally shows (clouds, roof, dew);
#               star images twice the usual size of the camera; elongated star images -> REJECT
#  4. transparency: the Tycho-2 zero point of every cell, corrected for the airmass of the
#               cell with the extinction coefficient fitted for the camera (the clear-sky
#               upper envelope of the zero point against airmass), and the airmass-corrected
#               limiting magnitude, against the clear level of the same field (the
#               BASELINE_PERCENTILE of the field's dark-time frames, cell by cell, so fields
#               that are never high in the sky are judged by their own best frames):
#               within KEEP_* -> KEEP, beyond REJECT_* -> REJECT (clouds, shallow), between ->
#               REVIEW; the two frames of an upload that disagree by more than
#               PAIR_MAX_ZP_DIFFERENCE_MAG: the worse one -> REVIEW (pair_mismatch)
#  5. a frame that would be kept but is more than FLOOR_BELOW_CAMERA_CLEAR_MAG (1 mag) below
#               the camera's airmass-corrected clear sky -> REVIEW (the best frames of a field
#               that is never clear are not kept unseen)
#
# Built-in checks stop the run before anything is moved: for every camera, frames taken in
# twilight and in moonlight must have a brighter sky than dark-time frames (otherwise the
# site, the clock or the ephemeris is wrong), and the computed altitude of the frame centre
# must agree with the CENTALT header keyword where the camera writes it.
#
# Coordinated with VaST: util/archive_frame_quality (src/archive_frame_quality.c) measures,
# util/sun_moon and util/examples/test_sun_moon_ephemeris.sh check the ephemeris against astropy.
# The camera settings (SExtractor configuration, site, extinction) are in
# archive_frame_quality_cameras.txt next to this script.
#
# Frames moved out of good_wcs_fd_images/ are not seen by coord_forced_photometry.py until they
# return from a good/ folder. Uncalibrated frames are never touched: only wcs_fd_* frames in
# good_wcs_fd_images/ are considered.
#
# DRY RUN BY DEFAULT: frames are measured (that is kept), but nothing is moved without
# --execute. Run it as the owner of the work directory (sudo -u apache); a lock file keeps two
# runs from overlapping. Never a CGI entry point.
#
# Usage:
#   ./archive_frame_quality.sh [options]
#     --execute               move the frames (default: measure and report only)
#     --measure-only          measure the new frames and stop (for the first pass over the archive)
#     --workdir DIR           work directory (default: IMAGE_DATA_ROOT from local_config.sh)
#     --vast DIR              VaST directory with util/archive_frame_quality
#                             (default: VAST_REFERENCE_COPY from local_config.sh)
#     --jobs N                frames measured in parallel (default 4)
#     --limit N               measure at most N new frames in this run
#     --make-label-sample DIR N
#                             hard-link N frames (a third of each verdict where possible) into DIR,
#                             with DIR/good and DIR/bad, to be sorted by eye (DIR must be on the
#                             same file system as the archive)
#     --read-labels DIR       record the frames sorted into DIR/good and DIR/bad as labels
#     -h, --help
#
# Exit codes: 0 done, 1 usage or configuration error, 2 a measurement or a move failed,
# 3 a built-in check failed (nothing was moved).

# Not a CGI entry point.
if [ -n "${GATEWAY_INTERFACE:-}" ] || [ -n "${REQUEST_METHOD:-}" ]; then
 printf 'Content-Type: text/plain\n\nERROR: this script must not be run as CGI\n'
 exit 1
fi

set -o nounset
set -o pipefail
LC_ALL=C
export LC_ALL

SCRIPTDIR=$(dirname "$(readlink -f "$0")")

##### Thresholds #####
# The user's decisions of 2026-11-23: twilight, moonlight and the floor below the camera's
# clear sky. The other limits are starting values, to be set from frames sorted by eye.
TWILIGHT_SUN_ALT_DEG=-18
MOON_BRIGHTENING_MAG=0.5
FLOOR_BELOW_CAMERA_CLEAR_MAG=1.0
# The zero-point deficits of the cells against the clear level of the field: the median cell
# (the transparency of the whole frame) and the third-worst cell (a patch of cloud; a single
# cell is too noisy: the worst of 24 cells of a clear frame was 0.27-0.34 mag in the test)
KEEP_MAX_MEDIAN_DEFICIT_MAG=0.2
REJECT_MIN_MEDIAN_DEFICIT_MAG=0.75
KEEP_MAX_PATCH_DEFICIT_MAG=0.4
REJECT_MIN_PATCH_DEFICIT_MAG=1.0
# a field with too few dark-time frames: the frame zero point against the camera clear sky
KEEP_MAX_CAMERA_DEFICIT_MAG=0.3
REJECT_MIN_CAMERA_DEFICIT_MAG=1.0
KEEP_MAX_LIM_DEFICIT_MAG=0.5
REJECT_MIN_LIM_DEFICIT_MAG=1.0
# the fraction of the Tycho-2 stars found in a cell, below the usual fraction of that cell
KEEP_MAX_FOUND_DEFICIT=0.2
REJECT_MIN_FOUND_DEFICIT=0.5
PAIR_MAX_ZP_DIFFERENCE_MAG=0.3
FOCUS_MAX_FWHM_FACTOR=2.0
TRAILED_MIN_ELONGATION=1.6
BASELINE_PERCENTILE=90
MIN_FIELD_FRAMES=5
# the extinction fit: airmass bins with enough cells, enough bins over enough airmass
KFIT_MIN_CELLS_PER_BIN=20
KFIT_MIN_BINS=3
KFIT_MIN_AIRMASS_SPAN=0.75
KFIT_MIN_K=0.02
KFIT_MAX_K=0.5
# the built-in checks
SANITY_MIN_FRAMES=10
SANITY_MIN_SKY_EXCESS_MAG=0.2
SANITY_MAX_ALT_DIFFERENCE_DEG=0.5
SANITY_MIN_GOOD_ALT_FRACTION=0.95

die() {
 echo "ERROR: $*" >&2
 exit 1
}
log() {
 echo "$(date '+%Y-%m-%d %H:%M:%S') $*"
}
usage() {
 sed -n '2,/^# Exit codes/p' "$0" | sed 's/^# \{0,1\}//'
}

EXECUTE=0
MEASURE_ONLY=0
WORKDIR=""
VAST_DIR=""
JOBS=4
LIMIT=0
LABEL_SAMPLE_DIR=""
LABEL_SAMPLE_N=0
READ_LABELS_DIR=""
while [ $# -gt 0 ]; do
 case "$1" in
  --execute) EXECUTE=1 ;;
  --measure-only) MEASURE_ONLY=1 ;;
  --workdir) shift; WORKDIR="${1:-}" ;;
  --vast) shift; VAST_DIR="${1:-}" ;;
  --jobs) shift; JOBS="${1:-}" ;;
  --limit) shift; LIMIT="${1:-}" ;;
  --make-label-sample) shift; LABEL_SAMPLE_DIR="${1:-}"; shift; LABEL_SAMPLE_N="${1:-}" ;;
  --read-labels) shift; READ_LABELS_DIR="${1:-}" ;;
  -h|--help) usage; exit 0 ;;
  *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
 esac
 shift
done
[[ "$JOBS" =~ ^[0-9]+$ ]] && [ "$JOBS" -ge 1 ] || die "--jobs wants a positive number"
[[ "$LIMIT" =~ ^[0-9]+$ ]] || die "--limit wants a number"
if [ -n "$LABEL_SAMPLE_DIR" ]; then
 [[ "$LABEL_SAMPLE_N" =~ ^[0-9]+$ ]] && [ "$LABEL_SAMPLE_N" -ge 3 ] || die "--make-label-sample wants DIR and a number of frames (3 or more)"
fi
for cmd in find awk sort flock readlink mv ln xargs od; do
 command -v "$cmd" >/dev/null 2>&1 || die "required command '$cmd' is not installed"
done

##### Configuration #####
# Read single variables from local_config.sh in a subshell (sourcing it also activates the
# Python venv and exports notification settings)
read_config_var() {
 local cfg="$SCRIPTDIR/local_config.sh"
 [ -s "$cfg" ] || return 0
 # shellcheck disable=SC1090
 ( set +u; source "$cfg" >/dev/null 2>&1; printf '%s' "${!1:-}" ) 2>/dev/null
}
if [ -z "$WORKDIR" ]; then
 WORKDIR=$(read_config_var IMAGE_DATA_ROOT)
 if [ -z "$WORKDIR" ] && [ -d "$SCRIPTDIR/uploads" ]; then
  WORKDIR="$SCRIPTDIR/uploads"
 fi
fi
[ -n "$WORKDIR" ] || die "no work directory: set IMAGE_DATA_ROOT in local_config.sh or pass --workdir"
[ -d "$WORKDIR" ] || die "work directory $WORKDIR does not exist"
WORKDIR=$(readlink -f "$WORKDIR")
if [ -z "$VAST_DIR" ]; then
 VAST_DIR=$(read_config_var VAST_REFERENCE_COPY)
fi
[ -n "$VAST_DIR" ] || die "no VaST directory: set VAST_REFERENCE_COPY in local_config.sh or pass --vast"
VAST_DIR=$(readlink -f "$VAST_DIR")
[ -x "$VAST_DIR/util/archive_frame_quality" ] || die "$VAST_DIR/util/archive_frame_quality is missing - update and rebuild VaST there"
CAMERA_TABLE="$SCRIPTDIR/archive_frame_quality_cameras.txt"
[ -s "$CAMERA_TABLE" ] || die "the camera settings $CAMERA_TABLE are missing"

GOOD_DIR="$WORKDIR/good_wcs_fd_images"
QUALITY_DIR="$WORKDIR/frame_quality"
MEASUREMENTS="$QUALITY_DIR/measurements.txt"
LABELS="$QUALITY_DIR/labels.txt"
PENDING_DIR="$QUALITY_DIR/pending"
[ -d "$GOOD_DIR" ] || die "$GOOD_DIR does not exist - run sort_workdir_images.sh first"
mkdir -p "$PENDING_DIR" || die "cannot create $PENDING_DIR"
touch "$MEASUREMENTS" "$LABELS" || die "cannot write in $QUALITY_DIR"

exec 9>"$QUALITY_DIR/.archive_frame_quality.lock" || die "cannot create the lock file"
flock -n 9 || die "another archive_frame_quality.sh is running"

RUN_STAMP=$(date '+%Y%m%d_%H%M%S')
WORK_TMP=$(mktemp -d "$QUALITY_DIR/.run_${RUN_STAMP}_XXXXXX") || die "cannot create a temporary directory"
trap 'rm -rf "$WORK_TMP"' EXIT

log "archive_frame_quality.sh $( [ $EXECUTE -eq 1 ] && echo EXECUTE || echo 'DRY RUN (nothing is moved; add --execute)')"
log "work directory $WORKDIR, VaST $VAST_DIR"

# Move the complete pending measurement lines into the measurements file
merge_pending() {
 local f
 for f in "$PENDING_DIR"/*.afq; do
  [ -f "$f" ] || continue
  if grep -q ' frame=' "$f" && [ "$(tail -c 1 "$f" | od -An -c | tr -d ' ')" = '\n' ]; then
   cat "$f" >> "$MEASUREMENTS"
  fi
  rm -f "$f"
 done
}
merge_pending

# The upload directory a measured frame belongs to (from the measurements file)
img_dir_of_frame() {
 awk -v f="$1" '{ for (i = 1; i <= NF; i++) { if ($i == "frame=" f) { for (j = 1; j <= NF; j++) if (substr($j, 1, 8) == "img_dir=") { print substr($j, 9) ; exit } } } }' "$MEASUREMENTS"
}

##### Labels: frames sorted by eye #####
add_label() {
 # Arguments: FRAME_NAME good|bad
 printf '%s\t%s\t%s\n' "$1" "$2" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LABELS"
}
N_RETURNED=0
for SORTED in "$QUALITY_DIR"/review/*/good/*.fits "$QUALITY_DIR"/rejected/*/good/*.fits; do
 [ -f "$SORTED" ] || continue
 NAME=$(basename "$SORTED")
 IMG=$(img_dir_of_frame "$NAME")
 if [ -z "$IMG" ]; then
  log "WARNING: $SORTED is not in the measurements - left where it is"
  continue
 fi
 if [ $EXECUTE -eq 1 ]; then
  if [ -e "$GOOD_DIR/$IMG/$NAME" ]; then
   log "WARNING: $GOOD_DIR/$IMG/$NAME exists already - $SORTED left where it is"
   continue
  fi
  mkdir -p "$GOOD_DIR/$IMG" && mv -n "$SORTED" "$GOOD_DIR/$IMG/" && [ -f "$GOOD_DIR/$IMG/$NAME" ] || { log "ERROR: could not return $SORTED to $GOOD_DIR/$IMG/"; exit 2; }
  add_label "$NAME" good
  N_RETURNED=$((N_RETURNED + 1))
 else
  log "would return (sorted as good): $SORTED -> $GOOD_DIR/$IMG/"
 fi
done
for SORTED in "$QUALITY_DIR"/review/*/bad/*.fits "$QUALITY_DIR"/rejected/*/bad/*.fits; do
 [ -f "$SORTED" ] || continue
 NAME=$(basename "$SORTED")
 if ! awk -F'\t' -v f="$NAME" '$1 == f && $2 == "bad" {found = 1} END {exit found ? 0 : 1}' "$LABELS"; then
  add_label "$NAME" bad
 fi
done
if [ -n "$READ_LABELS_DIR" ]; then
 [ -d "$READ_LABELS_DIR/good" ] || [ -d "$READ_LABELS_DIR/bad" ] || die "$READ_LABELS_DIR has neither good/ nor bad/"
 N_READ=0
 for SORTED in "$READ_LABELS_DIR"/good/*.fits; do
  [ -f "$SORTED" ] || continue
  add_label "$(basename "$SORTED")" good
  N_READ=$((N_READ + 1))
 done
 for SORTED in "$READ_LABELS_DIR"/bad/*.fits; do
  [ -f "$SORTED" ] || continue
  add_label "$(basename "$SORTED")" bad
  N_READ=$((N_READ + 1))
 done
 log "recorded $N_READ labels from $READ_LABELS_DIR"
fi
[ $N_RETURNED -gt 0 ] && log "returned $N_RETURNED frames sorted as good to good_wcs_fd_images/"

##### Measure the new frames #####
awk '{ for (i = 1; i <= NF; i++) if (substr($i, 1, 6) == "frame=") print substr($i, 7) }' "$MEASUREMENTS" | sort -u > "$WORK_TMP/measured.txt"
find "$GOOD_DIR" -mindepth 2 -maxdepth 2 -type f -name 'wcs_fd_*.fits' | sort > "$WORK_TMP/archive_frames.txt"
# (getline, not the NR == FNR idiom, which fails when the first file is empty)
awk -v measured_file="$WORK_TMP/measured.txt" 'BEGIN { while ((getline line < measured_file) > 0) measured[line] = 1 } { n = split($0, p, "/") ; if (!(p[n] in measured)) print }' "$WORK_TMP/archive_frames.txt" > "$WORK_TMP/to_measure.txt"
if [ "$LIMIT" -gt 0 ]; then
 head -n "$LIMIT" "$WORK_TMP/to_measure.txt" > "$WORK_TMP/to_measure_limited.txt"
 mv "$WORK_TMP/to_measure_limited.txt" "$WORK_TMP/to_measure.txt"
fi
N_TO_MEASURE=$(wc -l < "$WORK_TMP/to_measure.txt" | tr -d ' ')
log "$(wc -l < "$WORK_TMP/archive_frames.txt" | tr -d ' ') frames in good_wcs_fd_images/, $N_TO_MEASURE to measure ($JOBS at a time)"

# Measure one frame; the line goes to PENDING_DIR first, so an interrupted run loses nothing
measure_one_frame() {
 local frame="$1"
 local img
 local name
 local pattern
 local sex_config
 local site
 local extinction
 local dark_sky
 local site_option
 local matched
 img=$(basename "$(dirname "$frame")")
 name=$(basename "$frame")
 matched=0
 # the first camera line whose glob matches the upload name
 while read -r pattern sex_config site extinction dark_sky; do
  case "$pattern" in
   ''|'#'*) continue ;;
  esac
  # shellcheck disable=SC2254
  case "$img" in
   $pattern) matched=1 ; break ;;
  esac
 done < "$CAMERA_TABLE"
 if [ $matched -eq 0 ] || [ -z "$sex_config" ]; then
  echo "WARNING: no camera line matches $img in $CAMERA_TABLE - $name not measured" >&2
  return 0
 fi
 site_option=""
 if [ "$site" != "header" ]; then
  site_option="--site $site"
 fi
 # shellcheck disable=SC2086
 if ( cd "$VAST_DIR" && util/archive_frame_quality "$frame" --sex-config "$sex_config" --extinction "$extinction" --dark-sky "$dark_sky" $site_option ) > "$PENDING_DIR/$name.afq.tmp" 2> "$PENDING_DIR/$name.afq.err"; then
  printf 'img_dir=%s %s\n' "$img" "$(tail -n 1 "$PENDING_DIR/$name.afq.tmp")" > "$PENDING_DIR/$name.afq.new"
  mv -f "$PENDING_DIR/$name.afq.new" "$PENDING_DIR/$name.afq"
  rm -f "$PENDING_DIR/$name.afq.tmp" "$PENDING_DIR/$name.afq.err"
 else
  echo "WARNING: util/archive_frame_quality failed on $frame: $(tail -n 2 "$PENDING_DIR/$name.afq.err" | tr '\n' ' ')" >&2
  rm -f "$PENDING_DIR/$name.afq.tmp"
 fi
 return 0
}
export -f measure_one_frame
export VAST_DIR PENDING_DIR CAMERA_TABLE
if [ "$N_TO_MEASURE" -gt 0 ]; then
 tr '\n' '\0' < "$WORK_TMP/to_measure.txt" | xargs -0 -P "$JOBS" -n 1 bash -c 'measure_one_frame "$1"' _
 merge_pending
 log "measured; $(wc -l < "$MEASUREMENTS" | tr -d ' ') frames in $MEASUREMENTS"
fi
if [ $MEASURE_ONLY -eq 1 ]; then
 log "--measure-only: stopping here"
 exit 0
fi

##### Judge all measured frames #####
# Count, median and BASELINE_PERCENTILE of samples "type<TAB>key<TAB>value" (stdin), per type
# and key, as "type<TAB>key<TAB>n<TAB>median<TAB>percentile"
percentiles() {
 sort -t "$(printf '\t')" -k1,1 -k2,2 -k3,3n | awk -F'\t' -v pct="$BASELINE_PERCENTILE" '
 function flush() {
  if (n > 0) {
   im = int(0.5 * n + 0.999999) ; if (im < 1) im = 1
   ip = int(pct / 100.0 * n + 0.999999) ; if (ip < 1) ip = 1 ; if (ip > n) ip = n
   printf "%s\t%s\t%d\t%s\t%s\n", type, key, n, val[im], val[ip]
  }
  n = 0
 }
 { if ($1 != type || $2 != key) { flush() ; type = $1 ; key = $2 } ; n++ ; val[n] = $3 }
 END { flush() }'
}

# Pass 1: one tab-separated line per frame (frames_raw.tsv) with the cell zero points taken
# back to raw (the measuring tool corrected them with the camera table's extinction), and the
# samples for the extinction fit: the raw cell zero points of the dark-time frames per camera
# and airmass bin. The last label of a frame counts. (The labels are read with getline: the
# NR == FNR idiom fails when the first file is empty.)
awk -v twilight="$TWILIGHT_SUN_ALT_DEG" -v moon="$MOON_BRIGHTENING_MAG" -v kfit_samples="$WORK_TMP/kfit_samples.tsv" -v labels_file="$LABELS" '
BEGIN {
 FS = "\n" ; twilight += 0 ; moon += 0
 while ((getline line < labels_file) > 0) { split(line, l, "\t") ; label[l[1]] = l[2] }
 close(labels_file)
 printf "" > kfit_samples
}
{
 split("", v)
 n = split($0, w, " ")
 for (i = 1; i <= n; i++) { p = index(w[i], "=") ; if (p > 1) v[substr(w[i], 1, p - 1)] = substr(w[i], p + 1) }
 frame = v["frame"]
 if (frame == "" || frame in seen) next
 seen[frame] = 1
 camera = v["camera"]
 # the field: the frame name without the wcs_ and fd_ prefixes, up to the date
 name = frame ; sub(/^wcs_/, "", name) ; sub(/^fd_/, "", name)
 if (match(name, /_[12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]/)) field = substr(name, 1, RSTART - 1)
 else field = v["object"]
 lab = (frame in label) ? label[frame] : "none"
 sun = v["sun_alt"] + 0
 dm = v["moon_sky_brightening"] + 0
 dark = (sun <= twilight && dm <= moon) ? 1 : 0
 ok = (v["status"] == "OK") ? 1 : 0
 k_tool = v["extinction"] + 0
 nc = split(v["cell_zp_corr"], c, ",")
 split(v["cell_airmass"], x, ",")
 raw = ""
 for (i = 1; i <= nc; i++) {
  if (c[i] == "NA") r = "NA"
  else {
   r = sprintf("%.4f", c[i] - k_tool * x[i])
   if (ok && dark && lab != "bad") {
    xi = x[i] + 0
    if (xi < 2) b = int(xi * 4) / 4
    else if (xi < 4) b = int(xi * 2) / 2
    else b = int(xi)
    print "KFIT\t" camera "|" b "\t" r > kfit_samples
    print "KFITX\t" camera "|" b "\t" xi > kfit_samples
   }
  }
  raw = raw (i > 1 ? "," : "") r
 }
 printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", frame, v["img_dir"], camera, field, v["status"], v["sun_alt"], v["moon_sky_brightening"], v["lim_mag"], v["fwhm"], v["elongation"], v["sky_rate"], v["center_alt"], v["header_centalt"], lab, v["center_airmass"], raw, v["cell_airmass"], v["cell_found"], dark, ok, k_tool
}' "$MEASUREMENTS" > "$WORK_TMP/frames_raw.tsv"

# The extinction coefficient of every camera: a straight line through the clear-sky upper
# envelope (the BASELINE_PERCENTILE of the raw cell zero points) of the airmass bins
percentiles < "$WORK_TMP/kfit_samples.tsv" > "$WORK_TMP/kfit_bins.tsv"
awk -F'\t' -v min_cells="$KFIT_MIN_CELLS_PER_BIN" -v min_bins="$KFIT_MIN_BINS" -v min_span="$KFIT_MIN_AIRMASS_SPAN" -v kmin="$KFIT_MIN_K" -v kmax="$KFIT_MAX_K" '
$1 == "KFIT" && $3 >= min_cells + 0 { zp[$2] = $5 }
$1 == "KFITX" { xm[$2] = $4 }
END {
 for (key in zp) {
  if (!(key in xm)) continue
  split(key, p, "|") ; cam = p[1]
  nb[cam]++ ; sx[cam] += xm[key] ; sy[cam] += zp[key] ; sxx[cam] += xm[key] * xm[key] ; sxy[cam] += xm[key] * zp[key]
  if (!(cam in lo) || xm[key] < lo[cam]) lo[cam] = xm[key]
  if (!(cam in hi) || xm[key] > hi[cam]) hi[cam] = xm[key]
 }
 for (cam in nb) {
  if (nb[cam] < min_bins + 0 || hi[cam] - lo[cam] < min_span + 0) continue
  den = nb[cam] * sxx[cam] - sx[cam] * sx[cam]
  if (den <= 0) continue
  k = -1.0 * (nb[cam] * sxy[cam] - sx[cam] * sy[cam]) / den
  if (k < kmin + 0) k = kmin + 0
  if (k > kmax + 0) k = kmax + 0
  printf "%s\t%.4f\t%d\t%.2f\t%.2f\n", cam, k, nb[cam], lo[cam], hi[cam]
 }
}' "$WORK_TMP/kfit_bins.tsv" > "$WORK_TMP/kfit.tsv"

# Pass 2: the zero points corrected with the fitted extinction coefficient (the median of the
# cells for the frame), the airmass-corrected limiting magnitude and the found fractions of
# the cells (frames.tsv), and the samples of the baselines (samples.tsv)
awk -F'\t' -v kfit_file="$WORK_TMP/kfit.tsv" -v samples="$WORK_TMP/samples.tsv" -v twilight="$TWILIGHT_SUN_ALT_DEG" -v moon="$MOON_BRIGHTENING_MAG" '
function median_of_list(list,   n, a, i, j, t, cnt, s) {
 n = split(list, a, ",") ; cnt = 0 ; split("", s)
 for (i = 1; i <= n; i++) if (a[i] != "NA") { cnt++ ; s[cnt] = a[i] + 0 }
 if (cnt == 0) return "NA"
 for (i = 2; i <= cnt; i++) { t = s[i] ; j = i - 1 ; while (j >= 1 && s[j] > t) { s[j + 1] = s[j] ; j-- } ; s[j + 1] = t }
 if (cnt % 2 == 1) return s[(cnt + 1) / 2]
 return (s[cnt / 2] + s[cnt / 2 + 1]) / 2
}
BEGIN {
 twilight += 0 ; moon += 0
 while ((getline line < kfit_file) > 0) { split(line, q, "\t") ; kfit[q[1]] = q[2] + 0 }
 close(kfit_file)
 printf "" > samples
}
{
 frame = $1 ; camera = $3 ; field = $4 ; sun = $6 + 0 ; dm = $7 + 0 ; lab = $14 ; dark = $19 ; ok = $20
 k = (camera in kfit) ? kfit[camera] : $21 + 0
 nc = split($16, r, ",") ; split($17, x, ",") ; split($18, fnd, ",")
 cells = "" ; found = ""
 for (i = 1; i <= nc; i++) {
  if (r[i] == "NA") cz = "NA"
  else cz = sprintf("%.4f", r[i] + k * x[i])
  cells = cells (i > 1 ? "," : "") cz
  split(fnd[i], nn, "/")
  if (nn[2] + 0 >= 5) ff = sprintf("%.3f", nn[1] / nn[2])
  else ff = "NA"
  found = found (i > 1 ? "," : "") ff
  if (ok && dark && lab != "bad") {
   if (cz != "NA") print "CELL\t" camera "|" field "|" i "\t" cz > samples
   if (ff != "NA") print "FOUND\t" camera "|" field "|" i "\t" ff > samples
  }
 }
 zp = (ok ? median_of_list(cells) : "NA")
 lim = "NA"
 if ($8 != "NA" && $8 != "") lim = sprintf("%.3f", $8 + k * ($15 - 1.0))
 if (ok && dark && lab != "bad") {
  print "NFIELD\t" camera "|" field "\t1" > samples
  if (zp != "NA") print "CAMZP\t" camera "\t" zp > samples
  if ($9 != "NA") print "CAMFWHM\t" camera "\t" $9 > samples
  if (lim != "NA") print "LIM\t" camera "|" field "\t" lim > samples
 }
 # the sky of dark, twilight and moonlit frames for the built-in check
 if ($11 != "") {
  if (sun > twilight) print "SKY\t" camera "|twilight\t" $11 > samples
  else if (dm > moon) print "SKY\t" camera "|moon\t" $11 > samples
  else if (dm < 0.05) print "SKY\t" camera "|dark\t" $11 > samples
 }
 printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%.4f\n", frame, $2, camera, field, $5, $6, $7, zp, lim, $9, $10, $11, $12, $13, lab, cells, found, k
}' "$WORK_TMP/frames_raw.tsv" > "$WORK_TMP/frames.tsv"
percentiles < "$WORK_TMP/samples.tsv" > "$WORK_TMP/baselines.tsv"

# Pass 3: the verdicts. verdicts.tsv: frame, img_dir, verdict, reason, camera, field, details
# frames.tsv: 1 frame 2 img_dir 3 camera 4 field 5 status 6 sun_alt 7 moon_brightening 8 zp_corr
# 9 lim_corr 10 fwhm 11 elongation 12 sky_rate 13 center_alt 14 header_centalt 15 label
# 16 cell zp_corr 17 cell found fraction 18 extinction
awk -F'\t' -v twilight="$TWILIGHT_SUN_ALT_DEG" -v moon="$MOON_BRIGHTENING_MAG" \
 -v floor_mag="$FLOOR_BELOW_CAMERA_CLEAR_MAG" -v keep_median="$KEEP_MAX_MEDIAN_DEFICIT_MAG" -v reject_median="$REJECT_MIN_MEDIAN_DEFICIT_MAG" \
 -v keep_patch="$KEEP_MAX_PATCH_DEFICIT_MAG" -v reject_patch="$REJECT_MIN_PATCH_DEFICIT_MAG" \
 -v keep_cam="$KEEP_MAX_CAMERA_DEFICIT_MAG" -v reject_cam="$REJECT_MIN_CAMERA_DEFICIT_MAG" \
 -v keep_lim="$KEEP_MAX_LIM_DEFICIT_MAG" -v reject_lim="$REJECT_MIN_LIM_DEFICIT_MAG" -v keep_found="$KEEP_MAX_FOUND_DEFICIT" \
 -v reject_found="$REJECT_MIN_FOUND_DEFICIT" -v pair_max="$PAIR_MAX_ZP_DIFFERENCE_MAG" -v fwhm_factor="$FOCUS_MAX_FWHM_FACTOR" \
 -v trailed="$TRAILED_MIN_ELONGATION" -v min_field="$MIN_FIELD_FRAMES" -v baselines_file="$WORK_TMP/baselines.tsv" '
BEGIN {
 twilight += 0 ; moon += 0 ; floor_mag += 0 ; keep_median += 0 ; reject_median += 0 ; keep_patch += 0 ; reject_patch += 0
 keep_cam += 0 ; reject_cam += 0 ; keep_lim += 0 ; reject_lim += 0
 keep_found += 0 ; reject_found += 0 ; pair_max += 0 ; fwhm_factor += 0 ; trailed += 0 ; min_field += 0
 while ((getline line < baselines_file) > 0) {
  split(line, b, "\t") ; key = b[1] "|" b[2]
  n_base[key] = b[3] ; median_base[key] = b[4] ; pct_base[key] = b[5]
 }
 close(baselines_file)
}
{
 frame = $1 ; img = $2 ; camera = $3 ; field = $4 ; status = $5 ; sun = $6 + 0 ; dm = $7 + 0
 zp = $8 ; lim = $9 ; fwhm = $10 ; elong = $11 ; lab = $15 ; cells = $16 ; found = $17
 verdict = "" ; reason = "" ; details = ""
 cam_clear = ("CAMZP|" camera in pct_base) ? pct_base["CAMZP|" camera] : "NA"
 cam_fwhm = ("CAMFWHM|" camera in median_base) ? median_base["CAMFWHM|" camera] : "NA"
 n_field = ("NFIELD|" camera "|" field in n_base) ? n_base["NFIELD|" camera "|" field] + 0 : 0
 # the largest loss of Tycho-2 stars in a cell against the usual fraction of that cell (1 for a
 # field with few frames)
 max_found_deficit = 0
 nfc = split(found, ff, ",")
 for (i = 1; i <= nfc; i++) {
  if (ff[i] == "NA") continue
  key = "FOUND|" camera "|" field "|" i
  usual = 1.0
  if ((key in pct_base) && n_base[key] + 0 >= min_field) usual = pct_base[key] + 0
  d = usual - ff[i]
  if (d > max_found_deficit) max_found_deficit = d
 }
 if (lab == "good") { verdict = "KEEP" ; reason = "sorted_as_good" }
 else if (lab == "bad") { verdict = "REJECT" ; reason = "sorted_as_bad" }
 else if (sun > twilight) { verdict = "REJECT" ; reason = "twilight" ; details = "sun_alt=" sun }
 else if (dm > moon) { verdict = "REJECT" ; reason = "moon" ; details = "moon_sky_brightening=" dm }
 else if (status != "OK" || zp == "NA") { verdict = "REJECT" ; reason = "no_stars" }
 else if (max_found_deficit >= reject_found) { verdict = "REJECT" ; reason = "obstructed" ; details = "max_found_deficit=" sprintf("%.3f", max_found_deficit) }
 else if (fwhm != "NA" && cam_fwhm != "NA" && fwhm + 0 > fwhm_factor * cam_fwhm) { verdict = "REJECT" ; reason = "focus" ; details = "fwhm=" fwhm " camera_median=" cam_fwhm }
 else if (elong != "NA" && elong + 0 > trailed) { verdict = "REJECT" ; reason = "trailed" ; details = "elongation=" elong }
 else {
  # transparency against the clear level of the field, cell by cell (sorted deficits: the
  # median cell and the third-worst cell), and the limiting magnitude
  n_cmp = 0 ; split("", deficit)
  nc = split(cells, c, ",")
  for (i = 1; i <= nc; i++) {
   key = "CELL|" camera "|" field "|" i
   if (c[i] == "NA" || !(key in pct_base) || n_base[key] + 0 < min_field) continue
   n_cmp++ ; deficit[n_cmp] = pct_base[key] - c[i]
  }
  for (i = 2; i <= n_cmp; i++) { t = deficit[i] ; j = i - 1 ; while (j >= 1 && deficit[j] > t) { deficit[j + 1] = deficit[j] ; j-- } ; deficit[j + 1] = t }
  median_deficit = 0 ; patch_deficit = 0
  if (n_cmp > 0) {
   if (n_cmp % 2 == 1) median_deficit = deficit[(n_cmp + 1) / 2]
   else median_deficit = (deficit[n_cmp / 2] + deficit[n_cmp / 2 + 1]) / 2
   patch_deficit = deficit[n_cmp]
   if (n_cmp >= 3) patch_deficit = deficit[n_cmp - 2]
  }
  lim_deficit = -99
  key = "LIM|" camera "|" field
  if (lim != "NA" && (key in pct_base) && n_base[key] + 0 >= min_field) lim_deficit = pct_base[key] - lim
  found_text = " max_found_deficit=" sprintf("%.3f", max_found_deficit)
  if (n_field < min_field || n_cmp == 0) {
   # a field with too few dark-time frames: the camera clear sky alone
   if (cam_clear == "NA") { verdict = "REVIEW" ; reason = "no_baseline" }
   else {
    d = cam_clear - zp
    details = "below_camera_clear=" sprintf("%.3f", d) " field_frames=" n_field found_text
    if (d >= reject_cam) { verdict = "REJECT" ; reason = "clouds" }
    else if (d <= keep_cam && max_found_deficit <= keep_found) { verdict = "KEEP" ; reason = "clear" }
    else { verdict = "REVIEW" ; reason = "few_field_frames" }
   }
  } else {
   lim_text = "NA"
   if (lim_deficit != -99) lim_text = sprintf("%.3f", lim_deficit)
   details = "median_cell_deficit=" sprintf("%.3f", median_deficit) " patch_deficit=" sprintf("%.3f", patch_deficit) " lim_deficit=" lim_text found_text
   if (median_deficit >= reject_median || patch_deficit >= reject_patch) { verdict = "REJECT" ; reason = "clouds" }
   else if (lim_deficit >= reject_lim) { verdict = "REJECT" ; reason = "shallow" }
   else if (median_deficit <= keep_median && patch_deficit <= keep_patch && lim_deficit <= keep_lim && max_found_deficit <= keep_found) { verdict = "KEEP" ; reason = "clear" }
   else { verdict = "REVIEW" ; reason = "transparency" }
  }
  # the floor: never keep unseen a frame far below the camera clear sky
  if (verdict == "KEEP" && cam_clear != "NA" && cam_clear - zp > floor_mag) { verdict = "REVIEW" ; reason = "below_camera_clear_sky" ; details = details " below_camera_clear=" sprintf("%.3f", cam_clear - zp) }
 }
 nf++ ; f_frame[nf] = frame ; f_img[nf] = img ; f_verdict[nf] = verdict ; f_reason[nf] = reason ; f_camera[nf] = camera ; f_field[nf] = field ; f_details[nf] = details ; f_zp[nf] = zp ; f_lab[nf] = lab
}
END {
 # the two (or more) frames of an upload: the worse one goes to review when they disagree
 for (i = 1; i <= nf; i++) {
  if (f_verdict[i] != "KEEP" || f_zp[i] == "NA" || f_lab[i] == "good") continue
  for (j = 1; j <= nf; j++) {
   if (j == i || f_img[j] != f_img[i] || f_zp[j] == "NA") continue
   if (f_zp[j] - f_zp[i] > pair_max) { f_verdict[i] = "REVIEW" ; f_reason[i] = "pair_mismatch" ; f_details[i] = f_details[i] " pair_difference=" sprintf("%.3f", f_zp[j] - f_zp[i]) }
  }
 }
 for (i = 1; i <= nf; i++) printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n", f_frame[i], f_img[i], f_verdict[i], f_reason[i], f_camera[i], f_field[i], f_details[i]
}' "$WORK_TMP/frames.tsv" > "$WORK_TMP/verdicts.tsv"
VERDICTS="$QUALITY_DIR/verdicts_${RUN_STAMP}.tsv"
cp "$WORK_TMP/verdicts.tsv" "$VERDICTS"

##### Built-in checks #####
SANITY_FAILED=0
if [ -s "$WORK_TMP/kfit.tsv" ]; then
 awk -F'\t' '{ printf "extinction: %s k=%s mag/airmass from %d airmass bins, airmass %s to %s\n", $1, $2, $3, $4, $5 }' "$WORK_TMP/kfit.tsv"
else
 echo "extinction: not enough dark-time cells for a fit - the camera table values are used"
fi
# 1. Twilight and moonlit frames must have a brighter sky than dark-time frames, per camera
SKY_CHECK=$(awk -F'\t' -v min_n="$SANITY_MIN_FRAMES" -v excess="$SANITY_MIN_SKY_EXCESS_MAG" '
$1 == "SKY" { split($2, p, "|") ; n[p[1] "|" p[2]] = $3 ; med[p[1] "|" p[2]] = $4 ; cams[p[1]] = 1 }
END {
 factor = 10 ^ (0.4 * excess)
 for (c in cams) {
  if (!((c "|dark") in n) || n[c "|dark"] < min_n + 0) { print "SKIPPED " c " (fewer than " min_n " dark frames)" ; continue }
  for (k = 1; k <= 2; k++) {
   cl = "twilight"
   if (k == 2) cl = "moon"
   if (!((c "|" cl) in n) || n[c "|" cl] < min_n + 0) { print "SKIPPED " c " " cl " (fewer than " min_n " frames)" ; continue }
   status = "OK"
   if (med[c "|" cl] + 0 < factor * med[c "|dark"]) status = "FAILED"
   print status " " c " " cl " sky " med[c "|" cl] " ADU/s vs dark " med[c "|dark"] " ADU/s"
  }
 }
}' "$WORK_TMP/baselines.tsv")
echo "$SKY_CHECK" | grep -v '^$' | sed 's/^/check: sky: /'
if echo "$SKY_CHECK" | grep -q '^FAILED'; then
 SANITY_FAILED=1
fi
# 2. The computed altitude of the frame centre against the CENTALT header keyword
ALT_CHECK=$(awk -F'\t' -v maxdiff="$SANITY_MAX_ALT_DIFFERENCE_DEG" -v min_frac="$SANITY_MIN_GOOD_ALT_FRACTION" -v min_n="$SANITY_MIN_FRAMES" '
$14 != "NA" && $14 != "" { n[$3]++ ; d = $13 - $14 ; if (d < 0) d = -d ; if (d <= maxdiff + 0) good[$3]++ }
END {
 for (c in n) {
  if (n[c] < min_n + 0) { print "SKIPPED " c " (" n[c] " frames with CENTALT)" ; continue }
  frac = good[c] / n[c]
  status = "OK"
  if (frac < min_frac + 0) status = "FAILED"
  printf "%s %s %d of %d frames within %s deg of CENTALT\n", status, c, good[c], n[c], maxdiff
 }
}' "$WORK_TMP/frames.tsv")
echo "$ALT_CHECK" | grep -v '^$' | sed 's/^/check: altitude: /'
if echo "$ALT_CHECK" | grep -q '^FAILED'; then
 SANITY_FAILED=1
fi

##### Report #####
log "verdicts of $(wc -l < "$WORK_TMP/verdicts.tsv" | tr -d ' ') measured frames (in $VERDICTS):"
awk -F'\t' '{ n[$5 " " $3 " " $4]++ } END { for (k in n) printf "  %-60s %d\n", k, n[k] }' "$WORK_TMP/verdicts.tsv" | sort

# The frames that are still in good_wcs_fd_images/ and have to move
awk -F'\t' '$3 == "REVIEW" || $3 == "REJECT"' "$WORK_TMP/verdicts.tsv" | while IFS=$'\t' read -r FRAME IMG VERDICT REASON CAMERA FIELD DETAILS; do
 if [ -f "$GOOD_DIR/$IMG/$FRAME" ]; then
  printf '%s\t%s\t%s\t%s\n' "$FRAME" "$IMG" "$VERDICT" "$REASON"
 fi
done > "$WORK_TMP/to_move.tsv"
N_TO_MOVE=$(wc -l < "$WORK_TMP/to_move.tsv" | tr -d ' ')
log "$N_TO_MOVE frames to move out of good_wcs_fd_images/"

##### A sample of frames to be sorted by eye #####
if [ -n "$LABEL_SAMPLE_DIR" ]; then
 mkdir -p "$LABEL_SAMPLE_DIR/good" "$LABEL_SAMPLE_DIR/bad" || die "cannot create $LABEL_SAMPLE_DIR"
 # a third of each verdict (more of the others when one is short), at random
 awk -F'\t' -v n="$LABEL_SAMPLE_N" -v seed="$RUN_STAMP" '
 BEGIN { srand(substr(seed, 10) + 0) }
 $4 != "sorted_as_good" && $4 != "sorted_as_bad" { k = $3 ; cnt[k]++ ; row[k, cnt[k]] = $1 "\t" $2 }
 END {
  split("KEEP REVIEW REJECT", order, " ")
  left = n
  for (o = 1; o <= 3; o++) {
   k = order[o] ; want = int(left / (4 - o) + 0.5)
   if (want > cnt[k]) want = cnt[k]
   for (i = 1; i <= want; i++) {
    # a random pick without repetition (the picked row is replaced by the last unpicked one)
    j = int(rand() * (cnt[k] - i + 1)) + 1
    print row[k, j]
    row[k, j] = row[k, cnt[k] - i + 1]
   }
   left -= want
  }
 }' "$WORK_TMP/verdicts.tsv" > "$WORK_TMP/label_sample.tsv"
 N_LINKED=0
 while IFS=$'\t' read -r FRAME IMG; do
  SRC="$GOOD_DIR/$IMG/$FRAME"
  if [ ! -f "$SRC" ]; then
   SRC=$(find "$QUALITY_DIR/review" "$QUALITY_DIR/rejected" -name "$FRAME" -type f 2>/dev/null | head -n 1)
  fi
  [ -n "$SRC" ] && [ -f "$SRC" ] || continue
  [ -e "$LABEL_SAMPLE_DIR/$FRAME" ] && continue
  ln "$SRC" "$LABEL_SAMPLE_DIR/$FRAME" 2>/dev/null || die "cannot hard-link $SRC into $LABEL_SAMPLE_DIR (it must be on the same file system)"
  N_LINKED=$((N_LINKED + 1))
 done < "$WORK_TMP/label_sample.tsv"
 log "hard-linked $N_LINKED frames into $LABEL_SAMPLE_DIR (verdicts in $VERDICTS); sort them with"
 echo "  cd $LABEL_SAMPLE_DIR && for i in *.fits ;do echo \$i ; ~/vast_test/vast/pgfv -- \"\$i\" &>/dev/null && mv -v -- \"\$i\" good/ && continue ; mv -v -- \"\$i\" bad/ ;done"
 echo "  and record the labels with: $0 --read-labels $LABEL_SAMPLE_DIR"
fi

if [ $SANITY_FAILED -ne 0 ]; then
 log "A BUILT-IN CHECK FAILED (see the check: lines above) - nothing is moved. A wrong site, clock or ephemeris would make every twilight and moonlight verdict wrong."
 exit 3
fi

if [ $EXECUTE -eq 0 ]; then
 log "dry run: nothing moved (add --execute)"
 awk -F'\t' '{ folder = "rejected" ; if ($3 == "REVIEW") folder = "review" ; printf "  would move %s/%s -> %s/%s/\n", $2, $1, folder, $4 }' "$WORK_TMP/to_move.tsv" | head -n 20
 if [ "$N_TO_MOVE" -gt 20 ]; then
  echo "  ... and $((N_TO_MOVE - 20)) more"
 fi
 exit 0
fi

##### Move #####
MANIFEST="$QUALITY_DIR/moves_${RUN_STAMP}.tsv"
printf 'time\tframe\tfrom\tto\tverdict\treason\n' > "$MANIFEST"
N_MOVED=0
while IFS=$'\t' read -r FRAME IMG VERDICT REASON; do
 if [ "$VERDICT" = "REVIEW" ]; then
  DEST="$QUALITY_DIR/review/$REASON"
 else
  DEST="$QUALITY_DIR/rejected/$REASON"
 fi
 mkdir -p "$DEST/good" "$DEST/bad" || { log "ERROR: cannot create $DEST"; exit 2; }
 if [ -e "$DEST/$FRAME" ]; then
  log "WARNING: $DEST/$FRAME exists already - $GOOD_DIR/$IMG/$FRAME left where it is"
  continue
 fi
 if ! mv -n "$GOOD_DIR/$IMG/$FRAME" "$DEST/" || [ ! -f "$DEST/$FRAME" ]; then
  log "ERROR: could not move $GOOD_DIR/$IMG/$FRAME to $DEST/"
  exit 2
 fi
 printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$FRAME" "$GOOD_DIR/$IMG" "$DEST" "$VERDICT" "$REASON" >> "$MANIFEST"
 N_MOVED=$((N_MOVED + 1))
done < "$WORK_TMP/to_move.tsv"
log "moved $N_MOVED frames (manifest $MANIFEST)"
exit 0

#!/usr/bin/env bash
#
# sort_workdir_images.sh - sort the processed image directories (img_*) of the
# upload work directory (IMAGE_DATA_ROOT, the uploads/ tree) into three
# directories, by the outcome of their transient-search run.
#
# autoprocess.sh leaves the images of every upload in IMAGE_DATA_ROOT/img_<S>
# (raw frames, fd_* dark/flat-calibrated frames, wcs_fd_* plate-solved frames,
# fd_*.cat SExtractor sidecars) and the report in results_<timestamp>_<S>.
# combine_reports.sh writes one row per report into the nightly
# <date>_<evening|morning>_summary.html. Every img_<S> whose run finished at
# least --min-age-hours ago (default 48) is sorted by its summary row:
#
#  GOOD   status OK           -> the wcs_fd_* FITS files (fully calibrated
#          images) go to <good>/img_<S>/; every other file of the directory
#          (raw frames, fd_* frames, catalogs) goes to <other>/img_<S>/
#  NOREF  not OK, error "ERROR: too few refereence images for the field ..."
#          (no reference images for this field - the frames cannot be
#          calibrated yet)    -> the whole directory to <noref>/img_<S>/
#  OTHER  everything else (any other ERROR, CAMERA STUCK, field excluded from
#          the combined report) -> the whole directory to <other>/img_<S>/
#
# By default the three directories are created inside the work directory:
#   good_wcs_fd_images/  no_reference_image/  other_images_to_delete/
# so the moves are instant renames on the same filesystem. Nothing in the
# pipeline looks inside them (every tool scans only the top-level img_*
# entries), and the img_<S> names are kept, so good_wcs_fd_images/ has the
# uploads-style layout that IMAGE_QUARANTINE_DIR expects should it be moved
# to a big disk later.
#
# A directory that cannot be judged safely is left in place and reported: a
# file in it is younger than --min-age-hours, a vast_* working copy of its run
# still exists (run in progress or crashed), it has no results_* directory, a
# results directory of it has no summary row (the run never completed), it
# contains subdirectories or symlinks, or its name is not autoprocess-style.
#
# Side effect of a move: the FITS download links of the per-field report and
# the fastplot charts of the candidates of that run point into
# uploads/img_<S>/ and stop working. Nothing else reads the moved files.
#
# DRY RUN BY DEFAULT. Nothing is moved without --execute. Run as the owner of
# the work directory (sudo -u apache) or as root, by hand or from cron; a lock
# file keeps two runs from overlapping. Cron example (daily at noon):
#   0 12 * * * apache cd /var/www/tau.kirx.net/cgi-bin/unmw && ./sort_workdir_images.sh --execute > uploads/sort_workdir_images_cron.txt 2>&1
# Never a CGI entry point: it refuses to run in a CGI environment and
# .htaccess denies web access to every .sh file.
#
# Usage:
#   ./sort_workdir_images.sh [options]
#     --execute                 move the files (default: report only)
#     --workdir DIR             work directory holding the img_* directories (default: IMAGE_DATA_ROOT from local_config.sh)
#     --summary-dir DIR         directory holding the *_summary.html files and the results_*/vast_* directories
#                               when it differs from the work directory (default: DATA_PROCESSING_ROOT
#                               from local_config.sh, else the work directory)
#     --good DIR                destination of the good wcs_fd_ images (default: <workdir>/good_wcs_fd_images)
#     --noref DIR               destination of the no-reference-image directories (default: <workdir>/no_reference_image)
#     --other DIR               destination of everything else (default: <workdir>/other_images_to_delete)
#     --min-age-hours N         leave directories with files younger than N hours (default 48)
#     --limit N                 move at most N directories in this run
#     --only GOOD|NOREF|OTHER   move only the directories of this category
#     --keep-catalogs           keep the fd_*.cat/.cat.aperture sidecars with the good images
#     --too-many-candidates-as-good
#                               treat ERROR rows caused only by the combined-report candidate
#                               limit ("too many candidates ...") as GOOD
#     -h, --help
#
# Exit codes: 0 done (or dry run), 1 usage/configuration error, 2 a move
# failed (the run stops there; rerun to finish - a partly moved directory is
# recognised and completed, existing files are never overwritten).

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

EXECUTE=0
WORKDIR=""
SUMMARY_DIR=""
DEST_GOOD=""
DEST_NOREF=""
DEST_OTHER=""
MIN_AGE_HOURS=48
LIMIT=0
ONLY=""
KEEP_CATALOGS=0
TOO_MANY_AS_GOOD=0

# The exact message transient_factory_test31.sh writes when a field has fewer
# than two reference images (historical "refereence" spelling; the pattern also
# accepts the corrected spelling).
NOREF_REGEX='ERROR: too few refere+nce images for the field'
# combine_reports.sh writes this comment when a completed run is left out of
# the combined report because of the candidate-number limits.
TOO_MANY_REGEX='^too many candidates \('

usage() {
 sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
 sed -n '/^# Usage:/,/^# Exit codes/p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
 echo "ERROR: $*" >&2
 exit 1
}

while [ $# -gt 0 ]; do
 case "$1" in
  --execute) EXECUTE=1 ;;
  --workdir) shift; WORKDIR="${1:-}" ;;
  --summary-dir) shift; SUMMARY_DIR="${1:-}" ;;
  --good) shift; DEST_GOOD="${1:-}" ;;
  --noref) shift; DEST_NOREF="${1:-}" ;;
  --other) shift; DEST_OTHER="${1:-}" ;;
  --min-age-hours) shift; MIN_AGE_HOURS="${1:-}" ;;
  --limit) shift; LIMIT="${1:-}" ;;
  --only) shift; ONLY="${1:-}" ;;
  --keep-catalogs) KEEP_CATALOGS=1 ;;
  --too-many-candidates-as-good) TOO_MANY_AS_GOOD=1 ;;
  -h|--help) usage; exit 0 ;;
  *) die "unknown option $1 (see --help)" ;;
 esac
 shift
done

case "$MIN_AGE_HOURS" in ''|*[!0-9]*) die "--min-age-hours needs a whole number of hours" ;; esac
case "$LIMIT" in ''|*[!0-9]*) die "--limit needs a whole number" ;; esac
if [ -n "$ONLY" ]; then
 case "$ONLY" in GOOD|NOREF|OTHER) ;; *) die "--only takes GOOD, NOREF or OTHER" ;; esac
fi
for cmd in find stat awk sort flock readlink mktemp mv; do
 command -v "$cmd" >/dev/null 2>&1 || die "required command '$cmd' is not installed"
done

##### Configuration #####
# Only IMAGE_DATA_ROOT is needed from local_config.sh; read it in a subshell
# (sourcing the file also activates the Python venv and exports notification
# settings).
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
# The summaries are written where combine_reports.sh runs (DATA_PROCESSING_ROOT),
# the results_* directories where autoprocess.sh creates them; both are the work
# directory itself on a standard installation. Both places are searched.
[ -n "$SUMMARY_DIR" ] || SUMMARY_DIR=$(read_config_var DATA_PROCESSING_ROOT)
[ -n "$SUMMARY_DIR" ] || SUMMARY_DIR="$WORKDIR"
[ -d "$SUMMARY_DIR" ] || die "summary directory $SUMMARY_DIR does not exist"
SUMMARY_DIR=$(readlink -f "$SUMMARY_DIR")
SEARCH_DIRS=( "$WORKDIR" )
[ "$SUMMARY_DIR" = "$WORKDIR" ] || SEARCH_DIRS+=( "$SUMMARY_DIR" )
[ -n "$DEST_GOOD" ] || DEST_GOOD="$WORKDIR/good_wcs_fd_images"
[ -n "$DEST_NOREF" ] || DEST_NOREF="$WORKDIR/no_reference_image"
[ -n "$DEST_OTHER" ] || DEST_OTHER="$WORKDIR/other_images_to_delete"
LOG_DIR="$WORKDIR/sort_workdir_images_logs"

# A destination may not be the work directory itself, and a destination inside
# the work directory may not carry a name the pipeline scans for.
check_destination() {
 local label="$1" dir="$2" base
 base=$(basename "$dir")
 case "$base" in
  img_*|results_*|vast_*|web_upload_*|monitoring|bad|.*) die "$label: the name $base clashes with the directories the pipeline uses" ;;
 esac
 if [ -e "$dir" ]; then
  [ -d "$dir" ] || die "$label $dir exists and is not a directory"
  [ "$(readlink -f "$dir")" != "$WORKDIR" ] || die "$label must not be the work directory itself"
 fi
}
check_destination "--good" "$DEST_GOOD"
check_destination "--noref" "$DEST_NOREF"
check_destination "--other" "$DEST_OTHER"
[ "$DEST_GOOD" != "$DEST_NOREF" ] && [ "$DEST_GOOD" != "$DEST_OTHER" ] && [ "$DEST_NOREF" != "$DEST_OTHER" ] || die "the three destinations must be different directories"

MIN_AGE_SEC=$((MIN_AGE_HOURS * 3600))
NOW=$(date +%s)
RUN_STAMP=$(date +%Y%m%d_%H%M%S)

echo "sort_workdir_images.sh $(date '+%Y-%m-%d %H:%M:%S %Z') $( [ $EXECUTE -eq 1 ] && echo EXECUTE || echo 'DRY RUN (nothing is moved; add --execute)')"
echo "work directory: $WORKDIR"
[ "$SUMMARY_DIR" = "$WORKDIR" ] || echo "summaries, results_* and vast_* also searched in: $SUMMARY_DIR"
echo "good wcs_fd_ images -> $DEST_GOOD"
echo "no reference image  -> $DEST_NOREF"
echo "everything else     -> $DEST_OTHER"
echo "minimum age ${MIN_AGE_HOURS} h; keep catalogs with good images: $KEEP_CATALOGS; too-many-candidates rows as good: $TOO_MANY_AS_GOOD"

##### Summary index: results directory -> status, comment #####
# One summary row per line; the status cell (new markup <span class='status-ok'>,
# legacy <font color='green'>) is immediately followed by the log link cell
# naming the results directory; the last cell is the comment.
shopt -s nullglob
SUMMARY_FILES=( "$WORKDIR"/*_summary.html )
[ "$SUMMARY_DIR" = "$WORKDIR" ] || SUMMARY_FILES+=( "$SUMMARY_DIR"/*_summary.html )
shopt -u nullglob
[ ${#SUMMARY_FILES[@]} -gt 0 ] || die "no *_summary.html files in ${SEARCH_DIRS[*]} (pass --summary-dir)"
INDEX=$(mktemp) || die "cannot create a temporary file"
PLAN=$(mktemp) || die "cannot create a temporary file"
LIST_GOOD=$(mktemp) || die "cannot create a temporary file"
LIST_OTHER=$(mktemp) || die "cannot create a temporary file"
trap 'rm -f "$INDEX" "$PLAN" "$LIST_GOOD" "$LIST_OTHER"' EXIT
awk '
/<tr>/ && /results_/ {
 line = $0
 if (match(line, /<td>(<span class=.status-[a-z]+.>|<font color=.[^>]+.>)[^<]*<\/(span|font)><\/td><td><a href=.\.\/results_[^\/]*\/. target=._blank.>log<\/a>/)) {
  cell = substr(line, RSTART, RLENGTH)
  st = cell; sub(/^<td>(<span class=.status-[a-z]+.>|<font color=.[^>]+.>)/, "", st); sub(/<\/(span|font)>.*/, "", st)
  r = cell; sub(/.*href=.\.\//, "", r); sub(/\/. target.*/, "", r)
  c = line; sub(/.*<td>/, "", c); sub(/<\/td><\/tr>.*/, "", c); gsub(/\t/, " ", c)
  print r "\t" st "\t" c
 }
}' "${SUMMARY_FILES[@]}" > "$INDEX"
declare -A ROW_STATUS ROW_COMMENT
while IFS=$'\t' read -r r st cm; do
 ROW_STATUS["$r"]="$st"
 ROW_COMMENT["$r"]="$cm"
done < "$INDEX"
echo "summary rows indexed: ${#ROW_STATUS[@]} from ${#SUMMARY_FILES[@]} summary files"

##### Classification #####
is_good_file() {
 case "$1" in
  wcs_fd_*.fits|wcs_fd_*.fit|wcs_fd_*.fts|wcs_fd_*.fits.fz|wcs_fd_*.fit.fz|wcs_fd_*.fts.fz) return 0 ;;
 esac
 if [ $KEEP_CATALOGS -eq 1 ]; then
  case "$1" in
   fd_*.cat|fd_*.cat.aperture|wcs_fd_*.cat|wcs_fd_*.cat.aperture) return 0 ;;
  esac
 fi
 return 1
}

# classify <img dir>: sets CAT (GOOD, NOREF, OTHER or SKIP), REASON, N_GOOD,
# N_OTHER, B_GOOD, B_OTHER and fills LIST_GOOD / LIST_OTHER with the
# NUL-separated file names to move to <good> and to the other destination.
classify() {
 local d="$1" name S newest bad_entry results st cm any_ok=0 any_noref=0 all_toomany=1 n_results=0 f size
 CAT=SKIP; REASON=""; N_GOOD=0; N_OTHER=0; B_GOOD=0; B_OTHER=0
 : > "$LIST_GOOD"; : > "$LIST_OTHER"
 name=$(basename "$d")
 S="${name#img_}"
 if [ -z "$S" ] || [ "$S" = "$name" ]; then REASON="unexpected name"; return; fi
 case "$S" in *[[:space:]]*|*/*) REASON="unexpected characters in the name"; return ;; esac
 # a working copy of a run on this directory: vast_<S> or vast_reprocess_img_<S>_<key>
 local sd
 for sd in "${SEARCH_DIRS[@]}"; do
  if compgen -G "$sd/vast_*${S}*" >/dev/null; then REASON="vast_ working directory present (run in progress or crashed)"; return; fi
 done
 bad_entry=$(find "$d" -mindepth 1 ! -type f -print -quit 2>/dev/null)
 if [ -n "$bad_entry" ]; then REASON="contains a subdirectory or a symlink ($(basename "$bad_entry"))"; return; fi
 # no file may be younger than the minimum age (the plate-solved files appear
 # while the run is still going)
 newest=$(find "$d" -mindepth 1 -type f -printf '%T@\n' 2>/dev/null | sort -n | tail -n1)
 newest="${newest%.*}"
 if [ -z "$newest" ]; then REASON="empty directory"; return; fi
 if [ $((NOW - newest)) -lt $MIN_AGE_SEC ]; then REASON="modified less than $MIN_AGE_HOURS h ago"; return; fi
 # the summary rows of every run on this directory
 for results in $(for sd in "${SEARCH_DIRS[@]}"; do printf '%s\n' "$sd"/results_[0-9]*_"$S" "$sd"/results_[0-9]*_reprocess_img_"$S"_*; done); do
  [ -d "$results" ] || continue
  n_results=$((n_results + 1))
  st="${ROW_STATUS[$(basename "$results")]:-}"
  if [ -z "$st" ]; then REASON="results directory $(basename "$results") has no summary row (run not completed)"; return; fi
  cm="${ROW_COMMENT[$(basename "$results")]:-}"
  if [ "$st" = "OK" ]; then
   any_ok=1
  else
   if [[ "$cm" =~ $NOREF_REGEX ]]; then any_noref=1; fi
   if ! [[ "$cm" =~ $TOO_MANY_REGEX ]]; then all_toomany=0; fi
  fi
  if [ -z "$REASON" ]; then REASON="${cm:-$st}"; REASON="${REASON:0:120}"; fi
 done
 if [ $n_results -eq 0 ]; then REASON="no results directory"; return; fi
 if [ $any_ok -eq 1 ]; then
  CAT=GOOD
 elif [ $any_noref -eq 1 ]; then
  CAT=NOREF
 elif [ $TOO_MANY_AS_GOOD -eq 1 ] && [ $all_toomany -eq 1 ]; then
  CAT=GOOD; REASON="too many candidates (treated as good): $REASON"
 else
  CAT=OTHER
 fi
 # file routing
 while IFS= read -r -d '' f; do
  f="${f#./}"
  size=$(stat -c %s -- "$d/$f") || { CAT=SKIP; REASON="cannot stat $f"; return; }
  if [ "$CAT" = GOOD ] && is_good_file "$f"; then
   printf '%s\0' "$f" >> "$LIST_GOOD"; N_GOOD=$((N_GOOD + 1)); B_GOOD=$((B_GOOD + size))
  else
   printf '%s\0' "$f" >> "$LIST_OTHER"; N_OTHER=$((N_OTHER + 1)); B_OTHER=$((B_OTHER + size))
  fi
 done < <(cd "$d" && find . -mindepth 1 -maxdepth 1 -type f -print0 | sort -z)
 if [ "$CAT" = GOOD ] && [ $N_GOOD -eq 0 ]; then
  CAT=OTHER; REASON="status OK but no wcs_fd_ images in the directory! $REASON"
 fi
}

##### Plan #####
# plan rows: name \t CAT \t N_GOOD \t B_GOOD \t N_OTHER \t B_OTHER \t REASON
declare -A CNT BYTES SKIP_REASONS
for k in GOOD NOREF OTHER SKIP; do CNT[$k]=0; done
BYTES[GOOD_good]=0; BYTES[GOOD_other]=0; BYTES[NOREF]=0; BYTES[OTHER]=0
N_IMG=0
while IFS= read -r -d '' d; do
 N_IMG=$((N_IMG + 1))
 classify "$d"
 printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(basename "$d")" "$CAT" "$N_GOOD" "$B_GOOD" "$N_OTHER" "$B_OTHER" "$REASON" >> "$PLAN"
 CNT[$CAT]=$(( ${CNT[$CAT]} + 1 ))
 case "$CAT" in
  GOOD) BYTES[GOOD_good]=$(( ${BYTES[GOOD_good]} + B_GOOD )); BYTES[GOOD_other]=$(( ${BYTES[GOOD_other]} + B_OTHER )) ;;
  NOREF) BYTES[NOREF]=$(( ${BYTES[NOREF]} + B_OTHER )) ;;
  OTHER) BYTES[OTHER]=$(( ${BYTES[OTHER]} + B_OTHER )) ;;
  SKIP) key="${REASON%%:*}"; key="${key%% (*}"; SKIP_REASONS["$key"]=$(( ${SKIP_REASONS["$key"]:-0} + 1 )) ;;
 esac
done < <(find "$WORKDIR" -mindepth 1 -maxdepth 1 -type d -name 'img_*' -print0 | sort -z)

gib() { awk -v b="$1" 'BEGIN { printf "%.1f GiB", b / 1073741824 }'; }
echo
echo "img_* directories: $N_IMG"
echo "GOOD  ${CNT[GOOD]} dirs: $(gib ${BYTES[GOOD_good]}) of wcs_fd_ images -> good; $(gib ${BYTES[GOOD_other]}) of other files -> other"
echo "NOREF ${CNT[NOREF]} dirs: $(gib ${BYTES[NOREF]}) -> no reference image"
echo "OTHER ${CNT[OTHER]} dirs: $(gib ${BYTES[OTHER]}) -> other"
echo "left in place: ${CNT[SKIP]} dirs"
for key in "${!SKIP_REASONS[@]}"; do
 printf '   %6d  %s\n' "${SKIP_REASONS[$key]}" "$key"
done | sort -rn
N_ODD=$(awk -F'\t' '$2=="SKIP" && $7 !~ /^modified less than/' "$PLAN" | wc -l)
if [ "$N_ODD" -gt 0 ]; then
 echo "left in place for a reason other than age:"
 awk -F'\t' '$2=="SKIP" && $7 !~ /^modified less than/ {printf "   %s  %s\n", $1, $7}' "$PLAN" | head -n 40
 [ "$N_ODD" -gt 40 ] && echo "   ... ($N_ODD in total, see the plan file)"
fi
if [ $(( ${CNT[NOREF]} + ${CNT[OTHER]} )) -gt 0 ]; then
 echo "error messages of the NOREF and OTHER directories:"
 awk -F'\t' '$2=="NOREF" || $2=="OTHER" {c=$7; sub(/ \(SD ratio=.*/, "", c); sub(/The distance between.*/, "", c); printf "%s\t%s\n", $2, substr(c, 1, 100)}' "$PLAN" | sort | uniq -c | sort -rn | head -n 25 | sed 's/^/   /'
fi

if [ $EXECUTE -eq 0 ]; then
 if [ -d "$LOG_DIR" ] && [ -w "$LOG_DIR" ]; then
  PLAN_COPY="$LOG_DIR/plan_${RUN_STAMP}_dryrun.tsv"
 else
  PLAN_COPY="${TMPDIR:-/tmp}/sort_workdir_images_plan_${RUN_STAMP}.tsv"
 fi
 cp "$PLAN" "$PLAN_COPY" 2>/dev/null && echo "plan written to $PLAN_COPY (name, category, n_good, bytes_good, n_other, bytes_other, reason)"
 echo "DRY RUN - nothing was moved. Add --execute to move."
 exit 0
fi

##### Execute #####
[ -w "$WORKDIR" ] || die "the work directory is not writable by $(id -un) - run as its owner (sudo -u apache) or as root"
exec 9>"$WORKDIR/.sort_workdir_images.lock" || die "cannot create the lock file"
flock -n 9 || die "another sort_workdir_images.sh is running"
for dir in "$DEST_GOOD" "$DEST_NOREF" "$DEST_OTHER" "$LOG_DIR"; do
 if [ ! -d "$dir" ]; then
  mkdir -p "$dir" || die "cannot create $dir"
  chown --reference="$WORKDIR" "$dir" 2>/dev/null || true
 fi
 [ -w "$dir" ] || die "$dir is not writable by $(id -un)"
done
LOG="$LOG_DIR/sort_workdir_images.log"
MANIFEST="$LOG_DIR/manifest_${RUN_STAMP}.tsv"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }
printf 'time\tcategory\timg_dir\tfile\tbytes\tdestination\n' > "$MANIFEST"
cp "$PLAN" "$LOG_DIR/plan_${RUN_STAMP}.tsv"
log "start: workdir=$WORKDIR good=$DEST_GOOD noref=$DEST_NOREF other=$DEST_OTHER min_age=${MIN_AGE_HOURS}h limit=$LIMIT only=${ONLY:-all} keep_catalogs=$KEEP_CATALOGS user=$(id -un)"

# move_files <category> <img name> <src dir> <dest root> <NUL list of names>
# Moves the listed files of <src dir> into <dest root>/<img name>/, never
# overwriting an existing file; a name that already exists at the destination
# stays in place and stops the run.
move_files() {
 local cat="$1" name="$2" src="$3" list="$5" dest="$4/$2" f
 [ -s "$list" ] || return 0
 if [ ! -d "$dest" ]; then
  mkdir -p "$dest" || { log "STOP: cannot create $dest"; exit 2; }
  chown --reference="$src" "$dest" 2>/dev/null || true
  chmod --reference="$src" "$dest" 2>/dev/null || true
 fi
 while IFS= read -r -d '' f; do
  if [ -e "$dest/$f" ]; then
   log "STOP: $dest/$f already exists - not overwriting; $src/$f left in place, please inspect"
   exit 2
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$cat" "$name" "$f" "$(stat -c %s -- "$src/$f" 2>/dev/null || echo '?')" "$dest" >> "$MANIFEST"
  if ! mv -n -- "$src/$f" "$dest/$f"; then
   log "STOP: cannot move $src/$f to $dest/ - rerun to resume"
   exit 2
  fi
 done < "$list"
}

N_DONE=0
while IFS=$'\t' read -r name cat _rest; do
 case "$cat" in GOOD|NOREF|OTHER) ;; *) continue ;; esac
 [ -n "$ONLY" ] && [ "$ONLY" != "$cat" ] && continue
 if [ $LIMIT -gt 0 ] && [ $N_DONE -ge $LIMIT ]; then break; fi
 d="$WORKDIR/$name"
 [ -d "$d" ] || continue
 # judge the directory again right before touching it
 classify "$d"
 if [ "$CAT" != "$cat" ]; then
  log "skip $name: changed since the plan was made ($cat -> $CAT: $REASON)"
  continue
 fi
 log "$CAT $name: $N_GOOD good file(s) $(gib $B_GOOD), $N_OTHER other file(s) $(gib $B_OTHER) - $REASON"
 case "$CAT" in
  GOOD)
   move_files GOOD "$name" "$d" "$DEST_GOOD" "$LIST_GOOD"
   move_files GOOD "$name" "$d" "$DEST_OTHER" "$LIST_OTHER"
   ;;
  NOREF) move_files NOREF "$name" "$d" "$DEST_NOREF" "$LIST_OTHER" ;;
  OTHER) move_files OTHER "$name" "$d" "$DEST_OTHER" "$LIST_OTHER" ;;
 esac
 if [ -n "$(find "$d" -mindepth 1 -print -quit 2>/dev/null)" ]; then
  log "STOP: $d is not empty after the move - please inspect"
  exit 2
 fi
 rmdir "$d" || { log "STOP: cannot remove the emptied directory $d"; exit 2; }
 N_DONE=$((N_DONE + 1))
done < "$PLAN"
log "done: $N_DONE directories moved; manifest $MANIFEST"
exit 0

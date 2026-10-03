"""Shared machinery for the NMW source monitoring feature.

Design: source_monitoring_design.md. Key points:
- $NMW_CALIBRATION/monitoring_list.txt is the SINGLE source of truth for
  every monitored source's coordinates and name (plain ASCII, one source
  per line: 'HH:MM:SS.SS +DD:MM:SS.S Name with spaces').
- The per-source registry directory uploads/monitoring/<source_id>/ holds
  ONLY the measurement ledger (measured_images.txt), the backfill_done
  marker and the derived products (lightcurve.dat, upperlimits.dat,
  lightcurve_aavso.txt, plot, index.html). No JSON anywhere.
- The ledger is keyed by image basename: an image is never measured twice,
  which makes archive/recent overlaps and upload reprocessing harmless.
  Rows with an EXCLUDED_STATUSES status (edge, saturated, bad_region, the
  bad_wcs / no_nearby_stars refusals of the forced photometry tool, run_error
  for the images of rejected transient-search runs, ...) stay in the ledger
  (never re-measured) but are excluded from every published product. The
  manual modes honour the verdicts of the transient-search runs (see
  RUN_VERDICTS_BASENAME).
"""

import datetime
import decimal
import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import nmw_coord_lib as ncl
from nmw_coord_lib import html_escape

# The public data-product basenames
LEDGER_BASENAME = 'measured_images.txt'
BACKFILL_MARKER_BASENAME = 'backfill_done'
LIGHTCURVE_BASENAME = 'lightcurve.dat'
UPPERLIMITS_BASENAME = 'upperlimits.dat'
AAVSO_BASENAME = 'lightcurve_aavso.txt'

# The recent-window lightcurve plot shown above the full-range plot on the
# source page. The window is anchored at the NEWEST published point (not at
# the wall clock) so a source that stopped being observed still shows its
# last month of data instead of an empty panel.
RECENT_PLOT_WINDOW_DAYS = 30.0
RECENT_PLOT_PNG_BASENAME = 'lightcurve_recent.png'
RECENT_PLOT_EPS_BASENAME = 'lightcurve_recent.eps'

MONITORING_SUBDIR = 'monitoring'
LOCK_SUBDIR = '.monitoring_locks'
GLOBAL_LOCK_BASENAME = 'monitoring_global.lock'

# Measurement statuses excluded from all published products (still recorded
# in the ledger so the image is never re-measured). 'bad_wcs' and
# 'no_nearby_stars' come from the frame-level sanity checks of VaST's
# util/forced_photometry (a TAN-only plate solution on a wide field; too few
# detected stars around the position - a cloud patch); 'run_error' marks the
# images of a transient-search run that was rejected, see RUN_ERROR_STATUS.
EXCLUDED_STATUSES = ('edge', 'saturated', 'bad_region', 'nan_pixel',
                     'calib_fail', 'fail', 'tool_fail',
                     'run_error', 'bad_wcs', 'no_nearby_stars')

# Within-visit consistency check (monitoring products only). The transient
# pipeline takes its second-epoch frames of one field minutes apart, so
# consecutive detections from the same camera closer in time than
# VISIT_GROUP_MAX_GAP_DAYS form one visit. When the magnitudes within a
# visit disagree by more than max(VISIT_CONSISTENCY_MAG_TOLERANCE,
# VISIT_CONSISTENCY_ERR_SCALE * the largest per-point error of the visit),
# a real star cannot have done that - at least one frame is corrupted
# (typically by patchy clouds) and there is no way to tell which one, so
# every detection of that visit is excluded from the published products
# (lightcurve.dat, the plot and the AAVSO file) and listed in
# EXCLUDED_MEASUREMENTS_BASENAME instead.
# A visit that contains BOTH a detection and an upper limit is discarded
# whole on the same grounds, regardless of the magnitudes involved: a star
# cannot cross the detection threshold within minutes, so either the
# "detection" is a noise excursion or the "upper limit" is an unphysically
# deep one produced by a misplaced aperture - and the data cannot tell
# which. Visits consisting of upper limits only are always kept (the
# achieved depth may legitimately differ between frames).
VISIT_GROUP_MAX_GAP_DAYS = 0.007
VISIT_CONSISTENCY_MAG_TOLERANCE = 0.3
VISIT_CONSISTENCY_ERR_SCALE = 4.0

# Measurements excluded by the quality checks (the within-visit consistency
# check above and the per-frame cloud check applied at ingest time, status
# 'cloudy') are published in this file and on the source page instead of
# the lightcurve/plot/AAVSO products.
EXCLUDED_MEASUREMENTS_BASENAME = 'excluded_measurements.dat'
REASON_VISIT = 'visit_inconsistent'
REASON_CLOUDY = 'cloudy_frame'
# Ledger status token and displayed reason of measurements excluded by hand
# (monitoring_update.py --exclude-measurement)
MANUAL_STATUS = 'manual'
REASON_MANUAL = 'manual_exclusion'

# Ledger status of the images of a transient-search run that was rejected:
# the run raised a processing ERROR or failed (see RUN_VERDICTS_BASENAME).
# Such images are processed-and-rejected and the row is never published. It
# is written by the ingest of the run (autoprocess.sh calls --ingest-rejected
# on the rows of the factory, which emits run_error rows itself for a field
# it refused to measure), and by the manual modes when they meet an image of
# a rejected run that is not in the ledger yet. It keeps the magnitude and
# error the factory measured when it had measured the image before the run
# turned red (an upper limit keeps its 99.0000 error), and carries
# 99.0000 99.0000 otherwise. The manual modes never re-measure such an
# image; unlike the other terminal statuses, the row is replaced by the
# ingest of a later reprocessing run of the image that succeeds
# (SUPERSEDABLE_STATUSES).
RUN_ERROR_STATUS = 'run_error'
SUPERSEDABLE_STATUSES = (RUN_ERROR_STATUS,)

# Displayed reason for the EXCLUDED_STATUSES that are REPORTED on the source
# page and in EXCLUDED_MEASUREMENTS_BASENAME. These rows carry no usable
# magnitude (bad_wcs, no_nearby_stars and some run_error rows keep the value
# that was measured, which is shown but means nothing), but they are listed
# rather than dropped: the frame did cover the position and the measurement
# was refused or failed, so leaving them out makes a rejected measurement
# look like an image that was never taken.
#
# 'edge' is deliberately NOT in this table. It means the position falls off
# the frame, or its aperture and sky annulus reach past the border, i.e. the
# image genuinely does not cover the source - there "no data" is the correct
# impression, and reporting it would bury the informative rows (edge is by
# far the most common of these statuses on partially covering fields).
REASON_FOR_EXCLUDED_STATUS = {
    'bad_region': 'bad_ccd_region',
    'saturated': 'saturated',
    'nan_pixel': 'blank_pixels_in_aperture',
    'calib_fail': 'magnitude_calibration_failed',
    'fail': 'measurement_failed',
    'tool_fail': 'measurement_failed',
    'run_error': 'processing_error_in_run',
    'bad_wcs': 'unreliable_plate_solution',
    'no_nearby_stars': 'no_stars_around_position',
}

# Name and coordinate validation for monitoring_list.txt lines
NAME_CHARSET_RE = re.compile(r'^[A-Za-z0-9+.()= _-]+$')
SOURCE_ID_RE = re.compile(r'^[A-Za-z0-9+.()=_-]+$')
RA_SEXAGESIMAL_RE = re.compile(r'^\d{1,2}:\d{2}:\d{2}(\.\d+)?$')
DEC_SEXAGESIMAL_RE = re.compile(r'^[+-]?\d{1,3}:\d{2}:\d{2}(\.\d+)?$')

# NMW_CALIBRATION resolution mirrors transient_factory_test31.sh
NMW_CALIBRATION_FALLBACK_DIRS = (
    os.path.expanduser('~/nmw_calibration'),
    '/dataX/cgi-bin/unmw/uploads/nmw_calibration',
    '/home/apache/nmw_calibration',
    '/var/www/nmw_calibration',
)

MONITORING_LIST_BASENAME = 'monitoring_list.txt'

# Optional per-source manual detection threshold, one plain number in
# this file inside the source's registry directory (machine-local, since
# the threshold depends on the camera's pixel scale/PSF; managed with
# monitoring_update.py --set-detection-threshold). Detections FAINTER
# than the threshold are published as upper limits at the measured
# magnitude: the use case is a photometric aperture contaminated by an
# unrelated nearby source, where below the threshold a measurement only
# bounds the target brightness from above, and only a target outshining
# the contaminant yields a true detection.
DETECTION_THRESHOLD_BASENAME = 'detection_threshold.txt'
# Displayed status of threshold-demoted rows on the source page table
STATUS_BELOW_THRESHOLD = 'below_threshold'

FITS_EXTENSIONS = ('.fts', '.fits', '.fit')


# ---------- the master list ----------

def resolve_nmw_calibration_dir():
    """Return the calibration directory, mirroring the factory's fallback
    chain; the NMW_CALIBRATION environment variable wins. None if nothing
    exists."""
    env = os.environ.get('NMW_CALIBRATION', '').strip()
    if env and os.path.isdir(env):
        return env
    for candidate in NMW_CALIBRATION_FALLBACK_DIRS:
        if os.path.isdir(candidate):
            return candidate
    return None


def monitoring_list_path():
    """Absolute path of monitoring_list.txt, or None when the calibration
    directory or the list file does not exist."""
    calib_dir = resolve_nmw_calibration_dir()
    if not calib_dir:
        return None
    path = os.path.join(calib_dir, MONITORING_LIST_BASENAME)
    return path if os.path.isfile(path) else None


def read_detection_threshold(source_dir):
    """The manual detection threshold of one source, or None. The value is
    the first non-comment token of detection_threshold.txt in the source's
    registry directory; an unparseable file is reported to stderr and
    treated as no threshold."""
    path = os.path.join(source_dir, DETECTION_THRESHOLD_BASENAME)
    try:
        with open(path) as fh:
            content = fh.read()
    except OSError:
        return None
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            mag = float(line.split()[0])
        except ValueError:
            break
        if 0.0 < mag < 30.0:
            return mag
        break
    sys.stderr.write('{}: unparseable or out-of-range threshold - '
                     'ignored\n'.format(path))
    return None


def write_detection_threshold(source_dir, mag):
    """Set (mag as float) or clear (mag None) the manual detection
    threshold of one source. Returns the human-readable action taken."""
    path = os.path.join(source_dir, DETECTION_THRESHOLD_BASENAME)
    if mag is None:
        try:
            os.remove(path)
            return 'threshold cleared'
        except OSError:
            return 'no threshold was set'
    _write_text_atomic(path,
                       '# manual detection threshold: detections fainter'
                       ' than this magnitude are published as upper'
                       ' limits\n{:.4f}\n'.format(mag))
    return 'threshold set to {:.2f}'.format(mag)


def apply_detection_threshold(detections, upperlimits, threshold):
    """Demote detections fainter than the manual threshold to upper limits
    at the measured magnitude (the aperture is contaminated, so such a
    measurement only bounds the target from above). Demoted rows get the
    STATUS_BELOW_THRESHOLD display status. Returns the new
    (detections, upperlimits) pair; no-op when threshold is None."""
    if threshold is None:
        return detections, upperlimits
    demoted = [r for r in detections if r['mag_float'] > threshold]
    if not demoted:
        return detections, upperlimits
    for row in demoted:
        row['status'] = STATUS_BELOW_THRESHOLD
    kept = [r for r in detections if r['mag_float'] <= threshold]
    return kept, sorted(upperlimits + demoted,
                        key=lambda r: r['jd_float'])


def sanitize_source_id(name):
    """Display name -> registry directory name: trim, collapse whitespace
    runs to single underscores, strip trailing dots/underscores. Returns
    None when the result is empty or contains disallowed characters."""
    cleaned = re.sub(r'\s+', '_', name.strip())
    cleaned = cleaned.rstrip('._')
    if not cleaned or not SOURCE_ID_RE.match(cleaned):
        return None
    return cleaned


def _ra_in_range(ra):
    """The regex checks the shape only; this checks the numeric ranges."""
    hours, minutes, seconds = ra.split(':')
    return int(hours) < 24 and int(minutes) < 60 and float(seconds) < 60.0


def _dec_in_range(dec):
    degrees, minutes, seconds = dec.lstrip('+-').split(':')
    if int(minutes) >= 60 or float(seconds) >= 60.0:
        return False
    if int(degrees) > 90:
        return False
    if int(degrees) == 90 and (int(minutes) or float(seconds)):
        return False
    return True


def round_sexagesimal(value, decimals, is_ra):
    """Round a validated 'HH:MM:SS.SSS' / '[+-]DD:MM:SS.SS' string to
    `decimals` places of seconds for display, keeping the sexagesimal
    form. Mirrors VaST lib/deg2hms: half-up rounding on the seconds field
    with the carry chain seconds -> minutes -> hours/degrees, the 24h wrap
    for RA, and an explicit sign for Dec. The seconds field is rounded as a
    decimal string, so no float round trip touches the value. Returns the
    input unchanged when it does not parse (the list page must never fail
    on a cosmetic step)."""
    text = value.strip()
    sign = ''
    if not is_ra:
        sign = '-' if text.startswith('-') else '+'
    try:
        first, minutes, seconds = text.lstrip('+-').split(':')
        first = int(first)
        minutes = int(minutes)
        seconds = decimal.Decimal(seconds).quantize(
            decimal.Decimal(1).scaleb(-decimals),
            rounding=decimal.ROUND_HALF_UP)
    except (ValueError, decimal.InvalidOperation):
        return value
    if seconds >= 60:
        seconds -= 60
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        first += 1
    if is_ra and first >= 24:
        first -= 24
    return '{}{:02d}:{:02d}:{:0{width}.{decimals}f}'.format(
        sign, first, minutes, seconds,
        width=3 + decimals if decimals else 2, decimals=decimals)


def display_ra(ra):
    """RA as HH:MM:SS.SS for the human-readable source list."""
    return round_sexagesimal(ra, 2, True)


def display_dec(dec):
    """Dec as +DD:MM:SS.S for the human-readable source list."""
    return round_sexagesimal(dec, 1, False)


def parse_monitoring_list(path):
    """Parse monitoring_list.txt.

    Returns (entries, problems): entries is a list of dicts with keys
    ra, dec, name, source_id, line_no (first occurrence of each id wins);
    problems is a list of human-readable strings for skipped/suspect lines.
    """
    entries = []
    problems = []
    seen_ids = {}
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return [], ['cannot read {}: {}'.format(path, exc)]
    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            problems.append('line {}: expected "RA Dec Name", got: {}'.format(
                line_no, raw_line.rstrip()))
            continue
        ra, dec, name = parts[0], parts[1], parts[2].strip()
        if not RA_SEXAGESIMAL_RE.match(ra) or not _ra_in_range(ra):
            problems.append('line {}: bad RA "{}"'.format(line_no, ra))
            continue
        if not DEC_SEXAGESIMAL_RE.match(dec) or not _dec_in_range(dec):
            problems.append('line {}: bad Dec "{}"'.format(line_no, dec))
            continue
        if not NAME_CHARSET_RE.match(name):
            problems.append(
                'line {}: name "{}" has characters outside '
                '[A-Za-z0-9+.()= _-]'.format(line_no, name))
            continue
        source_id = sanitize_source_id(name)
        if source_id is None:
            problems.append('line {}: name "{}" sanitizes to nothing'.format(
                line_no, name))
            continue
        if source_id in seen_ids:
            problems.append(
                'line {}: id "{}" collides with line {} - skipped'.format(
                    line_no, source_id, seen_ids[source_id]))
            continue
        for existing_id in seen_ids:
            if existing_id.lower() == source_id.lower() \
                    and existing_id != source_id:
                problems.append(
                    'line {}: WARNING id "{}" differs only by case from '
                    '"{}"'.format(line_no, source_id, existing_id))
        seen_ids[source_id] = line_no
        entries.append({'ra': ra, 'dec': dec, 'name': name,
                        'source_id': source_id, 'line_no': line_no})
    return entries, problems


# ---------- registry paths and locks ----------

def monitoring_root(uploads_dir):
    return os.path.join(uploads_dir, MONITORING_SUBDIR)


def source_dir_path(uploads_dir, source_id):
    return os.path.join(monitoring_root(uploads_dir), source_id)


def _lock_dir(uploads_dir):
    path = os.path.join(uploads_dir, LOCK_SUBDIR)
    os.makedirs(path, mode=0o755, exist_ok=True)
    return path


def acquire_global_lock(uploads_dir):
    """Non-blocking global monitoring lock (reconcile/rescan serialization).
    Returns the open file object (keep it alive) or None when another
    instance holds it - the caller must exit with a message, not queue."""
    path = os.path.join(_lock_dir(uploads_dir), GLOBAL_LOCK_BASENAME)
    fh = open(path, 'w')
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def acquire_source_lock(uploads_dir, source_id):
    """Blocking per-source lock protecting ledger appends and product
    rebuilds. Returns the open file object; closing releases."""
    path = os.path.join(_lock_dir(uploads_dir),
                        'src_{}.lock'.format(source_id))
    fh = open(path, 'w')
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


# ---------- the measurement ledger ----------

def ledger_key(basename):
    """Dedup key for a measured image: the basename with a trailing .fz
    stripped, so an image measured while in uploads/img_* is recognized as
    already-measured after it gets fpack-compressed on archiving
    (wcs_fd_X.fts and wcs_fd_X.fts.fz are the same observation)."""
    if basename.endswith('.fz'):
        return basename[:-3]
    return basename


def read_ledger(source_dir):
    """Return (rows, keys): rows are dicts with keys basename, jd, mag,
    err, status, camera (all strings); keys is the dedup set of
    ledger_key() values."""
    rows = []
    basenames = set()
    path = os.path.join(source_dir, LEDGER_BASENAME)
    try:
        with open(path) as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                parts = stripped.split()
                if len(parts) < 6:
                    continue
                rows.append({'basename': parts[0], 'jd': parts[1],
                             'mag': parts[2], 'err': parts[3],
                             'status': parts[4], 'camera': parts[5]})
                basenames.add(ledger_key(parts[0]))
    except OSError:
        pass
    return rows, basenames


def format_ledger_row(basename, jd, mag, err, status, camera):
    return '{} {} {} {} {} {}'.format(basename, jd, mag, err, status,
                                      camera or 'unknown')


def append_ledger_rows(uploads_dir, source_id, new_rows,
                       supersede_statuses=()):
    """Append rows (list of dicts as returned by read_ledger) whose basenames
    are not yet in the ledger. A row whose image IS already in the ledger is
    skipped, unless the existing row's status is in supersede_statuses: then
    the new row replaces it (the ledger is rewritten atomically). The callers
    that record a SUCCESSFUL measurement pass SUPERSEDABLE_STATUSES, so a
    reprocessing run that succeeds replaces the run_error rows of an earlier
    rejected run. Check-then-write happens under the per-source lock.
    Returns the number of rows appended or replaced."""
    source_dir = source_dir_path(uploads_dir, source_id)
    os.makedirs(source_dir, mode=0o755, exist_ok=True)
    lock_fh = acquire_source_lock(uploads_dir, source_id)
    try:
        existing_rows, existing = read_ledger(source_dir)
        path = os.path.join(source_dir, LEDGER_BASENAME)
        supersedable = set()
        if supersede_statuses:
            supersedable = set(ledger_key(r['basename']) for r in existing_rows
                               if r['status'] in supersede_statuses)
        replacements = {}
        to_append = []
        seen = set()
        for row in new_rows:
            key = ledger_key(row['basename'])
            if key in seen:
                continue
            if key in existing:
                if key in supersedable:
                    replacements[key] = row
                    seen.add(key)
                continue
            to_append.append(row)
            seen.add(key)
        if replacements:
            with open(path) as fh:
                lines = fh.read().splitlines()
            out_lines = []
            for line in lines:
                parts = line.split()
                is_row = len(parts) >= 6 and not line.lstrip().startswith('#')
                if is_row and ledger_key(parts[0]) in replacements and parts[4] in supersede_statuses:
                    row = replacements.pop(ledger_key(parts[0]))
                    out_lines.append(format_ledger_row(
                        row['basename'], row['jd'], row['mag'], row['err'],
                        row['status'], row['camera']))
                    continue
                out_lines.append(line)
            _write_text_atomic(path, '\n'.join(out_lines) + '\n')
        n_replaced = len(supersedable & seen) if supersede_statuses else 0
        need_header = not os.path.exists(path)
        if to_append:
            with open(path, 'a') as fh:
                if need_header:
                    fh.write('# image_basename JD mag err status camera\n')
                for row in to_append:
                    fh.write(format_ledger_row(
                        row['basename'], row['jd'], row['mag'], row['err'],
                        row['status'], row['camera']) + '\n')
        return len(to_append) + n_replaced
    finally:
        lock_fh.close()


def rewrite_measurement_status(uploads_dir, source_id, image_basename,
                               restore=False):
    """Flip the status of the ledger row(s) of one source that match
    image_basename (compared via ledger_key, so a trailing .fz does not
    matter), rewriting the ledger atomically under the per-source lock -
    the same lock append_ledger_rows takes, so a concurrent autoprocess
    ingest simply waits the few milliseconds this takes.

    restore=False: 'detection'/'upperlimit' rows become MANUAL_STATUS (the
    manual quality exclusion; the published products drop the point on the
    next rebuild). restore=True: MANUAL_STATUS rows go back to 'upperlimit'
    when they hold an upper limit - the error is the 99.0000 no-value marker
    (or the magnitude carries a '<' prefix) - and to 'detection' otherwise
    (the original magnitude and error are still in the row).

    Returns the number of rows changed (0 when the image is not in this
    source's ledger or no row was in a flippable state)."""
    source_dir = source_dir_path(uploads_dir, source_id)
    ledger_path = os.path.join(source_dir, LEDGER_BASENAME)
    if not os.path.isfile(ledger_path):
        return 0
    key = ledger_key(os.path.basename(image_basename))
    lock_fh = acquire_source_lock(uploads_dir, source_id)
    try:
        with open(ledger_path) as fh:
            lines = fh.read().splitlines()
        changed = 0
        out_lines = []
        for line in lines:
            parts = line.split()
            if (len(parts) >= 6 and not line.lstrip().startswith('#')
                    and ledger_key(parts[0]) == key):
                status = parts[4]
                if not restore and status in ('detection', 'upperlimit'):
                    parts[4] = MANUAL_STATUS
                    out_lines.append(' '.join(parts))
                    changed += 1
                    continue
                if restore and status == MANUAL_STATUS:
                    err_value = _float_or_none(parts[3])
                    is_limit = parts[2].startswith('<') or err_value is None or err_value >= 90.0
                    parts[4] = 'upperlimit' if is_limit else 'detection'
                    out_lines.append(' '.join(parts))
                    changed += 1
                    continue
            out_lines.append(line)
        if changed:
            tmp = '{}.tmp{}'.format(ledger_path, os.getpid())
            with open(tmp, 'w') as fh:
                fh.write('\n'.join(out_lines) + '\n')
            os.replace(tmp, ledger_path)
        return changed
    finally:
        lock_fh.close()


# ---------- transient-search run verdicts ----------
#
# The ingest gate in autoprocess.sh keeps the measurements of a run that
# raised a processing ERROR (or failed) out of the lightcurves. The manual
# modes of monitoring_update.py (--reconcile, --rescan-recent,
# --rescan-archive) find images on disk long after their run - in uploads,
# in the quarantine tree, in the long-term archive - so they need the run's
# verdict too, from a place that outlives the processing logs and the moves
# of the image files: autoprocess.sh appends the verdict of every run to
# RUN_VERDICTS_BASENAME in IMAGE_DATA_ROOT, one line per image of the upload:
#   <unix time> <verdict> <results_* directory> <image name> [<reason>]
# The image name is the uploaded file name, without the wcs_/fd_/d_ prefixes
# and the .fz suffix of the copies the pipeline makes (image_core_name), so
# every copy of the image finds its line. The newest line of an image wins:
# a reprocessing run overrides the verdict of the original run. The verdict
# is also kept in the monitoring ledgers, where the images of a rejected run
# get RUN_ERROR_STATUS rows.
# For images processed before the list existed, the verdict comes from the
# report (index.html) of the newest run of the image that left one, judged by
# the rules autoprocess.sh applies (verdict_from_report). The run is found
# through the name of the img_* directory that holds the image or, for an
# archive copy, through the preview image of the frame that the run left in
# its results_* directory (written since about 2026-01). With neither, the
# verdict is unknown and the image is measured as before.
RUN_VERDICTS_BASENAME = 'transient_search_verdicts.txt'
RUN_VERDICT_OK = 'ok'
# 'error': the report holds a processing ERROR; 'failed': the transient
# search exited with an error code, left no complete report or never finished
REJECTING_RUN_VERDICTS = ('error', 'failed')
# Written by autoprocess.sh right before the transient search starts: the
# plate-solved wcs_* images appear in the upload directory while the run is
# still going, and a manual rescan must not measure them before the run's
# verdict is known. A run that never finished (the machine went down
# mid-run) leaves 'pending' behind for good; after
# PENDING_RUN_VERDICT_STALE_SECONDS it counts as 'failed'.
RUN_VERDICT_PENDING = 'pending'
PENDING_RUN_VERDICT_STALE_SECONDS = 86400
# Written into the report when the transient search reaches its end; a
# report without it comes from a run that was killed
REPORT_COMPLETE_MARKER = 'Processing complete!'
# The verdicts that judge the images. A 'failed' run (killed, crashed, no
# complete report, nonzero exit) and a 'pending' run that never finished did
# not judge them, so an earlier conclusive verdict of the same image stands
# (a reprocessing run killed by a reboot does not reject a frame that a
# previous run processed successfully).
CONCLUSIVE_RUN_VERDICTS = (RUN_VERDICT_OK, 'error')
KNOWN_RUN_VERDICTS = (RUN_VERDICT_PENDING, RUN_VERDICT_OK) + REJECTING_RUN_VERDICTS
# A long manual run lists the results_* directories again, when it meets an
# upload directory missing from its index, at most this often
RESULTS_INDEX_REFRESH_SECONDS = 600

_RESULTS_DIR_RE = re.compile(r'^results_(\d{8}_\d{6})_(.+)$')
# A reprocessing run of img_<name> is named results_<ts>_reprocess_img_<name>_<key>
# where <key> is the new session key, "<pid>" or "<pid>_<8 letters/digits>"
_REPROCESS_RUN_RE = re.compile(r'^reprocess_(img_.+?)_\d+(?:_[A-Za-z0-9]{8})?$')
# <date>_CI_<field>_ in an upload (dataset) name, as astrocam-go makes it
_DATASET_DATE_FIELD_RE = re.compile(r'(\d{4}-\d{2}-\d{2})_CI_([^_]+)_')
# <field>_<date>_ at the start of an image name
_IMAGE_FIELD_DATE_RE = re.compile(r'^([^_]+)_(\d{4}-\d{2}-\d{2})_')


def image_core_name(path):
    """The uploaded file name of an image from the path of any of its
    copies: the basename without a trailing .fz and without the wcs_, fd_
    and d_ prefixes the pipeline adds (wcs_fd_X.fits.fz -> X.fits).
    autoprocess.sh applies the same rule when it writes
    RUN_VERDICTS_BASENAME."""
    name = os.path.basename(path)
    if name.endswith('.fz'):
        name = name[:-3]
    for prefix in ('wcs_', 'fd_', 'd_'):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name


class RunVerdictList:
    """The lines of RUN_VERDICTS_BASENAME per image core name: newest[core]
    is the newest line of the image and conclusive[core] the newest one with
    a CONCLUSIVE_RUN_VERDICTS verdict, each a (unix time, verdict, results_*
    name, reason) tuple. The file is append-only, so refresh() reads only
    what was appended since the previous call (a file that got shorter is
    read anew): a manual run that lasts hours sees the verdicts of the
    uploads processed meanwhile. A partial last line - an append in
    progress - is left for the next refresh. A list that exists but cannot
    be read is reported once through log; the verdicts then come from the
    reports only."""

    def __init__(self, image_data_root, log=None):
        self.path = os.path.join(image_data_root, RUN_VERDICTS_BASENAME)
        self.log = log
        self.newest = {}
        self.conclusive = {}
        self._offset = 0
        self._warned = False

    def refresh(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self._offset:
            self.newest = {}
            self.conclusive = {}
            self._offset = 0
        if size == self._offset:
            return
        try:
            with open(self.path, 'rb') as fh:
                fh.seek(self._offset)
                chunk = fh.read(size - self._offset)
        except OSError as exc:
            if self.log is not None and not self._warned:
                self.log('WARNING: cannot read {} ({}) - the verdicts of the '
                         'transient search runs come from their reports '
                         'only'.format(self.path, exc))
                self._warned = True
            return
        end = chunk.rfind(b'\n')
        if end < 0:
            return
        self._offset += end + 1
        for raw in chunk[:end].split(b'\n'):
            self._add_line(raw.decode('ascii', 'replace'))

    def _add_line(self, line):
        parts = line.split(None, 4)
        if len(parts) < 4 or parts[1] not in KNOWN_RUN_VERDICTS:
            return
        unixtime = _float_or_none(parts[0])
        if unixtime is None:
            return
        record = (unixtime, parts[1], parts[2],
                  parts[4].strip() if len(parts) > 4 else '')
        core = parts[3]
        known = self.newest.get(core)
        if known is None or unixtime >= known[0]:
            self.newest[core] = record
        if parts[1] in CONCLUSIVE_RUN_VERDICTS:
            known = self.conclusive.get(core)
            if known is None or unixtime >= known[0]:
                self.conclusive[core] = record


def read_run_verdicts(image_data_root, log=None):
    """{image core name: (unix time, verdict, results_* name, reason)} of
    the newest line of each image in RUN_VERDICTS_BASENAME; {} when the
    list does not exist yet."""
    verdict_list = RunVerdictList(image_data_root, log)
    verdict_list.refresh()
    return verdict_list.newest


def verdict_from_report(report_path):
    """(verdict, reason) of a transient-search run from its report
    (index.html), by the rules autoprocess.sh applies: 'error' and the first
    line containing ERROR (tags stripped); 'failed' when the report lacks
    REPORT_COMPLETE_MARKER (the run was killed); RUN_VERDICT_OK otherwise.
    None when the report is missing or empty: the run stopped before the
    transient search began (an aborted reprocessing, say) and says nothing
    about the images."""
    have_report = False
    complete = False
    try:
        with open(report_path, errors='replace') as fh:
            for line in fh:
                have_report = True
                if 'ERROR' in line:
                    return ('error', re.sub(r'<[^>]*>', '', line).strip())
                if REPORT_COMPLETE_MARKER in line:
                    complete = True
    except OSError:
        return None
    if not have_report:
        return None
    if not complete:
        return ('failed', 'the transient search did not complete (no "{}" '
                'in its report)'.format(REPORT_COMPLETE_MARKER))
    return (RUN_VERDICT_OK, '')


def upload_dir_of_results_dir(results_dirname):
    """(img_dir_name, run_timestamp) for a results_* directory name, both
    None when the name does not follow the autoprocess.sh convention."""
    m = _RESULTS_DIR_RE.match(results_dirname)
    if not m:
        return None, None
    timestamp, rest = m.group(1), m.group(2)
    reprocess = _REPROCESS_RUN_RE.match(rest)
    if reprocess:
        return reprocess.group(1), timestamp
    return 'img_' + rest, timestamp


class RunVerdictResolver:
    """Verdicts of the transient-search runs that processed images; see the
    comment above RUN_VERDICTS_BASENAME. One instance serves a whole manual
    run: the verdict list is followed as it grows (RunVerdictList), the
    results_* index is listed again when an upload directory is missing
    from it (at most every RESULTS_INDEX_REFRESH_SECONDS), and the report of
    a run is read once. now fixes the clock (tests)."""

    def __init__(self, image_data_root, now=None, log=None):
        self.image_data_root = image_data_root
        self.fixed_now = now
        self.log = log
        self.verdict_list = RunVerdictList(image_data_root, log)
        self._runs_by_upload = None
        self._runs_by_date_field = None
        self._results_index_time = 0.0
        self._report_verdicts = {}
        self._second_epoch_names = {}

    def _now(self):
        return time.time() if self.fixed_now is None else self.fixed_now

    def _load_results_index(self, force=False):
        if self._runs_by_upload is not None and not force:
            return
        self._results_index_time = time.time()
        self._runs_by_upload = {}
        self._runs_by_date_field = {}
        try:
            names = os.listdir(self.image_data_root)
        except OSError:
            names = []
        for name in names:
            img_name, timestamp = upload_dir_of_results_dir(name)
            if img_name is None:
                continue
            self._runs_by_upload.setdefault(img_name, []).append(
                (timestamp, name))
            date_field = _DATASET_DATE_FIELD_RE.search(img_name)
            if date_field:
                self._runs_by_date_field.setdefault(
                    (date_field.group(1), date_field.group(2)), []).append(
                        (timestamp, name))
        for runs in self._runs_by_upload.values():
            runs.sort(reverse=True)
        for runs in self._runs_by_date_field.values():
            runs.sort(reverse=True)

    def _verdict_of_run(self, results_name):
        if results_name not in self._report_verdicts:
            self._report_verdicts[results_name] = verdict_from_report(
                os.path.join(self.image_data_root, results_name,
                             'index.html'))
        return self._report_verdicts[results_name]

    def _processed_as_new_image(self, results_dir, core):
        """False when the run's list of its images
        (fits_images_for_download.txt, written since 2026-07) does not name
        the frame as a second-epoch image: the frame was then the run's
        reference image, whose preview the run also leaves. True when it
        does, or when the run has no such list."""
        if results_dir not in self._second_epoch_names:
            names = None
            try:
                with open(os.path.join(results_dir,
                                       'fits_images_for_download.txt'),
                          errors='replace') as fh:
                    names = set()
                    for line in fh:
                        parts = line.split()
                        if len(parts) >= 3 and parts[1] == 'second-epoch':
                            names.add(image_core_name(parts[2]))
            except OSError:
                names = None
            self._second_epoch_names[results_dir] = names
        names = self._second_epoch_names[results_dir]
        return names is None or core in names

    def _verdict_from_reports(self, image_path, core):
        """The verdict from the reports of the runs of the image, newest
        first: the first conclusive one, else the newest 'failed' one; None
        when no run of the image left a report."""
        self._load_results_index()
        dir_name = os.path.basename(os.path.dirname(
            os.path.abspath(image_path)))
        candidates = []
        if dir_name.startswith('img_'):
            candidates = self._runs_by_upload.get(dir_name)
            if candidates is None and time.time() - self._results_index_time > RESULTS_INDEX_REFRESH_SECONDS:
                self._load_results_index(force=True)
                candidates = self._runs_by_upload.get(dir_name)
            candidates = candidates or []
        need_preview = False
        if not candidates:
            # An archive copy: the runs of the field around the date of the
            # frame that left a preview of it
            m = _IMAGE_FIELD_DATE_RE.match(core)
            if m is None:
                return None
            try:
                day = datetime.date(int(m.group(2)[0:4]),
                                    int(m.group(2)[5:7]),
                                    int(m.group(2)[8:10]))
            except ValueError:
                return None
            for delta in (-1, 0, 1):
                date = (day + datetime.timedelta(days=delta)).isoformat()
                candidates = candidates + self._runs_by_date_field.get(
                    (date, m.group(1)), [])
            candidates = sorted(candidates, reverse=True)
            need_preview = True
        inconclusive = None
        for _timestamp, results_name in candidates:
            results_dir = os.path.join(self.image_data_root, results_name)
            if need_preview:
                has_preview = os.path.exists(os.path.join(
                    results_dir, core + '_preview.png')) or os.path.exists(
                        os.path.join(results_dir, 'fd_' + core + '_preview.png'))
                if not has_preview:
                    continue
                if not self._processed_as_new_image(results_dir, core):
                    continue
            from_report = self._verdict_of_run(results_name)
            if from_report is None:
                continue
            result = (from_report[0], from_report[1][:500],
                      os.path.join(results_dir, 'index.html'))
            if from_report[0] in CONCLUSIVE_RUN_VERDICTS:
                return result
            if inconclusive is None:
                inconclusive = result
        return inconclusive

    def _judge_listed(self, newest, conclusive):
        """(verdict, reason, origin) from the newest list line of an image
        and its newest conclusive one (or None)."""
        unixtime, verdict, results_name, reason = newest
        origin = '{} ({})'.format(RUN_VERDICTS_BASENAME, results_name)
        if verdict == RUN_VERDICT_PENDING:
            if self._now() - unixtime <= PENDING_RUN_VERDICT_STALE_SECONDS:
                return (verdict, reason, origin)
            verdict = 'failed'
            reason = ('the transient search run {} started {} never '
                      'finished'.format(results_name, time.strftime(
                          '%Y-%m-%d %H:%M', time.localtime(unixtime))))
        if verdict == 'failed' and conclusive is not None:
            # The later run did not judge the images: the earlier verdict stands
            return (conclusive[1], conclusive[3], '{} ({}; the later run {} '
                    'did not complete)'.format(RUN_VERDICTS_BASENAME,
                                               conclusive[2], results_name))
        return (verdict, reason, origin)

    def verdict_for_image(self, image_path):
        """(verdict, reason, origin) for one image file: verdict is
        RUN_VERDICT_OK, one of REJECTING_RUN_VERDICTS, RUN_VERDICT_PENDING
        (the run is in progress) or None when unknown; origin names where
        the verdict came from (for the log)."""
        self.verdict_list.refresh()
        core = image_core_name(image_path)
        newest = self.verdict_list.newest.get(core)
        if newest is not None:
            return self._judge_listed(newest,
                                      self.verdict_list.conclusive.get(core))
        from_reports = self._verdict_from_reports(image_path, core)
        if from_reports is not None:
            return from_reports
        return (None, '', 'no verdict')

    def verdict_for_copies(self, image_paths):
        """(verdict, reason, origin, path) for one image found at several
        paths (copies sharing a ledger key: the same frame in an img_*
        directory and in the archive, say), path being the copy the verdict
        came from. A successful run wins, then a rejected run, then a run in
        progress; unknown when no copy has a verdict."""
        found = {}
        for path in image_paths:
            verdict, reason, origin = self.verdict_for_image(path)
            if verdict is not None and verdict not in found:
                found[verdict] = (verdict, reason, origin, path)
        preference = (RUN_VERDICT_OK,) + REJECTING_RUN_VERDICTS + (RUN_VERDICT_PENDING,)
        for wanted in preference:
            if wanted in found:
                return found[wanted]
        return (None, '', 'no verdict', image_paths[0])


def _float_or_none(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def classify_ledger_rows(rows):
    """Split ledger rows into (detections, upperlimits, quality_excluded),
    each JD-sorted and with parsed jd/mag floats attached. Rows with the
    'cloudy' status (condemned by the per-frame cloud check at ingest time)
    go to quality_excluded, and so do the rows whose status says the
    measurement itself could not be made (EXCLUDED_STATUSES: the position
    fell on a masked CCD region, off the frame edge, on a saturated star
    and so on). The latter carry no magnitude - mag_float is None - but
    they are reported rather than dropped, so that a rejected image is
    never mistaken for an image that was never taken."""
    detections = []
    upperlimits = []
    quality_excluded = []
    for row in rows:
        if row['status'] in EXCLUDED_STATUSES:
            if row['status'] not in REASON_FOR_EXCLUDED_STATUS:
                continue
            jd = _float_or_none(row['jd'])
            if jd is None:
                continue
            parsed = dict(row)
            parsed['jd_float'] = jd
            mag = _float_or_none(row['mag'].lstrip('<'))
            parsed['mag_float'] = mag if mag is not None and mag <= 90.0 \
                else None
            # bad_wcs, no_nearby_stars and run_error rows may keep the
            # measured magnitude and error (99.0000 for an upper limit)
            parsed['err_float'] = None
            if parsed['mag_float'] is not None:
                parsed['err_float'] = _float_or_none(row['err'])
            parsed['reason'] = REASON_FOR_EXCLUDED_STATUS[row['status']]
            quality_excluded.append(parsed)
            continue
        jd = _float_or_none(row['jd'])
        mag = _float_or_none(row['mag'].lstrip('<'))
        if jd is None or mag is None or mag > 90.0:
            continue
        parsed = dict(row)
        parsed['jd_float'] = jd
        parsed['mag_float'] = mag
        if row['status'] == 'detection':
            parsed['err_float'] = _float_or_none(row['err'])
            detections.append(parsed)
        elif row['status'] == 'upperlimit':
            upperlimits.append(parsed)
        elif row['status'] == 'cloudy':
            parsed['err_float'] = _float_or_none(row['err'])
            parsed['reason'] = REASON_CLOUDY
            quality_excluded.append(parsed)
        elif row['status'] == MANUAL_STATUS:
            parsed['err_float'] = _float_or_none(row['err'])
            parsed['reason'] = REASON_MANUAL
            quality_excluded.append(parsed)
    detections.sort(key=lambda r: r['jd_float'])
    upperlimits.sort(key=lambda r: r['jd_float'])
    quality_excluded.sort(key=lambda r: r['jd_float'])
    return detections, upperlimits, quality_excluded


def split_inconsistent_visits(detections, upperlimits):
    """Partition JD-sorted detections and upper limits into
    (consistent_detections, consistent_upperlimits, inconsistent) using the
    within-visit consistency check described next to VISIT_GROUP_MAX_GAP_DAYS
    above. A visit containing both a detection and an upper limit is always
    discarded whole (see the comment block above). All returned lists stay
    JD-sorted. Rows are identified by their image basename (unique within a
    ledger)."""
    by_camera = {}
    for row in detections + upperlimits:
        by_camera.setdefault(row['camera'], []).append(row)
    bad_basenames = set()
    for camera_rows in by_camera.values():
        camera_rows.sort(key=lambda r: r['jd_float'])
        visit = []
        for row in camera_rows + [None]:
            if visit and (row is None or
                          row['jd_float'] - visit[-1]['jd_float'] >
                          VISIT_GROUP_MAX_GAP_DAYS):
                visit_dets = [v for v in visit
                              if v['status'] == 'detection']
                if visit_dets and len(visit_dets) != len(visit):
                    # mixed detection + upper limit visit: discard whole
                    for v in visit:
                        bad_basenames.add(v['basename'])
                elif len(visit_dets) > 1:
                    mags = [v['mag_float'] for v in visit_dets]
                    errs = [v['err_float']
                            if v['err_float'] is not None
                            and v['err_float'] < 90.0 else 0.0
                            for v in visit_dets]
                    tolerance = max(VISIT_CONSISTENCY_MAG_TOLERANCE,
                                    VISIT_CONSISTENCY_ERR_SCALE * max(errs))
                    if max(mags) - min(mags) > tolerance:
                        for v in visit_dets:
                            bad_basenames.add(v['basename'])
                visit = []
            if row is not None:
                visit.append(row)
    consistent_det = [r for r in detections
                      if r['basename'] not in bad_basenames]
    consistent_lim = [r for r in upperlimits
                      if r['basename'] not in bad_basenames]
    inconsistent = sorted(
        [r for r in detections + upperlimits
         if r['basename'] in bad_basenames],
        key=lambda r: r['jd_float'])
    return consistent_det, consistent_lim, inconsistent


# ---------- camera descriptions and AAVSO output ----------

def parse_factory_camera_comments(factory_text):
    """CAMERA_SETTINGS token -> AAVSO_COMMENT_STRING, parsed out of
    transient_factory_test31.sh so the camera table is never duplicated."""
    comments = {}
    from nmw_forced_phot_lib import _camera_block_body
    for camera in re.findall(r'\[\s*"\$CAMERA_SETTINGS"\s*=\s*"([^"]+)"',
                             factory_text):
        if camera in comments:
            continue
        body = _camera_block_body(factory_text, camera)
        if not body:
            continue
        match = re.search(r'AAVSO_COMMENT_STRING="([^"]+)"', body)
        if match:
            comments[camera] = match.group(1)
    return comments


def resolve_aavso_obscode(cfg, vast_dir):
    """OBSCODE precedence: AAVSO_OBSCODE from local_config.sh, then the
    AAVSO_previously_used_header.txt in the VaST reference copy, then XXX."""
    code = (cfg.get('AAVSO_OBSCODE') or '').strip()
    if code:
        return code
    header_path = os.path.join(vast_dir, 'AAVSO_previously_used_header.txt')
    try:
        with open(header_path) as fh:
            for line in fh:
                if line.startswith('#OBSCODE='):
                    code = line.split('=', 1)[1].strip()
                    if code:
                        return code
    except OSError:
        pass
    return 'XXX'


def software_version_string(vast_dir):
    try:
        result = subprocess.run([os.path.join(vast_dir, 'vast'), '--version'],
                                capture_output=True, text=True, timeout=10,
                                cwd=vast_dir)
        version = result.stdout.strip().splitlines()
        if version:
            return version[0].strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 'VaST'


_ATEL_DATE_CACHE = {}


def jd_to_atel_date(vast_dir, jd_str):
    """Convert a JD (string or number) to an ATel-style UTC calendar date
    (YYYY-MM-DD.fffff) using util/get_image_date, so the calendar dates shown
    on the monitoring pages use the exact same convention as the rest of VaST.
    Returns 'na' on any failure (missing binary, non-numeric JD, timeout).
    Memoized within a process run - the JD -> date mapping is deterministic,
    and the same JD appears in both the measurement table and the JD range."""
    jd_str = '{}'.format(jd_str)
    if jd_str in _ATEL_DATE_CACHE:
        return _ATEL_DATE_CACHE[jd_str]
    atel = 'na'
    if vast_dir:
        try:
            float(jd_str)  # only hand a real number to the tool
            proc = subprocess.run(
                [os.path.join(vast_dir, 'util', 'get_image_date'), jd_str],
                capture_output=True, text=True, timeout=30, cwd=vast_dir)
            for line in proc.stdout.splitlines():
                if 'ATel style' in line:
                    atel = line.split()[-1]
                    break
        except (OSError, subprocess.TimeoutExpired, ValueError):
            atel = 'na'
    _ATEL_DATE_CACHE[jd_str] = atel
    return atel


def aavso_filter_for_band(band):
    """Calibration band letter -> AAVSO filter code for an unfiltered CCD
    calibrated to that band (CV = unfiltered with V zero point, etc.)."""
    if not band or band == 'V':
        return 'CV'
    if band in ('R', 'Rc'):
        return 'CR'
    if band in ('I', 'Ic'):
        return 'CI'
    return 'C' + band


def aavso_star_name(display_name):
    """Bare star identifier for the AAVSO NAME field. The monitoring list
    conventions are 'identifier - comment' (e.g. 'AT 2026rdg - Nova in
    Aql 2026') and 'identifier = alias' (e.g. 'AU CVn = 1308+326'), but
    VSX/WebObs resolve only the bare identifier (checked empirically:
    the '=' form returns zero rows from the VSX API while the bare name
    resolves). The cut is at the first '-' or '=' that follows
    whitespace, so hyphenated identifiers such as ASAS-SN names survive
    intact."""
    return re.sub(r'\s[-=].*$', '', display_name).strip()


def write_aavso_file(source_dir, name, detections, upperlimits, obscode,
                     software, camera_comments, camera_bands):
    """AAVSO Extended Format file including the upper limits as fainter-than
    records: MAG prefixed with '<' and MERR set to 'na', per the AAVSO
    Extended File Format specification. NOTES carries the camera
    description (commas replaced - the field delimiter is a comma)."""
    name = aavso_star_name(name)
    records = []
    for row in detections:
        records.append((row['jd_float'], row, False))
    for row in upperlimits:
        records.append((row['jd_float'], row, True))
    records.sort(key=lambda item: item[0])
    lines = ['#TYPE=EXTENDED',
             '#OBSCODE={}'.format(obscode),
             '#SOFTWARE={}'.format(software),
             '#DELIM=,',
             '#DATE=JD',
             '#OBSTYPE=CCD',
             '#NAME,DATE,MAG,MERR,FILT,TRANS,MTYPE,CNAME,CMAG,KNAME,KMAG,'
             'AMASS,GROUP,CHART,NOTES']
    for jd, row, is_limit in records:
        camera = row['camera']
        band = camera_bands.get(camera, 'V')
        notes = camera_comments.get(camera, camera).replace(',', ';')
        if is_limit:
            mag_field = '<{:.3f}'.format(row['mag_float'])
            err_field = 'na'
        else:
            mag_field = '{:.3f}'.format(row['mag_float'])
            err = row.get('err_float')
            err_field = '{:.3f}'.format(err) if err is not None \
                and err < 90.0 else 'na'
        lines.append('{},{:.5f},{},{},{},NO,STD,ENSEMBLE,na,na,na,na,1,na,{}'
                     .format(name, jd, mag_field, err_field,
                             aavso_filter_for_band(band), notes))
    _write_text_atomic(os.path.join(source_dir, AAVSO_BASENAME),
                       '\n'.join(lines) + '\n')


# ---------- derived products ----------

def _write_text_atomic(path, text):
    tmp = '{}.tmp{}'.format(path, os.getpid())
    with open(tmp, 'w') as fh:
        fh.write(text)
    os.replace(tmp, path)


def rebuild_source_products(uploads_dir, entry, cfg, factory_text):
    """Rebuild every derived file of one source from its ledger: the
    four-column lightcurve, the upper-limits file, the AAVSO file, the plot
    and the source page. Idempotent; caller holds no lock (we take the
    per-source lock here)."""
    from nmw_forced_phot_lib import render_lightcurve_plots, band_for_camera
    source_id = entry['source_id']
    source_dir = source_dir_path(uploads_dir, source_id)
    if not os.path.isdir(source_dir):
        return
    vast_dir = (cfg.get('VAST_REFERENCE_COPY') or '').strip()
    lock_fh = acquire_source_lock(uploads_dir, source_id)
    try:
        rows, _ = read_ledger(source_dir)
        detections, upperlimits, quality_excluded = \
            classify_ledger_rows(rows)
        detections, upperlimits, inconsistent = \
            split_inconsistent_visits(detections, upperlimits)
        for row in inconsistent:
            row['reason'] = REASON_VISIT
        excluded = sorted(quality_excluded + inconsistent,
                          key=lambda r: r['jd_float'])

        # Manual per-source detection threshold - applied AFTER the
        # visit-consistency check so a flare-rise visit straddling the
        # threshold keeps its detection instead of being discarded as a
        # mixed detection+limit visit.
        threshold = read_detection_threshold(source_dir)
        detections, upperlimits = apply_detection_threshold(
            detections, upperlimits, threshold)

        # The trailing field-name column is extracted from the image
        # basename; the plot readers parse only the leading numeric columns
        # and ignore trailing tokens, so it does not disturb them.
        lc_lines = ['# JD(UTC) mag err camera field']
        for row in detections:
            err = row.get('err_float')
            lc_lines.append('{:.5f} {:.4f} {:.4f} {} {}'.format(
                row['jd_float'], row['mag_float'],
                err if err is not None and err < 90.0 else 0.001,
                row['camera'],
                ncl.field_name_from_fits(row['basename'])))
        lc_path = os.path.join(source_dir, LIGHTCURVE_BASENAME)
        _write_text_atomic(lc_path, '\n'.join(lc_lines) + '\n')

        ul_lines = ['# JD(UTC) limit_mag camera field']
        for row in upperlimits:
            ul_lines.append('{:.5f} {:.4f} {} {}'.format(
                row['jd_float'], row['mag_float'], row['camera'],
                ncl.field_name_from_fits(row['basename'])))
        ul_path = os.path.join(source_dir, UPPERLIMITS_BASENAME)
        _write_text_atomic(ul_path, '\n'.join(ul_lines) + '\n')

        inc_lines = ['# JD(UTC) mag err camera reason image_basename',
                     '# measurements excluded from lightcurve.dat, the plot'
                     ' and the AAVSO file',
                     '# mag/err 99.0000 mean the measurement could not be'
                     ' made on that image (see the reason column);'
                     ' a magnitude with err 99.0000 is an upper limit;'
                     ' the magnitudes of the unreliable_plate_solution,'
                     ' no_stars_around_position and processing_error_in_run'
                     ' rows were measured but are not trustworthy']
        for row in excluded:
            err = row.get('err_float')
            mag = row.get('mag_float')
            if mag is None:
                # The measurement could not be made at all (masked region,
                # frame edge, saturation): no magnitude exists, so the
                # magnitude and error columns carry the 99.0000 no-value
                # marker used throughout the ledger.
                inc_lines.append('{:.5f} 99.0000 99.0000 {} {} {}'.format(
                    row['jd_float'], row['camera'], row['reason'],
                    row['basename']))
                continue
            # An upper limit (or a row without an error) keeps the 99.0000
            # no-value marker in the error column
            inc_lines.append('{:.5f} {:.4f} {:.4f} {} {} {}'.format(
                row['jd_float'], mag,
                err if err is not None and err < 90.0 else 99.0,
                row['camera'], row['reason'], row['basename']))
        inc_path = os.path.join(source_dir, EXCLUDED_MEASUREMENTS_BASENAME)
        _write_text_atomic(inc_path, '\n'.join(inc_lines) + '\n')

        cameras = sorted(set(r['camera'] for r in detections + upperlimits))
        camera_comments = parse_factory_camera_comments(factory_text) \
            if factory_text else {}
        camera_bands = {}
        for camera in cameras:
            try:
                camera_bands[camera] = band_for_camera(factory_text, camera) \
                    or 'V'
            except Exception:
                camera_bands[camera] = 'V'
        write_aavso_file(source_dir, entry['name'], detections, upperlimits,
                         resolve_aavso_obscode(cfg, vast_dir),
                         software_version_string(vast_dir) if vast_dir
                         else 'VaST',
                         camera_comments, camera_bands)

        # The plot readers parse only the leading numeric columns, so the
        # 4-column lightcurve.dat and 3-column upperlimits.dat feed them
        # directly (trailing camera tokens are ignored).
        png_basename = None
        recent_png_basename = None
        if detections or upperlimits:
            png_basename, _ = render_lightcurve_plots(
                vast_dir, source_dir, entry['ra'], entry['dec'],
                lc_path if detections else None,
                ul_path if upperlimits else None)
            recent_png_basename = _render_recent_plot(
                vast_dir, source_dir, entry, detections, upperlimits)
        if recent_png_basename is None:
            # keep the rebuild idempotent: no lingering recent plot when the
            # current data does not produce one
            for stale_basename in (RECENT_PLOT_PNG_BASENAME,
                                   RECENT_PLOT_EPS_BASENAME):
                try:
                    os.remove(os.path.join(source_dir, stale_basename))
                except OSError:
                    pass

        _write_source_page(source_dir, entry, rows, detections, upperlimits,
                           excluded, png_basename, recent_png_basename,
                           cameras, vast_dir,
                           page_message=(cfg.get('MONITORING_PAGE_MESSAGE')
                                         or '').strip(),
                           threshold=threshold)
    finally:
        lock_fh.close()


def _render_recent_plot(vast_dir, source_dir, entry, detections,
                        upperlimits):
    """Render the last-RECENT_PLOT_WINDOW_DAYS lightcurve plot (same style
    as the full-range plot) into source_dir as lightcurve_recent.png/.eps.
    The window ends at the newest published point. Returns the PNG basename
    or None when the recent plot is not rendered: no points in the window,
    or ALL points are within the window (the full-range plot already IS the
    last-month view then, and showing it twice would be pointless).
    Best-effort: any rendering problem just leaves the page without the
    recent plot."""
    from nmw_forced_phot_lib import render_lightcurve_plots
    all_jd = [r['jd_float'] for r in detections + upperlimits]
    if not all_jd:
        return None
    cutoff = max(all_jd) - RECENT_PLOT_WINDOW_DAYS
    if min(all_jd) >= cutoff:
        # everything already fits in the window - the full plot covers it
        return None
    recent_det = [r for r in detections if r['jd_float'] >= cutoff]
    recent_ul = [r for r in upperlimits if r['jd_float'] >= cutoff]
    if not recent_det and not recent_ul:
        return None
    tmp_dir = tempfile.mkdtemp(prefix='.recent_plot_', dir=source_dir)
    try:
        lc_path = None
        if recent_det:
            lines = ['# JD(UTC) mag err camera field']
            for row in recent_det:
                err = row.get('err_float')
                lines.append('{:.5f} {:.4f} {:.4f} {} {}'.format(
                    row['jd_float'], row['mag_float'],
                    err if err is not None and err < 90.0 else 0.001,
                    row['camera'],
                    ncl.field_name_from_fits(row['basename'])))
            lc_path = os.path.join(tmp_dir, LIGHTCURVE_BASENAME)
            with open(lc_path, 'w') as fh:
                fh.write('\n'.join(lines) + '\n')
        ul_path = None
        if recent_ul:
            lines = ['# JD(UTC) limit_mag camera field']
            for row in recent_ul:
                lines.append('{:.5f} {:.4f} {} {}'.format(
                    row['jd_float'], row['mag_float'], row['camera'],
                    ncl.field_name_from_fits(row['basename'])))
            ul_path = os.path.join(tmp_dir, UPPERLIMITS_BASENAME)
            with open(ul_path, 'w') as fh:
                fh.write('\n'.join(lines) + '\n')
        # The renderers hardcode the lightcurve.png/.eps output names, so
        # render into the temporary directory and move the products to the
        # recent-plot names next to the full-range plot.
        png_basename, eps_basename = render_lightcurve_plots(
            vast_dir, tmp_dir, entry['ra'], entry['dec'], lc_path, ul_path)
        if not png_basename:
            return None
        os.replace(os.path.join(tmp_dir, png_basename),
                   os.path.join(source_dir, RECENT_PLOT_PNG_BASENAME))
        if eps_basename:
            os.replace(os.path.join(tmp_dir, eps_basename),
                       os.path.join(source_dir, RECENT_PLOT_EPS_BASENAME))
        return RECENT_PLOT_PNG_BASENAME
    except (OSError, ValueError):
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _write_source_page(source_dir, entry, ledger_rows, detections,
                       upperlimits, excluded, png_basename,
                       recent_png_basename, cameras,
                       vast_dir, page_message='', threshold=None):
    name = entry['name']
    title = 'Monitored source {}'.format(name)
    parts = ['<html><head><title>{}</title>\n{}\n</head><body>\n'.format(
        html_escape(title), ncl._PAGE_CSS)]
    parts.append('<h2>{}</h2>\n'.format(html_escape(title)))
    excluded_note = ''
    if excluded:
        excluded_note = (' &middot; {} excluded from the'
                         ' lightcurve'.format(len(excluded)))
    parts.append('<p>Position: <span class="code">{} {}</span>'
                 ' &middot; cameras: {} &middot; {} detections,'
                 ' {} upper limits{}</p>\n'.format(
                     html_escape(entry['ra']), html_escape(entry['dec']),
                     html_escape(' '.join(cameras) or 'none yet'),
                     len(detections), len(upperlimits), excluded_note))
    if threshold is not None:
        parts.append(
            '<p class="secondary"><b>Manual detection threshold:</b>'
            ' measurements fainter than mag {:.2f} are published as upper'
            ' limits at the measured magnitude, because the photometric'
            ' aperture is contaminated by light from an unrelated nearby'
            ' source. Below that threshold a measurement only bounds the'
            ' target brightness from above; only when the target outshines'
            ' the contaminant does a measurement count as a detection'
            ' (status <span class="code">{}</span> in the table below marks'
            ' the demoted measurements).</p>\n'.format(
                threshold, html_escape(STATUS_BELOW_THRESHOLD)))
    if detections or upperlimits:
        all_jd = [r['jd_float'] for r in detections + upperlimits]
        jd_min = min(all_jd)
        jd_max = max(all_jd)
        parts.append('<p>Date range: <span class="code">{}</span> .. '
                     '<span class="code">{}</span> (UTC) &middot; '
                     'JD <span class="code">{:.5f}</span> .. '
                     '<span class="code">{:.5f}</span></p>\n'.format(
                         html_escape(jd_to_atel_date(vast_dir,
                                                     '{:.5f}'.format(jd_min))),
                         html_escape(jd_to_atel_date(vast_dir,
                                                     '{:.5f}'.format(jd_max))),
                         jd_min, jd_max))
    # The last-30-days plot (when rendered) goes ABOVE the full-range plot;
    # both carry a heading so the reader knows which time span is which.
    for plot_file, plot_label in (
            (recent_png_basename,
             'Lightcurve - last {:.0f} days'.format(RECENT_PLOT_WINDOW_DAYS)),
            (png_basename, 'Lightcurve - full time range')):
        if not plot_file:
            continue
        # Cache-bust: the plot is overwritten in place at the same URL as the
        # lightcurve grows, but the served images carry a long/immutable
        # Cache-Control (fine for the write-once archive-photometry images).
        # Appending the plot's mtime makes each regenerated plot a new URL so
        # browsers fetch the fresh one instead of a stale cached copy.
        try:
            plot_version = int(os.path.getmtime(
                os.path.join(source_dir, plot_file)))
        except OSError:
            plot_version = 0
        parts.append('<h3>{}</h3>\n'.format(html_escape(plot_label)))
        parts.append('<p><img src="{}?v={}" style="max-width:100%"></p>\n'
                     .format(html_escape(plot_file), plot_version))
    parts.append('<p>Data files: <a href="{lc}">{lc}</a>'
                 ' (JD mag err camera field)'
                 ' &middot; <a href="{ul}">{ul}</a>'
                 ' (JD limit_mag camera field)'
                 ' &middot; <a href="{av}">{av}</a> (AAVSO Extended Format'
                 ' incl. fainter-than records)</p>\n'.format(
                     lc=LIGHTCURVE_BASENAME, ul=UPPERLIMITS_BASENAME,
                     av=AAVSO_BASENAME))
    parts.append('<p><a href="../index.html">All monitored sources</a></p>\n')
    from nmw_forced_phot_lib import wide_field_photometry_caveat_html
    parts.append(wide_field_photometry_caveat_html())
    # Per-installation note from local_config.sh (MONITORING_PAGE_MESSAGE),
    # e.g. a data-usage statement. HTML-escaped so the variable can only
    # ever inject plain text; nothing is shown when it is unset or empty.
    if page_message:
        parts.append('<p>{}</p>\n'.format(html_escape(page_message)))
    # Show ALL published measurements (detections + upper limits), newest
    # first, with the ATel-style calendar date as the first column.
    table_rows = sorted(detections + upperlimits,
                        key=lambda r: r['jd_float'], reverse=True)
    if table_rows:
        table_fmt = '{:<16} {:<17} {:<7} {:<8} {:<11} {:<11} {}\n'
        parts.append('<h3>Photometry table (newest measurements first)</h3>\n'
                     '<pre>\n')
        parts.append(table_fmt.format(
            'Date (UTC)', 'JD(UTC)', 'mag', 'err', 'status', 'camera',
            'image'))
        for row in table_rows:
            parts.append(table_fmt.format(
                html_escape(jd_to_atel_date(vast_dir, row['jd'])),
                html_escape(row['jd']), html_escape(row['mag']),
                html_escape(row['err']), html_escape(row['status']),
                html_escape(row['camera']), html_escape(row['basename'])))
        parts.append('</pre>\n')
    if excluded:
        parts.append(
            '<h3>Measurements excluded from the lightcurve</h3>\n'
            '<p class="secondary">These measurements are excluded from the'
            ' lightcurve, the plot and the AAVSO file.'
            ' Reason <span class="code">{rv}</span>: frames of the same'
            ' field taken minutes apart disagree by more than the expected'
            ' measurement scatter - a real star cannot change that fast, so'
            ' at least one frame of the visit is corrupted (typically by'
            ' patchy clouds) and there is no way to tell which one.'
            ' Reason <span class="code">{rc}</span>: the frame failed the'
            ' cloud check - its field stars disagree with the reference'
            ' frame in a way uniform transparency loss cannot explain.'
            ' The next five reasons mean the measurement could not be made'
            ' on that image at all, so no magnitude is shown:'
            ' <span class="code">{rb}</span> - the position falls on a'
            ' masked part of the detector (bad_region.lst of that camera);'
            ' <span class="code">{rs}</span> - the star is there but too'
            ' bright to measure; <span class="code">{rn}</span>,'
            ' <span class="code">{rk}</span> and'
            ' <span class="code">{rf}</span> - the aperture contained blank'
            ' pixels, the frame could not be calibrated, or the measuring'
            ' tool failed. These images WERE taken and WERE measured: they'
            ' are listed here so that a rejected measurement is never'
            ' mistaken for a gap in the coverage.'
            ' Three more reasons reject a measurement that was or could'
            ' have been made, so a magnitude may be shown, but it means'
            ' nothing: <span class="code">{re}</span> - the transient search'
            ' run that processed the image raised a processing error or'
            ' failed, so none of its measurements is used;'
            ' <span class="code">{rw}</span> - the image was plate-solved'
            ' without a distortion polynomial, so away from the frame centre'
            ' the aperture may be tens of arcseconds off the source;'
            ' <span class="code">{rz}</span> - (almost) no stars were'
            ' detected within half a degree of the position on a frame rich'
            ' enough in stars to expect dozens there, typically a thick'
            ' cloud over the source.'
            ' The excluded rows are kept in'
            ' <a href="{f}">{f}</a>.</p>\n<pre>\n'.format(
                rv=REASON_VISIT, rc=REASON_CLOUDY,
                rb=REASON_FOR_EXCLUDED_STATUS['bad_region'],
                rs=REASON_FOR_EXCLUDED_STATUS['saturated'],
                rn=REASON_FOR_EXCLUDED_STATUS['nan_pixel'],
                rk=REASON_FOR_EXCLUDED_STATUS['calib_fail'],
                rf=REASON_FOR_EXCLUDED_STATUS['fail'],
                re=REASON_FOR_EXCLUDED_STATUS[RUN_ERROR_STATUS],
                rw=REASON_FOR_EXCLUDED_STATUS['bad_wcs'],
                rz=REASON_FOR_EXCLUDED_STATUS['no_nearby_stars'],
                f=EXCLUDED_MEASUREMENTS_BASENAME))
        table_fmt = '{:<16} {:<17} {:<7} {:<8} {:<28} {:<11} {}\n'
        parts.append(table_fmt.format(
            'Date (UTC)', 'JD(UTC)', 'mag', 'err', 'reason', 'camera',
            'image'))
        for row in sorted(excluded, key=lambda r: r['jd_float'],
                          reverse=True):
            # A row with no magnitude shows a dash rather than the 99.0000
            # no-value marker, which would read as a real measurement.
            if row.get('mag_float') is None:
                mag_text, err_text = '-', '-'
            else:
                mag_text, err_text = row['mag'], row['err']
            parts.append(table_fmt.format(
                html_escape(jd_to_atel_date(vast_dir, row['jd'])),
                html_escape(row['jd']), html_escape(mag_text),
                html_escape(err_text), html_escape(row['reason']),
                html_escape(row['camera']), html_escape(row['basename'])))
        parts.append('</pre>\n')
    parts.append('</body></html>\n')
    _write_text_atomic(os.path.join(source_dir, 'index.html'), ''.join(parts))


def rebuild_central_index(uploads_dir, entries, vast_dir):
    """The central monitoring page: one row per activated source, plus a
    pending list for list entries not activated on this machine."""
    root = monitoring_root(uploads_dir)
    if not os.path.isdir(root):
        return
    parts = ['<html><head><title>Monitored sources</title>\n{}\n</head>'
             '<body>\n<h2>Monitored sources</h2>\n'.format(ncl._PAGE_CSS)]
    activated = []
    pending = []
    for entry in entries:
        source_dir = source_dir_path(uploads_dir, entry['source_id'])
        if os.path.isdir(source_dir):
            activated.append(entry)
        else:
            pending.append(entry)
    if activated:
        parts.append('<table border="1" cellpadding="4">\n'
                     '<tr><th>Source</th><th>RA</th><th>Dec</th>'
                     '<th>Detections</th><th>Upper limits</th>'
                     '<th>Last date (UTC)</th><th>Last JD</th></tr>\n')
        for entry in sorted(activated, key=lambda e: e['name'].lower()):
            source_dir = source_dir_path(uploads_dir, entry['source_id'])
            rows, _ = read_ledger(source_dir)
            detections, upperlimits, _excluded = classify_ledger_rows(rows)
            detections, upperlimits, _inconsistent = \
                split_inconsistent_visits(detections, upperlimits)
            all_jd = [r['jd_float'] for r in detections + upperlimits]
            if all_jd:
                last_jd_num = max(all_jd)
                last_jd = '{:.5f}'.format(last_jd_num)
                last_date = jd_to_atel_date(vast_dir, last_jd)
            else:
                last_jd = 'no data'
                last_date = 'no data'
            # Rounded coordinates keep the table tidy; the full-precision
            # list values stay in the tooltip and on the per-source page.
            parts.append('<tr><td><a href="{}/index.html">{}</a></td>'
                         '<td class="code" title="{}">{}</td>'
                         '<td class="code" title="{}">{}</td>'
                         '<td>{}</td><td>{}</td><td class="code">{}</td>'
                         '<td class="code">{}</td>'
                         '</tr>\n'.format(
                             html_escape(entry['source_id']),
                             html_escape(entry['name']),
                             html_escape(entry['ra']),
                             html_escape(display_ra(entry['ra'])),
                             html_escape(entry['dec']),
                             html_escape(display_dec(entry['dec'])),
                             len(detections), len(upperlimits),
                             html_escape(last_date), last_jd))
        parts.append('</table>\n')
    else:
        parts.append('<p>No sources are activated on this machine yet.</p>\n')
    if pending:
        parts.append('<p class="code">{} source(s) in monitoring_list.txt '
                     'are not activated on this machine - run '
                     'monitoring_update.py --reconcile: {}</p>\n'.format(
                         len(pending),
                         html_escape(', '.join(e['name'] for e in pending))))
    parts.append(
        '<p class="secondary">Want your favorite sources added to the'
        ' monitoring list? E-mail'
        ' <a href="mailto:kirx@kirx.net">kirx@kirx.net</a> or open a pull'
        ' request on GitHub updating <a href="https://github.com/kirxkirx/'
        'nmw_calibration/blob/main/monitoring_list.txt">'
        'monitoring_list.txt</a>.</p>\n')
    parts.append('</body></html>\n')
    _write_text_atomic(os.path.join(root, 'index.html'), ''.join(parts))


# ---------- image enumeration ----------

def looks_like_fits(basename):
    lower = basename.lower()
    return lower.endswith(FITS_EXTENSIONS) \
        or lower.endswith(tuple(e + '.fz' for e in FITS_EXTENSIONS))


def list_all_recent_field_images(uploads_dir, covering_fields):
    """Every plate-solved (wcs_*) image of the covering fields in ALL
    uploads/img_* directories, regardless of age (the monitoring backfill
    and rescans have no window - unlike coord_forced_photometry.py)."""
    images = []
    try:
        upload_entries = sorted(os.listdir(uploads_dir))
    except OSError:
        return images
    for entry in upload_entries:
        if not entry.startswith('img_'):
            continue
        dir_path = os.path.join(uploads_dir, entry)
        if not os.path.isdir(dir_path):
            continue
        try:
            files = sorted(os.listdir(dir_path))
        except OSError:
            continue
        for basename in files:
            if not basename.startswith('wcs_'):
                continue
            if not looks_like_fits(basename):
                continue
            if ncl.field_name_from_fits(basename) not in covering_fields:
                continue
            images.append(os.path.join(dir_path, basename))
    return images

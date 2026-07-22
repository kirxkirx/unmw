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
  edge/saturated/bad_region rows stay in the ledger (never retried) but are
  excluded from every published product.
"""

import fcntl
import os
import re
import shutil
import subprocess
import tempfile

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
# in the ledger so the image is never re-measured)
EXCLUDED_STATUSES = ('edge', 'saturated', 'bad_region', 'nan_pixel',
                     'calib_fail', 'fail', 'tool_fail')

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


def append_ledger_rows(uploads_dir, source_id, new_rows):
    """Append rows (list of dicts as returned by read_ledger) whose basenames
    are not yet in the ledger. Check-then-append happens under the per-source
    lock. Returns the number of rows actually appended."""
    source_dir = source_dir_path(uploads_dir, source_id)
    os.makedirs(source_dir, mode=0o755, exist_ok=True)
    lock_fh = acquire_source_lock(uploads_dir, source_id)
    try:
        _, existing = read_ledger(source_dir)
        path = os.path.join(source_dir, LEDGER_BASENAME)
        need_header = not os.path.exists(path)
        n_added = 0
        with open(path, 'a') as fh:
            if need_header:
                fh.write('# image_basename JD mag err status camera\n')
            for row in new_rows:
                if ledger_key(row['basename']) in existing:
                    continue
                fh.write(format_ledger_row(
                    row['basename'], row['jd'], row['mag'], row['err'],
                    row['status'], row['camera']) + '\n')
                existing.add(ledger_key(row['basename']))
                n_added += 1
        return n_added
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
    when their magnitude carries the '<' prefix and to 'detection'
    otherwise (the original magnitude and error are still in the row).

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
                    parts[4] = ('upperlimit' if parts[2].startswith('<')
                                else 'detection')
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


def _float_or_none(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def classify_ledger_rows(rows):
    """Split ledger rows into (detections, upperlimits, quality_excluded),
    each JD-sorted and with parsed jd/mag floats attached. Rows with the
    'cloudy' status (condemned by the per-frame cloud check at ingest time)
    go to quality_excluded; everything else that is neither a detection nor
    an upper limit is excluded from the published products entirely."""
    detections = []
    upperlimits = []
    quality_excluded = []
    for row in rows:
        if row['status'] in EXCLUDED_STATUSES:
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


def split_inconsistent_visits(detections):
    """Partition JD-sorted detections into (consistent, inconsistent) using
    the within-visit consistency check described next to
    VISIT_GROUP_MAX_GAP_DAYS above. Both returned lists stay JD-sorted.
    Rows are identified by their image basename (unique within a ledger)."""
    by_camera = {}
    for row in detections:
        by_camera.setdefault(row['camera'], []).append(row)
    bad_basenames = set()
    for camera_rows in by_camera.values():
        visit = []
        for row in camera_rows + [None]:
            if visit and (row is None or
                          row['jd_float'] - visit[-1]['jd_float'] >
                          VISIT_GROUP_MAX_GAP_DAYS):
                if len(visit) > 1:
                    mags = [v['mag_float'] for v in visit]
                    errs = [v['err_float']
                            if v['err_float'] is not None
                            and v['err_float'] < 90.0 else 0.0
                            for v in visit]
                    tolerance = max(VISIT_CONSISTENCY_MAG_TOLERANCE,
                                    VISIT_CONSISTENCY_ERR_SCALE * max(errs))
                    if max(mags) - min(mags) > tolerance:
                        for v in visit:
                            bad_basenames.add(v['basename'])
                visit = []
            if row is not None:
                visit.append(row)
    consistent = [r for r in detections
                  if r['basename'] not in bad_basenames]
    inconsistent = [r for r in detections
                    if r['basename'] in bad_basenames]
    return consistent, inconsistent


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
        detections, inconsistent = split_inconsistent_visits(detections)
        for row in inconsistent:
            row['reason'] = REASON_VISIT
        excluded = sorted(quality_excluded + inconsistent,
                          key=lambda r: r['jd_float'])

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
                     ' and the AAVSO file by the quality checks']
        for row in excluded:
            err = row.get('err_float')
            inc_lines.append('{:.5f} {:.4f} {:.4f} {} {} {}'.format(
                row['jd_float'], row['mag_float'],
                err if err is not None and err < 90.0 else 0.001,
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
                                         or '').strip())
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
                       vast_dir, page_message=''):
    name = entry['name']
    title = 'Monitored source {}'.format(name)
    parts = ['<html><head><title>{}</title>\n{}\n</head><body>\n'.format(
        html_escape(title), ncl._PAGE_CSS)]
    parts.append('<h2>{}</h2>\n'.format(html_escape(title)))
    excluded_note = ''
    if excluded:
        excluded_note = (' &middot; {} excluded by the quality'
                         ' checks'.format(len(excluded)))
    parts.append('<p>Position: <span class="code">{} {}</span>'
                 ' &middot; cameras: {} &middot; {} detections,'
                 ' {} upper limits{}</p>\n'.format(
                     html_escape(entry['ra']), html_escape(entry['dec']),
                     html_escape(' '.join(cameras) or 'none yet'),
                     len(detections), len(upperlimits), excluded_note))
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
            '<h3>Measurements excluded by the quality checks</h3>\n'
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
            ' The excluded rows are kept in'
            ' <a href="{f}">{f}</a>.</p>\n<pre>\n'.format(
                rv=REASON_VISIT, rc=REASON_CLOUDY,
                f=EXCLUDED_MEASUREMENTS_BASENAME))
        table_fmt = '{:<16} {:<17} {:<7} {:<8} {:<18} {:<11} {}\n'
        parts.append(table_fmt.format(
            'Date (UTC)', 'JD(UTC)', 'mag', 'err', 'reason', 'camera',
            'image'))
        for row in sorted(excluded, key=lambda r: r['jd_float'],
                          reverse=True):
            parts.append(table_fmt.format(
                html_escape(jd_to_atel_date(vast_dir, row['jd'])),
                html_escape(row['jd']), html_escape(row['mag']),
                html_escape(row['err']), html_escape(row['reason']),
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
            detections, _inconsistent = split_inconsistent_visits(detections)
            all_jd = [r['jd_float'] for r in detections + upperlimits]
            if all_jd:
                last_jd_num = max(all_jd)
                last_jd = '{:.5f}'.format(last_jd_num)
                last_date = jd_to_atel_date(vast_dir, last_jd)
            else:
                last_jd = 'no data'
                last_date = 'no data'
            parts.append('<tr><td><a href="{}/index.html">{}</a></td>'
                         '<td class="code">{}</td><td class="code">{}</td>'
                         '<td>{}</td><td>{}</td><td class="code">{}</td>'
                         '<td class="code">{}</td>'
                         '</tr>\n'.format(
                             html_escape(entry['source_id']),
                             html_escape(entry['name']),
                             html_escape(entry['ra']),
                             html_escape(entry['dec']),
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

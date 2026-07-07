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
import subprocess

import nmw_coord_lib as ncl
from nmw_coord_lib import html_escape

# The public data-product basenames
LEDGER_BASENAME = 'measured_images.txt'
BACKFILL_MARKER_BASENAME = 'backfill_done'
LIGHTCURVE_BASENAME = 'lightcurve.dat'
UPPERLIMITS_BASENAME = 'upperlimits.dat'
AAVSO_BASENAME = 'lightcurve_aavso.txt'

MONITORING_SUBDIR = 'monitoring'
LOCK_SUBDIR = '.monitoring_locks'
GLOBAL_LOCK_BASENAME = 'monitoring_global.lock'

# Measurement statuses excluded from all published products (still recorded
# in the ledger so the image is never re-measured)
EXCLUDED_STATUSES = ('edge', 'saturated', 'bad_region', 'nan_pixel',
                     'calib_fail', 'fail', 'tool_fail')

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

def read_ledger(source_dir):
    """Return (rows, basenames): rows are dicts with keys basename, jd, mag,
    err, status, camera (all strings); basenames is the dedup set."""
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
                basenames.add(parts[0])
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
                if row['basename'] in existing:
                    continue
                fh.write(format_ledger_row(
                    row['basename'], row['jd'], row['mag'], row['err'],
                    row['status'], row['camera']) + '\n')
                existing.add(row['basename'])
                n_added += 1
        return n_added
    finally:
        lock_fh.close()


def _float_or_none(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def classify_ledger_rows(rows):
    """Split ledger rows into (detections, upperlimits), each JD-sorted and
    with parsed jd/mag floats attached; everything else is excluded from the
    published products."""
    detections = []
    upperlimits = []
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
    detections.sort(key=lambda r: r['jd_float'])
    upperlimits.sort(key=lambda r: r['jd_float'])
    return detections, upperlimits


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


def write_aavso_file(source_dir, name, detections, upperlimits, obscode,
                     software, camera_comments, camera_bands):
    """AAVSO Extended Format file including the upper limits as fainter-than
    records: MAG prefixed with '<' and MERR set to 'na', per the AAVSO
    Extended File Format specification. NOTES carries the camera
    description (commas replaced - the field delimiter is a comma)."""
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
        detections, upperlimits = classify_ledger_rows(rows)

        lc_lines = ['# JD(UTC) mag err camera']
        for row in detections:
            err = row.get('err_float')
            lc_lines.append('{:.5f} {:.4f} {:.4f} {}'.format(
                row['jd_float'], row['mag_float'],
                err if err is not None and err < 90.0 else 0.001,
                row['camera']))
        lc_path = os.path.join(source_dir, LIGHTCURVE_BASENAME)
        _write_text_atomic(lc_path, '\n'.join(lc_lines) + '\n')

        ul_lines = ['# JD(UTC) limit_mag camera']
        for row in upperlimits:
            ul_lines.append('{:.5f} {:.4f} {}'.format(
                row['jd_float'], row['mag_float'], row['camera']))
        ul_path = os.path.join(source_dir, UPPERLIMITS_BASENAME)
        _write_text_atomic(ul_path, '\n'.join(ul_lines) + '\n')

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
        if detections or upperlimits:
            png_basename, _ = render_lightcurve_plots(
                vast_dir, source_dir, entry['ra'], entry['dec'],
                lc_path if detections else None,
                ul_path if upperlimits else None)

        _write_source_page(source_dir, entry, rows, detections, upperlimits,
                           png_basename, cameras)
    finally:
        lock_fh.close()


def _write_source_page(source_dir, entry, ledger_rows, detections,
                       upperlimits, png_basename, cameras):
    name = entry['name']
    title = 'Monitored source {}'.format(name)
    parts = ['<html><head><title>{}</title>\n{}\n</head><body>\n'.format(
        html_escape(title), ncl._PAGE_CSS)]
    parts.append('<h2>{}</h2>\n'.format(html_escape(title)))
    parts.append('<p>Position: <span class="code">{} {}</span>'
                 ' &middot; cameras: {} &middot; {} detections,'
                 ' {} upper limits</p>\n'.format(
                     html_escape(entry['ra']), html_escape(entry['dec']),
                     html_escape(' '.join(cameras) or 'none yet'),
                     len(detections), len(upperlimits)))
    if detections or upperlimits:
        all_jd = [r['jd_float'] for r in detections + upperlimits]
        parts.append('<p>JD range: <span class="code">{:.5f}</span> .. '
                     '<span class="code">{:.5f}</span></p>\n'.format(
                         min(all_jd), max(all_jd)))
    if png_basename:
        parts.append('<p><img src="{}" style="max-width:100%"></p>\n'.format(
            html_escape(png_basename)))
    parts.append('<p>Data files: <a href="{lc}">{lc}</a> (JD mag err camera)'
                 ' &middot; <a href="{ul}">{ul}</a> (JD limit_mag camera)'
                 ' &middot; <a href="{av}">{av}</a> (AAVSO Extended Format'
                 ' incl. fainter-than records)</p>\n'.format(
                     lc=LIGHTCURVE_BASENAME, ul=UPPERLIMITS_BASENAME,
                     av=AAVSO_BASENAME))
    parts.append('<p><a href="../index.html">All monitored sources</a></p>\n')
    recent = [r for r in ledger_rows
              if r['status'] not in EXCLUDED_STATUSES][-50:]
    if recent:
        parts.append('<h3>Most recent measurements</h3>\n<pre>\n')
        parts.append('JD             mag     err     status      camera'
                     '      image\n')
        for row in reversed(recent):
            parts.append('{:<14} {:<7} {:<7} {:<11} {:<11} {}\n'.format(
                html_escape(row['jd']), html_escape(row['mag']),
                html_escape(row['err']), html_escape(row['status']),
                html_escape(row['camera']), html_escape(row['basename'])))
        parts.append('</pre>\n')
    parts.append('</body></html>\n')
    _write_text_atomic(os.path.join(source_dir, 'index.html'), ''.join(parts))


def rebuild_central_index(uploads_dir, entries):
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
                     '<th>Last JD</th></tr>\n')
        for entry in sorted(activated, key=lambda e: e['name'].lower()):
            source_dir = source_dir_path(uploads_dir, entry['source_id'])
            rows, _ = read_ledger(source_dir)
            detections, upperlimits = classify_ledger_rows(rows)
            all_jd = [r['jd_float'] for r in detections + upperlimits]
            last_jd = '{:.5f}'.format(max(all_jd)) if all_jd else 'no data'
            parts.append('<tr><td><a href="{}/index.html">{}</a></td>'
                         '<td class="code">{}</td><td class="code">{}</td>'
                         '<td>{}</td><td>{}</td><td class="code">{}</td>'
                         '</tr>\n'.format(
                             html_escape(entry['source_id']),
                             html_escape(entry['name']),
                             html_escape(entry['ra']),
                             html_escape(entry['dec']),
                             len(detections), len(upperlimits), last_jd))
        parts.append('</table>\n')
    else:
        parts.append('<p>No sources are activated on this machine yet.</p>\n')
    if pending:
        parts.append('<p class="code">{} source(s) in monitoring_list.txt '
                     'are not activated on this machine - run '
                     'monitoring_update.py --reconcile: {}</p>\n'.format(
                         len(pending),
                         html_escape(', '.join(e['name'] for e in pending))))
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

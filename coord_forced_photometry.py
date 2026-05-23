#!/usr/bin/env python3
"""
CGI: forced-photometry lightcurve at a sky position over a recent time window.

The user enters one sky position; the page finds every wcs_fd_ image in the
uploads/ directory (the WINDOW_DAYS most recent days by img_<YYYY-MM-DD> dir date) whose
field covers that position, runs the C forced-photometry implementation on each
(util/forced_photometry.sh with FORCED_PHOTOMETRY_ONLY_C=yes), and presents the
results -- newest first -- as an HTML table (with a full-frame preview and a
zoom-in cutout marked with a red circle of the photometric aperture, plus a
link to the FITS file) and as a copy-paste ASCII table.

Which fields cover the position is determined exactly like coord_search.py: by
running lib/bin/sky2xy over $REFERENCE_IMAGES. The reference set contains every
camera's (co-pointed) references, so multi-camera setups are handled without
special-casing. The calibration band is derived per camera by parsing
util/transients/transient_factory_test31.sh, and can be overridden on the form.

Shares its engine (coordinate parsing, the sky2xy scan, thumbnail rendering,
config loading, page chrome) with coord_search.py via nmw_coord_lib.py.

Configuration (read from local_config.sh next to this script):
  REFERENCE_IMAGES                directory containing reference FITS images
  VAST_REFERENCE_COPY             path to the VaST source/install tree
  URL_OF_DATA_PROCESSING_ROOT     URL prefix for the served uploads/ directory
  COORD_SEARCH_THUMBNAIL_PIXELS   in-page thumbnail size (optional)
  COORD_FORCED_PHOT_ZOOMIN_PIXELS zoom-in half-width in source pixels (optional)

Per-request output directory uploads/forced_phot_<pid><rand>/ is left in place;
external housekeeping prunes uploads/forced_phot_* (this CGI prunes nothing).
"""

# Handle cgi module removal in Python 3.13+
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
try:
    import cgi
    import cgitb
except ImportError:
    import sys
    sys.exit("Error: 'cgi' module not found. "
             "For Python 3.13+, install: pip install legacy-cgi")

import datetime
import glob
import os
import random
import re
import shutil
import string
import subprocess
import sys
import time

import nmw_coord_lib as ncl
from nmw_coord_lib import (
    html_escape, _PAGE_CSS, back_link_url, form_page_url, emit_redirect,
    emit_message_page, parse_coordinates, read_config_vars,
    acquire_concurrency_slot, run_sky2xy_scan, get_image_metadata,
    make_zoomout_thumbnail, make_zoomin_thumbnail, render_thumbnail_link,
    field_name_from_fits, HIRES_THUMBNAIL_MULTIPLIER,
)

# The shared page-chrome helpers build their links from ncl.DEFAULT_FORM_PATH;
# point it at this page's input form.
DEFAULT_FORM_PATH = '/unmw/coord_forced_photometry.html'
ncl.DEFAULT_FORM_PATH = DEFAULT_FORM_PATH

# ---------- code-level constants (not deployment-specific) ----------
TEMP_PARENT = 'uploads'                 # mirrors upload.py's upload_dir
TEMP_DIR_PREFIX = 'forced_phot_'
WINDOW_DAYS = 2                         # TESTING: was 7 ("last one week"); shortened to keep tests quick
# TESTING: temporary cap on the number of images measured per request so the
# end-to-end loop completes in minutes during UI/feature work. Set to None to
# disable the cap (production value).
MAX_IMAGES_FOR_TESTING = 5
FORCED_PHOT_MAX_CONCURRENT = 5          # each request uses its own VaST working copy, so this only caps server load
FORCED_PHOT_TIMEOUT_SECONDS = 600       # per-image safety cap on forced_photometry.sh
VAST_COPY_TIMEOUT_SECONDS = 300         # cap on the per-request rsync of the VaST tree
# Per-request disposable VaST working copy (mirrors autoprocess.sh): rsync the
# reference tree excluding large/static data, then symlink that data back.
VAST_WORK_DIR_PREFIX = 'vast_forced_phot_'
VAST_COPY_EXCLUDES = ('astorb.dat', 'lib/catalogs', 'src', '.git', '.github')
DEFAULT_THUMBNAIL_PIXELS = 256
MIN_THUMBNAIL_PIXELS = 32
MAX_THUMBNAIL_PIXELS = 4096
DEFAULT_ZOOMIN_PIXELS = 40              # half-width of the zoom-in (source px); small so the aperture ring shows
DEFAULT_BAND = 'V'
# Filters util/forced_photometry.sh accepts (mirrors its own validation).
VALID_BANDS = ('B', 'V', 'R', 'Rc', 'I', 'Ic', 'r', 'i', 'g')
# Per-upload directory name: img_<YYYY-MM-DD>_<...>. Only these are considered.
IMG_DIR_RE = re.compile(r'^img_(\d{4})-(\d{2})-(\d{2})_')
FITS_EXTENSIONS = ('.fits', '.fit', '.fts')

# Extracts the YYYY-MM-DD_HH-MM-SS timestamp embedded in a wcs_fd_ filename;
# sorting on this alone reproduces JD order closely enough for streamed output.
_IMG_TS_RE = re.compile(r'(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})')

# Apache's CGI buffer is ~4 KB. Each streamed table row is appended with this
# whitespace comment so the buffer crosses the flush threshold within a couple
# of rows instead of stalling until many rows have accumulated.
_ROW_FLUSH_PAD = "<!-- " + (" " * 1500) + " -->\n"

FACTORY_REL_PATH = os.path.join('util', 'transients', 'transient_factory_test31.sh')


# ---------- band derivation (parse transient_factory_test31.sh) ----------

def _read_factory_text(vast_dir):
    """Return the text of transient_factory_test31.sh, or '' on failure."""
    path = os.path.join(vast_dir, FACTORY_REL_PATH)
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ''


def camera_settings_for_path(factory_text, path):
    """Apply the factory's camera-detection rules to a file path.

    Mirrors the block of
        if [[ "$INPUT_PATH..." == *"PATTERN"* ]] ... export CAMERA_SETTINGS="X"
    rules near the top of transient_factory_test31.sh: the first camera whose
    any pattern is a substring of the path wins. Returns the CAMERA_SETTINGS
    name, or '' if none match.
    """
    # Each rule is one or more *"PATTERN"* tests followed by CAMERA_SETTINGS="X".
    # Capture, in source order, (list_of_patterns, camera_name).
    rule_re = re.compile(
        r'((?:==\s*\*"[^"]+"\*\s*\]\]\s*(?:\|\|\s*\[\[[^\]]*)?)+?).*?'
        r'export\s+CAMERA_SETTINGS="([^"]+)"',
        re.DOTALL)
    pat_re = re.compile(r'==\s*\*"([^"]+)"\*')
    for m in rule_re.finditer(factory_text):
        patterns = pat_re.findall(m.group(1))
        camera = m.group(2)
        for pat in patterns:
            if pat in path:
                return camera
    return ''


def band_for_camera(factory_text, camera):
    """Derive the calibration band letter for a CAMERA_SETTINGS value.

    If the camera's settings block sets PHOTOMETRIC_CALIBRATION explicitly
    (e.g. APASS_I), the band is the token after the underscore (APASS_I -> I,
    APASS_V/TYCHO2_V -> V, ...). Otherwise the factory's field-of-view default
    applies, which is V for both narrow (APASS_V) and wide (TYCHO2_V) fields.
    """
    if camera:
        # Look inside the 'if [ "$CAMERA_SETTINGS" = "CAMERA" ];then ... fi'
        # block for an explicit PHOTOMETRIC_CALIBRATION assignment.
        blk_re = re.compile(
            r'\[\s*"\$CAMERA_SETTINGS"\s*=\s*"' + re.escape(camera) +
            r'"\s*\]\s*;?\s*then(.*?)\n\s*fi',
            re.DOTALL)
        bm = blk_re.search(factory_text)
        if bm:
            pm = re.search(r'PHOTOMETRIC_CALIBRATION="([^"]+)"', bm.group(1))
            if pm:
                token = pm.group(1).rsplit('_', 1)[-1]
                if token:
                    return token
    return DEFAULT_BAND


def derive_band(factory_text, path, override):
    """Return the band to use: the override if valid, else the parsed band."""
    if override:
        return override
    camera = camera_settings_for_path(factory_text, path)
    band = band_for_camera(factory_text, camera)
    if band not in VALID_BANDS:
        band = DEFAULT_BAND
    return band


# ---------- forced photometry + date helpers ----------

def get_jd_and_atel_date(vast_dir, fits_path):
    """Return (jd_str, atel_date_str) from util/get_image_date, or (None, None).

    Uses get_image_date for consistency with the rest of the codebase. Both
    values are trimmed to 4 decimal places (e.g. '2461181.2822', '2026-05-20.7822').
    """
    tool = os.path.join(vast_dir, 'util', 'get_image_date')
    try:
        result = subprocess.run([tool, fits_path], capture_output=True,
                                text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    jd = None
    atel = None
    for line in result.stdout.splitlines():
        s = line.strip()
        if jd is None and s.startswith('JD '):
            try:
                jd = '{:.4f}'.format(float(s.split()[1]))
            except (IndexError, ValueError):
                pass
        elif atel is None and s.startswith('ATel style '):
            tok = s.split()[-1]
            # Trim the day fraction to 4 digits (truncate, never carry).
            m = re.match(r'(\d{4}-\d{2}-\d{2})\.(\d+)', tok)
            if m:
                atel = '{}.{}'.format(m.group(1), m.group(2)[:4])
            else:
                atel = tok
    return jd, atel


def _log_skip(debug_log, fits_path, reason, returncode, stderr):
    """Append a diagnostic record for a skipped image to debug_log.

    Best-effort and never raises -- it records why an image yielded no
    measurement (returncode + the tail of forced_photometry.sh's stderr) so the
    operator can see the real cause instead of only the summary count.
    """
    if not debug_log:
        return
    try:
        with open(debug_log, 'a') as fh:
            fh.write('=== %s ===\n' % os.path.basename(fits_path))
            fh.write('reason: %s\n' % reason)
            if returncode is not None:
                fh.write('returncode: %s\n' % returncode)
            if stderr:
                tail = '\n'.join(stderr.splitlines()[-20:])
                fh.write('stderr tail:\n%s\n' % tail)
            fh.write('\n')
    except OSError:
        pass


def run_forced_photometry_c(work_dir, local_config_path, fits_path, ra, dec, band,
                            debug_log=None):
    """Run the C-only forced photometry on one image inside the working copy.

    work_dir is a per-request rsync copy of the VaST tree (see
    setup_vast_working_copy): forced_photometry.sh (via
    calibrate_single_image.sh / solve_plate_with_UCAC5) uses some paths relative
    to the VaST tree and writes its scratch (plate-solve products, catalogs,
    calib.txt) into the current directory, so running inside the disposable
    working copy keeps that scratch isolated and leaves $VAST_REFERENCE_COPY
    untouched.

    local_config.sh is sourced first -- exactly as autoprocess.sh does before
    it runs transient_factory_test31.sh -- so the calibration runs with the same
    environment the production pipeline uses (Python venv, VAST_SEXTRACTOR_CACHE_DIR,
    data-root exports). The bare Apache CGI environment lacks this, which is why
    forced photometry failed for every image until we matched autoprocess.sh.

    Returns a dict with keys jd, mag, err, status, basename, aperture, x, y,
    or None if the target is off the frame / the tool failed.
    """
    script = os.path.join(work_dir, 'util', 'forced_photometry.sh')
    env = os.environ.copy()
    env['FORCED_PHOTOMETRY_ONLY_C'] = 'yes'
    if local_config_path and os.path.isfile(local_config_path):
        # Source local_config.sh (its stdout sent to stderr so it cannot pollute
        # the forced-photometry result on stdout), then exec the script.
        cmd = ['bash', '-c', '. "$1" 1>&2; exec "$2" "$3" "$4" "$5" "$6"',
               'bash', local_config_path, script, fits_path, ra, dec, band]
    else:
        cmd = [script, fits_path, ra, dec, band]
    try:
        result = subprocess.run(
            cmd,
            cwd=work_dir, env=env, capture_output=True, text=True,
            timeout=FORCED_PHOT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _log_skip(debug_log, fits_path,
                  'timeout after %ds' % FORCED_PHOT_TIMEOUT_SECONDS,
                  None, getattr(exc, 'stderr', None))
        return None
    except OSError as exc:
        _log_skip(debug_log, fits_path, 'OSError: %s' % exc, None, None)
        return None
    if result.returncode != 0:
        # Non-zero exit includes the target-off-image case -> skip this image.
        _log_skip(debug_log, fits_path,
                  'forced_photometry.sh exited %d' % result.returncode,
                  result.returncode, result.stderr)
        return None
    aperture = None
    x = y = None
    c_line = None
    lines = result.stdout.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith('# aperture_diameter_pix:'):
            try:
                aperture = float(line.split(':', 1)[1].strip())
            except ValueError:
                aperture = None
        elif line.startswith('# target_pixel:'):
            toks = line.split(':', 1)[1].split()
            if len(toks) >= 2:
                try:
                    x = float(toks[0])
                    y = float(toks[1])
                except ValueError:
                    x = y = None
        elif line.startswith('# C implementation:'):
            if idx + 1 < len(lines):
                c_line = lines[idx + 1].strip()
    if not c_line or x is None or y is None:
        _log_skip(debug_log, fits_path,
                  'missing output markers (c_line=%s x=%s y=%s)'
                  % (bool(c_line), x, y), result.returncode, result.stderr)
        return None
    toks = c_line.split()
    if len(toks) < 5:
        _log_skip(debug_log, fits_path, 'malformed C line: %r' % c_line,
                  result.returncode, result.stderr)
        return None
    return {
        'jd': toks[0],
        'mag': toks[1],
        'err': toks[2],
        'status': toks[3],
        'basename': toks[-1],
        'aperture': aperture,
        'x': x,
        'y': y,
    }


# ---------- per-request VaST working copy ----------

def setup_vast_working_copy(vast_ref, parent_dir):
    """Make a disposable per-request copy of the VaST tree, the same way
    autoprocess.sh does: rsync the reference copy into parent_dir (excluding
    large/static data), then symlink that data back. forced_photometry.sh is
    then run inside the returned directory so its scratch (plate-solve products,
    catalogs, calib.txt) stays isolated and the reference copy is left clean;
    the caller rm -rf's it when the request is done.

    Returns the absolute working-copy path, or None on failure.
    """
    vast_ref = os.path.realpath(vast_ref)
    rand = ''.join(random.choice(string.ascii_letters) for _ in range(8))
    # Must be absolute: forced_photometry.sh is later run with cwd=work, and a
    # relative work path would make bash resolve the script path against that
    # same cwd, doubling the path (parent_dir 'uploads' is relative to cwd).
    work = os.path.abspath(os.path.join(parent_dir, '{}{}{}'.format(
        VAST_WORK_DIR_PREFIX, os.getpid(), rand)))
    cmd = ['rsync', '-a', '--whole-file', '--no-times', '--omit-dir-times']
    for ex in VAST_COPY_EXCLUDES:
        cmd.extend(['--exclude', ex])
    cmd.extend([vast_ref + '/', work])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=VAST_COPY_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not os.path.isdir(work):
        return None
    # Symlink the excluded large/static data back (mirrors autoprocess.sh).
    try:
        os.symlink(os.path.join(vast_ref, 'astorb.dat'),
                   os.path.join(work, 'astorb.dat'))
    except OSError:
        pass
    try:
        cat_link = os.path.join(work, 'lib', 'catalogs')
        if not os.path.exists(cat_link):
            os.symlink(os.path.join(vast_ref, 'lib', 'catalogs'), cat_link)
    except OSError:
        pass
    return work


# ---------- image discovery ----------

def list_recent_field_images(uploads_dir, covering_fields, window_days):
    """Return absolute paths of wcs_fd_ images in the last window_days whose
    field is in covering_fields. Newest directory date first.

    Only directories named img_<YYYY-MM-DD>_... are considered.
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=window_days - 1)
    images = []
    try:
        entries = os.listdir(uploads_dir)
    except OSError:
        return images
    dated = []
    for name in entries:
        m = IMG_DIR_RE.match(name)
        if not m:
            continue
        try:
            ddate = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if ddate < cutoff:
            continue
        dated.append((ddate, name))
    # Newest directory date first; the per-image JD sort happens later anyway.
    dated.sort(reverse=True)
    for _ddate, name in dated:
        dpath = os.path.join(uploads_dir, name)
        if not os.path.isdir(dpath):
            continue
        for fname in sorted(os.listdir(dpath)):
            if not fname.startswith('wcs_fd_'):
                continue
            if os.path.splitext(fname)[1].lower() not in FITS_EXTENSIONS:
                continue
            if field_name_from_fits(fname) in covering_fields:
                images.append(os.path.abspath(os.path.join(dpath, fname)))
    return images


# ---------- output ----------

def fits_url(url_prefix, fits_abs_path, uploads_abs):
    """Build the served URL of a FITS file under uploads/, or '' if outside."""
    try:
        rel = os.path.relpath(fits_abs_path, uploads_abs)
    except ValueError:
        return ''
    if rel.startswith('..'):
        return ''
    return '{}/{}'.format(url_prefix, rel)


def ascii_table(rows):
    """Build the fixed-width, space-padded ASCII table text."""
    header = ['date', 'JD', 'mag/limit', 'err', 'status', 'field', 'image_basename']
    body = [[r['atel'], r['jd'], r['mag'], r['err'], r['status'],
             r['field'], r['basename']] for r in rows]
    widths = [len(h) for h in header]
    for line in body:
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))
    # The image_basename (last column) is not padded -- it is the line's tail.
    def fmt(cols):
        out = []
        for i, cell in enumerate(cols):
            if i == len(cols) - 1:
                out.append(cell)
            else:
                out.append(cell.ljust(widths[i]))
        return '  '.join(out).rstrip()
    return '\n'.join([fmt(header)] + [fmt(line) for line in body])


def main():
    cgitb.enable()

    # cwd = the directory of this script (even if reached via a symlink), so
    # ./local_config.sh and uploads/ resolve correctly.
    script_dir = os.path.dirname(os.path.realpath(__file__))
    # local_config.sh sits next to this script; forced photometry sources it
    # (like autoprocess.sh) so it runs with the production VaST environment.
    local_config_path = os.path.join(script_dir, 'local_config.sh')
    try:
        os.chdir(script_dir)
    except OSError as err:
        emit_message_page(
            "Internal error",
            "<p>Cannot chdir to {}: {}</p>".format(
                html_escape(script_dir), html_escape(err)),
            status_line="Status: 500 Internal Server Error")
        return

    form = cgi.FieldStorage()
    raw_coords = (form.getfirst('coords', '') or '').strip()
    band_override = (form.getfirst('band', '') or '').strip()

    if not raw_coords:
        emit_redirect(form_page_url())
        return

    try:
        ra, dec = parse_coordinates(raw_coords)
    except ValueError as err:
        emit_message_page(
            "Invalid coordinates",
            "<p>Could not parse coordinates: <b>{}</b></p>"
            "<p>You typed: <span class='code'>{}</span></p>".format(
                html_escape(err), html_escape(raw_coords)))
        return

    if band_override and band_override not in VALID_BANDS:
        emit_message_page(
            "Invalid band",
            "<p>Unsupported band: <span class='code'>{}</span></p>"
            "<p>Supported bands: {}</p>".format(
                html_escape(band_override),
                ' '.join(VALID_BANDS)))
        return

    slot = acquire_concurrency_slot(prefix='forced_phot',
                                    max_concurrent=FORCED_PHOT_MAX_CONCURRENT)
    if slot is None:
        emit_message_page(
            "Server busy",
            "<p>The maximum number of concurrent forced-photometry requests "
            "({0} of {0}) is currently running. Please try again in a "
            "minute.</p>".format(FORCED_PHOT_MAX_CONCURRENT),
            status_line="Status: 503 Service Unavailable")
        return

    try:
        work_dir = None  # per-request VaST working copy; cleaned up in finally
        cfg = read_config_vars(
            'REFERENCE_IMAGES', 'VAST_REFERENCE_COPY',
            'URL_OF_DATA_PROCESSING_ROOT', 'COORD_SEARCH_THUMBNAIL_PIXELS',
            'COORD_FORCED_PHOT_ZOOMIN_PIXELS')
        ref_dir = cfg['REFERENCE_IMAGES'].strip()
        vast_dir = cfg['VAST_REFERENCE_COPY'].strip()
        url_prefix = cfg['URL_OF_DATA_PROCESSING_ROOT'].strip().rstrip('/')
        thumb_raw = cfg['COORD_SEARCH_THUMBNAIL_PIXELS'].strip()
        zoomin_raw = cfg['COORD_FORCED_PHOT_ZOOMIN_PIXELS'].strip()

        try:
            thumb_pixels = int(thumb_raw) if thumb_raw else DEFAULT_THUMBNAIL_PIXELS
        except ValueError:
            thumb_pixels = DEFAULT_THUMBNAIL_PIXELS
        if thumb_pixels < MIN_THUMBNAIL_PIXELS or thumb_pixels > MAX_THUMBNAIL_PIXELS:
            thumb_pixels = DEFAULT_THUMBNAIL_PIXELS
        try:
            zoomin_pixels = int(zoomin_raw) if zoomin_raw else DEFAULT_ZOOMIN_PIXELS
        except ValueError:
            zoomin_pixels = DEFAULT_ZOOMIN_PIXELS
        if zoomin_pixels < 5:
            zoomin_pixels = DEFAULT_ZOOMIN_PIXELS

        if not ref_dir or not os.path.isdir(ref_dir):
            emit_message_page(
                "Configuration error",
                "<p>Reference image directory not found: "
                "<span class='code'>{}</span></p>".format(html_escape(ref_dir)),
                status_line="Status: 500 Internal Server Error")
            return
        if not vast_dir or not os.path.isdir(vast_dir):
            emit_message_page(
                "Configuration error",
                "<p>VaST install directory not found: "
                "<span class='code'>{}</span></p>".format(html_escape(vast_dir)),
                status_line="Status: 500 Internal Server Error")
            return
        if not url_prefix:
            emit_message_page(
                "Configuration error",
                "<p><span class='code'>URL_OF_DATA_PROCESSING_ROOT</span> is "
                "not set in <span class='code'>local_config.sh</span>.</p>",
                status_line="Status: 500 Internal Server Error")
            return

        if not os.path.isdir(TEMP_PARENT):
            try:
                os.makedirs(TEMP_PARENT, mode=0o755)
            except OSError as err:
                emit_message_page(
                    "Configuration error",
                    "<p>Cannot create '{}': {}</p>".format(
                        html_escape(TEMP_PARENT), html_escape(err)),
                    status_line="Status: 500 Internal Server Error")
                return
        uploads_abs = os.path.abspath(TEMP_PARENT)

        # Per-request output directory; left in place for external housekeeping.
        rand = ''.join(random.choice(string.ascii_letters) for _ in range(8))
        sub = '{}{}{}'.format(TEMP_DIR_PREFIX, os.getpid(), rand)
        out_dir = os.path.join(TEMP_PARENT, sub)
        try:
            os.makedirs(out_dir, mode=0o755)
        except OSError as err:
            emit_message_page(
                "Internal error",
                "<p>Cannot create output directory '{}': {}</p>".format(
                    html_escape(out_dir), html_escape(err)),
                status_line="Status: 500 Internal Server Error")
            return

        # ---- Find which fields cover the position (both/all cameras). ----
        matches, _truncated = run_sky2xy_scan(ref_dir, ra, dec, vast_dir)
        covering_fields = set(field_name_from_fits(p) for p, _x, _y in matches)

        # ---- Find the recent images of those fields. ----
        images = []
        if covering_fields:
            images = list_recent_field_images(TEMP_PARENT, covering_fields,
                                              WINDOW_DAYS)
            # Stream rows in (approximate) newest-first order without waiting
            # for all images to be measured. The timestamp embedded in the
            # wcs_fd_ filename closely tracks JD and is known without opening
            # the file, so it makes a cheap proxy sort key.
            def _img_ts(p):
                m = _IMG_TS_RE.search(os.path.basename(p))
                return m.group(1) if m else ''
            images.sort(key=_img_ts, reverse=True)
            # TESTING: cap how many images we actually measure so UI/feature
            # iterations finish in minutes. Remembered so we can warn the user.
            total_matching = len(images)
            if MAX_IMAGES_FOR_TESTING is not None:
                images = images[:MAX_IMAGES_FOR_TESTING]
            capped_for_testing = (len(images) < total_matching)

        # ---- Stream the page header. ----
        page_title = "Forced-photometry lightcurve"
        print("Content-Type: text/html\n", flush=True)
        print("<html><head><title>{}</title>".format(html_escape(page_title)))
        print(_PAGE_CSS)
        # Page-local CSS in <head> so muted status lines streamed before the
        # table (e.g. "Preparing working copy of VaST...") are styled from
        # the moment they hit the browser, with no later restyle flash.
        print("<style type='text/css'>"
              "tr.skipped td { color: #888; font-size: 90%; "
              "background: #f8f8f8; }"
              " p.secondary { color: #666; font-style: italic; }"
              "</style>")
        print("</head><body>")
        print("<!-- {} -->".format(' ' * 4000))  # past Apache's CGI buffer
        print("<h2>{}</h2>".format(html_escape(page_title)))
        print("<p>Position: <span class='code'>{} {}</span>; "
              "last {} days.</p>".format(html_escape(ra), html_escape(dec),
                                         WINDOW_DAYS), flush=True)

        if not covering_fields:
            print("<div class='notice'>No reference field covers this "
                  "position.</div>")
            print("<br><a href='{}'>Search again</a>".format(
                html_escape(back_link_url())))
            print("</body></html>")
            return
        print("<p>Covering field(s): <b>{}</b></p>".format(
            html_escape(', '.join(sorted(covering_fields)))), flush=True)
        if not images:
            print("<div class='notice'>No images of these fields in the last "
                  "{} days.</div>".format(WINDOW_DAYS))
            print("<br><a href='{}'>Search again</a>".format(
                html_escape(back_link_url())))
            print("</body></html>")
            return
        print("<p>Performing forced photometry on {} images; this will "
              "take a while...</p>".format(len(images)), flush=True)
        if capped_for_testing:
            # Operator-visible reminder that the TESTING cap is in effect, so
            # nobody mistakes a 5-of-50 lightcurve for the full result.
            print("<p class='secondary'><i>Testing mode: measuring only the "
                  "first {} of {} matching images.</i></p>".format(
                      len(images), total_matching), flush=True)
        # Give the user something to watch during the ~30 s rsync that builds
        # the per-request working copy of VaST; without this line the page
        # sits silent until the first measurement row arrives.
        print("<p class='secondary'>Preparing working copy of VaST...</p>",
              flush=True)

        # ---- Disposable VaST working copy (autoprocess.sh style) so forced
        # photometry's scratch stays isolated from $VAST_REFERENCE_COPY. ----
        work_dir = setup_vast_working_copy(vast_dir, TEMP_PARENT)
        if work_dir is None:
            print("<div class='notice'>Could not set up the calibration "
                  "working copy of VaST; cannot measure.</div>")
            print("<br><a href='{}'>Search again</a>".format(
                html_escape(back_link_url())))
            print("</body></html>")
            return

        # ---- Streamed results table. We open the table immediately and emit
        # one <tr> per image as it finishes (success or skip) so the page
        # fills in instead of waiting for all measurements before any output
        # appears. The ASCII table is rendered once at the end, because its
        # column widths depend on the full result set.
        # Why-skipped diagnostics for any image that produced no measurement
        # are appended here (kept with the request output for inspection).
        skip_log = os.path.join(out_dir, 'forced_phot_skipped.log')
        factory_text = _read_factory_text(vast_dir)
        sub_name = os.path.basename(out_dir)
        # Hi-res click-through PNGs are HIRES_THUMBNAIL_MULTIPLIER times larger
        # than the in-page thumbnails (capped at MAX_THUMBNAIL_PIXELS).
        hires_pixels = min(MAX_THUMBNAIL_PIXELS,
                           thumb_pixels * HIRES_THUMBNAIL_MULTIPLIER)
        # Explanatory line; appears just above the table, then becomes context
        # for the rows that start arriving below it.
        print("<p class='secondary'>Each finished measurement appears as a "
              "row in the table below; the page keeps filling in until all "
              "images are processed.</p>", flush=True)
        print("<table class='main'>")
        print("<tr><th>Date (UTC)</th><th>JD (UTC)</th><th>mag</th><th>err</th>"
              "<th>Status</th><th>Band</th><th>Field</th>"
              "<th>Cutout</th><th>Image</th></tr>", flush=True)
        results = []
        for img in images:
            band = derive_band(factory_text, img, band_override)
            fp = run_forced_photometry_c(work_dir, local_config_path, img, ra, dec, band,
                                         debug_log=skip_log)
            if fp is None:
                # Faint placeholder so processing progress stays visible even
                # when several images in a row produce no measurement.
                print(_html_skipped_row(
                    img, field_name_from_fits(img),
                    fits_url(url_prefix, img, uploads_abs)) + _ROW_FLUSH_PAD,
                    flush=True)
                continue
            jd, atel = get_jd_and_atel_date(vast_dir, img)
            if jd is None:
                jd = '{:.4f}'.format(float(fp['jd'])) if _is_float(fp['jd']) else fp['jd']
            if atel is None:
                atel = '-'
            meta = get_image_metadata(img, vast_dir)
            nx = meta.get('nx') if meta else None
            ny = meta.get('ny') if meta else None
            png_preview = None
            png_preview_hires = None
            png_cutout = None
            png_cutout_hires = None
            if nx and ny:
                # Two PNGs per image: the small in-page thumbnail and a
                # higher-resolution version reached by clicking the thumbnail.
                png_preview = make_zoomout_thumbnail(
                    img, fp['x'], fp['y'], nx, ny, out_dir, vast_dir, thumb_pixels)
                png_preview_hires = make_zoomout_thumbnail(
                    img, fp['x'], fp['y'], nx, ny, out_dir, vast_dir, hires_pixels,
                    suffix='zoomout_hires')
            png_cutout = make_zoomin_thumbnail(
                img, fp['x'], fp['y'], out_dir, vast_dir, thumb_pixels,
                zoomin_pixels, aperture_circle_diameter=fp['aperture'])
            png_cutout_hires = make_zoomin_thumbnail(
                img, fp['x'], fp['y'], out_dir, vast_dir, hires_pixels,
                zoomin_pixels, suffix='zoomin_hires',
                aperture_circle_diameter=fp['aperture'])
            # Pre-format mag/err once so HTML and ASCII renderers use the
            # same string (rounded to 2 d.p.; '>' prefix on upper limits).
            r = {
                'jd': jd, 'atel': atel,
                'mag': _fmt_mag(fp['mag'], fp['status']),
                'err': _fmt_err(fp['err']),
                'status': fp['status'], 'band': band,
                'field': field_name_from_fits(img),
                'basename': fp['basename'],
                'fits_url': fits_url(url_prefix, img, uploads_abs),
                'png_preview': png_preview,
                'png_preview_hires': png_preview_hires,
                'png_cutout': png_cutout,
                'png_cutout_hires': png_cutout_hires,
            }
            results.append(r)
            print(_html_row(r, url_prefix, sub_name) + _ROW_FLUSH_PAD,
                  flush=True)
        print("</table>", flush=True)

        # ---- ASCII table for copy/paste -- rendered only after the loop so
        # column widths reflect the full result set. ----
        if results:
            print("<h3>ASCII table</h3>")
            print("<textarea rows='{}' cols='110' readonly "
                  "onclick='this.select()'>{}</textarea>".format(
                      min(40, len(results) + 2),
                      html_escape(ascii_table(results))))
        else:
            print("<div class='notice'>None of the {} image(s) yielded a "
                  "measurement (target off-frame or calibration failed).</div>".format(
                      len(images)))

        print("<br><br><a href='{}'>Search again</a>".format(
            html_escape(back_link_url())))
        print("</body></html>")
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        slot.close()


def _is_float(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _fmt_mag(mag_str, status):
    """Format a magnitude string to two decimal places, prepending '>' when
    the measurement is an upper limit. Falls through unchanged if the value
    is not numeric so any error/sentinel text passes to the user verbatim.
    """
    if not _is_float(mag_str):
        return mag_str
    rounded = '{:.2f}'.format(float(mag_str))
    return '>' + rounded if status == 'upperlimit' else rounded


def _fmt_err(err_str):
    """Format an error string to two decimal places, leaving non-numeric
    values (e.g. dashes for upper limits) untouched.
    """
    if not _is_float(err_str):
        return err_str
    return '{:.2f}'.format(float(err_str))


def _html_row(r, url_prefix, sub):
    """Render one streamed results <tr>. Layout (9 columns, no separate FITS
    column): Date | JD | mag | err | Status | Band | Field | Cutout | Image,
    where 'Image' contains the zoom-out thumbnail with a FITS link directly
    below it. Both thumbnails open a higher-resolution PNG on click via
    render_thumbnail_link.
    """
    fits_link = ''
    if r['fits_url']:
        fits_link = ("<br><a href='{u}' target='_blank'>FITS</a>".format(
            u=html_escape(r['fits_url'])))
    image_cell = render_thumbnail_link(
        r.get('png_preview'), r.get('png_preview_hires'),
        'image', r['basename'], url_prefix, sub) + fits_link
    cutout_cell = render_thumbnail_link(
        r.get('png_cutout'), r.get('png_cutout_hires'),
        'cutout', r['basename'], url_prefix, sub)
    return ("<tr>"
            "<td>{atel}</td><td>{jd}</td><td>{mag}</td><td>{err}</td>"
            "<td>{st}</td><td>{band}</td><td><b>{field}</b></td>"
            "<td>{cut}</td><td>{img}</td>"
            "</tr>".format(
                atel=html_escape(r['atel']), jd=html_escape(r['jd']),
                mag=html_escape(r['mag']), err=html_escape(r['err']),
                st=html_escape(r['status']), band=html_escape(r['band']),
                field=html_escape(r['field']),
                cut=cutout_cell, img=image_cell))


def _html_skipped_row(img_path, field_name, fits_link_url):
    """Faint placeholder row (9 columns) for an image that produced no
    measurement. The Cutout + Image columns are merged into one cell that
    carries the filename, the reason, and the FITS link -- so each streamed
    skip still advances the table by one row even when the thumbnails are
    unavailable.
    """
    base = os.path.basename(img_path)
    fits_link = ''
    if fits_link_url:
        fits_link = (" &mdash; <a href='{u}' target='_blank'>FITS</a>".format(
            u=html_escape(fits_link_url)))
    return ("<tr class='skipped'>"
            "<td>&mdash;</td><td>&mdash;</td><td>&mdash;</td><td>&mdash;</td>"
            "<td><i>skipped</i></td><td>&mdash;</td><td><b>{field}</b></td>"
            "<td colspan='2'><span class='code'>{base}</span> "
            "&mdash; off-frame or no measurement{fits}</td>"
            "</tr>".format(field=html_escape(field_name),
                           base=html_escape(base), fits=fits_link))


if __name__ == "__main__":
    main()

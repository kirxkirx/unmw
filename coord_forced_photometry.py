#!/usr/bin/env python3
"""
CGI: forced-photometry lightcurve at a sky position over a recent time window.

The user enters one sky position; the page finds every wcs_fd_ image in the
uploads/ directory (the most recent days by img_<YYYY-MM-DD> dir date; the day window and the maximum image count are both user-selectable on the input form) whose
field covers that position, runs the C forced-photometry implementation on each
(util/forced_photometry.sh with FORCED_PHOTOMETRY_ONLY_C=yes), and presents the
results -- newest first -- as an HTML table (with a full-frame preview and a
zoom-in cutout marked with a red circle of the photometric aperture, plus a
link to the FITS file) and as a copy-paste plain-text photometry table.

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

import concurrent.futures
import datetime
import glob
import os
import random
import re
import shutil
import signal
import string
import subprocess
import sys
import time
import urllib.parse

import nmw_coord_lib as ncl
from nmw_coord_lib import (
    html_escape, _PAGE_CSS, form_page_url, emit_redirect,
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
# Form-field defaults and safety caps for the "Look back (days)" and
# "Max images" inputs on the input form. The per-request values come from
# main() parsing the form; out-of-range values are clamped to [1, MAX_*].
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 30
DEFAULT_MAX_IMAGES = 8
MAX_MAX_IMAGES = 50
FORCED_PHOT_MAX_CONCURRENT = 3          # each request uses its own VaST working copy, so this only caps server load
# Phase 1 (parallel UCAC5+APASS plate-solve) worker cap. The effective number
# of workers per request is min(len(images), os.cpu_count() or 4, this).
# Server-wide peak parallel solve_plate processes = this * FORCED_PHOT_MAX_CONCURRENT.
FORCED_PHOT_PARALLEL_SOLVE_WORKERS = 8
FORCED_PHOT_TIMEOUT_SECONDS = 900       # per-image safety cap on forced_photometry.sh
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
# Safe-shape regex for ra/dec strings handed to subprocesses. Identical
# character class to nmw_coord_lib.COORDS_REGEX (digits, colon, +/-, period)
# minus whitespace and tab, since by the time a value reaches a subprocess
# call site it has already been split into a single token. Used as the
# defense-in-depth gate just before subprocess.run; parse_coordinates above
# is the primary validator.
_SAFE_COORD_RE = re.compile(r'^[0-9:+\-.]{1,32}$')


def _canonicalize_coord(token):
    """Re-parse a single ra-or-dec token through int()/float() and return
    a string assembled from those numeric values. Used right before any
    subprocess.run that takes coordinates in argv.

    Upstream parse_coordinates() and _SAFE_COORD_RE already constrain the
    token to digits, ':', '+', '-', and '.'. But CodeQL's
    py/command-line-injection query does not recognize re.match() as a
    sanitizer in its taint flow, so user-derived ra/dec strings appear
    "tainted" all the way into subprocess argv and the warning sticks.
    int()/float() outputs ARE recognized as sanitized; rebuilding the
    string from those numeric values terminates the taint at this
    function and silences the false positive without weakening the
    actual guarantee (which is already provided upstream).

    Accepts sexagesimal "HH:MM:SS.s" / "[+-]DD:MM:SS.s" or a decimal
    degree string. Raises ValueError on any unexpected shape, mirroring
    parse_coordinates() behaviour.
    """
    parts = token.split(':')
    if len(parts) == 1:
        # Decimal degrees.
        return '{:.8f}'.format(float(parts[0]))
    if len(parts) != 3:
        raise ValueError('invalid sexagesimal token: {!r}'.format(token))
    deg_part = parts[0]
    # Sign must come from a string LITERAL in each branch, not by slicing
    # the user-derived deg_part -- otherwise CodeQL sees the slice as
    # tainted and the taint propagates through the format string into
    # the subprocess argv (defeating the int/float sanitization below).
    if deg_part.startswith('-'):
        sign = '-'
        deg_part = deg_part[1:]
    elif deg_part.startswith('+'):
        sign = '+'
        deg_part = deg_part[1:]
    else:
        sign = ''
    return '{}{:02d}:{:02d}:{:09.6f}'.format(
        sign, int(deg_part), int(parts[1]), float(parts[2]))

# Per-upload directory name: img_<YYYY-MM-DD>_<...>. Only these are considered.
IMG_DIR_RE = re.compile(r'^img_(\d{4})-(\d{2})-(\d{2})_')
# Plain and funpack-compressed FITS endings. The compressed-suffix variants
# follow the same convention transient_factory_test31.sh uses
# (FITS_FILE_COMPRESSION_POSTFIX = .fz), so an upload of foo.fits.fz parks the
# wcs_fd_foo.fits.fz file in the per-night dir.
FITS_FILE_ENDINGS = ('.fits.fz', '.fit.fz', '.fts.fz', '.fits', '.fit', '.fts')


def _looks_like_fits(name):
    lname = name.lower()
    return any(lname.endswith(end) for end in FITS_FILE_ENDINGS)

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


def _camera_block_body(factory_text, camera):
    """Return the text inside `if [ "$CAMERA_SETTINGS" = "<camera>" ];then ... fi`.

    Indent-aware: the closing `fi` is matched at the same column as the
    opening `if`, so nested `if/fi` blocks (e.g. the DARK_FRAMES_DIR_OR_FILE
    and FLAT_FIELD_DIR_OR_FILE conditionals) do not prematurely end the
    match. Returns '' if no such block exists.
    """
    if not camera:
        return ''
    opening_re = re.compile(
        r'^(\s*)if\s*\[\s*"\$CAMERA_SETTINGS"\s*=\s*"' + re.escape(camera) +
        r'"\s*\]\s*;?\s*then\s*$',
        re.MULTILINE)
    om = opening_re.search(factory_text)
    if not om:
        return ''
    indent = om.group(1)
    body_start = om.end()
    # Closing `fi` at the same column as the opening `if`.
    closing_re = re.compile(r'^' + re.escape(indent) + r'fi\s*$', re.MULTILINE)
    cm = closing_re.search(factory_text, body_start)
    if not cm:
        return ''
    return factory_text[body_start:cm.start()]


def band_for_camera(factory_text, camera):
    """Derive the calibration band letter for a CAMERA_SETTINGS value.

    If the camera's settings block sets PHOTOMETRIC_CALIBRATION explicitly
    (e.g. APASS_I), the band is the token after the underscore (APASS_I -> I,
    APASS_V/TYCHO2_V -> V, ...). Otherwise the factory's field-of-view default
    applies, which is V for both narrow (APASS_V) and wide (TYCHO2_V) fields.
    """
    body = _camera_block_body(factory_text, camera)
    if body:
        pm = re.search(r'PHOTOMETRIC_CALIBRATION="([^"]+)"', body)
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


def sextractor_config_for_camera(factory_text, camera):
    """Return the SExtractor config filename optimised for the given camera.

    transient_factory_test31.sh assigns SEXTRACTOR_CONFIG_FILES per camera, with
    a script-wide comment that documents the convention:
        "Typically, the first run is optimized to detect bright targets while
         the second one is optimized for faint targets"
    so when two (or more) files are listed, we pick the second one. With a
    single file we use that. If the camera's block does not set
    SEXTRACTOR_CONFIG_FILES, we fall back to the script's global default at
    the top (`if [ -z "$SEXTRACTOR_CONFIG_FILES" ];then ... fi`). Inside the
    block, the LAST uncommented assignment wins (later `SEXTRACTOR_CONFIG_FILES=
    "..."` shadows the earlier ones). The bash variable `${CAMERA_SETTINGS}`
    is expanded so `default.sex.${CAMERA_SETTINGS}` becomes
    `default.sex.<camera>`.

    Returns the config filename (e.g. "default.sex.telephoto_lens_vSTL") or
    None if no config can be resolved -- in which case the caller should
    leave the working copy's generic default.sex untouched.
    """
    files_str = None
    # Per-camera block, mirroring band_for_camera.
    body = _camera_block_body(factory_text, camera)
    if body:
        for line in body.splitlines():
            stripped = line.lstrip()
            if stripped.startswith('#'):
                continue
            m = re.search(r'SEXTRACTOR_CONFIG_FILES="([^"]+)"', stripped)
            if m:
                files_str = m.group(1)  # last uncommented assignment wins
    # Global default at the top of the script.
    if files_str is None:
        global_re = re.compile(
            r'\[\s*-z\s+"\$SEXTRACTOR_CONFIG_FILES"\s*\]\s*;?\s*then'
            r'(.*?)\n\s*fi',
            re.DOTALL)
        gm = global_re.search(factory_text)
        if gm:
            for line in gm.group(1).splitlines():
                stripped = line.lstrip()
                if stripped.startswith('#'):
                    continue
                m = re.search(r'SEXTRACTOR_CONFIG_FILES="([^"]+)"', stripped)
                if m:
                    files_str = m.group(1)
                    break
    if not files_str:
        return None
    # Expand ${CAMERA_SETTINGS} / $CAMERA_SETTINGS.
    files_str = files_str.replace('${CAMERA_SETTINGS}', camera or '')
    files_str = files_str.replace('$CAMERA_SETTINGS', camera or '')
    parts = files_str.split()
    if not parts:
        return None
    # Faint-targets convention: second file when two or more are listed.
    return parts[1] if len(parts) >= 2 else parts[0]


def derive_sextractor_config(factory_text, path):
    """Return the SExtractor config filename for the camera in path, or None."""
    camera = camera_settings_for_path(factory_text, path)
    return sextractor_config_for_camera(factory_text, camera)


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


def _funpack_to_workdir(work_dir, fits_path):
    """Decompress a `.fz` upload into work_dir; return the funpacked path.

    For non-`.fz` inputs returns fits_path unchanged. Per-image tools that
    don't read fpack-compressed FITS reliably -- the lib/bin SExtractor
    binary (Unknown TFORM), and sky2xy (in some builds correct pixel
    coords but misreads dimensions so on-image targets get tagged as off
    image) -- consume the funpacked sibling instead. VaST tools that DO
    handle .fz natively (get_image_date, fov_of_wcs_calibrated_image.sh,
    fits2png, make_finding_chart, forced_photometry C engine) keep using
    the original path, so the served FITS link still points at the
    upload as the user submitted it.

    Returns the funpacked path on success, or None on funpack failure
    (caller treats the image as unprocessable). The funpacked sibling
    lives inside the disposable per-request work_dir and is removed when
    the request's work_dir is rm -rf'd.
    """
    if not fits_path.endswith('.fz'):
        return fits_path
    target = os.path.join(work_dir,
                          os.path.basename(fits_path)[:-len('.fz')])
    funpack = os.path.join(work_dir, 'util', 'funpack')
    try:
        result = subprocess.run([funpack, '-O', target, fits_path],
                                capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode != 0 or not os.path.isfile(target):
        return None
    return target


def _seed_sextractor_catalog(work_dir, fits_path, compute_path):
    """If transient_factory_test31.sh has already saved a SExtractor catalog
    next to fits_path, materialise it inside the per-request VaST working
    copy and register it in vast_images_catalogs.log so that
    sextract_single_image_noninteractive uses it instead of re-running
    SExtractor.

    Two candidate persisted catalog basenames are tried, in order, against
    the directory holding the original upload:
      <orig_dir>/fd_<rest>.cat       (original with leading `wcs_` stripped)
      <orig_dir>/<orig_basename>.cat (original basename verbatim)
    Both `<...>.cat` and `<...>.cat.aperture` must be present for the hit
    to count. The first candidate covers transient_factory_test31.sh runs
    where CALIBRATION_STATUS_PREFIX was `fd_` (catalog saved as
    `fd_<...>.cat`); the second covers `wcs_fd_` runs. For .fz uploads the
    `.fz` suffix appears in the saved basename naturally
    (`fd_<...>.fits.fz.cat`), so no special-casing is needed here.

    The materialised catalog inside work_dir is keyed to compute_path's
    basename, not the original's. compute_path is what
    sextract_single_image_noninteractive is invoked with later (the
    funpacked sibling for `.fz` uploads, the original path otherwise),
    and find_catalog_in_vast_images_catalogs_log in
    src/autodetect_aperture.c does an exact strcmp on the FITS-filename
    argv against the second column of vast_images_catalogs.log, so the
    log line we write here must use compute_path verbatim.

    The catalog and aperture files are touched after copying so their
    mtime is fresh -- defeats the `default.sex` newer-than-catalog check
    in src/autodetect_aperture.c that would otherwise force a recompute.

    Returns 'cache_hit' on success (catalog materialised, log line
    written), None otherwise. On None the caller lets
    sextract_single_image_noninteractive run for real on compute_path,
    which always succeeds because compute_path is always uncompressed.
    """
    orig_dir = os.path.dirname(fits_path)
    orig_base = os.path.basename(fits_path)
    candidates = []
    if orig_base.startswith('wcs_'):
        candidates.append(os.path.join(orig_dir, orig_base[len('wcs_'):]))
    candidates.append(os.path.join(orig_dir, orig_base))
    cat_src = None
    ap_src = None
    for cand in candidates:
        c = cand + '.cat'
        a = c + '.aperture'
        if os.path.isfile(c) and os.path.isfile(a):
            cat_src = c
            ap_src = a
            break
    if cat_src is None:
        return None
    compute_base = os.path.basename(compute_path)
    cat_dst = os.path.join(work_dir, compute_base + '.cat')
    ap_dst = cat_dst + '.aperture'
    try:
        shutil.copy(cat_src, cat_dst)
        shutil.copy(ap_src, ap_dst)
        # Bump mtime to now so the mtime check vs default.sex always passes.
        os.utime(cat_dst, None)
        os.utime(ap_dst, None)
    except OSError:
        return None
    log_path = os.path.join(work_dir, 'vast_images_catalogs.log')
    try:
        with open(log_path, 'a') as fh:
            fh.write('{}.cat {}\n'.format(compute_base, compute_path))
    except OSError:
        return None
    return 'cache_hit'


def _kill_process_group(proc):
    """SIGKILL the whole process group led by proc (started with
    start_new_session=True). Sends SIGTERM first for a brief grace period so
    children can clean up temp files, then SIGKILL whatever is left.
    Best-effort; never raises."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _run_capture_session(cmd, cwd=None, env=None, timeout=None):
    """subprocess.run(capture_output=True, text=True) workalike that runs the
    child in its OWN session/process group and, on timeout, SIGKILLs the whole
    group instead of just the immediate child.

    Plain subprocess.run(timeout=...) only kills the direct child on
    TimeoutExpired. Here the direct child is a `bash -c` wrapper that execs
    forced_photometry.sh / solve_plate_with_UCAC5, which spawn grandchildren
    (calibrate_single_image.sh, solve-field, ...). Killing only the wrapper
    orphans those grandchildren; solve_plate_with_UCAC5 has no internal time
    limit and would then keep burning a CPU core indefinitely, reparented to
    apache/init. start_new_session=True puts the whole subtree in one process
    group so os.killpg() takes it all down at once.

    Returns subprocess.CompletedProcess. Re-raises subprocess.TimeoutExpired
    (carrying whatever output was captured) so existing handlers keep working.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # Reap the (now dead) group leader and drain any buffered output.
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out, err = ('', '')
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _phase1_solve_one(work_dir, local_config_path, fits_path):
    """One Phase-1 task: funpack (if needed) -> seed catalog -> sextract
    -> solve_plate, all on compute_path (= funpacked sibling for `.fz`
    uploads, fits_path otherwise).

    Returns (fits_path, compute_path, returncode, stderr_tail,
    cache_status) so the parent can build the compute_path map for
    Phase 2, log failures uniformly via _log_skip, and count cache hits.
    compute_path is None when funpack failed; the parent then logs a
    skip and Phase 2 won't try to measure the image.

    Two binaries run sequentially per image, both inside the same
    Phase-1 worker task. Across images, tasks run in parallel.

    1. lib/sextract_single_image_noninteractive <compute_path>
       - produces image_pid<PID>.cat in cwd (default.param, 24-column,
         multi-aperture format)
       - APPENDS the catalog<->fits mapping to vast_images_catalogs.log
       This is the catalog Phase 2's forced_photometry.sh Step 1 looks
       for via the log; without it, Phase 2 re-runs SExtractor on every
       image. When _seed_sextractor_catalog returns 'cache_hit' the log
       already contains a line keyed by compute_path pointing at the
       seeded catalog, so the binary short-circuits without running
       SExtractor for real.

    2. util/solve_plate_with_UCAC5 <compute_path>
       - via blind_plate_solve_with_astrometry_net() ->
         wcs_image_calibration.sh -> identify.sh's catalog block, which
         first calls
         lib/reformat_existing_sextractor_catalog_according_to_wcsparam.sh.
         Because step 1 already populated vast_images_catalogs.log with
         an entry for compute_path, reformat succeeds and produces
         wcs_<basename>.fits.cat (wcs.param, 10-column) WITHOUT running
         SExtractor again.
       - solve_plate then reads that wcs_<basename>.fits.cat and runs
         the UCAC5 + APASS network queries, writing the photometric
         wcs_<basename>.fits.cat.ucac5 that Phase 2's
         calibrate_single_image.sh short-circuits on.
    """
    compute_path = _funpack_to_workdir(work_dir, fits_path)
    if compute_path is None:
        return (fits_path, None, None,
                'funpack failed for {}'.format(fits_path), None)
    cache_status = _seed_sextractor_catalog(work_dir, fits_path, compute_path)
    env = os.environ.copy()
    def _bash_wrap(script_path):
        if local_config_path and os.path.isfile(local_config_path):
            return ['bash', '-c', '. "$1" 1>&2; exec "$2" "$3"',
                    'bash', local_config_path, script_path, compute_path]
        return [script_path, compute_path]
    # Step 1: SExtract -- catalog + log entry needed for everything downstream.
    sextract_script = os.path.join(work_dir, 'lib',
                                   'sextract_single_image_noninteractive')
    try:
        r1 = _run_capture_session(_bash_wrap(sextract_script), cwd=work_dir,
                                  env=env, timeout=FORCED_PHOT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        return (fits_path, compute_path, None,
                'sextract timeout: ' + _exc_stderr_text(exc),
                cache_status)
    except OSError as exc:
        return (fits_path, compute_path, None,
                'sextract OSError: {}'.format(exc), cache_status)
    if r1.returncode != 0:
        return (fits_path, compute_path, r1.returncode,
                'sextract exit %d:\n%s' % (r1.returncode,
                                           (r1.stderr or '')[-2000:]),
                cache_status)
    # Step 2: plate-solve + UCAC5+APASS query.
    script = os.path.join(work_dir, 'util', 'solve_plate_with_UCAC5')
    try:
        result = _run_capture_session(
            _bash_wrap(script), cwd=work_dir, env=env,
            timeout=FORCED_PHOT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        return (fits_path, compute_path, None,
                'solve_plate timeout: ' + _exc_stderr_text(exc),
                cache_status)
    except OSError as exc:
        return (fits_path, compute_path, None,
                'solve_plate OSError: {}'.format(exc), cache_status)
    return (fits_path, compute_path, result.returncode,
            result.stderr or '', cache_status)


def _phase1_parallel_solve_plate(work_dir, local_config_path, images,
                                 max_workers, debug_log,
                                 progress_callback=None):
    """Phase 1: per-image funpack (for `.fz` uploads), SExtractor-catalog
    seeding, lib/sextract_single_image_noninteractive, and
    util/solve_plate_with_UCAC5, all in parallel across images, so that
    each per-image wcs_<basename>.cat.ucac5 (photometric, APASS columns
    populated) is on disk in work_dir before the serial
    forced_photometry.sh loop starts. Phase 2's internal solve_plate call
    then short-circuits.

    All four steps run inside _phase1_solve_one (one task per image),
    not as a pre-sweep here, so each parallel worker is self-contained
    and aggregating the cache-hit / funpack / solve outcomes back into
    counters happens via the worker return value rather than shared
    state. Images for which funpack fails are skipped here AND in
    Phase 2 (compute_path_map.get(img) is None).

    Returns
        (n_solved, n_cache_hits, n_funpacked, compute_path_map, elapsed)
    where compute_path_map[fits_path] is the path Phase 2 must hand to
    forced_photometry.sh -- the funpacked sibling for `.fz` uploads, or
    fits_path itself for plain FITS. Images missing from the map are
    those whose funpack failed.

    If progress_callback is provided, it is invoked once per completed
    future as (n_done, n_total, fits_path, rc). The caller uses this to
    stream a flushed line per image so the browser sees regular bytes
    during the otherwise silent Phase 1 (~30-60 s per image on
    UCAC5+APASS). Exceptions raised by the callback are swallowed so a
    progress UI glitch cannot fail the request.
    """
    if not images:
        return (0, 0, 0, {}, 0.0)
    start = time.time()
    n_solved = 0
    n_cache_hits = 0
    n_funpacked = 0
    compute_path_map = {}
    n_done = 0
    n_total = len(images)
    workers = max(1, min(len(images), max_workers))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_phase1_solve_one, work_dir, local_config_path,
                             img) for img in images]
        for fut in concurrent.futures.as_completed(futures):
            fits_path, compute_path, rc, stderr, cache_status = fut.result()
            n_done += 1
            if compute_path is not None:
                compute_path_map[fits_path] = compute_path
                if compute_path != fits_path:
                    n_funpacked += 1
            if cache_status == 'cache_hit':
                n_cache_hits += 1
            if rc == 0:
                n_solved += 1
            else:
                # Same diagnostic channel as run_forced_photometry_c uses.
                if compute_path is None:
                    reason = 'Phase 1: funpack failed'
                elif rc is None:
                    reason = 'Phase 1: timeout or OSError invoking ' \
                             'solve_plate_with_UCAC5'
                else:
                    reason = ('Phase 1: solve_plate_with_UCAC5 exited {}'
                              .format(rc))
                _log_skip(debug_log, fits_path, reason, rc, stderr)
            if progress_callback is not None:
                try:
                    progress_callback(n_done, n_total, fits_path, rc)
                except Exception:
                    pass
    return (n_solved, n_cache_hits, n_funpacked, compute_path_map,
            time.time() - start)


def _exc_stderr_text(exc):
    """Return an exception's captured stderr as text.

    subprocess.TimeoutExpired carries stdout/stderr as raw bytes even when
    subprocess.run() was called with text=True (decoding only happens on the
    normal CompletedProcess return path, not on the timeout exception). Coerce
    bytes to str here so the timeout handlers do not raise
    'can only concatenate str (not "bytes") to str' while reporting the skip.
    """
    se = getattr(exc, 'stderr', None)
    if isinstance(se, bytes):
        return se.decode('utf-8', 'replace')
    return se or ''


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


def run_forced_photometry_c(work_dir, local_config_path, fits_path, compute_path,
                            ra, dec, band, debug_log=None):
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

    compute_path is the FITS path actually handed to forced_photometry.sh
    (and through it to sextract_single_image_noninteractive and sky2xy).
    It is the funpacked sibling in work_dir for `.fz` uploads, or
    fits_path itself for plain FITS. fits_path is retained for use in
    diagnostic messages and the debug_log entries so the operator still
    sees the original upload path on skip lines.

    Returns a dict with keys jd, mag, err, status, basename, aperture, x, y,
    or None if the target is off the frame / the tool failed.
    """
    # Defense-in-depth re-validation right before exec. Upstream
    # parse_coordinates (in nmw_coord_lib) and the VALID_BANDS check in main()
    # already enforce these; restating them here makes the trust boundary
    # explicit at the call site, survives a future upstream refactor, and
    # lets static analyzers (e.g. CodeQL py/command-line-injection) see the
    # validation immediately preceding the subprocess.run call below.
    if not _SAFE_COORD_RE.match(ra) or not _SAFE_COORD_RE.match(dec):
        _log_skip(debug_log, fits_path,
                  'rejected: ra/dec failed safe-shape check', None, None)
        return None
    if band not in VALID_BANDS:
        _log_skip(debug_log, fits_path,
                  'rejected: band %r not in VALID_BANDS' % band, None, None)
        return None
    # Numeric round-trip on ra/dec right before they go into argv:
    # explicitly terminates CodeQL's taint flow (the regex above is
    # functionally sufficient but not recognised as a sanitizer).
    try:
        ra_safe = _canonicalize_coord(ra)
        dec_safe = _canonicalize_coord(dec)
    except ValueError as err:
        _log_skip(debug_log, fits_path,
                  'rejected: ra/dec failed numeric canonicalization (%s)' % err,
                  None, None)
        return None
    script = os.path.join(work_dir, 'util', 'forced_photometry.sh')
    env = os.environ.copy()
    env['FORCED_PHOTOMETRY_ONLY_C'] = 'yes'
    # Pass EVERY user-derived value (compute_path, ra, dec, band) through
    # the subprocess environment rather than argv, and reference them
    # from the bash -c shell template via "$NAME". This leaves argv
    # containing only string literals and server-controlled paths
    # (local_config_path and script, both derived from the script's own
    # directory and the config-supplied vast_dir). CodeQL's
    # py/command-line-injection query follows argv flow, not env, so
    # this leaves no taint path into the subprocess command line. The
    # shell "$NAME" expansion is properly quoted, so the values reach
    # the inner exec as separate argv elements without word-splitting.
    # compute_path is what forced_photometry.sh and its sky2xy /
    # SExtractor sub-calls actually need to read; fits_path is the
    # original (possibly .fz) upload path retained for diagnostic
    # messages (debug_log, skip rows).
    env['FORCED_PHOT_FITS'] = compute_path
    env['FORCED_PHOT_RA'] = ra_safe
    env['FORCED_PHOT_DEC'] = dec_safe
    env['FORCED_PHOT_BAND'] = band
    if local_config_path and os.path.isfile(local_config_path):
        # Source local_config.sh (its stdout sent to stderr so it cannot pollute
        # the forced-photometry result on stdout), then exec the script.
        cmd = ['bash', '-c',
               '. "$1" 1>&2; '
               'exec "$2" "$FORCED_PHOT_FITS" "$FORCED_PHOT_RA" '
               '"$FORCED_PHOT_DEC" "$FORCED_PHOT_BAND"',
               'bash', local_config_path, script]
    else:
        # Same env-passthrough wrapper without the local_config sourcing.
        cmd = ['bash', '-c',
               'exec "$1" "$FORCED_PHOT_FITS" "$FORCED_PHOT_RA" '
               '"$FORCED_PHOT_DEC" "$FORCED_PHOT_BAND"',
               'bash', script]
    # DEBUG: capture per-image forced_photometry.sh stderr + wall-clock time
    # to a sibling log file so we can see why SExtractor reruns / what the
    # script actually did.
    _debug_t0 = time.time()
    try:
        result = _run_capture_session(
            cmd,
            cwd=work_dir, env=env,
            timeout=FORCED_PHOT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _log_skip(debug_log, fits_path,
                  'timeout after %ds' % FORCED_PHOT_TIMEOUT_SECONDS,
                  None, _exc_stderr_text(exc))
        return None
    except OSError as exc:
        _log_skip(debug_log, fits_path, 'OSError: %s' % exc, None, None)
        return None
    # DEBUG: drop the full stdout+stderr + per-image wall-clock time into a
    # sibling log so we can see why SExtractor reruns / what actually ran.
    if os.environ.get('DEBUG_KEEP_WORK_DIR'):
        _debug_elapsed = time.time() - _debug_t0
        _debug_path = os.path.join(
            os.path.dirname(debug_log) if debug_log else work_dir,
            'fp_stderr_' + os.path.basename(fits_path) + '.log')
        try:
            with open(_debug_path, 'w') as _fh:
                _fh.write('elapsed: {:.2f} s\nreturncode: {}\n'
                          '--- stdout ---\n{}\n--- stderr ---\n{}\n'.format(
                              _debug_elapsed, result.returncode,
                              result.stdout, result.stderr))
        except OSError:
            pass
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
            if not _looks_like_fits(fname):
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


def _write_lightcurve_data_files(out_dir, results):
    """Write the two ASCII files lib/lightcurve_png reads.

    Splits the in-memory `results` rows by status:
      - detections (status != 'upperlimit') -> lightcurve.dat (JD mag err)
      - upper limits                        -> upperlimits.dat (JD limit_mag)

    Both files use the same comment convention as read_lightcurve_point_raw()
    expects: lines starting with '#' are skipped.

    Returns (lc_path, ul_path):
      lc_path is the path to lightcurve.dat, always written when any rows
              are processable (even if it ends up containing only the header
              line in the upper-limits-only case -- lib/lightcurve_png needs
              a positional input).
      ul_path is the path to upperlimits.dat, or None if there were no
              upper-limit rows.
    Returns (None, None) if no row had a parseable JD.
    """
    lc_lines = []
    ul_lines = []
    for r in results:
        try:
            jd_val = float(r.get('jd'))
        except (ValueError, TypeError):
            continue  # skip rows with unparseable JD
        if r.get('status') == 'upperlimit':
            # For upper limits, r['mag'] looks like '>17.50' -- strip the '>'
            # before parsing.
            mag_str = (r.get('mag') or '').lstrip('>')
            try:
                mag_val = float(mag_str)
            except ValueError:
                continue
            ul_lines.append('{:.5f} {:.3f}\n'.format(jd_val, mag_val))
        else:
            try:
                mag_val = float(r.get('mag'))
                err_val = float(r.get('err'))
            except (ValueError, TypeError):
                continue
            lc_lines.append('{:.5f} {:.3f} {:.3f}\n'.format(
                jd_val, mag_val, err_val))
    if not lc_lines and not ul_lines:
        return None, None
    lc_path = os.path.join(out_dir, 'lightcurve.dat')
    try:
        with open(lc_path, 'w') as fh:
            fh.write('# JD mag err\n')
            fh.writelines(lc_lines)
    except OSError:
        return None, None
    ul_path = None
    if ul_lines:
        ul_path = os.path.join(out_dir, 'upperlimits.dat')
        try:
            with open(ul_path, 'w') as fh:
                fh.write('# JD limit_mag\n')
                fh.writelines(ul_lines)
        except OSError:
            ul_path = None
    return lc_path, ul_path


def _render_lightcurve_png(work_dir, out_dir, ra, dec, lc_path, ul_path):
    """Invoke lib/lightcurve_png to render lightcurve.png in out_dir.

    Returns the PNG basename ('lightcurve.png') on success, None on any
    failure (binary missing, exit non-zero, timeout, OSError, no output).
    Failures are best-effort logged to stderr but never raise -- the rest
    of the results page renders normally without the plot.
    """
    binary = os.path.join(work_dir, 'lib', 'lightcurve_png')
    if not os.path.isfile(binary):
        # Log to stderr (Apache error log) so the absence is diagnosable
        # rather than silent. The HTML page still renders normally.
        sys.stderr.write(
            'lightcurve_png: binary not found at {} -- skipping plot. '
            'Build VaST so lib/compile_pgplot_related_components.sh '
            'produces it.\n'.format(binary))
        return None
    if lc_path is None:
        return None
    # The CGI's cwd is the cgi-bin dir, but the subprocess runs with
    # cwd=work_dir (the per-request VaST working copy). Convert every path
    # we hand to the subprocess to absolute so it resolves regardless of
    # whose cwd it is interpreted against.
    lc_abs = os.path.abspath(lc_path)
    ul_abs = os.path.abspath(ul_path) if ul_path is not None else None
    out_png = os.path.abspath(os.path.join(out_dir, 'lightcurve.png'))
    # Numeric round-trip on ra/dec right before they go into argv. The
    # title is a single argv element (no shell), so injection is already
    # impossible, but CodeQL's taint analysis does not see that and
    # flags user-derived ra/dec flowing into subprocess argv. The
    # int()/float() coercion inside _canonicalize_coord is a recognised
    # taint barrier. On bad input we render the plot without the title
    # rather than skip it -- the lightcurve is more valuable than the
    # title.
    try:
        ra_safe = _canonicalize_coord(ra)
        dec_safe = _canonicalize_coord(dec)
        title = 'Forced photometry at {} {}'.format(ra_safe, dec_safe)
    except ValueError:
        title = 'Forced photometry lightcurve'
    cmd = [binary, lc_abs, '-o', out_png, '--title', title]
    if ul_abs is not None:
        cmd.extend(['--upperlimits', ul_abs])
    try:
        result = subprocess.run(cmd, cwd=work_dir,
                                capture_output=True, text=True,
                                timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        sys.stderr.write(
            'lightcurve_png: subprocess launch failed: {}\n'.format(exc))
        return None
    if result.returncode != 0:
        sys.stderr.write(
            'lightcurve_png: exit {} for cmd {!r}\nstderr:\n{}\n'.format(
                result.returncode, cmd,
                (result.stderr or '')[-1000:]))
        return None
    if not os.path.isfile(out_png):
        sys.stderr.write(
            'lightcurve_png: exit 0 but {} was not created\n'.format(out_png))
        return None
    return os.path.basename(out_png)


def ascii_table(rows):
    """Build the fixed-width, space-padded plain-text photometry table."""
    header = ['date', 'JD', 'mag/limit', 'err', 'status', 'field', 'image']
    body = [[r['atel'], r['jd'], r['mag'], r['err'], r['status'],
             r['field'], r['basename']] for r in rows]
    widths = [len(h) for h in header]
    for line in body:
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))
    # The "image" basename (last column) is not padded -- it is the line's tail.
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
    # Wall-clock start so the bottom of the page can report total and
    # per-image times.
    start_time = time.time()

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
    raw_window_days = (form.getfirst('window_days', '') or '').strip()
    raw_max_images = (form.getfirst('max_images', '') or '').strip()

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

    # Per-request "look back days" and "max images" values from the form.
    # Empty -> default; non-integer -> error page; out-of-range -> silent clamp.
    if raw_window_days:
        try:
            window_days = int(raw_window_days)
        except ValueError:
            emit_message_page(
                "Invalid days",
                "<p>The 'Look back (days)' field must be a whole number. "
                "You sent: <span class='code'>{}</span></p>".format(
                    html_escape(raw_window_days)))
            return
        window_days = max(1, min(window_days, MAX_WINDOW_DAYS))
    else:
        window_days = DEFAULT_WINDOW_DAYS

    if raw_max_images:
        try:
            max_images = int(raw_max_images)
        except ValueError:
            emit_message_page(
                "Invalid max images",
                "<p>The 'Max images' field must be a whole number. "
                "You sent: <span class='code'>{}</span></p>".format(
                    html_escape(raw_max_images)))
            return
        max_images = max(1, min(max_images, MAX_MAX_IMAGES))
    else:
        max_images = DEFAULT_MAX_IMAGES

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

    # URL of the input form pre-filled with this request's values. Used for
    # every "Search again" link so the user can re-run with the same
    # coordinates and tweak only window_days / max_images / band. Empty
    # band_override is omitted so the form's default ("auto") remains
    # selected on the return visit.
    search_again_params = {
        'coords': raw_coords,
        'window_days': str(window_days),
        'max_images': str(max_images),
    }
    if band_override:
        search_again_params['band'] = band_override
    search_again_url = '{}?{}'.format(
        DEFAULT_FORM_PATH, urllib.parse.urlencode(search_again_params))

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

        # ---- Stream the page header EARLY, before the slow reference-field
        # scan and the uploads-directory walk, so the user is not staring at
        # a blank "loading" page for the ~10-30 s that those steps take.
        # Everything above this point (form validation, concurrency slot,
        # config check, mkdir) is instant, so a failure there can still
        # return a clean HTTP status via emit_message_page. Failures
        # AFTER this point are surfaced as inline notice divs in the
        # already-open page (with no HTTP status), because we have already
        # committed to a 200 OK response.
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
                                         window_days), flush=True)

        # ---- Find which fields cover the position (both/all cameras). ----
        print("<p class='secondary'>Looking up which reference fields cover "
              "this position...</p>", flush=True)
        try:
            matches, sky2xy_truncated = run_sky2xy_scan(
                ref_dir, ra, dec, vast_dir)
        except (OSError, subprocess.SubprocessError) as err:
            print("<div class='notice'>ERROR: reference-field scan failed: "
                  "{} ({}).</div>".format(
                      html_escape(type(err).__name__), html_escape(err)))
            print("<br><a href='{}'>Search again</a>".format(
                html_escape(search_again_url)))
            print("</body></html>")
            return
        covering_fields = set(field_name_from_fits(p) for p, _x, _y in matches)
        if sky2xy_truncated:
            # run_sky2xy_scan returned partial results because the per-FITS
            # sky2xy loop exceeded SCAN_TIMEOUT_SECONDS. Warn but continue
            # with whatever covering fields we did find.
            print("<div class='notice'>WARNING: reference-field scan timed "
                  "out after {} s; the list of covering fields below may be "
                  "incomplete.</div>".format(ncl.SCAN_TIMEOUT_SECONDS),
                  flush=True)

        if not covering_fields:
            print("<div class='notice'>ERROR: no reference field covers the "
                  "specified sky position.</div>")
            print("<br><a href='{}'>Search again</a>".format(
                html_escape(search_again_url)))
            print("</body></html>")
            return
        print("<p>Covering field(s): <b>{}</b></p>".format(
            html_escape(', '.join(sorted(covering_fields)))), flush=True)

        # ---- Find the recent images of those fields. ----
        print("<p class='secondary'>Listing recent images of these "
              "fields...</p>", flush=True)
        try:
            images = list_recent_field_images(TEMP_PARENT, covering_fields,
                                              window_days)
        except OSError as err:
            print("<div class='notice'>ERROR: could not list uploads "
                  "directory <span class='code'>{}</span>: {} ({}).</div>"
                  .format(html_escape(TEMP_PARENT),
                          html_escape(type(err).__name__),
                          html_escape(err)))
            print("<br><a href='{}'>Search again</a>".format(
                html_escape(search_again_url)))
            print("</body></html>")
            return
        # Stream rows in (approximate) newest-first order without waiting
        # for all images to be measured. The timestamp embedded in the
        # wcs_fd_ filename closely tracks JD and is known without opening
        # the file, so it makes a cheap proxy sort key.
        def _img_ts(p):
            m = _IMG_TS_RE.search(os.path.basename(p))
            return m.group(1) if m else ''
        images.sort(key=_img_ts, reverse=True)
        # Honor the user-selected "Max images" cap from the form.
        # Remembered so we can tell the user when the cap actually clipped
        # the result set.
        total_matching = len(images)
        images = images[:max_images]
        capped_by_user = (len(images) < total_matching)

        if not images:
            print("<div class='notice'>ERROR: no images of these fields "
                  "found in the last {} days.</div>".format(window_days))
            print("<br><a href='{}'>Search again</a>".format(
                html_escape(search_again_url)))
            print("</body></html>")
            return
        print("<p>Performing forced photometry on {} images; this will "
              "take a while...</p>".format(len(images)), flush=True)
        if capped_by_user:
            # Tell the user when the "Max images" cap clipped the result set,
            # so nobody mistakes a 6-of-50 lightcurve for the full result.
            print("<p class='secondary'><i>Limited to the first {} of {} "
                  "matching images by the Max images setting.</i></p>".format(
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
                html_escape(search_again_url)))
            print("</body></html>")
            return

        # ---- Phase 1: run util/solve_plate_with_UCAC5 in parallel across
        # all images so each wcs_<basename>.cat.ucac5 (photometric) is on
        # disk before the serial Phase 2 starts. This is the network-bound
        # step (UCAC5 + APASS queries) and the only one that benefits much
        # from in-request parallelism. Phase 2's internal solve_plate call
        # then short-circuits via check_if_the_output_catalog_already_exist.
        # (Failures here just mean Phase 2 falls through to the normal
        # recompute path for that image.)
        skip_log = os.path.join(out_dir, 'forced_phot_skipped.log')
        phase1_workers = min(len(images), os.cpu_count() or 4,
                             FORCED_PHOT_PARALLEL_SOLVE_WORKERS)
        # Stream a flushed line per finished plate-solve so the browser
        # sees regular bytes during Phase 1 (~30-60 s per image on
        # UCAC5+APASS). Without this the page sits silent from the
        # "Preparing working copy" line above until the table header
        # below, which on larger image sets risks browser/proxy timeouts.
        print("<p class='secondary'>Plate-solving and photometric "
              "catalog-matching {n} images using {w} parallel workers; "
              "each line below appears as one image finishes...</p>".format(
                  n=len(images), w=phase1_workers),
              flush=True)
        _phase1_progress_start = time.time()

        def _phase1_progress(done, total, fits_path, rc):
            elapsed_so_far = time.time() - _phase1_progress_start
            status = 'solved' if rc == 0 else 'failed (rc={})'.format(rc)
            print("<p class='secondary'>&nbsp;&nbsp;{d}/{t} {st}: {b} "
                  "(at {e:.1f} s)</p>".format(
                      d=done, t=total, st=status,
                      b=html_escape(os.path.basename(fits_path)),
                      e=elapsed_so_far),
                  flush=True)

        n_phase1_solved, sextractor_cache_hits, n_funpacked, \
            compute_path_map, phase1_elapsed = \
            _phase1_parallel_solve_plate(
                work_dir, local_config_path, images, phase1_workers,
                skip_log, progress_callback=_phase1_progress)

        # ---- Streamed results table. We open the table immediately and emit
        # one <tr> per image as it finishes (success or skip) so the page
        # fills in instead of waiting for all measurements before any output
        # appears. The plain-text photometry table is rendered once at the end, because its
        # column widths depend on the full result set.
        # Why-skipped diagnostics for any image that produced no measurement
        # are appended here (kept with the request output for inspection).
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
        # SExtractor config selected per image, mirroring how
        # transient_factory_test31.sh picks per-camera (see
        # sextractor_config_for_camera). Copied over the working copy's
        # default.sex right before each measurement; falls through silently
        # if the chosen file is missing so we never fail the measurement on
        # this account -- the generic default.sex remains in place.
        work_dir_default_sex = os.path.join(work_dir, 'default.sex')
        results = []
        # SExtractor catalogs were already seeded by Phase 1 above (which
        # also counted cache hits into sextractor_cache_hits). Per-image
        # default.sex is still picked per camera here just before the
        # measurement runs.
        for img in images:
            band = derive_band(factory_text, img, band_override)
            sex_config_name = derive_sextractor_config(factory_text, img)
            if sex_config_name:
                src_sex = os.path.join(work_dir, sex_config_name)
                if os.path.isfile(src_sex):
                    try:
                        # copy2, not copy: we need the destination default.sex
                        # to inherit the source's older mtime (set by the
                        # request-start rsync) rather than getting bumped to
                        # "now". Otherwise sextract_single_image_noninteractive
                        # sees default.sex newer than the cached
                        # wcs_<basename>.fits.cat (whether produced by Phase 1
                        # or seeded from the autoprocess artifacts) and the
                        # mtime check in autodetect_aperture.c forces a full
                        # SExtractor recompute -- defeating the whole point of
                        # Phase 1 and the catalog cache.
                        shutil.copy2(src_sex, work_dir_default_sex)
                    except OSError:
                        pass  # keep whatever default.sex was already there
            # compute_path is the funpacked sibling for `.fz` uploads, or
            # img itself for plain FITS. If the image is missing from the
            # map, Phase 1's funpack failed for it and there is nothing to
            # measure -- emit a skip row and move on.
            compute_path = compute_path_map.get(img)
            if compute_path is None:
                print(_html_skipped_row(
                    img, field_name_from_fits(img),
                    fits_url(url_prefix, img, uploads_abs)) + _ROW_FLUSH_PAD,
                    flush=True)
                continue
            fp = run_forced_photometry_c(work_dir, local_config_path, img,
                                         compute_path, ra, dec, band,
                                         debug_log=skip_log)
            if fp is None:
                # Faint placeholder so processing progress stays visible even
                # when several images in a row produce no measurement.
                print(_html_skipped_row(
                    img, field_name_from_fits(img),
                    fits_url(url_prefix, img, uploads_abs)) + _ROW_FLUSH_PAD,
                    flush=True)
                continue
            # The C engine prints the basename of whatever path it was
            # handed, which for `.fz` uploads is the funpacked sibling.
            # Override with the original upload basename so the row labels
            # match the FITS link the user clicks through to.
            fp['basename'] = os.path.basename(img)
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

        # ---- Lightcurve PNG plot.
        # Write the two data files into the per-request output directory so
        # they stay alongside the cutout PNGs and remain inspectable. Then
        # invoke lib/lightcurve_png to render the plot. Any failure (binary
        # missing, PGPLOT without libpng, etc.) is silent -- the rest of the
        # page renders normally without the plot.
        if results:
            _lc_path, _ul_path = _write_lightcurve_data_files(out_dir, results)
            if _lc_path is not None:
                _png_basename = _render_lightcurve_png(
                    work_dir, out_dir, ra, dec, _lc_path, _ul_path)
                if _png_basename is not None:
                    _png_url = '{}/{}/{}'.format(
                        url_prefix, sub_name, _png_basename)
                    print("<p style='text-align: center;'>"
                          "<img src='{}' alt='Lightcurve plot' "
                          "style='max-width: 100%;'></p>".format(
                              html_escape(_png_url)),
                          flush=True)
                # Link the ASCII data files immediately under the plot, so
                # the underlying numbers stay one click away. Emitted even
                # when the PNG render failed (binary missing, etc.) -- the
                # data files are still useful on their own.
                _lc_base = os.path.basename(_lc_path)
                _lc_url = '{}/{}/{}'.format(url_prefix, sub_name, _lc_base)
                _links = ["<a href='{}'>{}</a> (detections)".format(
                              html_escape(_lc_url), html_escape(_lc_base))]
                if _ul_path is not None:
                    _ul_base = os.path.basename(_ul_path)
                    _ul_url = '{}/{}/{}'.format(
                        url_prefix, sub_name, _ul_base)
                    _links.append(
                        "<a href='{}'>{}</a> (upper limits)".format(
                            html_escape(_ul_url), html_escape(_ul_base)))
                print("<p class='secondary' style='text-align: center;'>"
                      "Data files: {}</p>".format(', '.join(_links)),
                      flush=True)

        # ---- Photometry table for copy/paste -- rendered only after the
        # loop so column widths reflect the full result set. A simple <pre>
        # block is much more readable than a <textarea>, which was forced to
        # a fixed character width that wrapped long rows awkwardly.
        if results:
            print("<h3>Photometry table</h3>")
            print("<pre>{}</pre>".format(html_escape(ascii_table(results))))
        else:
            print("<div class='notice'>None of the {} image(s) yielded a "
                  "measurement (target off-frame or calibration failed).</div>".format(
                      len(images)))

        # Wall-clock summary, styled like the other diagnostic lines.
        elapsed = time.time() - start_time
        n_processed = len(images)
        if n_processed > 0:
            print("<p class='secondary'>Total computation time: {tot} "
                  "(average {avg} per image over {n} processed).</p>".format(
                      tot=_fmt_duration(elapsed),
                      avg=_fmt_duration(elapsed / n_processed),
                      n=n_processed))
            # SExtractor cache effectiveness -- "reused" means a catalog
            # produced by an earlier autoprocess.sh run was found next to
            # the image and used in place of running SExtractor again.
            print("<p class='secondary'>SExtractor catalog: {hit} reused "
                  "from autoprocess artifacts, {miss} computed fresh.</p>".format(
                      hit=sextractor_cache_hits,
                      miss=n_processed - sextractor_cache_hits))
            # Funpack diagnostic -- only shown when at least one `.fz`
            # upload was processed. The funpacked siblings live inside
            # the per-request VaST working copy and are cleaned up with
            # it; sextract / sky2xy / forced_photometry.sh consume the
            # uncompressed file while thumbnails / metadata / the served
            # FITS link still reference the original .fz.
            if n_funpacked > 0:
                print("<p class='secondary'>Funpack: {n} .fz upload(s) "
                      "decompressed for SExtractor / sky2xy compatibility."
                      "</p>".format(n=n_funpacked))
            # Parallel UCAC5 + APASS plate-solve timing.
            print("<p class='secondary'>UCAC5 plate-solve: "
                  "{n} of {tot} image(s) solved in parallel in {t} "
                  "(workers: {w}).</p>".format(
                      n=n_phase1_solved, tot=n_processed,
                      t=_fmt_duration(phase1_elapsed),
                      w=phase1_workers))
        else:
            print("<p class='secondary'>Total computation time: "
                  "{}.</p>".format(_fmt_duration(elapsed)))

        print("<br><br><a href='{}'>Search again</a>".format(
            html_escape(search_again_url)))
        print("</body></html>")
    finally:
        if work_dir is not None:
            if os.environ.get('DEBUG_KEEP_WORK_DIR'):
                print('<!-- DEBUG: keeping work_dir {} -->'.format(work_dir))
                sys.stderr.write('DEBUG: keeping work_dir {}\n'.format(work_dir))
            else:
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


def _fmt_duration(seconds):
    """Human-friendly duration. Under a minute -> 'X.X s'; otherwise
    'M min S.S s'. Used for the wall-clock and per-image lines at the
    bottom of the page.
    """
    if seconds < 60.0:
        return '{:.1f} s'.format(seconds)
    minutes, secs = divmod(seconds, 60.0)
    return '{:d} min {:.1f} s'.format(int(minutes), secs)


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

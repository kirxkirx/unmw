#!/usr/bin/env python3
"""Shared forced-photometry engine for the NMW web pages.

Extracted verbatim from coord_forced_photometry.py so that the interactive
CGI (coord_forced_photometry.py) and the archival-photometry queue worker
(archive_phot_worker.py) run the exact same measurement code: per-camera
band and SExtractor-config derivation (parsed from
util/transients/transient_factory_test31.sh), the disposable per-request
VaST working copy, the parallel UCAC5+APASS plate-solve stage, the C-only
forced photometry call (util/forced_photometry.sh with
FORCED_PHOTOMETRY_ONLY_C=yes), lightcurve data/PNG generation, and the
shared HTML/ASCII result-row renderers.

This module must stay import-safe (no CGI work at import time) and must not
import the cgi module -- the queue worker runs headless.
"""

import concurrent.futures
import math
import os
import random
import re
import shutil
import signal
import string
import subprocess
import sys
import time

from nmw_coord_lib import html_escape, render_thumbnail_link, \
    _reformat_sexagesimal

# Phase 1 (parallel UCAC5+APASS plate-solve) worker cap. The effective number
# of workers per request is min(len(images), os.cpu_count() or 4, this).
# Server-wide peak parallel solve_plate processes = this * FORCED_PHOT_MAX_CONCURRENT.
FORCED_PHOT_PARALLEL_SOLVE_WORKERS = 8
FORCED_PHOT_TIMEOUT_SECONDS = 900       # per-image safety cap on forced_photometry.sh
VAST_COPY_TIMEOUT_SECONDS = 300         # cap on the per-request rsync of the VaST tree
# Floor for the magnitude error written to lightcurve.dat. The forced-photometry tools report the
# true formal error, which is ~0 for a bright high-SNR star; lib/lightcurve_png silently drops
# points whose error is 0.0 (its raw reader's isnormal() check rejects 0.0), so such points vanish
# from the plot. 0.001 is the smallest value that survives the '%.3f' lightcurve.dat formatting, so
# it keeps the point on the plot while barely perturbing the (already negligible) error.
MIN_PLOT_MAG_ERROR = 0.001
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


def _format_title_coords(ra, dec):
    """Plot-title coordinate strings: the same _canonicalize_coord numeric
    round-trip the argv call sites use (CodeQL taint barrier), then
    trimmed for display -- seconds of time to 2 decimals (RA), seconds of
    arc to 1 decimal (Dec), plain decimal degrees to 5 decimals. Raises
    ValueError on input that does not parse."""
    ra_c = _canonicalize_coord(ra)
    dec_c = _canonicalize_coord(dec)
    if ':' in ra_c:
        ra_disp = _reformat_sexagesimal(ra_c, 2)
    else:
        ra_disp = '{:.5f}'.format(float(ra_c))
    if ':' in dec_c:
        dec_disp = _reformat_sexagesimal(dec_c, 1)
    else:
        dec_disp = '{:+.5f}'.format(float(dec_c))
    return ra_disp, dec_disp


# Plain and funpack-compressed FITS endings. The compressed-suffix variants
# follow the same convention transient_factory_test31.sh uses
# (FITS_FILE_COMPRESSION_POSTFIX = .fz), so an upload of foo.fits.fz parks the
# wcs_fd_foo.fits.fz file in the per-night dir.
FITS_FILE_ENDINGS = ('.fits.fz', '.fit.fz', '.fts.fz', '.fits', '.fit', '.fts')


def _looks_like_fits(name):
    lname = name.lower()
    return any(lname.endswith(end) for end in FITS_FILE_ENDINGS)

# Extracts the YYYY-MM-DD_HH-MM-SS timestamp embedded in a plate-solved
# image filename; sorting on the zero-padded groups reproduces JD order
# closely enough for streamed output. Month/day/hour/minute/second may be
# single-digit (the Stas camera writes 2026-7-4_18-14-34), so the groups
# are captured separately and the caller zero-pads them before comparing.
_IMG_TS_RE = re.compile(
    r'(\d{4})-(\d{1,2})-(\d{1,2})_(\d{1,2})-(\d{1,2})-(\d{1,2})')

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
        # Cap the funpack run: a corrupt .fz or a stalled NFS/storage read must
        # not hang forever, which would wedge the ThreadPoolExecutor future and
        # keep the worker holding its queue slot with no self-healing path.
        result = subprocess.run([funpack, '-O', target, fits_path],
                                capture_output=True, text=True, timeout=900)
    except (subprocess.TimeoutExpired, OSError):
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

def setup_vast_working_copy(vast_ref, parent_dir, prefix=VAST_WORK_DIR_PREFIX):
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
        prefix, os.getpid(), rand)))
    cmd = ['rsync', '-a', '--whole-file', '--no-times', '--omit-dir-times']
    for ex in VAST_COPY_EXCLUDES:
        cmd.extend(['--exclude', ex])
    cmd.extend([vast_ref + '/', work])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=VAST_COPY_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        # A timeout/OSError may leave a half-copied tree behind; the caller
        # never learns its name, so remove it here rather than leaking a
        # partial VaST copy under uploads/ (self-amplifying when the failure
        # cause is a full disk).
        shutil.rmtree(work, ignore_errors=True)
        return None
    if result.returncode != 0 or not os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)
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
            # Floor the error at MIN_PLOT_MAG_ERROR so a near-zero (rounded-to-0.000) error does
            # not make lib/lightcurve_png drop the point (its raw reader's isnormal() rejects 0.0).
            # The 2-d.p. table above still shows 0.00 for these points, which is the honest value.
            if err_val < MIN_PLOT_MAG_ERROR:
                err_val = MIN_PLOT_MAG_ERROR
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
        ra_disp, dec_disp = _format_title_coords(ra, dec)
        title = 'Forced photometry at {} {}'.format(ra_disp, dec_disp)
    except ValueError:
        title = 'Forced photometry lightcurve'
    # PGPLOT truncates long device filenames (somewhere around 90
    # characters its PNG driver ends up opening a chopped path, prints
    # "plotting disabled" and writes nothing; older binaries even exit 0).
    # Never hand the binary the possibly-long destination path: render to
    # a SHORT name relative to the subprocess cwd (the VaST working copy,
    # cleaned up after the request, so a stray temp file cannot linger),
    # then move the file to its destination here. shutil.move survives a
    # cross-filesystem work_dir/out_dir split. Newer lightcurve_png
    # handles long paths itself, but going through the short name keeps
    # this safe with older deployed binaries too.
    tmp_name = 'lightcurve_png_tmp_{}.png'.format(os.getpid())
    tmp_abs = os.path.join(work_dir, tmp_name)
    cmd = [binary, lc_abs, '-o', tmp_name, '--title', title]
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
    if not os.path.isfile(tmp_abs) or os.path.getsize(tmp_abs) == 0:
        sys.stderr.write(
            'lightcurve_png: exit 0 but {} was not created\nstderr:\n{}\n'
            .format(tmp_abs, (result.stderr or '')[-1000:]))
        return None
    try:
        shutil.move(tmp_abs, out_png)
    except OSError as exc:
        sys.stderr.write(
            'lightcurve_png: cannot move {} to {}: {}\n'.format(
                tmp_abs, out_png, exc))
        try:
            os.unlink(tmp_abs)
        except OSError:
            pass
        return None
    if not os.path.isfile(out_png):
        sys.stderr.write(
            'lightcurve_png: {} was not created\n'.format(out_png))
        return None
    return os.path.basename(out_png)


# ---------- matplotlib lightcurve rendering ----------

MATPLOTLIB_FIGSIZE = (10.0, 5.0)
MATPLOTLIB_PNG_DPI = 150
MATPLOTLIB_LABEL_FONTSIZE = 15
MATPLOTLIB_TICK_FONTSIZE = 12
MATPLOTLIB_TITLE_FONTSIZE = 14
# Bright red detections with Paul Tol inspired desaturated blue for the
# upper limits: the muted blue makes the limit symbols recede so the red
# detections stand out. The pair keeps strong separation under all
# color-vision-deficiency types and >=3:1 contrast on white.
# lib/lightcurve_png (the no-matplotlib fallback) uses the same two
# colors -- keep them in sync.
MATPLOTLIB_DETECTION_COLOR = '#ff0000'
MATPLOTLIB_UPPER_LIMIT_COLOR = '#5588bb'


def _read_numeric_columns(path, n_columns):
    """Parse a whitespace-separated numeric table (lightcurve.dat /
    upperlimits.dat format): skip blank lines and # comments, keep rows
    whose first n_columns tokens all parse as floats. Returns a list of
    n_columns-tuples; [] on any I/O trouble."""
    rows = []
    if path is None:
        return rows
    try:
        with open(path) as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                tokens = stripped.split()
                if len(tokens) < n_columns:
                    continue
                try:
                    rows.append(tuple(float(tokens[i])
                                      for i in range(n_columns)))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def _render_lightcurve_matplotlib(out_dir, ra, dec, lc_path, ul_path):
    """Render lightcurve.png AND lightcurve.eps in out_dir with matplotlib.

    Style: no background grid, large axis labels, magnitude axis inverted
    (bright up), detections as red circles with error bars, upper limits
    as blue down-pointing triangles (clearly distinct from detections,
    and matching lib/lightcurve_png's red/blue convention), a legend when
    both kinds are present, JD axis relative to a round offset so the
    tick labels stay short, calendar date (UTC) ticks along the top axis.

    Returns (png_basename, eps_basename); eps_basename is None when only
    the EPS write failed. Returns (None, None) when matplotlib is not
    importable or anything else goes wrong -- the caller then falls back
    to the lib/lightcurve_png binary. Never raises."""
    # matplotlib wants a writable config/font-cache directory; the CGI/
    # worker environment often has no usable $HOME, which would cost a
    # slow tempdir font-cache rebuild on every import. Both consumers of
    # this module run with cwd = the unmw script dir, so park the cache
    # next to the other served state under uploads/.
    if not os.environ.get('MPLCONFIGDIR'):
        cache_dir = os.path.abspath(
            os.path.join('uploads', '.matplotlib_cache'))
        try:
            os.makedirs(cache_dir, mode=0o755, exist_ok=True)
            os.environ['MPLCONFIGDIR'] = cache_dir
        except OSError:
            pass  # matplotlib falls back to a temp dir (slower, works)
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        sys.stderr.write('matplotlib not importable; falling back to '
                         'lib/lightcurve_png for the lightcurve plot\n')
        return None, None
    detections = _read_numeric_columns(lc_path, 3)
    limits = _read_numeric_columns(ul_path, 2)
    if not detections and not limits:
        return None, None
    try:
        all_jd = [row[0] for row in detections] + [row[0] for row in limits]
        # Round-hundred JD offset keeps the tick labels short without
        # matplotlib's own confusing scientific-notation offset text.
        jd_offset = math.floor(min(all_jd) / 100.0) * 100.0
        # Same title (and CodeQL taint barrier) as _render_lightcurve_png.
        try:
            ra_disp, dec_disp = _format_title_coords(ra, dec)
            title = 'Forced photometry at {} {}'.format(ra_disp, dec_disp)
        except ValueError:
            title = 'Forced photometry lightcurve'
        fig = plt.figure(figsize=MATPLOTLIB_FIGSIZE)
        ax = fig.add_subplot(111)
        if detections:
            ax.errorbar([row[0] - jd_offset for row in detections],
                        [row[1] for row in detections],
                        yerr=[row[2] for row in detections],
                        fmt='o', color=MATPLOTLIB_DETECTION_COLOR,
                        markersize=4.5,
                        elinewidth=1.0, capsize=0, zorder=3,
                        label='detection')
        if limits:
            ax.plot([row[0] - jd_offset for row in limits],
                    [row[1] for row in limits],
                    marker='v', linestyle='none',
                    color=MATPLOTLIB_UPPER_LIMIT_COLOR, markersize=8,
                    zorder=2, label='upper limit')
        ax.invert_yaxis()  # brighter is up
        ax.grid(False)
        ax.set_xlabel('JD - {:.0f}'.format(jd_offset),
                      fontsize=MATPLOTLIB_LABEL_FONTSIZE)
        ax.set_ylabel('Magnitude', fontsize=MATPLOTLIB_LABEL_FONTSIZE)
        ax.set_title(title, fontsize=MATPLOTLIB_TITLE_FONTSIZE)
        ax.tick_params(labelsize=MATPLOTLIB_TICK_FONTSIZE)
        # Calendar date (UTC) ticks along the top axis, JD stays on the
        # bottom. JD 2440587.5 = 1970-01-01T00:00 UTC; the date2num() term
        # keeps the mapping correct for any matplotlib date-epoch setting.
        # Best-effort: secondary_xaxis needs matplotlib >= 3.1, so on any
        # failure the plot simply keeps only the JD axis.
        try:
            import datetime
            import matplotlib.dates as mdates
            epoch_offset = mdates.date2num(datetime.datetime(1970, 1, 1))

            def _jd_to_datenum(x):
                return x + jd_offset - 2440587.5 + epoch_offset

            def _datenum_to_jd(x):
                return x - epoch_offset + 2440587.5 - jd_offset

            secax = ax.secondary_xaxis(
                'top', functions=(_jd_to_datenum, _datenum_to_jd))
            date_locator = mdates.AutoDateLocator()
            secax.xaxis.set_major_locator(date_locator)
            secax.xaxis.set_major_formatter(
                mdates.ConciseDateFormatter(date_locator))
            secax.tick_params(labelsize=MATPLOTLIB_TICK_FONTSIZE)
        except Exception as exc:
            sys.stderr.write(
                'top calendar-date axis skipped: {}: {}\n'.format(
                    type(exc).__name__, exc))
        if detections and limits:
            ax.legend(fontsize=11, frameon=False)
        fig.tight_layout()
        out_abs = os.path.abspath(out_dir)
        png_abs = os.path.join(out_abs, 'lightcurve.png')
        fig.savefig(png_abs, dpi=MATPLOTLIB_PNG_DPI)
        eps_basename = None
        eps_abs = os.path.join(out_abs, 'lightcurve.eps')
        try:
            fig.savefig(eps_abs, format='eps')
            if os.path.isfile(eps_abs) and os.path.getsize(eps_abs) > 0:
                eps_basename = 'lightcurve.eps'
        except Exception as exc:
            sys.stderr.write(
                'matplotlib EPS lightcurve write failed: {}\n'.format(exc))
        plt.close(fig)
        if not os.path.isfile(png_abs) or os.path.getsize(png_abs) == 0:
            return None, None
        return 'lightcurve.png', eps_basename
    except Exception as exc:
        sys.stderr.write(
            'matplotlib lightcurve rendering failed: {}: {}\n'.format(
                type(exc).__name__, exc))
        try:
            plt.close('all')
        except Exception:
            pass
        return None, None


def render_lightcurve_plots(work_dir, out_dir, ra, dec, lc_path, ul_path):
    """Render the lightcurve plot into out_dir: matplotlib (PNG + EPS)
    when available, else the lib/lightcurve_png binary from the VaST
    working copy (PNG only). Returns (png_basename, eps_basename), either
    of which may be None; when eps_basename is None the caller must not
    link an EPS file on the results page."""
    png_basename, eps_basename = _render_lightcurve_matplotlib(
        out_dir, ra, dec, lc_path, ul_path)
    if png_basename is not None:
        return png_basename, eps_basename
    return (_render_lightcurve_png(work_dir, out_dir, ra, dec, lc_path,
                                   ul_path), None)


def wide_field_photometry_caveat_html():
    """A short note on why wide-field photometry is not very precise, shared by
    the monitoring, archival-forced-photometry and coordinate-forced-photometry
    result pages (all of which include _PAGE_CSS, so the 'notice' class is
    styled)."""
    return (
        "<div class='notice'>\n"
        "<b>Why these photometric measurements are not very precise</b>\n"
        "<p>Photometric measurements obtained with a wide-field camera are not"
        " very precise, for a number of reasons:</p>\n"
        "<ul>\n"
        "<li><b>Bad plate solution:</b> errors in the astrometric calibration"
        " result in misplacement of the photometric aperture. This may create"
        " outlier measurements.</li>\n"
        "<li><b>Undersampled PSF:</b> star images occupy only a few pixels, so"
        " any uncorrected pixel-to-pixel and intra-pixel sensitivity"
        " variations affect the measurements more than they would for"
        " well-sampled telescopic images in which each star is spread across"
        " many pixels.</li>\n"
        "<li><b>Differential atmospheric extinction across the image:</b> an"
        " airmass-dependent magnitude zero-point correction is applied, but it"
        " is not perfect.</li>\n"
        "<li><b>Color-dependent (second-order) extinction:</b> a star's"
        " brightness measured relative to the comparison stars in the field is"
        " elevation-dependent if the star's color differs from the average"
        " color of the comparison stars.</li>\n"
        "<li><b>Blending</b> with nearby stars in crowded fields affects the"
        " measurements differently depending on the aperture size, which is"
        " adjusted for each image individually based on the seeing and focus"
        " quality.</li>\n"
        "<li><b>Position-dependent PSF:</b> the same star may fall closer to"
        " the image center in one field and farther from it in another, so a"
        " different fraction of its PSF is covered by the aperture even when"
        " the same aperture is used to measure both images. Field-to-field and"
        " camera-to-camera calibration is therefore an issue, and its"
        " magnitude differs from source to source.</li>\n"
        "<li><b>Chromatic aberration:</b> many wide-field lenses suffer from"
        " it, leaving extremely red stars out of focus while the focus is"
        " optimal for average-colored stars.</li>\n"
        "<li><b>Clouds,</b> if present, dramatically degrade the quality of the"
        " photometry across the entire image.</li>\n"
        "</ul>\n"
        "<p>In practice, expect a photometric uncertainty of ~0.1 mag, or"
        " somewhat better in favorable conditions.</p>\n"
        "<p><b>The intended use of these data is a quick check of the"
        " current state of a source (flaring or quiescent) - not precise"
        " photometry.</b></p>\n"
        "</div>\n")


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

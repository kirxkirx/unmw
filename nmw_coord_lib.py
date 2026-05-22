#!/usr/bin/env python3
"""
Shared helpers for the NMW coordinate web pages.

Imported by coord_search.py and coord_forced_photometry.py so a fix to any
of these helpers updates both pages at once. This module must stay
import-safe (no CGI work at import time): the importing CGI sets
nmw_coord_lib.DEFAULT_FORM_PATH to its own input-form path.
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import fcntl
import html
import os
import random
import re
import string
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor


# Code-level operational constants (not deployment-specific).
MAX_CONCURRENT = 2
SCAN_TIMEOUT_SECONDS = 90
FITS2PNG_TIMEOUT_SECONDS = 30
FOV_TIMEOUT_SECONDS = 30
LOCK_DIR = '/tmp'
TEMP_PARENT = 'uploads'              # mirrors upload.py's upload_dir
TEMP_DIR_PREFIX = 'coord_search_'
DEFAULT_THUMBNAIL_PIXELS = 256       # fallback for in-page thumbnail size
HIRES_THUMBNAIL_MULTIPLIER = 4       # click-through PNG is this many times
                                     # bigger than the in-page thumbnail
MIN_THUMBNAIL_PIXELS = 32
MAX_THUMBNAIL_PIXELS = 4096
MAX_RESULTS_TO_PROCESS = 200         # safety cap on coord-search matches
LIST_ALL_MAX_FILES = 2000            # safety cap on the "show all" listing
LIST_ALL_TIMEOUT_SECONDS = 900       # wall-clock cap for the "show all" flow (15 min)
DEFAULT_FORM_PATH = '/unmw/coord_search.html'
DEFAULT_ZOOMIN_PIXELS = 200          # half-width of zoom-in thumbnail in source pix
DEFAULT_PARALLEL_WORKERS = 16        # threads rendering PNGs concurrently
MIN_PARALLEL_WORKERS = 1
MAX_PARALLEL_WORKERS = 32

# Whitelist of characters allowed in the raw coordinate string.
# Defends every later subprocess that takes the parsed values.
COORDS_REGEX = re.compile(r'^[0-9 :+\-.\t]{3,80}$')


# ---------- output helpers ----------

def html_escape(s):
    return html.escape(str(s), quote=True)


def field_name_from_fits(path):
    """Extract the NMW field name from a reference FITS basename.

    Mirrors util/transients/transient_factory_test31.sh:1423 -- strip an
    optional 'wcs_fd_' / 'wcs_' / 'fd_' calibration-status prefix, then take
    everything before the first underscore.
    """
    base = os.path.basename(path)
    for prefix in ('wcs_fd_', 'wcs_', 'fd_'):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return base.split('_', 1)[0]


_PAGE_CSS = """<style type="text/css">
body { color: #000; background: #fff;
 font-family: arial, helvetica, sans-serif;
 font-size: 12pt; line-height: 16pt;
 margin: 3mm 10mm 3mm 10mm; }
.code { font-family: courier; background: #ccc; color: #000; }
table.main { border-spacing: 5pt; border-collapse: collapse; }
table.main th, table.main td { padding: 4pt 10pt; border: 1px solid #ccc;
 text-align: left; vertical-align: top; }
.notice { background: #ffd; padding: 6pt; margin-bottom: 10pt; }
a:link, a:visited, a:active { color: #55f; text-decoration: none; }
a:hover { text-decoration: underline; }
</style>"""


def back_link_url():
    """Pick a sensible URL for the 'Search again' link."""
    referer = os.environ.get('HTTP_REFERER', '').strip()
    if referer:
        return referer
    return DEFAULT_FORM_PATH


def form_page_url():
    """Absolute URL of the input form page, derived from the current request.

    Mirrors the request's scheme and host:port so the redirect lands on the
    same deployment, e.g. a request to
      http://scan.sai.msu.ru:8889/cgi-bin/unmw/coord_search.py
    redirects to
      http://scan.sai.msu.ru:8889/unmw/coord_search.html
    and likewise for the :8888 and the https://tau.kirx.net deployments.
    Falls back to the bare path (DEFAULT_FORM_PATH) when the request
    environment does not identify the host.
    """
    # Scheme: HTTPS is "on"/"1" behind TLS; some servers set REQUEST_SCHEME.
    scheme = os.environ.get('REQUEST_SCHEME', '').strip().lower()
    if not scheme:
        https = os.environ.get('HTTPS', '').strip().lower()
        scheme = 'https' if https in ('on', '1') else 'http'

    # HTTP_HOST already carries the port the client connected to (when it is
    # not the scheme default), so prefer it over SERVER_NAME/SERVER_PORT.
    host = os.environ.get('HTTP_HOST', '').strip()
    if not host:
        name = os.environ.get('SERVER_NAME', '').strip()
        port = os.environ.get('SERVER_PORT', '').strip()
        if name:
            if port and port not in ('80', '443'):
                host = '{}:{}'.format(name, port)
            else:
                host = name
    if not host:
        return DEFAULT_FORM_PATH
    return '{}://{}{}'.format(scheme, host, DEFAULT_FORM_PATH)


def emit_redirect(url):
    """Send a 302 redirect to url, with an HTML fallback body."""
    print("Status: 302 Found")
    print("Location: {}".format(url))
    print("Content-Type: text/html\n")
    safe = html_escape(url)
    print("<html><head><title>Redirecting</title>"
          "<meta http-equiv='refresh' content='0; url={u}'></head>"
          "<body>Redirecting to <a href='{u}'>{u}</a></body></html>".format(
              u=safe))


def emit_message_page(title, body_html, status_line=None):
    if status_line:
        print(status_line)
    print("Content-Type: text/html\n")
    print("<html><head><title>{}</title>".format(html_escape(title)))
    print(_PAGE_CSS)
    print("</head><body>")
    print("<h2>{}</h2>".format(html_escape(title)))
    print(body_html)
    print("<br><br><a href='{}'>Search again</a>".format(html_escape(back_link_url())))
    print("</body></html>")


# ---------- coordinate parsing ----------

def parse_coordinates(raw):
    """Parse the user's coordinate string.

    Returns (ra, dec) ready to pass as separate arguments to sky2xy.
    Raises ValueError on any problem.
    """
    if raw is None:
        raise ValueError("no coordinate string supplied")
    s = raw.strip()
    if not s:
        raise ValueError("empty coordinate string")
    if not COORDS_REGEX.match(s):
        raise ValueError("invalid characters in coordinate string")

    if ':' in s:
        # Sexagesimal with colons: two whitespace-separated tokens expected.
        tokens = s.split()
        if len(tokens) != 2:
            raise ValueError(
                "expected 'RA DEC' as two whitespace-separated tokens "
                "when using colons")
        return tokens[0], tokens[1]

    tokens = s.split()
    if len(tokens) == 6:
        # Sexagesimal with spaces: HH MM SS.SS [+|-]DD MM SS.S
        try:
            for t in tokens:
                float(t)
        except ValueError:
            raise ValueError(
                "all six space-separated tokens must be numeric")
        ra = ':'.join(tokens[0:3])
        dec = ':'.join(tokens[3:6])
        return ra, dec

    if len(tokens) == 2:
        try:
            float(tokens[0])
            float(tokens[1])
        except ValueError:
            raise ValueError(
                "could not parse coordinates as decimal degrees")
        return tokens[0], tokens[1]

    raise ValueError(
        "could not detect coordinate format (expected 2 colon-tokens, "
        "6 space-tokens, or 2 decimal-degree tokens)")


# ---------- config loading ----------

def read_config_vars(*var_names):
    """Source local_config.sh in bash and return a dict of variable values.

    Values that contain shell expansion (e.g. URL_OF_DATA_PROCESSING_ROOT
    referencing $UNMW_FREE_PORT) require real bash sourcing rather than
    a Python-side parser.

    Missing variables come back as empty strings.
    """
    sep = '\x1f'  # ASCII Unit Separator: cannot legitimately appear in values
    parts = ['source ./local_config.sh']
    for name in var_names:
        # ${name-} expands to "" when name is unset, with no warning.
        parts.append('printf "%s{sep}" "${{{name}-}}"'.format(name=name, sep=sep))
    cmd = ' && '.join(parts)
    try:
        result = subprocess.run(
            ['bash', '-c', cmd],
            capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, OSError):
        return {n: '' for n in var_names}
    if result.returncode != 0:
        return {n: '' for n in var_names}
    chunks = result.stdout.split(sep)
    chunks = chunks[:len(var_names)]
    while len(chunks) < len(var_names):
        chunks.append('')
    return dict(zip(var_names, chunks))


# ---------- concurrency limit ----------

def acquire_concurrency_slot(prefix='coord_search', max_concurrent=MAX_CONCURRENT):
    """Try to acquire one of max_concurrent exclusive flock slots.

    Lock files are named '<prefix>_slot_<i>.lock' so different pages can use
    independent slot pools. Returns the open file object on success (caller
    must keep it alive until the end of the request), or None when no slot
    is free.
    """
    for i in range(1, max_concurrent + 1):
        path = os.path.join(LOCK_DIR, '{}_slot_{}.lock'.format(prefix, i))
        try:
            fd = open(path, 'w')
        except OSError:
            continue
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            fd.close()
            continue
    return None


# ---------- sky2xy scan ----------

# Single bash subprocess does the whole scan. ref_dir / ra / dec come in
# via the environment so they cannot be reinterpreted as shell tokens.
_BASH_SCAN_LOOP = r'''
for i in "$REF_DIR"/*; do
  [ -f "$i" ] || continue
  case "$i" in
    *.fits|*.fit|*.fts|*.FITS|*.FIT|*.FTS) ;;
    *) continue ;;
  esac
  printf '%s\t' "$i"
  lib/bin/sky2xy "$i" "$RA" "$DEC" 2>/dev/null
done | grep -v -e 'offscale' -e 'off image' | grep ' -> '
'''


def run_sky2xy_scan(ref_dir, ra, dec, vast_dir, max_results=MAX_RESULTS_TO_PROCESS):
    """Iterate FITS files in ref_dir, call sky2xy from vast_dir.

    Returns (matches, truncated_by_timeout) where matches is a list of
    (path, x, y) tuples.
    """
    env = os.environ.copy()
    env['REF_DIR'] = ref_dir
    env['RA'] = ra
    env['DEC'] = dec
    truncated = False
    stdout = ''
    try:
        result = subprocess.run(
            ['bash', '-c', _BASH_SCAN_LOOP],
            cwd=vast_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT_SECONDS,
        )
        stdout = result.stdout or ''
    except subprocess.TimeoutExpired as exc:
        truncated = True
        partial = exc.stdout or ''
        if isinstance(partial, (bytes, bytearray)):
            stdout = partial.decode('utf-8', errors='replace')
        else:
            stdout = partial

    matches = []
    for line in stdout.splitlines():
        if '\t' not in line:
            continue
        path, sky2xy_part = line.split('\t', 1)
        tokens = sky2xy_part.split()
        if len(tokens) < 2:
            continue
        try:
            x = float(tokens[-2])
            y = float(tokens[-1])
        except ValueError:
            continue
        matches.append((path, x, y))
        if len(matches) >= max_results:
            break
    return matches, truncated


# ---------- per-image helpers ----------

def get_image_metadata(fits_path, vast_dir):
    """Return image metadata dict from util/fov_of_wcs_calibrated_image.sh.

    Going through the script (rather than reading NAXIS directly) makes
    this work for compressed FITS files as well.

    Keys: nx, ny (int, pixels), arcmin_str (e.g. "941.5'x626.9'"),
    deg_str (e.g. "15.7degx10.4deg"), scale_x, scale_y (float, arcsec/pix),
    center_radec (e.g. "21:00:00.47 +30:00:01.6", or None).
    Returns None on failure.
    """
    try:
        result = subprocess.run(
            ['util/fov_of_wcs_calibrated_image.sh', fits_path],
            cwd=vast_dir,
            capture_output=True, text=True,
            timeout=FOV_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    out = result.stdout
    pix_m = re.search(r'(\d+)\s*x\s*(\d+)\s*pix', out)
    if not pix_m:
        return None

    info = {
        'nx': int(pix_m.group(1)),
        'ny': int(pix_m.group(2)),
        'arcmin_str': '',
        'deg_str': '',
        'scale_x': None,
        'scale_y': None,
        'center_radec': None,
    }

    # "Image center: 21:00:00.467 +30:00:01.62 J2000 4789.000 3195.000"
    # Reformat to the requested HH:MM:SS.SS +DD:MM:SS.s precision.
    center_m = re.search(
        r'Image center:\s*'
        r'(\d{1,2}:\d{2}:\d{2}\.?\d*)\s+'
        r'([+\-]\d{1,2}:\d{2}:\d{2}\.?\d*)',
        out)
    if center_m:
        ra = _reformat_sexagesimal(center_m.group(1), 2)
        dec = _reformat_sexagesimal(center_m.group(2), 1)
        info['center_radec'] = '{} {}'.format(ra, dec)

    arcmin_m = re.search(r"(\d+\.?\d*)'\s*x\s*(\d+\.?\d*)'", out)
    if arcmin_m:
        info['arcmin_str'] = "{}'x{}'".format(arcmin_m.group(1), arcmin_m.group(2))

    deg_m = re.search(r'(\d+\.?\d*)\s*\(deg\)\s*x\s*(\d+\.?\d*)\s*\(deg\)', out)
    if deg_m:
        # Store as ready-to-render HTML using the &deg; entity (CLAUDE.md
        # forbids non-ASCII source). Numeric captures are \d+\.?\d* so it is
        # safe to splice them into HTML without further escaping.
        info['deg_str'] = '{}&deg;x{}&deg;'.format(
            deg_m.group(1), deg_m.group(2))

    scale_m = re.search(
        r'(\d+\.?\d*)"/pix along the X axis and (\d+\.?\d*)"/pix along the Y axis',
        out)
    if scale_m:
        try:
            info['scale_x'] = float(scale_m.group(1))
            info['scale_y'] = float(scale_m.group(2))
        except ValueError:
            pass

    return info


def _reformat_sexagesimal(token, sec_decimals):
    """Round a 'HH:MM:SS.sss' / '[+-]DD:MM:SS.ss' token to sec_decimals
    decimal places on the seconds field, handling carry into minutes/hours.

    Returns the reformatted string, or the token unchanged if it does not
    look like sexagesimal.
    """
    sign = ''
    body = token
    if body[:1] in ('+', '-'):
        sign = body[0]
        body = body[1:]
    parts = body.split(':')
    if len(parts) != 3:
        return token
    try:
        a = int(parts[0])
        m = int(parts[1])
        s = round(float(parts[2]), sec_decimals)
    except ValueError:
        return token
    if s >= 60.0:
        s -= 60.0
        m += 1
    if m >= 60:
        m -= 60
        a += 1
    width = 3 + sec_decimals if sec_decimals > 0 else 2
    return '{sign}{a:02d}:{m:02d}:{s:0{w}.{d}f}'.format(
        sign=sign, a=a, m=m, s=s, w=width, d=sec_decimals)


def _run_pgfv_tool(argv, out_dir, png_w, png_h, fits_path, suffix):
    """Run a pgfv-family tool that writes <basename>.png to cwd, then rename.

    Returns the suffixed PNG name (relative to out_dir) on success, else None.
    Each request has its own out_dir, so no cross-request name collisions.
    Within one request, sequential calls would collide on '<base>.png' until
    we rename, so we rename immediately after each call.
    """
    env = os.environ.copy()
    env['PGPLOT_PNG_WIDTH'] = str(png_w)
    env['PGPLOT_PNG_HEIGHT'] = str(png_h)
    try:
        subprocess.run(
            argv,
            cwd=out_dir,
            env=env,
            capture_output=True,
            timeout=FITS2PNG_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    base = os.path.splitext(os.path.basename(fits_path))[0]
    src = os.path.join(out_dir, '{}.png'.format(base))
    if not (os.path.isfile(src) and os.path.getsize(src) > 0):
        return None
    dst_name = '{}_{}.png'.format(base, suffix)
    dst = os.path.join(out_dir, dst_name)
    try:
        os.replace(src, dst)
    except OSError:
        return None
    return dst_name


def zoomout_png_dims(nx, ny, thumb_pixels):
    """PNG dimensions for the zoom-out thumbnail.

    Height is fixed to thumb_pixels so zoom-in and zoom-out line up
    vertically when shown side-by-side in the results table; the width
    follows the source image aspect ratio.
    """
    png_h = thumb_pixels
    png_w = max(1, int(round(thumb_pixels * nx / float(ny))))
    return png_w, png_h


def make_zoomout_thumbnail(fits_path, x, y, nx, ny, out_dir, vast_dir,
                           thumb_pixels, suffix='zoomout'):
    """Full-frame view of the FITS image. If x and y are not None, draws a
    marker at pixel (x, y) (requires the pgfv.c edits). Pass x=y=None to
    render a plain full-frame preview with no marker.

    PNG dimensions follow source aspect ratio so the longer axis is
    thumb_pixels, matching the zoom-in's axes.
    """
    fits2png = os.path.join(vast_dir, 'util', 'fits2png')
    png_w, png_h = zoomout_png_dims(nx, ny, thumb_pixels)
    args = [fits2png, fits_path]
    if x is not None and y is not None:
        args.extend(['{:.3f}'.format(x), '{:.3f}'.format(y)])
    return _run_pgfv_tool(args, out_dir, png_w, png_h, fits_path, suffix)


def make_zoomin_thumbnail(fits_path, x, y, out_dir, vast_dir, thumb_pixels,
                          zoomin_pixels, suffix='zoomin',
                          aperture_circle_diameter=None):
    """Square zoom-in centred on (x, y), 2N x 2N source pixels.

    If aperture_circle_diameter (pixels) is given, draw a red circle of that
    diameter at the target -- requires the pgfv.c --targetaperturecircle option.
    Left as None (the default) the chart is drawn exactly as before.
    """
    tool = os.path.join(vast_dir, 'util', 'make_finding_chart')
    args = [tool, '--width', str(zoomin_pixels), '--nolabels']
    if aperture_circle_diameter is not None and aperture_circle_diameter > 0:
        args.extend(['--targetaperturecircle',
                     '{:.3f}'.format(aperture_circle_diameter)])
    args.extend(['--', fits_path, '{:.3f}'.format(x), '{:.3f}'.format(y)])
    return _run_pgfv_tool(args, out_dir, thumb_pixels, thumb_pixels,
                          fits_path, suffix)


def list_fits_files(ref_dir):
    """Return a sorted list of absolute paths to FITS files in ref_dir.

    Only top-level files (no recursion) with a recognised FITS extension are
    returned, matching the convention of run_sky2xy_scan.
    """
    paths = []
    try:
        entries = os.listdir(ref_dir)
    except OSError:
        return paths
    for name in entries:
        ext = os.path.splitext(name)[1].lower()
        if ext not in ('.fits', '.fit', '.fts'):
            continue
        full = os.path.join(ref_dir, name)
        if os.path.isfile(full):
            paths.append(full)
    paths.sort()
    return paths

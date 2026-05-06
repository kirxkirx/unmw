#!/usr/bin/env python3
"""
CGI for finding which reference fields cover a given sky position.

Reads the user's coordinate string from a POST form, parses one of three
formats (colon-sexagesimal, space-sexagesimal, decimal degrees), iterates
the FITS files in $REFERENCE_IMAGES, calls lib/bin/sky2xy on each one,
and produces an HTML table of matching fields with pixel coordinates,
distance to the nearest image edge, and a small thumbnail.

Configuration (read from local_config.sh next to this script):
  REFERENCE_IMAGES                directory containing reference FITS images
  VAST_REFERENCE_COPY             path to the VaST source/install tree
  URL_OF_DATA_PROCESSING_ROOT     URL prefix for the served uploads/ directory
  COORD_SEARCH_THUMBNAIL_PIXELS   thumbnail width/height in pixels
                                  (default 128, matching the smallest preview
                                  in util/transients/transient_factory_test31.sh)

Per-request output directory uploads/coord_search_<pid><rand>/ is left in
place; existing housekeeping that prunes uploads/web_upload_* should also
prune uploads/coord_search_*.
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

import fcntl
import html
import os
import random
import re
import string
import subprocess
import sys
import time


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
MAX_RESULTS_TO_PROCESS = 200         # safety cap on matches per request
LIST_ALL_TIMEOUT_SECONDS = 300       # wall-clock cap for the "show all" flow
DEFAULT_FORM_PATH = '/unmw/coord_search.html'
DEFAULT_ZOOMIN_PIXELS = 200          # half-width of zoom-in thumbnail in source pix

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

def acquire_concurrency_slot():
    """Try to acquire one of MAX_CONCURRENT exclusive flock slots.

    Returns the open file object on success (caller must keep it alive
    until the end of the request), or None when no slot is free.
    """
    for i in range(1, MAX_CONCURRENT + 1):
        path = os.path.join(LOCK_DIR, 'coord_search_slot_{}.lock'.format(i))
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


def run_sky2xy_scan(ref_dir, ra, dec, vast_dir):
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
        if len(matches) >= MAX_RESULTS_TO_PROCESS:
            break
    return matches, truncated


# ---------- per-image helpers ----------

def get_image_metadata(fits_path, vast_dir):
    """Return image metadata dict from util/fov_of_wcs_calibrated_image.sh.

    Going through the script (rather than reading NAXIS directly) makes
    this work for compressed FITS files as well.

    Keys: nx, ny (int, pixels), arcmin_str (e.g. "941.5'x626.9'"),
    deg_str (e.g. "15.7degx10.4deg"), scale_x, scale_y (float, arcsec/pix).
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
    }

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
                          zoomin_pixels, suffix='zoomin'):
    """Square zoom-in centred on (x, y), 2N x 2N source pixels."""
    tool = os.path.join(vast_dir, 'util', 'make_finding_chart')
    return _run_pgfv_tool(
        [tool, '--width', str(zoomin_pixels), '--nolabels', '--',
         fits_path, '{:.3f}'.format(x), '{:.3f}'.format(y)],
        out_dir, thumb_pixels, thumb_pixels, fits_path, suffix)


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


def render_image_size(arcmin_str, deg_str, nx, ny):
    """Three-line HTML for the Image size cell."""
    lines = []
    if arcmin_str:
        lines.append(html_escape(arcmin_str))
    if deg_str:
        # Already pre-rendered HTML with the &deg; entity.
        lines.append(deg_str)
    lines.append('{}x{} pix'.format(nx, ny))
    return '<br>'.join(lines)


def render_mean_scale(scale_x, scale_y):
    """Returns (formatted_html, mean_scale_value_or_None)."""
    if scale_x is None and scale_y is None:
        return '-', None
    if scale_y is None:
        m = scale_x
    elif scale_x is None:
        m = scale_y
    else:
        m = (scale_x + scale_y) / 2.0
    return '{:.2f}'.format(m), m


def render_thumbnail_link(thumb_name, hires_name, label, base, url_prefix, sub):
    """Anchor+image HTML cell for a thumbnail, opening the hi-res on click."""
    if not thumb_name:
        return "<i>unavailable</i>"
    thumb_url = html_escape('{}/{}/{}'.format(url_prefix, sub, thumb_name))
    target = hires_name if hires_name else thumb_name
    target_url = html_escape('{}/{}/{}'.format(url_prefix, sub, target))
    return ("<a href='{tu}' target='_blank'>"
            "<img src='{su}' alt='{l} of {b}' border='0'>"
            "</a>".format(tu=target_url, su=thumb_url, l=label,
                          b=html_escape(base)))


# ---------- main ----------

def main():
    cgitb.enable()

    # Make our cwd the directory containing this script, even if it was
    # reached via symlink (e.g. cgi-bin/unmw/coord_search.py -> ../../coord_search.py).
    # Relative paths like ./local_config.sh and uploads/ depend on this.
    script_dir = os.path.dirname(os.path.realpath(__file__))
    try:
        os.chdir(script_dir)
    except OSError as err:
        emit_message_page(
            "Internal error",
            "<p>Cannot chdir to {}: {}</p>".format(
                html_escape(script_dir), html_escape(err)),
            status_line="Status: 500 Internal Server Error",
        )
        return

    form = cgi.FieldStorage()

    # The landing page sets a hidden 'action' field via JS click handlers
    # on each submit button: 'search' for coord-search, 'list_all' for the
    # catalogue. The default value is 'search' (used when JS is disabled
    # or when the user submits via Enter in the coords input).
    list_all_mode = form.getfirst('action') == 'list_all'

    if list_all_mode:
        ra = dec = None
        raw_coords = ''
    else:
        raw_coords = (form.getfirst('coords', '') or '').strip()
        try:
            ra, dec = parse_coordinates(raw_coords)
        except ValueError as err:
            emit_message_page(
                "Invalid coordinates",
                "<p>Could not parse coordinates: <b>{}</b></p>"
                "<p>You typed: <span class='code'>{}</span></p>"
                "<p>Please use one of the accepted formats and try again.</p>".format(
                    html_escape(err), html_escape(raw_coords)),
            )
            return

    slot = acquire_concurrency_slot()
    if slot is None:
        emit_message_page(
            "Server busy",
            "<p>The maximum number of concurrent coordinate searches "
            "({} of {}) is currently running. Please try again in a few "
            "seconds.</p>".format(MAX_CONCURRENT, MAX_CONCURRENT),
            status_line="Status: 503 Service Unavailable",
        )
        return

    try:
        cfg = read_config_vars(
            'REFERENCE_IMAGES',
            'VAST_REFERENCE_COPY',
            'URL_OF_DATA_PROCESSING_ROOT',
            'COORD_SEARCH_THUMBNAIL_PIXELS',
            'COORD_SEARCH_ZOOMIN_PIXELS',
        )
        ref_dir = cfg['REFERENCE_IMAGES'].strip()
        vast_dir = cfg['VAST_REFERENCE_COPY'].strip()
        url_prefix = cfg['URL_OF_DATA_PROCESSING_ROOT'].strip().rstrip('/')
        thumb_raw = cfg['COORD_SEARCH_THUMBNAIL_PIXELS'].strip()
        zoomin_raw = cfg['COORD_SEARCH_ZOOMIN_PIXELS'].strip()

        try:
            thumb_pixels = int(thumb_raw) if thumb_raw else DEFAULT_THUMBNAIL_PIXELS
        except ValueError:
            thumb_pixels = DEFAULT_THUMBNAIL_PIXELS
        if thumb_pixels < MIN_THUMBNAIL_PIXELS or thumb_pixels > MAX_THUMBNAIL_PIXELS:
            thumb_pixels = DEFAULT_THUMBNAIL_PIXELS

        # Click-through PNGs are HIRES_THUMBNAIL_MULTIPLIER times bigger than
        # the in-page thumbnails, capped at MAX_THUMBNAIL_PIXELS.
        hires_pixels = min(MAX_THUMBNAIL_PIXELS,
                           thumb_pixels * HIRES_THUMBNAIL_MULTIPLIER)

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
                "<span class='code'>{}</span></p>"
                "<p>Set <span class='code'>REFERENCE_IMAGES</span> in "
                "<span class='code'>local_config.sh</span>.</p>".format(
                    html_escape(ref_dir)),
                status_line="Status: 500 Internal Server Error",
            )
            return
        if not vast_dir or not os.path.isdir(vast_dir):
            emit_message_page(
                "Configuration error",
                "<p>VaST install directory not found: "
                "<span class='code'>{}</span></p>"
                "<p>Set <span class='code'>VAST_REFERENCE_COPY</span> in "
                "<span class='code'>local_config.sh</span>.</p>".format(
                    html_escape(vast_dir)),
                status_line="Status: 500 Internal Server Error",
            )
            return
        if not url_prefix:
            emit_message_page(
                "Configuration error",
                "<p><span class='code'>URL_OF_DATA_PROCESSING_ROOT</span> "
                "is not set in <span class='code'>local_config.sh</span>.</p>",
                status_line="Status: 500 Internal Server Error",
            )
            return

        if not os.path.isdir(TEMP_PARENT):
            try:
                os.makedirs(TEMP_PARENT, mode=0o755)
            except OSError as err:
                emit_message_page(
                    "Configuration error",
                    "<p>Cannot create '{}': {}</p>".format(
                        html_escape(TEMP_PARENT), html_escape(err)),
                    status_line="Status: 500 Internal Server Error",
                )
                return

        # Per-request output directory; left in place for housekeeping to prune.
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
                status_line="Status: 500 Internal Server Error",
            )
            return
        out_dir_abs = os.path.abspath(out_dir)

        if list_all_mode:
            # ---- Catalogue mode: list every WCS-calibrated reference image.
            fits_paths = list_fits_files(ref_dir)
            if len(fits_paths) > MAX_RESULTS_TO_PROCESS:
                fits_paths = fits_paths[:MAX_RESULTS_TO_PROCESS]
                paths_truncated = True
            else:
                paths_truncated = False

            deadline = time.time() + LIST_ALL_TIMEOUT_SECONDS
            timed_out = False
            results = []
            for path in fits_paths:
                if time.time() > deadline:
                    timed_out = True
                    break
                meta = get_image_metadata(path, vast_dir)
                if meta is None:
                    continue
                nx, ny = meta['nx'], meta['ny']
                # Preview-only thumbnail (no marker, no hi-res), to keep the
                # all-images flow tractable on directories with many fields.
                png = make_zoomout_thumbnail(
                    path, None, None, nx, ny,
                    out_dir_abs, vast_dir, thumb_pixels, suffix='zoomout')
                results.append({
                    'path': path,
                    'nx': nx, 'ny': ny,
                    'arcmin_str': meta['arcmin_str'],
                    'deg_str': meta['deg_str'],
                    'scale_x': meta['scale_x'],
                    'scale_y': meta['scale_y'],
                    'png_zoomout': png,
                })

            results.sort(
                key=lambda r: (field_name_from_fits(r['path']), r['path']))

            print("Content-Type: text/html\n")
            print("<html><head><title>All reference images</title>")
            print(_PAGE_CSS)
            print("</head><body>")
            print("<h2>All reference images</h2>")
            print("<p>{} reference image(s) listed from "
                  "<span class='code'>{}</span></p>".format(
                      len(results), html_escape(ref_dir)))
            if paths_truncated:
                print("<div class='notice'>Reference image directory contains "
                      "more than {} files; only the first {} (alphabetical) "
                      "are listed.</div>".format(
                          MAX_RESULTS_TO_PROCESS, MAX_RESULTS_TO_PROCESS))
            if timed_out:
                print("<div class='notice'>Listing stopped after {} s; "
                      "results may be incomplete.</div>".format(
                          LIST_ALL_TIMEOUT_SECONDS))

            if not results:
                print("<p>No WCS-calibrated reference images found.</p>")
            else:
                print("<table class='main'>")
                print("<tr><th>Field</th><th>Reference image</th>"
                      "<th>Image size</th>"
                      "<th>Scale (&quot;/pix)</th>"
                      "<th>Zoom-out</th></tr>")
                for r in results:
                    base = os.path.basename(r['path'])
                    field = field_name_from_fits(r['path'])
                    size_html = render_image_size(
                        r['arcmin_str'], r['deg_str'], r['nx'], r['ny'])
                    scale_html, _ = render_mean_scale(
                        r['scale_x'], r['scale_y'])
                    # The all-images view does not produce a separate hi-res
                    # PNG; clicking the thumbnail opens the same preview.
                    zo_cell = render_thumbnail_link(
                        r['png_zoomout'], None, 'zoom-out',
                        base, url_prefix, sub)
                    print("<tr>"
                          "<td><b>{f}</b></td>"
                          "<td title='{full}'>{base}</td>"
                          "<td>{s}</td>"
                          "<td>{sc}</td>"
                          "<td>{zo}</td>"
                          "</tr>".format(
                              f=html_escape(field),
                              full=html_escape(r['path']),
                              base=html_escape(base),
                              s=size_html,
                              sc=scale_html,
                              zo=zo_cell))
                print("</table>")

            print("<br><br><a href='{}'>Search again</a>".format(
                html_escape(back_link_url())))
            print("</body></html>")
            return  # done with list-all flow

        # ---- Coord-search mode (default).
        matches, truncated = run_sky2xy_scan(ref_dir, ra, dec, vast_dir)

        # Annotate with image metadata and computed distances; drop ones we
        # cannot size (no useful row to display).
        results = []
        for path, x, y in matches:
            meta = get_image_metadata(path, vast_dir)
            if meta is None:
                continue
            nx, ny = meta['nx'], meta['ny']
            edge = int(round(min(x, y, nx - x, ny - y)))
            cx, cy = nx / 2.0, ny / 2.0
            from_center = int(round(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5))
            results.append({
                'path': path,
                'x': x, 'y': y,
                'nx': nx, 'ny': ny,
                'edge': edge,
                'from_center': from_center,
                'arcmin_str': meta['arcmin_str'],
                'deg_str': meta['deg_str'],
                'scale_x': meta['scale_x'],
                'scale_y': meta['scale_y'],
            })

        # Best-centred first: smallest distance to image centre.
        results.sort(key=lambda r: r['from_center'])

        for r in results:
            # Two PNGs per view: an in-page thumbnail and a higher-resolution
            # version that opens when the user clicks the thumbnail.
            r['png_zoomin'] = make_zoomin_thumbnail(
                r['path'], r['x'], r['y'], out_dir_abs, vast_dir,
                thumb_pixels, zoomin_pixels, suffix='zoomin')
            r['png_zoomin_hires'] = make_zoomin_thumbnail(
                r['path'], r['x'], r['y'], out_dir_abs, vast_dir,
                hires_pixels, zoomin_pixels, suffix='zoomin_hires')
            r['png_zoomout'] = make_zoomout_thumbnail(
                r['path'], r['x'], r['y'], r['nx'], r['ny'],
                out_dir_abs, vast_dir, thumb_pixels, suffix='zoomout')
            r['png_zoomout_hires'] = make_zoomout_thumbnail(
                r['path'], r['x'], r['y'], r['nx'], r['ny'],
                out_dir_abs, vast_dir, hires_pixels, suffix='zoomout_hires')

        # Build response page.
        print("Content-Type: text/html\n")
        print("<html><head><title>Coordinate search results</title>")
        print(_PAGE_CSS)
        print("</head><body>")
        print("<h2>Coordinate search results</h2>")
        print("<p>Searched for R.A. <b>{}</b>, Dec. <b>{}</b> (J2000) "
              "in <span class='code'>{}</span></p>".format(
                  html_escape(ra), html_escape(dec), html_escape(ref_dir)))

        if truncated:
            print("<div class='notice'>Scan stopped after {} s; "
                  "results may be incomplete.</div>".format(SCAN_TIMEOUT_SECONDS))

        if not results:
            print("<p>No reference images cover this sky position.</p>")
        else:
            print("<p>{} reference image(s) cover this position, sorted by "
                  "distance from image centre (best-centred first):</p>".format(
                      len(results)))
            print("<table class='main'>")
            print("<tr><th>Field</th><th>Reference image</th>"
                  "<th>X, Y (pix)</th>"
                  "<th>From center</th>"
                  "<th>Nearest edge</th>"
                  "<th>Image size</th>"
                  "<th>Scale (&quot;/pix)</th>"
                  "<th>Zoom-out</th><th>Zoom-in</th></tr>")
            for r in results:
                base = os.path.basename(r['path'])
                size_html = render_image_size(
                    r['arcmin_str'], r['deg_str'], r['nx'], r['ny'])
                scale_html, mean_scale = render_mean_scale(
                    r['scale_x'], r['scale_y'])

                # Convert pixel distances using the mean scale (arcsec/pix).
                # Three lines per cell, matching the Image size column:
                # arcminutes, degrees, pixels.
                def _distance_cell(pix):
                    if mean_scale is None:
                        return '{} pix'.format(pix)
                    arcmin = pix * mean_scale / 60.0
                    deg = arcmin / 60.0
                    return "{:.1f}'<br>{:.1f}&deg;<br>{} pix".format(
                        arcmin, deg, pix)

                edge_html = _distance_cell(r['edge'])
                center_html = _distance_cell(r['from_center'])

                field = field_name_from_fits(r['path'])
                zi_cell = render_thumbnail_link(
                    r['png_zoomin'], r['png_zoomin_hires'], 'zoom-in',
                    base, url_prefix, sub)
                zo_cell = render_thumbnail_link(
                    r['png_zoomout'], r['png_zoomout_hires'], 'zoom-out',
                    base, url_prefix, sub)
                print("<tr>"
                      "<td><b>{f}</b></td>"
                      "<td title='{full}'>{base}</td>"
                      "<td>{x:.1f}, {y:.1f}</td>"
                      "<td>{ch}</td>"
                      "<td>{eh}</td>"
                      "<td>{s}</td>"
                      "<td>{sc}</td>"
                      "<td>{zo}</td>"
                      "<td>{zi}</td>"
                      "</tr>".format(
                          f=html_escape(field),
                          full=html_escape(r['path']),
                          base=html_escape(base),
                          x=r['x'], y=r['y'],
                          ch=center_html,
                          eh=edge_html,
                          s=size_html,
                          sc=scale_html,
                          zi=zi_cell,
                          zo=zo_cell))
            print("</table>")

        print("<br><br><a href='{}'>Search again</a>".format(
            html_escape(back_link_url())))
        print("</body></html>")
    finally:
        try:
            slot.close()  # releases the flock
        except Exception:
            pass


if __name__ == "__main__":
    main()

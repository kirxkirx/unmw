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
from concurrent.futures import ThreadPoolExecutor


# Code-level operational constants (not deployment-specific).
MAX_CONCURRENT = 2
SCAN_TIMEOUT_SECONDS = 90
FITS2PNG_TIMEOUT_SECONDS = 30
FOV_TIMEOUT_SECONDS = 30
LOCK_DIR = '/tmp'
TEMP_PARENT = 'uploads'              # mirrors upload.py's upload_dir
TEMP_DIR_PREFIX = 'coord_search_'
# Thumbnail pixel sizing is shared with coord_forced_photometry.py via the
# nmw_coord_lib import below (DEFAULT_THUMBNAIL_PIXELS, HIRES_THUMBNAIL_MULTIPLIER,
# MIN/MAX_THUMBNAIL_PIXELS).
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


# ---------- shared helpers (single source of truth: nmw_coord_lib.py) ----------
# Shared with coord_forced_photometry.py so a fix to any of these updates
# both pages at once.
import nmw_coord_lib as ncl
from nmw_coord_lib import (
    html_escape, _PAGE_CSS, back_link_url, form_page_url, emit_redirect,
    emit_message_page, parse_coordinates, read_config_vars,
    acquire_concurrency_slot, run_sky2xy_scan, get_image_metadata,
    zoomout_png_dims, make_zoomout_thumbnail, make_zoomin_thumbnail,
    render_thumbnail_link, field_name_from_fits, list_fits_files,
    DEFAULT_THUMBNAIL_PIXELS, HIRES_THUMBNAIL_MULTIPLIER,
    MIN_THUMBNAIL_PIXELS, MAX_THUMBNAIL_PIXELS,
)

# The shared page-chrome helpers build their links from
# ncl.DEFAULT_FORM_PATH; point it at this page's input form.
ncl.DEFAULT_FORM_PATH = DEFAULT_FORM_PATH


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


def render_distance_cell(pix, mean_scale):
    """Three-line cell: arcmin, deg, pix. Pix-only when scale unknown."""
    if mean_scale is None:
        return '{} pix'.format(pix)
    arcmin = pix * mean_scale / 60.0
    deg = arcmin / 60.0
    return "{:.1f}'<br>{:.1f}&deg;<br>{} pix".format(arcmin, deg, pix)


def emit_match_row(r, url_prefix, sub):
    """Emit one <tr> for the coord-search results table and flush stdout."""
    base = os.path.basename(r['path'])
    field = field_name_from_fits(r['path'])
    size_html = render_image_size(
        r['arcmin_str'], r['deg_str'], r['nx'], r['ny'])
    scale_html, mean_scale = render_mean_scale(r['scale_x'], r['scale_y'])
    edge_html = render_distance_cell(r['edge'], mean_scale)
    center_html = render_distance_cell(r['from_center'], mean_scale)
    zi_cell = render_thumbnail_link(
        r.get('png_zoomin'), r.get('png_zoomin_hires'),
        'zoom-in', base, url_prefix, sub)
    zo_cell = render_thumbnail_link(
        r.get('png_zoomout'), r.get('png_zoomout_hires'),
        'zoom-out', base, url_prefix, sub)
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
              ch=center_html, eh=edge_html,
              s=size_html, sc=scale_html,
              zi=zi_cell, zo=zo_cell), flush=True)


def emit_listall_row(r, url_prefix, sub):
    """Emit one <tr> for the show-all table and flush stdout."""
    base = os.path.basename(r['path'])
    field = field_name_from_fits(r['path'])
    size_html = render_image_size(
        r['arcmin_str'], r['deg_str'], r['nx'], r['ny'])
    scale_html, _ = render_mean_scale(r['scale_x'], r['scale_y'])
    center_radec = r.get('center_radec') or '-'
    zo_cell = render_thumbnail_link(
        r.get('png_zoomout'), r.get('png_zoomout_hires'),
        'zoom-out', base, url_prefix, sub)
    print("<tr>"
          "<td><b>{f}</b></td>"
          "<td title='{full}'>{base}</td>"
          "<td>{cr}</td>"
          "<td>{s}</td>"
          "<td>{sc}</td>"
          "<td>{zo}</td>"
          "</tr>".format(
              f=html_escape(field),
              full=html_escape(r['path']),
              base=html_escape(base),
              cr=html_escape(center_radec),
              s=size_html, sc=scale_html, zo=zo_cell), flush=True)


# Number of columns in each table — used for inline error rows.
COORD_SEARCH_TABLE_COLS = 9
LIST_ALL_TABLE_COLS = 6


# ---------- main ----------

def main():
    cgitb.enable()
    request_start = time.time()

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
    # on each submit button: 'search' for coord-search, 'list_all' for
    # the show-all-reference-images view. The default value is 'search'
    # (used when JS is disabled or when the user submits via Enter in
    # the coords input).
    list_all_mode = form.getfirst('action') == 'list_all'

    if list_all_mode:
        ra = dec = None
        raw_coords = ''
    else:
        raw_coords = (form.getfirst('coords', '') or '').strip()
        # No search parameters supplied (e.g. the .py was opened directly):
        # send the user to the input form rather than showing an error.
        if not raw_coords:
            emit_redirect(form_page_url())
            return
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
            'COORD_SEARCH_PARALLEL_WORKERS',
        )
        ref_dir = cfg['REFERENCE_IMAGES'].strip()
        vast_dir = cfg['VAST_REFERENCE_COPY'].strip()
        url_prefix = cfg['URL_OF_DATA_PROCESSING_ROOT'].strip().rstrip('/')
        thumb_raw = cfg['COORD_SEARCH_THUMBNAIL_PIXELS'].strip()
        zoomin_raw = cfg['COORD_SEARCH_ZOOMIN_PIXELS'].strip()
        workers_raw = cfg['COORD_SEARCH_PARALLEL_WORKERS'].strip()

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

        try:
            parallel_workers = (int(workers_raw) if workers_raw
                                else DEFAULT_PARALLEL_WORKERS)
        except ValueError:
            parallel_workers = DEFAULT_PARALLEL_WORKERS
        if (parallel_workers < MIN_PARALLEL_WORKERS
                or parallel_workers > MAX_PARALLEL_WORKERS):
            parallel_workers = DEFAULT_PARALLEL_WORKERS

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
            # Streaming response: header + table opener flushed up front so
            # the user sees progress, rows emitted as their futures finish.
            page_title = "All reference images"
            print("Content-Type: text/html\n", flush=True)
            print("<html><head><title>{}</title>".format(
                html_escape(page_title)))
            print(_PAGE_CSS)
            print("</head><body>")
            # Push past Apache's CGI output buffer so the heading shows up
            # immediately rather than waiting for more data.
            print("<!-- {} -->".format(' ' * 4000))
            print("<h2>{}</h2>".format(html_escape(page_title)))
            print("<p>Listing reference images from "
                  "<span class='code'>{}</span> ...</p>".format(
                      html_escape(ref_dir)), flush=True)
            fits_paths = list_fits_files(ref_dir)
            if len(fits_paths) > LIST_ALL_MAX_FILES:
                fits_paths = fits_paths[:LIST_ALL_MAX_FILES]
                paths_truncated = True
            else:
                paths_truncated = False
            # Sort upfront by field name so streaming output order matches
            # the sorted view.
            fits_paths.sort(key=lambda p: (field_name_from_fits(p), p))

            print("<p>Found {} FITS file(s); generating thumbnails as they "
                  "complete ...</p>".format(len(fits_paths)), flush=True)
            if paths_truncated:
                print("<div class='notice'>Reference image directory contains "
                      "more than {} files; only the first {} (alphabetical) "
                      "are listed.</div>".format(
                          LIST_ALL_MAX_FILES, LIST_ALL_MAX_FILES), flush=True)

            print("<table class='main'>", flush=True)
            print("<tr><th>Field</th><th>Reference image</th>"
                  "<th>Image center (J2000)</th>"
                  "<th>Image size</th>"
                  "<th>Scale (&quot;/pix)</th>"
                  "<th>Zoom-out</th></tr>", flush=True)

            deadline = time.time() + LIST_ALL_TIMEOUT_SECONDS

            def _process_listall_entry(path):
                """Per-FITS: get metadata, render preview + hi-res zoom-out.

                Each thread owns one FITS file, so the two pgfv calls (which
                both write '<basename>.png' to cwd before being renamed) do
                not collide with other threads working on different files.
                Sequential within the thread guarantees that 'preview' is
                renamed before 'hires' overwrites it.
                """
                if time.time() > deadline:
                    return ('timeout', None)
                meta = get_image_metadata(path, vast_dir)
                if meta is None:
                    return ('no_meta', None)
                nx, ny = meta['nx'], meta['ny']
                png_preview = make_zoomout_thumbnail(
                    path, None, None, nx, ny,
                    out_dir_abs, vast_dir, thumb_pixels, suffix='zoomout')
                png_hires = make_zoomout_thumbnail(
                    path, None, None, nx, ny,
                    out_dir_abs, vast_dir, hires_pixels, suffix='zoomout_hires')
                return ('ok', {
                    'path': path,
                    'nx': nx, 'ny': ny,
                    'arcmin_str': meta['arcmin_str'],
                    'deg_str': meta['deg_str'],
                    'scale_x': meta['scale_x'],
                    'scale_y': meta['scale_y'],
                    'center_radec': meta['center_radec'],
                    'png_zoomout': png_preview,
                    'png_zoomout_hires': png_hires,
                })

            timed_out = False
            n_emitted = 0
            with ThreadPoolExecutor(max_workers=parallel_workers) as ex:
                # Submit all tasks; iterate futures in submission order so
                # rows appear in the user-expected (alphabetical) order even
                # though completion order may differ. The bottleneck is the
                # current-position task; subsequent already-finished ones
                # appear in quick succession after each blocking wait.
                futures = [ex.submit(_process_listall_entry, p)
                           for p in fits_paths]
                for fut, path in zip(futures, fits_paths):
                    try:
                        status, row = fut.result()
                    except Exception as err:
                        print("<tr><td colspan='{}'><b>render failed for "
                              "{}:</b> {}</td></tr>".format(
                                  LIST_ALL_TABLE_COLS,
                                  html_escape(os.path.basename(path)),
                                  html_escape(err)), flush=True)
                        continue
                    if status == 'timeout':
                        timed_out = True
                    elif status == 'ok':
                        emit_listall_row(row, url_prefix, sub)
                        n_emitted += 1

            print("</table>", flush=True)

            if timed_out:
                print("<div class='notice'>Listing stopped after {} s; "
                      "results may be incomplete.</div>".format(
                          LIST_ALL_TIMEOUT_SECONDS), flush=True)
            if n_emitted == 0:
                print("<p>No WCS-calibrated reference images found.</p>",
                      flush=True)

            print("<br><br><a href='{}'>Search again</a>".format(
                html_escape(back_link_url())), flush=True)
            print("<p style='color: #888; font-size: 90%;'>"
                  "Page generated in {:.1f} s.</p>".format(
                      time.time() - request_start), flush=True)
            print("</body></html>", flush=True)
            return  # done with list-all flow

        # ---- Coord-search mode (streaming).
        page_title = "Coordinate search results"
        print("Content-Type: text/html\n", flush=True)
        print("<html><head><title>{}</title>".format(html_escape(page_title)))
        print(_PAGE_CSS)
        print("</head><body>")
        print("<!-- {} -->".format(' ' * 4000))
        print("<h2>{}</h2>".format(html_escape(page_title)))
        print("<p>Searched for R.A. <b>{}</b>, Dec. <b>{}</b> (J2000) "
              "in <span class='code'>{}</span></p>".format(
                  html_escape(ra), html_escape(dec), html_escape(ref_dir)),
              flush=True)

        print("<p>Scanning reference images for matches ...</p>", flush=True)
        matches, truncated = run_sky2xy_scan(ref_dir, ra, dec, vast_dir)
        if truncated:
            print("<div class='notice'>Scan stopped after {} s; "
                  "results may be incomplete.</div>".format(
                      SCAN_TIMEOUT_SECONDS), flush=True)

        # Parallel metadata fetch (fast: ~1 s per image, no PNG yet) so we
        # can compute from-center and sort before opening the table.
        def _fetch_match_meta(item):
            path, x, y = item
            meta = get_image_metadata(path, vast_dir)
            if meta is None:
                return None
            nx, ny = meta['nx'], meta['ny']
            edge = int(round(min(x, y, nx - x, ny - y)))
            cx, cy = nx / 2.0, ny / 2.0
            from_center = int(round(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5))
            return {
                'path': path,
                'x': x, 'y': y,
                'nx': nx, 'ny': ny,
                'edge': edge,
                'from_center': from_center,
                'arcmin_str': meta['arcmin_str'],
                'deg_str': meta['deg_str'],
                'scale_x': meta['scale_x'],
                'scale_y': meta['scale_y'],
            }

        print("<p>Found {} candidate match(es); fetching image metadata "
              "...</p>".format(len(matches)), flush=True)
        results = []
        if matches:
            with ThreadPoolExecutor(max_workers=parallel_workers) as ex:
                for row in ex.map(_fetch_match_meta, matches):
                    if row is not None:
                        results.append(row)

        # Best-centred first: smallest distance to image centre. Sort BEFORE
        # submitting render tasks so the streaming order matches the sort.
        results.sort(key=lambda r: r['from_center'])

        if not results:
            print("<p>No reference images cover this sky position.</p>",
                  flush=True)
        else:
            print("<p>{} reference image(s) cover this position, sorted by "
                  "distance from image centre (best-centred first); rows "
                  "appear as thumbnails finish:</p>".format(len(results)),
                  flush=True)
            print("<table class='main'>", flush=True)
            print("<tr><th>Field</th><th>Reference image</th>"
                  "<th>X, Y (pix)</th>"
                  "<th>From center</th>"
                  "<th>Nearest edge</th>"
                  "<th>Image size</th>"
                  "<th>Scale (&quot;/pix)</th>"
                  "<th>Zoom-out</th><th>Zoom-in</th></tr>", flush=True)

            def _render_match(r):
                """All four PNGs for one matched FITS, rendered sequentially.

                Each thread owns one FITS file, so the four pgfv calls (each
                of which writes '<basename>.png' to cwd before being renamed)
                cannot collide with threads on other files. Sequential within
                the thread guarantees each rename completes before the next
                call writes a new '<basename>.png'.
                """
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
                    out_dir_abs, vast_dir, hires_pixels,
                    suffix='zoomout_hires')
                return r

            with ThreadPoolExecutor(max_workers=parallel_workers) as ex:
                # Submit in sort order; iterate futures in submission order
                # so rows appear sorted by from_center even though the
                # workers may finish out of submission order.
                futures = [ex.submit(_render_match, r) for r in results]
                for fut, r in zip(futures, results):
                    try:
                        fut.result()
                    except Exception as err:
                        print("<tr><td colspan='{}'><b>render failed for "
                              "{}:</b> {}</td></tr>".format(
                                  COORD_SEARCH_TABLE_COLS,
                                  html_escape(os.path.basename(r['path'])),
                                  html_escape(err)), flush=True)
                        continue
                    emit_match_row(r, url_prefix, sub)

            print("</table>", flush=True)

        print("<br><br><a href='{}'>Search again</a>".format(
            html_escape(back_link_url())), flush=True)
        print("<p style='color: #888; font-size: 90%;'>"
              "Page generated in {:.1f} s.</p>".format(
                  time.time() - request_start), flush=True)
        print("</body></html>", flush=True)
    finally:
        try:
            slot.close()  # releases the flock
        except Exception:
            pass


if __name__ == "__main__":
    main()

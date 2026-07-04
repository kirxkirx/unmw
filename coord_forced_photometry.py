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
config loading, page chrome) with coord_search.py via nmw_coord_lib.py, and
the measurement engine (band derivation, VaST working copy, plate-solve and
forced-photometry calls) with archive_phot_worker.py via nmw_forced_phot_lib.py.

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
import os
import random
import re
import shutil
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
    make_zoomout_thumbnail, make_zoomin_thumbnail,
    field_name_from_fits, HIRES_THUMBNAIL_MULTIPLIER,
)

# The measurement engine (band derivation, VaST working copy, parallel
# plate-solve stage, C forced photometry, lightcurve/table rendering) lives
# in nmw_forced_phot_lib.py, shared verbatim with the archival-photometry
# queue worker (archive_phot_worker.py).
from nmw_forced_phot_lib import (
    FORCED_PHOT_PARALLEL_SOLVE_WORKERS, DEFAULT_THUMBNAIL_PIXELS,
    MIN_THUMBNAIL_PIXELS, MAX_THUMBNAIL_PIXELS, DEFAULT_ZOOMIN_PIXELS,
    VALID_BANDS, _IMG_TS_RE, _looks_like_fits, _read_factory_text,
    derive_band, derive_sextractor_config, get_jd_and_atel_date,
    _phase1_parallel_solve_plate, run_forced_photometry_c,
    setup_vast_working_copy, _write_lightcurve_data_files,
    render_lightcurve_plots, ascii_table, fits_url, _is_float, _fmt_mag,
    _fmt_err, _fmt_duration, _html_row, _html_skipped_row,
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

# Per-upload directory name: img_<YYYY-MM-DD>_<...>. Only these are considered.
IMG_DIR_RE = re.compile(r'^img_(\d{4})-(\d{2})-(\d{2})_')

# Apache's CGI buffer is ~4 KB. Each streamed table row is appended with this
# whitespace comment so the buffer crosses the flush threshold within a couple
# of rows instead of stalling until many rows have accumulated.
_ROW_FLUSH_PAD = "<!-- " + (" " * 1500) + " -->\n"


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
                _png_basename, _eps_basename = render_lightcurve_plots(
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
                if _eps_basename is not None:
                    _eps_url = '{}/{}/{}'.format(
                        url_prefix, sub_name, _eps_basename)
                    _links.append(
                        "<a href='{}'>{}</a> (EPS figure)".format(
                            html_escape(_eps_url),
                            html_escape(_eps_basename)))
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


if __name__ == "__main__":
    main()

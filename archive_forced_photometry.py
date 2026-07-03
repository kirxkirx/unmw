#!/usr/bin/env python3
"""CGI: submit an archival forced-photometry job to the processing queue.

The user enters one sky position (plus an optional band override and an
optional UT date range); this page finds which reference fields cover the
position (sky2xy over $REFERENCE_IMAGES, exactly like coord_search.py),
collects every matching wcs_fd_ image from $IMAGE_ARCHIVE_DIR, reads the
observation date of each from its FITS header via util/get_image_date
(filename timestamps are never trusted for timing -- their timezone is not
knowable), applies the date range in JD, and enqueues ONE job that will
measure ALL matching images. There is deliberately no per-job image cap:
long-running jobs are the reason the queue exists.

The job is processed in the background by archive_phot_worker.py (kicked
from here, fork-and-forget); progress is watched on
archive_phot_status.py?job=<id> and the finished results stay at
uploads/archive_phot_<id>/index.html until the operator prunes old
results (archive_phot_prune.sh; nothing expires automatically).

Queue protection (no accounts, no CAPTCHA): total queued+running jobs are
capped (ARCHIVE_PHOT_MAX_QUEUE, default 10), queued+running jobs per client
IP are capped (ARCHIVE_PHOT_MAX_QUEUE_PER_IP, default 5), and a request
identical to a queued/running job (position within 1 arcsec, same band
override, same date range) is redirected to the existing job instead of
enqueued. Completed and failed jobs never block resubmission: submitting
the same target again is the designed way to re-measure after an archive
update, and the only way to retry a failed job.

Configuration (read from local_config.sh next to this script):
  IMAGE_ARCHIVE_DIR               directory holding the archival wcs_fd_ images
  IMAGE_ARCHIVE_URL               optional URL prefix serving that directory
  REFERENCE_IMAGES                directory containing reference FITS images
  VAST_REFERENCE_COPY             path to the VaST source/install tree
  URL_OF_DATA_PROCESSING_ROOT     URL prefix for the served uploads/ directory
  ARCHIVE_PHOT_MAX_QUEUE          queue length cap (default 10)
  ARCHIVE_PHOT_MAX_QUEUE_PER_IP   per-IP pending cap (default 5)
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
import subprocess
import time
import urllib.parse

import nmw_coord_lib as ncl
from nmw_coord_lib import (
    html_escape, _PAGE_CSS, form_page_url, emit_redirect,
    emit_message_page, parse_coordinates, read_config_vars,
    acquire_concurrency_slot, run_sky2xy_scan, field_name_from_fits,
)
from nmw_forced_phot_lib import VALID_BANDS
import nmw_archive_phot_lib as apl

# The shared page-chrome helpers build their links from ncl.DEFAULT_FORM_PATH;
# point it at this page's input form.
DEFAULT_FORM_PATH = '/unmw/archive_forced_photometry.html'
ncl.DEFAULT_FORM_PATH = DEFAULT_FORM_PATH

# Concurrent submissions to bound: each one runs the sky2xy reference scan
# and a batch of get_image_date header reads. Submissions are short (tens of
# seconds); the long work happens in the queue worker.
SUBMIT_MAX_CONCURRENT = 2

# Very rough wall-clock cost per archive image used only for the "expect
# roughly ..." line on the confirmation page: parallel UCAC5+APASS solve
# (~60 s / 8 workers) + serial photometry and four thumbnails on a 61 Mpix
# frame (~45 s).
ESTIMATED_SECONDS_PER_IMAGE = 55

# Same keep-alive trick as coord_forced_photometry.py: pad + flush so
# progress lines cross Apache's CGI buffer and reach the browser while the
# scan and the header-date batch run.
_FLUSH_PAD = "<!-- " + (" " * 1500) + " -->"


def _fmt_estimate(n_images):
    """Human-friendly 'expect roughly ...' duration for n_images."""
    seconds = n_images * ESTIMATED_SECONDS_PER_IMAGE
    if seconds < 3600:
        return '{} minutes'.format(max(2, int(round(seconds / 60.0))))
    hours = seconds / 3600.0
    return '{:.1f} hours'.format(hours)


def _cgi_base():
    """URL directory of this script, for links to sibling CGIs."""
    script_name = os.environ.get('SCRIPT_NAME', '')
    base = os.path.dirname(script_name) if script_name else ''
    return base or '/cgi-bin/unmw'


def _overview_url():
    return '{}/archive_phot_status.py'.format(_cgi_base())


def _status_url(job_id, extra=''):
    url = '{}?job={}'.format(_overview_url(), urllib.parse.quote(job_id))
    if extra:
        url += '&' + extra
    return url


def _parse_date_field(raw, label):
    """Validate an optional YYYY-MM-DD form field. Returns the normalized
    string ('' when empty). Raises ValueError with a user-facing message."""
    raw = (raw or '').strip()
    if not raw:
        return ''
    m = apl.DATE_INPUT_RE.match(raw)
    if not m:
        raise ValueError('{} must look like YYYY-MM-DD; you sent: {}'.format(
            label, raw))
    try:
        datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise ValueError('{} is not a valid calendar date: {}'.format(
            label, raw))
    return raw


def main():
    cgitb.enable()

    # cwd = the directory of this script (even if reached via a symlink), so
    # ./local_config.sh and uploads/ resolve correctly.
    script_dir = os.path.dirname(os.path.realpath(__file__))
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
    raw_date_from = (form.getfirst('date_from', '') or '').strip()
    raw_date_to = (form.getfirst('date_to', '') or '').strip()

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
    try:
        ra_deg = apl.coord_token_to_degrees(ra, is_ra=True)
        dec_deg = apl.coord_token_to_degrees(dec, is_ra=False)
    except ValueError as err:
        emit_message_page(
            "Invalid coordinates",
            "<p>Could not convert coordinates to degrees: <b>{}</b></p>".format(
                html_escape(err)))
        return

    if band_override and band_override not in VALID_BANDS:
        emit_message_page(
            "Invalid band",
            "<p>Unsupported band: <span class='code'>{}</span></p>"
            "<p>Supported bands: {}</p>".format(
                html_escape(band_override), ' '.join(VALID_BANDS)))
        return

    try:
        date_from = _parse_date_field(raw_date_from, "'From date'")
        date_to = _parse_date_field(raw_date_to, "'To date'")
    except ValueError as err:
        emit_message_page("Invalid date",
                          "<p>{}</p>".format(html_escape(err)))
        return
    if date_from and date_to and date_from > date_to:
        emit_message_page(
            "Invalid date range",
            "<p>'From date' {} is after 'To date' {}.</p>".format(
                html_escape(date_from), html_escape(date_to)))
        return
    jd_min = apl.date_string_to_jd(date_from) if date_from else None
    jd_max = apl.date_string_to_jd(date_to, end_of_day=True) if date_to \
        else None

    slot = acquire_concurrency_slot(prefix=apl.SUBMIT_SLOT_PREFIX,
                                    max_concurrent=SUBMIT_MAX_CONCURRENT,
                                    lock_dir=apl.archive_lock_dir())
    if slot is None:
        emit_message_page(
            "Server busy",
            "<p>The maximum number of concurrent job submissions "
            "({0} of {0}) is currently being processed. Please try again "
            "in a minute.</p>".format(SUBMIT_MAX_CONCURRENT),
            status_line="Status: 503 Service Unavailable")
        return

    try:
        cfg = read_config_vars(
            'REFERENCE_IMAGES', 'VAST_REFERENCE_COPY',
            'URL_OF_DATA_PROCESSING_ROOT', 'IMAGE_ARCHIVE_DIR',
            'ARCHIVE_PHOT_MAX_QUEUE', 'ARCHIVE_PHOT_MAX_QUEUE_PER_IP')
        ref_dir = cfg['REFERENCE_IMAGES'].strip()
        vast_dir = cfg['VAST_REFERENCE_COPY'].strip()
        url_prefix = cfg['URL_OF_DATA_PROCESSING_ROOT'].strip().rstrip('/')
        archive_dir = cfg['IMAGE_ARCHIVE_DIR'].strip()
        max_queue = apl.parse_positive_int(cfg['ARCHIVE_PHOT_MAX_QUEUE'],
                                           apl.DEFAULT_MAX_QUEUE)
        max_queue_per_ip = apl.parse_positive_int(
            cfg['ARCHIVE_PHOT_MAX_QUEUE_PER_IP'],
            apl.DEFAULT_MAX_QUEUE_PER_IP)

        if not archive_dir or not os.path.isdir(archive_dir):
            emit_message_page(
                "Not configured",
                "<p>Archival forced photometry is not configured on this "
                "server: <span class='code'>IMAGE_ARCHIVE_DIR</span> is not "
                "set in <span class='code'>local_config.sh</span> or does "
                "not point to a directory.</p>",
                status_line="Status: 500 Internal Server Error")
            return
        if not ref_dir or not os.path.isdir(ref_dir):
            emit_message_page(
                "Configuration error",
                "<p>Reference image directory not found: "
                "<span class='code'>{}</span></p>".format(
                    html_escape(ref_dir)),
                status_line="Status: 500 Internal Server Error")
            return
        if not vast_dir or not os.path.isdir(vast_dir):
            emit_message_page(
                "Configuration error",
                "<p>VaST install directory not found: "
                "<span class='code'>{}</span></p>".format(
                    html_escape(vast_dir)),
                status_line="Status: 500 Internal Server Error")
            return
        if not url_prefix:
            emit_message_page(
                "Configuration error",
                "<p><span class='code'>URL_OF_DATA_PROCESSING_ROOT</span> is "
                "not set in <span class='code'>local_config.sh</span>.</p>",
                status_line="Status: 500 Internal Server Error")
            return

        if not os.path.isdir(apl.TEMP_PARENT):
            try:
                os.makedirs(apl.TEMP_PARENT, mode=0o755)
            except OSError as err:
                emit_message_page(
                    "Configuration error",
                    "<p>Cannot create '{}': {}</p>".format(
                        html_escape(apl.TEMP_PARENT), html_escape(err)),
                    status_line="Status: 500 Internal Server Error")
                return

        remote_addr = os.environ.get('REMOTE_ADDR', 'unknown')

        # ---- Queue protection: duplicate suppression and the caps. All of
        # this is cheap (a few small JSON files) and happens before any
        # body is streamed, so it can still answer with real HTTP statuses.
        # The dead-worker sweep runs first: a job orphaned by a crash or
        # reboot must become 'failed' NOW, or the duplicate check below
        # would bounce this resubmission off the zombie forever (the
        # worker that would normally sweep it only starts on a kick).
        apl.sweep_dead_running_jobs()
        pending = apl.list_pending_jobs()

        dup_id = apl.find_duplicate_pending(pending, ra_deg, dec_deg,
                                            band_override, date_from,
                                            date_to)
        if dup_id is not None:
            emit_redirect(_status_url(dup_id))
            return

        if len(pending) >= max_queue:
            emit_message_page(
                "Queue full",
                "<p>The archival photometry queue is full ({} of {} jobs "
                "pending). Please try again later.</p>"
                "<p><a href='{}'>Queue status</a></p>".format(
                    len(pending), max_queue,
                    html_escape(_overview_url())),
                status_line="Status: 503 Service Unavailable")
            return
        n_mine = sum(1 for _jid, _d, _st, rq in pending
                     if rq.get('remote_addr') == remote_addr)
        if n_mine >= max_queue_per_ip:
            emit_message_page(
                "Too many pending requests",
                "<p>There are already {} queued or running jobs submitted "
                "from your address (limit {}). Please wait for them to "
                "finish.</p>".format(n_mine, max_queue_per_ip),
                status_line="Status: 503 Service Unavailable")
            return

        # ---- Stream the page header EARLY (coord_forced_photometry.py
        # style): the reference-field scan and the FITS-header date batch
        # below take tens of seconds, and the flushed progress lines keep
        # bytes moving so neither the browser nor a proxy times out.
        # Failures after this point are inline notices on the committed
        # 200 response.
        page_title = "Archival forced photometry: job submission"
        print("Content-Type: text/html\n", flush=True)
        print("<html><head><title>{}</title>".format(html_escape(page_title)))
        print(_PAGE_CSS)
        print("<style type='text/css'>"
              "p.secondary { color: #666; font-style: italic; }"
              "</style>")
        print("</head><body>")
        print("<!-- {} -->".format(' ' * 4000))  # past Apache's CGI buffer
        print("<h2>{}</h2>".format(html_escape(page_title)))
        range_text = ''
        if date_from or date_to:
            range_text = "; UT date range {} .. {}".format(
                html_escape(date_from) if date_from else '(archive start)',
                html_escape(date_to) if date_to else '(archive end)')
        print("<p>Position: <span class='code'>{} {}</span>; band: "
              "<span class='code'>{}</span>{}.</p>".format(
                  html_escape(ra), html_escape(dec),
                  html_escape(band_override) if band_override else 'auto',
                  range_text), flush=True)

        # ---- Which reference fields cover the position? ----
        print("<p class='secondary'>Looking up which reference fields cover "
              "this position...</p>{}".format(_FLUSH_PAD), flush=True)
        try:
            matches, sky2xy_truncated = run_sky2xy_scan(
                ref_dir, ra, dec, vast_dir)
        except (OSError, subprocess.SubprocessError) as err:
            print("<div class='notice'>ERROR: reference-field scan failed: "
                  "{} ({}).</div>".format(
                      html_escape(type(err).__name__), html_escape(err)))
            print("</body></html>")
            return
        covering_fields = set(field_name_from_fits(p)
                              for p, _x, _y in matches)
        if sky2xy_truncated:
            print("<div class='notice'>WARNING: reference-field scan timed "
                  "out after {} s; the list of covering fields may be "
                  "incomplete.</div>".format(ncl.SCAN_TIMEOUT_SECONDS),
                  flush=True)
        if not covering_fields:
            print("<div class='notice'>ERROR: no reference field covers the "
                  "specified sky position.</div>")
            print("<br><a href='{}'>Submit another position</a>".format(
                html_escape(form_page_url())))
            print("</body></html>")
            return
        print("<p>Covering field(s): <b>{}</b></p>{}".format(
            html_escape(', '.join(sorted(covering_fields))), _FLUSH_PAD),
            flush=True)

        # ---- Collect the matching archive images. ----
        print("<p class='secondary'>Searching the image archive...</p>{}"
              .format(_FLUSH_PAD), flush=True)
        snapshot_paths = apl.discover_archive_images(archive_dir,
                                                     covering_fields)
        if not snapshot_paths:
            print("<div class='notice'>ERROR: the image archive contains no "
                  "images of these fields.</div>")
            print("<br><a href='{}'>Submit another position</a>".format(
                html_escape(form_page_url())))
            print("</body></html>")
            return
        print("<p>{} archive image(s) of the covering field(s) found.</p>{}"
              .format(len(snapshot_paths), _FLUSH_PAD), flush=True)

        # ---- Observation dates from the FITS headers. ----
        print("<p class='secondary'>Reading observation dates from {} FITS "
              "headers (util/get_image_date, {} parallel readers)...</p>{}"
              .format(len(snapshot_paths), apl.GET_IMAGE_DATE_WORKERS,
                      _FLUSH_PAD), flush=True)

        def _date_progress(n_done, n_total):
            # A dot every 20 headers keeps bytes flowing on big archives
            # without flooding the page.
            if n_done % 20 == 0 or n_done == n_total:
                print("<!-- dates {}/{} -->{}".format(n_done, n_total,
                                                      _FLUSH_PAD),
                      flush=True)

        jd_by_path = apl.get_jd_batch(vast_dir, snapshot_paths,
                                      progress_callback=_date_progress)
        date_failed = sorted(p for p, jd in jd_by_path.items() if jd is None)
        images = []
        for path in snapshot_paths:
            jd = jd_by_path.get(path)
            if jd is None:
                continue
            if jd_min is not None and jd < jd_min:
                continue
            if jd_max is not None and jd >= jd_max:
                continue
            images.append({'path': path, 'jd': jd})
        # Newest first, matching the result-table convention of
        # coord_forced_photometry.py. Ordering uses header JD only.
        images.sort(key=lambda e: e['jd'], reverse=True)

        if date_failed:
            print("<p class='secondary'>{} image(s) excluded because their "
                  "observation date could not be read from the FITS "
                  "header.</p>".format(len(date_failed)), flush=True)
        if not images:
            print("<div class='notice'>ERROR: no archive images remain "
                  "after applying the date range.</div>")
            print("<br><a href='{}'>Submit another position</a>".format(
                html_escape(form_page_url())))
            print("</body></html>")
            return

        # ---- Prior jobs at this position: purely informational. A
        # completed/failed twin never blocks the new job. ----
        prior_same_params = apl.find_prior_jobs(
            ra_deg, dec_deg, band_override=band_override,
            date_from=date_from, date_to=date_to, limit=1)
        if prior_same_params:
            p_id, _p_dir, p_st, p_rq = prior_same_params[0]
            n_new = len(set(snapshot_paths) -
                        set(p_rq.get('snapshot_paths', [])))
            print("<p class='secondary'>This exact request was measured "
                  "before as job <a href='{}'>{}</a> ({}); the archive has "
                  "gained {} matching image(s) since then.</p>".format(
                      html_escape(_status_url(p_id)), html_escape(p_id),
                      html_escape(p_st.get('state', '?')), n_new),
                  flush=True)
        else:
            prior_any = apl.find_prior_jobs(ra_deg, dec_deg, limit=3)
            for p_id, _p_dir, p_st, _p_rq in prior_any:
                print("<p class='secondary'>Earlier job at this position "
                      "(different parameters): <a href='{}'>{}</a> ({})."
                      "</p>".format(
                          html_escape(_status_url(p_id)), html_escape(p_id),
                          html_escape(p_st.get('state', '?'))), flush=True)

        # ---- Create the job and kick the worker. ----
        request_dict = {
            'coords_raw': raw_coords,
            'ra': ra,
            'dec': dec,
            'ra_deg': ra_deg,
            'dec_deg': dec_deg,
            'band_override': band_override,
            'date_from': date_from,
            'date_to': date_to,
            'jd_min': jd_min,
            'jd_max': jd_max,
            'covering_fields': sorted(covering_fields),
            'snapshot_paths': snapshot_paths,
            'images': images,
            'n_date_failed': len(date_failed),
            'date_failed_paths': date_failed,
            'submit_time_utc': time.strftime('%Y-%m-%d %H:%M:%S UTC',
                                             time.gmtime()),
            'remote_addr': remote_addr,
            'user_agent': os.environ.get('HTTP_USER_AGENT', ''),
            'cgi_base': _cgi_base(),
            'form_path': apl.FORM_PAGE_PATH,
        }
        # The pre-stream duplicate/caps checks ran before the reference
        # scan and the header-date batch -- tens of seconds ago. Re-check
        # and create the job under the claim lock so two concurrent
        # submissions of the same target cannot both slip through, and the
        # caps cannot overshoot. If the lock is unavailable (should not
        # happen), fall back to creating without the recheck.
        claim = None
        try:
            claim = apl.acquire_claim_lock()
        except OSError:
            pass
        try:
            pending = apl.list_pending_jobs()
            dup_id = apl.find_duplicate_pending(pending, ra_deg, dec_deg,
                                                band_override, date_from,
                                                date_to)
            if dup_id is not None:
                print("<div class='notice'>An identical job was queued by "
                      "someone else while this submission was being "
                      "prepared; follow it instead: <a href='{}'>{}</a>"
                      "</div>".format(html_escape(_status_url(dup_id)),
                                      html_escape(dup_id)))
                print("</body></html>")
                return
            n_mine = sum(1 for _jid, _d, _st, rq in pending
                         if rq.get('remote_addr') == remote_addr)
            if len(pending) >= max_queue or n_mine >= max_queue_per_ip:
                print("<div class='notice'>The queue filled up while this "
                      "submission was being prepared ({} pending, {} from "
                      "your address); please try again later.</div>"
                      .format(len(pending), n_mine))
                print("<br><a href='{}'>Queue status</a>".format(
                    html_escape(_overview_url())))
                print("</body></html>")
                return
            try:
                job_id, _job_dir = apl.create_job(request_dict)
            except OSError as err:
                print("<div class='notice'>ERROR: could not create the job "
                      "directory: {} ({}).</div>".format(
                          html_escape(type(err).__name__), html_escape(err)))
                print("</body></html>")
                return
        finally:
            if claim is not None:
                claim.close()

        apl.kick_worker(script_dir)

        position = apl.queue_position(job_id)
        status_url = _status_url(job_id)
        print("<h3>Job <span class='code'>{}</span> queued</h3>".format(
            html_escape(job_id)))
        print("<p>{} image(s) will be measured; expect very roughly "
              "{} of processing once the job starts.</p>".format(
                  len(images), html_escape(_fmt_estimate(len(images)))))
        if position is not None and position > 1:
            print("<p>Queue position: {} (jobs are processed in submission "
                  "order).</p>".format(position))
        print("<p>Follow the progress at <a href='{0}'>the job status "
              "page</a> &mdash; bookmark it; it will redirect to the "
              "persistent results page when the job finishes. You can close "
              "this window at any time.</p>".format(html_escape(status_url)))
        print("<p class='secondary'>Results stay at a stable URL; very old "
              "results may eventually be removed during server "
              "housekeeping, so download the data files you want to "
              "keep.</p>")
        print("<p class='secondary'>This page will jump to the status page "
              "in a few seconds.</p>")
        print("<script type='text/javascript'>setTimeout(function() {{ "
              "window.location = '{}'; }}, 5000);</script>".format(
                  status_url.replace("'", "\\'")))
        print("</body></html>")
    finally:
        slot.close()


if __name__ == "__main__":
    main()

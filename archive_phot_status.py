#!/usr/bin/env python3
"""CGI: status pages for the archival forced-photometry queue.

Three views:
  archive_phot_status.py                    queue overview (recent jobs)
  archive_phot_status.py?job=<id>           one job: queue position while
                                            queued, progress while running,
                                            redirect to the permanent
                                            results page when finished
  archive_phot_status.py?job=<id>&check_updates=1
                                            for a finished job: compare the
                                            archive against the snapshot
                                            recorded at submission time and
                                            advise whether re-measuring
                                            would pick up new images

Self-healing: every request first sweeps 'running' jobs whose worker
process is dead (crash, reboot) into the terminal 'failed' state, and
whenever queued jobs exist with no live worker this page re-kicks the
worker -- so with the CGI-kick-only design (no cron) a stranded queue
recovers as soon as anyone looks at any status page.

Beyond that sweep this page never mutates job state; failed jobs stay
failed (re-measuring requires a fresh submission from the input form)."""

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

import os
import time
import urllib.parse

import nmw_coord_lib as ncl
from nmw_coord_lib import (
    html_escape, _PAGE_CSS, emit_redirect, emit_message_page,
    read_config_vars, acquire_concurrency_slot,
)
import nmw_archive_phot_lib as apl

DEFAULT_FORM_PATH = '/unmw/archive_forced_photometry.html'
ncl.DEFAULT_FORM_PATH = DEFAULT_FORM_PATH

# Status pages while the job is queued/running reload themselves at this
# interval; the overview a bit slower.
JOB_REFRESH_SECONDS = 20
OVERVIEW_REFRESH_SECONDS = 60
OVERVIEW_MAX_JOBS = 50
PROGRESS_TAIL_LINES = 15
# The update check reads FITS headers of the archive images that appeared
# after the job was submitted; cap the header reads so one click cannot
# start an unbounded amount of work.
UPDATE_CHECK_MAX_HEADER_READS = 200


def _cgi_base():
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


def _render_page(title, body_html, refresh_seconds=None, status_line=None):
    """Full page with the shared CSS chrome and an optional meta-refresh.
    body_html is inserted raw -- caller pre-escapes user-derived data."""
    if status_line:
        print(status_line)
    print("Content-Type: text/html\n")
    print("<html><head><title>{}</title>".format(html_escape(title)))
    if refresh_seconds:
        print("<meta http-equiv='refresh' content='{}'>".format(
            int(refresh_seconds)))
    print(_PAGE_CSS)
    print("<style type='text/css'>"
          "p.secondary { color: #666; font-style: italic; }"
          "</style>")
    print("</head><body>")
    print("<h2>{}</h2>".format(html_escape(title)))
    print(body_html)
    print("<br><a href='{}'>Queue overview</a> &middot; "
          "<a href='{}'>Submit a new job</a>".format(
              html_escape(_overview_url()),
              html_escape(DEFAULT_FORM_PATH)))
    print("</body></html>")


def _progress_tail(job_dir):
    """Last PROGRESS_TAIL_LINES of the worker's progress.log, or ''."""
    path = os.path.join(job_dir, 'progress.log')
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            data = fh.read().decode('utf-8', 'replace')
    except OSError:
        return ''
    lines = data.splitlines()
    return '\n'.join(lines[-PROGRESS_TAIL_LINES:])


def _job_summary_html(request):
    range_text = ''
    if request.get('date_from') or request.get('date_to'):
        range_text = "; UT date range {} .. {}".format(
            html_escape(request.get('date_from') or '(archive start)'),
            html_escape(request.get('date_to') or '(archive end)'))
    return ("<p>Position: <span class='code'>{} {}</span>; band: "
            "<span class='code'>{}</span>{}; {} image(s); submitted {}.</p>"
            .format(html_escape(request.get('ra', '?')),
                    html_escape(request.get('dec', '?')),
                    html_escape(request.get('band_override') or 'auto'),
                    range_text,
                    len(request.get('images', [])),
                    html_escape(request.get('submit_time_utc', '?'))))


def _maybe_kick_worker(script_dir, max_running):
    """Restart the worker when queued jobs exist but no worker is alive.
    This is the crash/reboot recovery path of the no-cron design."""
    queued = [1 for _jid, _d, st, _rq in apl.list_pending_jobs()
              if st.get('state') == apl.STATE_QUEUED]
    if queued and apl.worker_slots_locked(max_running) == 0:
        apl.kick_worker(script_dir)
        return True
    return False


def _results_url(url_prefix, job_id):
    return '{}/{}{}/index.html'.format(url_prefix, apl.JOB_DIR_PREFIX,
                                       job_id)


def _show_overview(script_dir, max_running):
    kicked = _maybe_kick_worker(script_dir, max_running)
    rows = []
    for job_id, d in apl.list_job_dirs()[:OVERVIEW_MAX_JOBS]:
        st = apl.read_state(d) or {}
        rq = apl.read_request(d) or {}
        state = st.get('state', '?')
        progress = ''
        if state == apl.STATE_RUNNING:
            progress = ' ({}/{})'.format(st.get('n_done', '?'),
                                         st.get('n_total', '?'))
        rows.append(
            "<tr><td><a href='{url}'><span class='code'>{jid}</span></a>"
            "</td><td>{state}{prog}</td><td><span class='code'>{ra} {dec}"
            "</span></td><td>{band}</td><td>{dfrom}</td><td>{dto}</td>"
            "<td>{n}</td><td>{sub}</td></tr>".format(
                url=html_escape(_status_url(job_id)),
                jid=html_escape(job_id),
                state=html_escape(state), prog=html_escape(progress),
                ra=html_escape(rq.get('ra', '?')),
                dec=html_escape(rq.get('dec', '?')),
                band=html_escape(rq.get('band_override') or 'auto'),
                dfrom=html_escape(rq.get('date_from') or '-'),
                dto=html_escape(rq.get('date_to') or '-'),
                n=len(rq.get('images', [])),
                sub=html_escape(rq.get('submit_time_utc', '?'))))
    body = []
    if not rows:
        body.append("<p>No archival photometry jobs on record.</p>")
    else:
        body.append("<table class='main'>")
        body.append("<tr><th>Job</th><th>State</th><th>Position</th>"
                    "<th>Band</th><th>From</th><th>To</th>"
                    "<th>Images</th><th>Submitted (UTC)</th></tr>")
        body.extend(rows)
        body.append("</table>")
    if kicked:
        body.append("<p class='secondary'>The queue worker was restarted "
                    "for the waiting job(s).</p>")
    _render_page("Archival forced photometry queue", '\n'.join(body),
                 refresh_seconds=OVERVIEW_REFRESH_SECONDS)


def _show_update_check(job_id, job_dir, state, request, cfg):
    archive_dir = cfg['IMAGE_ARCHIVE_DIR'].strip()
    vast_dir = cfg['VAST_REFERENCE_COPY'].strip()
    url_prefix = cfg['URL_OF_DATA_PROCESSING_ROOT'].strip().rstrip('/')
    if not archive_dir or not os.path.isdir(archive_dir):
        _render_page("Archive not available",
                     "<p><span class='code'>IMAGE_ARCHIVE_DIR</span> is not "
                     "configured or not a directory.</p>")
        return
    # The archive walk plus FITS-header reads below are the expensive part
    # of this page; share the submission slot pool so a burst of clicks
    # cannot pile up unbounded work.
    slot = acquire_concurrency_slot(prefix=apl.SUBMIT_SLOT_PREFIX,
                                    max_concurrent=2,
                                    lock_dir=apl.archive_lock_dir())
    if slot is None:
        _render_page("Server busy",
                     "<p>Another archive scan is in progress; please retry "
                     "in a minute.</p>",
                     status_line="Status: 503 Service Unavailable")
        return
    try:
        covering_fields = set(request.get('covering_fields', []))
        snapshot = set(request.get('snapshot_paths', []))
        current = apl.discover_archive_images(archive_dir, covering_fields)
        new_paths = sorted(set(current) - snapshot)
        jd_min = request.get('jd_min')
        jd_max = request.get('jd_max')
        n_new = len(new_paths)
        truncated = False
        note = ''
        if new_paths and (jd_min is not None or jd_max is not None):
            to_check = new_paths[:UPDATE_CHECK_MAX_HEADER_READS]
            truncated = len(new_paths) > len(to_check)
            jd_by_path = apl.get_jd_batch(vast_dir, to_check)
            n_new = 0
            for _p, jd in jd_by_path.items():
                if jd is None:
                    continue
                if jd_min is not None and jd < jd_min:
                    continue
                if jd_max is not None and jd >= jd_max:
                    continue
                n_new += 1
            if truncated:
                note = (" (only the first {} of {} new images were checked "
                        "against the job's date range)".format(
                            len(to_check), len(new_paths)))
        # Resubmission link built from the constant form path -- never from
        # a stored URL that could carry a client-supplied Host header.
        resubmit = '{}?{}'.format(
            request.get('form_path') or apl.FORM_PAGE_PATH,
            urllib.parse.urlencode({
                k: v for k, v in (
                    ('coords', request.get('coords_raw', '')),
                    ('band', request.get('band_override', '')),
                    ('date_from', request.get('date_from', '')),
                    ('date_to', request.get('date_to', '')),
                ) if v}))
        body = [_job_summary_html(request)]
        if n_new > 0:
            body.append("<div class='notice'>The archive has gained "
                        "<b>{}</b> image(s) matching this job since it was "
                        "submitted{}. Re-measuring is recommended: "
                        "<a href='{}'>submit the same request again</a> "
                        "(the old results stay untouched).</div>".format(
                            n_new, html_escape(note),
                            html_escape(resubmit)))
        elif truncated:
            body.append("<div class='notice'>{} new field image(s) have "
                        "appeared in the archive, but only the first {} "
                        "could be checked against this job's date range "
                        "and none of those fall inside it. Whether the "
                        "remaining ones do could NOT be verified; "
                        "<a href='{}'>re-run the request</a> to be sure."
                        "</div>".format(len(new_paths),
                                        UPDATE_CHECK_MAX_HEADER_READS,
                                        html_escape(resubmit)))
        else:
            body.append("<p>The archive has not gained any image matching "
                        "this job since it was submitted; the existing "
                        "results are up to date.</p>")
        if state.get('state') == apl.STATE_DONE or \
                state.get('state') == apl.STATE_FAILED:
            body.append("<p><a href='{}'>Back to the job results</a></p>"
                        .format(html_escape(_results_url(url_prefix,
                                                         job_id))))
        _render_page("Archive update check: job {}".format(job_id),
                     '\n'.join(body))
    finally:
        slot.close()


def _show_job(job_id, job_dir, script_dir, cfg, max_running):
    state = apl.read_state(job_dir)
    request = apl.read_request(job_dir)
    if state is None or request is None:
        _render_page("Job metadata unreadable",
                     "<p>Job <span class='code'>{}</span> exists but its "
                     "metadata cannot be read.</p>".format(
                         html_escape(job_id)),
                     status_line="Status: 500 Internal Server Error")
        return
    url_prefix = cfg['URL_OF_DATA_PROCESSING_ROOT'].strip().rstrip('/')
    st = state.get('state')

    if st in (apl.STATE_DONE, apl.STATE_FAILED):
        results = _results_url(url_prefix, job_id)
        if os.path.isfile(os.path.join(job_dir, 'index.html')):
            emit_redirect(results)
            return
        # Results page missing (should not happen): show what we know.
        body = [_job_summary_html(request),
                "<p>Job state: <b>{}</b>, but the results page was not "
                "written.</p>".format(html_escape(st))]
        if state.get('error'):
            body.append("<div class='notice'>{}</div>".format(
                html_escape(state['error'])))
        _render_page("Job {}".format(job_id), '\n'.join(body))
        return

    if st == apl.STATE_QUEUED:
        kicked = _maybe_kick_worker(script_dir, max_running)
        position = apl.queue_position(job_id)
        body = [_job_summary_html(request)]
        body.append("<p>State: <b>queued</b>{}.</p>".format(
            ", position {} in the queue".format(position)
            if position else ''))
        if kicked:
            body.append("<p class='secondary'>The queue worker was "
                        "restarted for this job.</p>")
        body.append("<p class='secondary'>This page reloads every {} s; "
                    "bookmark it to check back later. It will redirect to "
                    "the permanent results page when the job finishes.</p>"
                    .format(JOB_REFRESH_SECONDS))
        _render_page("Job {}".format(job_id), '\n'.join(body),
                     refresh_seconds=JOB_REFRESH_SECONDS)
        return

    if st == apl.STATE_RUNNING:
        body = [_job_summary_html(request)]
        n_done = state.get('n_done', 0)
        n_total = state.get('n_total', len(request.get('images', [])))
        stage = state.get('stage', '')
        elapsed_text = ''
        try:
            elapsed = time.time() - float(state.get('started_epoch'))
            elapsed_text = " for {:.0f} min".format(elapsed / 60.0)
        except (TypeError, ValueError):
            pass
        body.append("<p>State: <b>running</b>{} &mdash; {} of {} image(s) "
                    "done{}.</p>".format(
                        elapsed_text, n_done, n_total,
                        ' ({})'.format(html_escape(stage)) if stage else ''))
        tail = _progress_tail(job_dir)
        if tail:
            body.append("<p class='secondary'>Recent progress:</p>"
                        "<pre>{}</pre>".format(html_escape(tail)))
        body.append("<p class='secondary'>This page reloads every {} s and "
                    "will redirect to the permanent results page when the "
                    "job finishes.</p>".format(JOB_REFRESH_SECONDS))
        _render_page("Job {}".format(job_id), '\n'.join(body),
                     refresh_seconds=JOB_REFRESH_SECONDS)
        return

    _render_page("Job {}".format(job_id),
                 _job_summary_html(request) +
                 "<p>Unrecognized job state: <span class='code'>{}</span>"
                 "</p>".format(html_escape(str(st))))


def main():
    cgitb.enable()
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
    job_id = (form.getfirst('job', '') or '').strip()
    check_updates = (form.getfirst('check_updates', '') or '').strip()

    # Heal jobs orphaned by a crash or reboot: without this, a dead
    # 'running' job with an otherwise empty queue would never be swept
    # (workers only start on kicks, and kicks only happen for queued
    # jobs), showing 'running' forever and blocking identical
    # resubmission via the duplicate check.
    apl.sweep_dead_running_jobs()

    cfg = read_config_vars('URL_OF_DATA_PROCESSING_ROOT',
                           'IMAGE_ARCHIVE_DIR', 'VAST_REFERENCE_COPY',
                           'ARCHIVE_PHOT_MAX_RUNNING')
    max_running = min(apl.MAX_RUNNING_HARD_CAP,
                      apl.parse_positive_int(cfg['ARCHIVE_PHOT_MAX_RUNNING'],
                                             apl.DEFAULT_MAX_RUNNING))
    if not cfg['URL_OF_DATA_PROCESSING_ROOT'].strip():
        emit_message_page(
            "Configuration error",
            "<p><span class='code'>URL_OF_DATA_PROCESSING_ROOT</span> is "
            "not set in <span class='code'>local_config.sh</span>.</p>",
            status_line="Status: 500 Internal Server Error")
        return

    if not job_id:
        _show_overview(script_dir, max_running)
        return

    if not apl.is_valid_job_id(job_id):
        _render_page("Unknown job",
                     "<p>Malformed job id.</p>",
                     status_line="Status: 404 Not Found")
        return
    job_dir = apl.job_dir_path(job_id)
    if not os.path.isdir(job_dir):
        _render_page("Unknown job",
                     "<p>No job <span class='code'>{}</span> on record "
                     "(it may have been pruned).</p>".format(
                         html_escape(job_id)),
                     status_line="Status: 404 Not Found")
        return

    if check_updates:
        state = apl.read_state(job_dir) or {}
        request = apl.read_request(job_dir)
        if request is None:
            _render_page("Job metadata unreadable",
                         "<p>Job <span class='code'>{}</span> exists but "
                         "its metadata cannot be read.</p>".format(
                             html_escape(job_id)),
                         status_line="Status: 500 Internal Server Error")
            return
        _show_update_check(job_id, job_dir, state, request, cfg)
        return

    _show_job(job_id, job_dir, script_dir, cfg, max_running)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Job store and queue helpers for the archival forced photometry pages.

The archival photometry feature runs long jobs (an uncapped number of large
archive images per request) through an on-disk FIFO queue instead of holding
an HTTP connection open. This module holds everything the three programs
involved share:

  archive_forced_photometry.py  (CGI)  validates a request, enforces the
                                       queue caps, creates the job directory
                                       and kicks the worker
  archive_phot_status.py        (CGI)  renders per-job status pages and the
                                       queue overview, re-kicks the worker
  archive_phot_worker.py     (no CGI)  claims queued jobs one at a time and
                                       measures them

A job is one directory uploads/archive_phot_<id>/ where
<id> = <UTC YYYYMMDD-HHMMSS>_<8 random ASCII letters>; the timestamp prefix
makes lexicographic order match submission (FIFO) order, the random suffix
keeps result URLs practically unguessable. Inside:

  request.json   immutable request parameters + the frozen, JD-sorted list
                 of archive images to measure and the full field-matched
                 archive snapshot (for later "did the archive gain images?"
                 checks)
  state.json     queued | running | done | failed, worker pid, progress
                 counters; written atomically, single writer at any time
                 (the submit CGI creates it, then only the worker that
                 claimed the job updates it)
  progress.log   one line per processed image, appended by the worker
  index.html     the permanent results page, written by the worker on
                 completion (success or failure report)

Job directories are permanent; archive_phot_prune.sh deletes old ones on
the operator's request. A failed job is terminal: nothing in the queue ever
re-runs it -- re-measuring the same target requires a new request (which is
deliberately allowed, e.g. after the archive has been updated).

This module must stay import-safe: no CGI work at import time.
"""

import concurrent.futures
import datetime
import fcntl
import json
import math
import os
import random
import re
import string
import subprocess
import sys
import time

import urllib.parse

import nmw_coord_lib as ncl
from nmw_coord_lib import field_name_from_fits, html_escape, _PAGE_CSS
from nmw_forced_phot_lib import _looks_like_fits, get_jd_and_atel_date

TEMP_PARENT = 'uploads'                 # mirrors upload.py's upload_dir
JOB_DIR_PREFIX = 'archive_phot_'
JOB_ID_RE = re.compile(r'^\d{8}-\d{6}_[A-Za-z]{8}$')

STATE_QUEUED = 'queued'
STATE_RUNNING = 'running'
STATE_DONE = 'done'
STATE_FAILED = 'failed'

# Path of the input form as served under htdocs (see move_to_htdocs/).
# Stored links on permanent results pages are built from this constant --
# never from the client-supplied Host header, which must not end up in
# pages shown to OTHER users.
FORM_PAGE_PATH = '/unmw/archive_forced_photometry.html'

# Worker single-instance control: a slot pool of flock files (the
# nmw_coord_lib.acquire_concurrency_slot pattern). ARCHIVE_PHOT_MAX_RUNNING
# slots = that many concurrently running jobs, each in its own worker
# process with its own disposable VaST working copy.
#
# Unlike the coord pages' locks, these do NOT live in /tmp: everything
# that mutates the queue (both CGIs and the worker) runs as the web-server
# user, and a lock file created in world-writable /tmp by a different user
# (say, an operator testing from a shell) would be unopenable for the
# web-server user forever after -- acquire would see every slot as "busy"
# while the probe would see them as "free", silently wedging the queue.
# Keeping the locks in a dot-directory under uploads/ (same data root, one
# owner) removes that failure mode.
WORKER_SLOT_PREFIX = 'archive_phot_worker'
SUBMIT_SLOT_PREFIX = 'archive_phot_submit'
# Short-lived lock serializing queue-state transitions: the claim of the
# oldest queued job, the dead-worker sweep, and the submit CGI's final
# recheck-and-create step.
CLAIM_LOCK_BASENAME = 'archive_phot_claim.lock'
LOCK_SUBDIR = '.archive_phot_locks'
WORKER_SCRIPT_BASENAME = 'archive_phot_worker.py'
WORKER_LOG_BASENAME = 'archive_phot_worker.log'  # appended under uploads/

PROGRESS_LOG_BASENAME = 'progress.log'

# A running job whose recorded worker pid has been dead for longer than
# this is declared failed by the sweep (grace period against observing a
# claim mid-flight).
DEAD_PID_GRACE_SECONDS = 60

# Queue protection defaults; the local_config.sh variables
# ARCHIVE_PHOT_MAX_QUEUE / ARCHIVE_PHOT_MAX_QUEUE_PER_IP /
# ARCHIVE_PHOT_MAX_RUNNING override them.
DEFAULT_MAX_QUEUE = 10
DEFAULT_MAX_QUEUE_PER_IP = 5
DEFAULT_MAX_RUNNING = 2
MAX_RUNNING_HARD_CAP = 8

# Two submissions target "the same position" when they are closer than this.
DUPLICATE_MATCH_ARCSEC = 1.0

# util/get_image_date subprocesses run in parallel when reading observation
# dates out of many FITS headers (submission and update checks).
GET_IMAGE_DATE_WORKERS = 8

# Subdirectories of IMAGE_ARCHIVE_DIR that hold rejected frames and must
# never be measured.
ARCHIVE_EXCLUDED_DIR_NAMES = ('bad',)

ARCHIVE_IMAGE_PREFIX = 'wcs_fd_'
DATE_INPUT_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')

# Cap on how many newest job directories the dedup / prior-job scans read.
# Job dirs are permanent, so an unbounded scan would slow down as history
# accumulates; anything older than the newest 200 jobs is irrelevant to
# "is this a duplicate?" and "when was this position last measured?".
RECENT_JOBS_SCAN_LIMIT = 200


# ---------- job ids and directories ----------

def new_job_id():
    rand = ''.join(random.choice(string.ascii_letters) for _ in range(8))
    return time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '_' + rand


def is_valid_job_id(job_id):
    """True when job_id has the expected shape. Used by the CGIs to reject
    anything that could smuggle path components before the id is ever
    joined into a filesystem path."""
    return bool(JOB_ID_RE.match(job_id or ''))


def job_dir_path(job_id):
    return os.path.join(TEMP_PARENT, JOB_DIR_PREFIX + job_id)


# ---------- small JSON file helpers ----------

def read_json_file(path):
    """Parsed JSON content or None on any error (missing, unreadable,
    truncated mid-write is impossible thanks to write_json_atomic, but a
    manual edit could still leave junk)."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json_atomic(path, obj):
    """Write JSON via a temp file + os.replace so readers never observe a
    partially written file."""
    tmp = '{}.tmp{}'.format(path, os.getpid())
    with open(tmp, 'w') as fh:
        json.dump(obj, fh, indent=1)
        fh.write('\n')
    os.replace(tmp, path)


def read_state(job_dir):
    return read_json_file(os.path.join(job_dir, 'state.json'))


def read_request(job_dir):
    return read_json_file(os.path.join(job_dir, 'request.json'))


def update_state(job_dir, **fields):
    """Merge fields into the job's state.json. Single-writer discipline
    (only the worker owning the job calls this after creation) makes the
    read-modify-write safe without a lock."""
    path = os.path.join(job_dir, 'state.json')
    state = read_json_file(path) or {}
    state.update(fields)
    write_json_atomic(path, state)
    return state


# ---------- queue listing ----------

def list_job_dirs():
    """All job directories as (job_id, job_dir) tuples, newest first.
    Entries whose name does not match the expected job-id shape are
    ignored (they are not ours)."""
    try:
        entries = os.listdir(TEMP_PARENT)
    except OSError:
        return []
    out = []
    for name in entries:
        if not name.startswith(JOB_DIR_PREFIX):
            continue
        job_id = name[len(JOB_DIR_PREFIX):]
        if not is_valid_job_id(job_id):
            continue
        d = os.path.join(TEMP_PARENT, name)
        if os.path.isdir(d):
            out.append((job_id, d))
    out.sort(reverse=True)
    return out


def list_pending_jobs():
    """Queued and running jobs as (job_id, job_dir, state, request) tuples,
    oldest first (queue order). Jobs with unreadable metadata are skipped."""
    out = []
    for job_id, d in list_job_dirs():
        st = read_state(d)
        if not st or st.get('state') not in (STATE_QUEUED, STATE_RUNNING):
            continue
        rq = read_request(d)
        if rq is None:
            continue
        out.append((job_id, d, st, rq))
    out.sort(key=lambda t: t[0])
    return out


def queue_position(job_id):
    """1-based position of job_id among queued jobs (oldest = 1), or None
    if the job is not currently queued."""
    queued = [jid for jid, _d, st, _rq in list_pending_jobs()
              if st.get('state') == STATE_QUEUED]
    try:
        return queued.index(job_id) + 1
    except ValueError:
        return None


# ---------- coordinates ----------

def coord_token_to_degrees(token, is_ra):
    """Convert one parse_coordinates() output token (decimal degrees or
    H:M:S / [+-]D:M:S sexagesimal) to decimal degrees. Raises ValueError
    on unexpected shapes, mirroring parse_coordinates behaviour."""
    token = (token or '').strip()
    if ':' not in token:
        return float(token)
    parts = token.split(':')
    if len(parts) != 3:
        raise ValueError('invalid sexagesimal token: {!r}'.format(token))
    head = parts[0]
    sign = 1.0
    if head.startswith('-'):
        sign = -1.0
        head = head[1:]
    elif head.startswith('+'):
        head = head[1:]
    value = float(head) + float(parts[1]) / 60.0 + float(parts[2]) / 3600.0
    if is_ra:
        value = value * 15.0
    return sign * value


def angular_separation_arcsec(ra1_deg, dec1_deg, ra2_deg, dec2_deg):
    """Angular separation in arcseconds (haversine formula, numerically
    stable at the ~1 arcsec separations the duplicate check cares about)."""
    ra1 = math.radians(ra1_deg)
    dec1 = math.radians(dec1_deg)
    ra2 = math.radians(ra2_deg)
    dec2 = math.radians(dec2_deg)
    sin_ddec = math.sin((dec2 - dec1) / 2.0)
    sin_dra = math.sin((ra2 - ra1) / 2.0)
    a = sin_ddec * sin_ddec + \
        math.cos(dec1) * math.cos(dec2) * sin_dra * sin_dra
    a = min(1.0, max(0.0, a))
    return math.degrees(2.0 * math.asin(math.sqrt(a))) * 3600.0


# ---------- observation dates ----------

def date_string_to_jd(date_str, end_of_day=False):
    """'YYYY-MM-DD' (interpreted as a UT calendar date) -> JD at 00:00 UT,
    or at 24:00 UT when end_of_day is set. Raises ValueError on bad input.

    Image timestamps embedded in filenames are never used for time
    filtering or ordering -- their timezone is not knowable. All
    comparisons happen in JD as reported by util/get_image_date from the
    FITS headers."""
    m = DATE_INPUT_RE.match((date_str or '').strip())
    if not m:
        raise ValueError('date must be YYYY-MM-DD: {!r}'.format(date_str))
    d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    jd = d.toordinal() + 1721424.5
    return jd + 1.0 if end_of_day else jd


def get_jd_batch(vast_dir, paths, max_workers=GET_IMAGE_DATE_WORKERS,
                 progress_callback=None):
    """Read the observation date of every FITS file in paths by running
    util/get_image_date (via get_jd_and_atel_date) in a thread pool.

    Returns {path: jd_float_or_None}; None means the date could not be
    read (missing/corrupted header). progress_callback, when given, is
    called as (n_done, n_total) after each file; its exceptions are
    swallowed so a progress-UI glitch cannot fail the batch."""
    results = {}
    if not paths:
        return results
    workers = max(1, min(len(paths), max_workers))
    n_total = len(paths)
    n_done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(get_jd_and_atel_date, vast_dir, p): p
                   for p in paths}
        for fut in concurrent.futures.as_completed(futures):
            path = futures[fut]
            try:
                jd_str, _atel = fut.result()
            except Exception:
                jd_str = None
            jd = None
            if jd_str is not None:
                try:
                    jd = float(jd_str)
                except (TypeError, ValueError):
                    jd = None
            results[path] = jd
            n_done += 1
            if progress_callback is not None:
                try:
                    progress_callback(n_done, n_total)
                except Exception:
                    pass
    return results


# ---------- archive image discovery ----------

def discover_archive_images(archive_dir, covering_fields):
    """Absolute paths of every wcs_fd_ FITS image under archive_dir whose
    field name is in covering_fields, sorted by path.

    os.walk is used (rather than a flat listdir) so the current flat
    layout and a future layout with subdirectories (e.g. one per month)
    both work unchanged. Subdirectories named in
    ARCHIVE_EXCLUDED_DIR_NAMES (rejected frames) and hidden entries are
    pruned from the walk."""
    out = []
    for root, dirnames, filenames in os.walk(archive_dir):
        dirnames[:] = [d for d in dirnames
                       if d not in ARCHIVE_EXCLUDED_DIR_NAMES
                       and not d.startswith('.')]
        for fname in filenames:
            if not fname.startswith(ARCHIVE_IMAGE_PREFIX):
                continue
            if not _looks_like_fits(fname):
                continue
            if field_name_from_fits(fname) not in covering_fields:
                continue
            out.append(os.path.abspath(os.path.join(root, fname)))
    out.sort()
    return out


# ---------- duplicate and prior-job lookups ----------

def _request_matches_position(rq, ra_deg, dec_deg):
    try:
        sep = angular_separation_arcsec(
            ra_deg, dec_deg, float(rq['ra_deg']), float(rq['dec_deg']))
    except (KeyError, TypeError, ValueError):
        return False
    return sep <= DUPLICATE_MATCH_ARCSEC


def find_duplicate_pending(pending, ra_deg, dec_deg, band_override,
                           date_from, date_to):
    """job_id of a queued/running job identical to the given request
    (position within DUPLICATE_MATCH_ARCSEC, same band override, same date
    range), or None. Completed and failed jobs never count as duplicates:
    resubmitting a finished target is the designed way to re-measure it
    after an archive update, and the only way to retry a failure."""
    for job_id, _d, _st, rq in pending:
        if (rq.get('band_override') or '') != (band_override or ''):
            continue
        if (rq.get('date_from') or '') != (date_from or ''):
            continue
        if (rq.get('date_to') or '') != (date_to or ''):
            continue
        if _request_matches_position(rq, ra_deg, dec_deg):
            return job_id
    return None


def find_prior_jobs(ra_deg, dec_deg, band_override=None, date_from=None,
                    date_to=None, limit=5):
    """Completed/failed jobs at the same position, newest first, as
    (job_id, job_dir, state, request) tuples. When band_override /
    date_from / date_to are given (not None) only jobs with those exact
    parameters match. Scans at most RECENT_JOBS_SCAN_LIMIT newest jobs."""
    out = []
    for job_id, d in list_job_dirs()[:RECENT_JOBS_SCAN_LIMIT]:
        st = read_state(d)
        if not st or st.get('state') not in (STATE_DONE, STATE_FAILED):
            continue
        rq = read_request(d)
        if rq is None:
            continue
        if band_override is not None and \
                (rq.get('band_override') or '') != band_override:
            continue
        if date_from is not None and (rq.get('date_from') or '') != date_from:
            continue
        if date_to is not None and (rq.get('date_to') or '') != date_to:
            continue
        if not _request_matches_position(rq, ra_deg, dec_deg):
            continue
        out.append((job_id, d, st, rq))
        if len(out) >= limit:
            break
    return out


# ---------- job creation ----------

def create_job(request_dict):
    """Atomically create the job directory: everything is written under a
    hidden temporary name first, then os.rename publishes it, so a worker
    listing uploads/ never sees a half-written job. Returns
    (job_id, job_dir). Raises OSError on filesystem trouble."""
    job_id = new_job_id()
    final_dir = job_dir_path(job_id)
    tmp_dir = os.path.join(TEMP_PARENT,
                           '.{}tmp_{}'.format(JOB_DIR_PREFIX, job_id))
    os.makedirs(tmp_dir, mode=0o755)
    request_dict = dict(request_dict)
    request_dict['job_id'] = job_id
    write_json_atomic(os.path.join(tmp_dir, 'request.json'), request_dict)
    write_json_atomic(os.path.join(tmp_dir, 'state.json'), {
        'state': STATE_QUEUED,
        'pid': None,
        'submitted': request_dict.get('submit_time_utc', ''),
        'n_total': len(request_dict.get('images', [])),
        'n_done': 0,
    })
    os.rename(tmp_dir, final_dir)
    return job_id, final_dir


# ---------- worker kick ----------

def parse_positive_int(raw, default):
    """Config-value helper: int(raw) when it parses to >= 1, else default."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def archive_lock_dir():
    """Directory holding the queue's flock files (worker slots, submit
    slots, claim lock), created on first use. Lives under uploads/ so that
    every lock file is owned by the web-server user (see the comment at
    WORKER_SLOT_PREFIX). Falls back to the shared /tmp LOCK_DIR when the
    directory cannot be created -- degraded but functional."""
    path = os.path.abspath(os.path.join(TEMP_PARENT, LOCK_SUBDIR))
    try:
        os.makedirs(path, mode=0o755, exist_ok=True)
    except OSError:
        return ncl.LOCK_DIR
    return path


def worker_slots_locked(max_running):
    """Number of worker slot flocks currently held (= live worker
    processes). Probing takes and immediately releases each free slot;
    that cannot disturb a real worker, which holds its slot for life."""
    locked = 0
    lock_dir = archive_lock_dir()
    for i in range(1, max_running + 1):
        path = os.path.join(lock_dir,
                            '{}_slot_{}.lock'.format(WORKER_SLOT_PREFIX, i))
        try:
            fh = open(path, 'w')
        except OSError:
            continue
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            locked += 1
        fh.close()  # closing releases the probe lock if we took it
    return locked


def acquire_claim_lock():
    """Blocking flock serializing queue-state transitions (job claim, dead
    worker sweep, submit-time recheck-and-create). The critical sections
    are a few small file reads/writes, so blocking is fine. Returns the
    open file object; closing it releases the lock. Raises OSError when
    the lock file cannot be created/opened -- callers decide whether that
    is fatal (worker) or degrades gracefully (CGIs)."""
    path = os.path.join(archive_lock_dir(), CLAIM_LOCK_BASENAME)
    fh = open(path, 'w')
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


# ---------- dead-worker sweep ----------

def pid_alive(pid):
    """Best-effort 'is this pid still our worker?'. A plain kill(pid, 0)
    survives pid reuse (any process, or EPERM from another user's
    process, would look alive), so when /proc is available the process
    name is verified too: our workers always have archive_phot_worker in
    their command line. Without /proc (non-Linux), an existing pid is
    conservatively treated as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, TypeError, ValueError):
        pass  # EPERM etc.: some process exists; identity checked below
    try:
        with open('/proc/{}/cmdline'.format(pid), 'rb') as fh:
            cmdline = fh.read()
    except OSError:
        # No /proc entry readable: cannot verify identity, assume alive
        # rather than falsely failing a live job.
        return True
    return WORKER_SCRIPT_BASENAME.encode() in cmdline


def sweep_dead_running_jobs(log=None):
    """Mark running jobs whose worker process is gone as FAILED (terminal;
    the queue never retries them) and write their failure results page.

    Called under the claim lock from the worker before each claim, and
    directly by the CGIs (which take the claim lock here) so that a job
    orphaned by a crash or reboot is healed by the next page view or
    submission even when nothing is queued -- otherwise a zombie
    'running' job would permanently block identical resubmission via the
    duplicate check. Returns the number of jobs swept; never raises."""
    try:
        lock = acquire_claim_lock()
    except OSError:
        return 0
    try:
        return _sweep_dead_running_jobs_locked(log)
    finally:
        lock.close()


def _sweep_dead_running_jobs_locked(log=None):
    """The sweep body; caller must hold the claim lock."""
    now = time.time()
    n_swept = 0
    for job_id, job_dir, st, rq in list_pending_jobs():
        if st.get('state') != STATE_RUNNING:
            continue
        pid = st.get('pid')
        if pid == os.getpid():
            continue
        try:
            started = float(st.get('started_epoch') or 0)
        except (TypeError, ValueError):
            started = 0
        if now - started < DEAD_PID_GRACE_SECONDS:
            continue
        if isinstance(pid, int) and pid_alive(pid):
            continue
        error = ('The worker process (pid {}) that was measuring this job '
                 'died without finishing (server restart or crash). The '
                 'job is marked failed and will not be retried '
                 'automatically; submit the same request again to '
                 're-measure.'.format(pid))
        message = ('sweep: marking orphaned running job {} as failed '
                   '(dead pid {})'.format(job_id, pid))
        if log is not None:
            try:
                log(message)
            except Exception:
                pass
        else:
            sys.stderr.write(message + '\n')
        update_state(job_dir, state=STATE_FAILED, error=error,
                     finished=time.strftime('%Y-%m-%d %H:%M:%S UTC',
                                            time.gmtime()),
                     finished_epoch=now, stage='')
        write_failure_page(job_dir, job_id, rq, error)
        n_swept += 1
    return n_swept


# ---------- results-page building blocks ----------
# Shared by the worker (success and failure pages) and by the sweep above
# (failure pages written from a CGI process).

RESULTS_PAGE_LOCAL_CSS = ("<style type='text/css'>"
                          "tr.skipped td { color: #888; font-size: 90%; "
                          "background: #f8f8f8; }"
                          " p.secondary { color: #666; font-style: italic; }"
                          "</style>")


def results_page_head(title):
    return ("<html><head><title>{}</title>\n{}\n{}\n</head><body>\n"
            "<h2>{}</h2>\n".format(html_escape(title), _PAGE_CSS,
                                   RESULTS_PAGE_LOCAL_CSS,
                                   html_escape(title)))


def job_summary_html(job_id, request):
    range_text = ''
    if request.get('date_from') or request.get('date_to'):
        range_text = "; UT date range {} .. {}".format(
            html_escape(request.get('date_from') or '(archive start)'),
            html_escape(request.get('date_to') or '(archive end)'))
    return ("<p>Job <span class='code'>{}</span>: position "
            "<span class='code'>{} {}</span>; band: <span class='code'>{}"
            "</span>{}; submitted {} from {}.</p>".format(
                html_escape(job_id),
                html_escape(request.get('ra', '?')),
                html_escape(request.get('dec', '?')),
                html_escape(request.get('band_override') or 'auto'),
                range_text,
                html_escape(request.get('submit_time_utc', '?')),
                html_escape(request.get('remote_addr', '?'))))


def job_links_html(job_id, request):
    """Footer links shared by the success and failure results pages:
    archive update check, prefilled resubmission, queue overview. All
    host-relative: cgi_base comes from SCRIPT_NAME recorded at submit time
    (server-controlled) and the form path is the FORM_PAGE_PATH constant,
    NEVER a URL derived from the client-supplied Host header."""
    cgi_base = request.get('cgi_base') or '/cgi-bin/unmw'
    status_url = '{}/archive_phot_status.py?job={}'.format(cgi_base, job_id)
    check_url = status_url + '&check_updates=1'
    overview_url = '{}/archive_phot_status.py'.format(cgi_base)
    params = {}
    for key, form_key in (('coords_raw', 'coords'), ('band_override', 'band'),
                          ('date_from', 'date_from'), ('date_to', 'date_to')):
        value = request.get(key)
        if value:
            params[form_key] = value
    resubmit_url = '{}?{}'.format(FORM_PAGE_PATH,
                                  urllib.parse.urlencode(params))
    return ("<p><a href='{check}'>Check whether the archive gained new "
            "images for this job</a> &middot; "
            "<a href='{resubmit}'>Re-run this request</a> &middot; "
            "<a href='{overview}'>Queue overview</a></p>".format(
                check=html_escape(check_url),
                resubmit=html_escape(resubmit_url),
                overview=html_escape(overview_url)))


def write_index_html_atomic(job_dir, html_text):
    tmp = os.path.join(job_dir, '.index.html.tmp{}'.format(os.getpid()))
    with open(tmp, 'w') as fh:
        fh.write(html_text)
    os.replace(tmp, os.path.join(job_dir, 'index.html'))


def progress_tail_text(job_dir, max_lines=30):
    try:
        with open(os.path.join(job_dir, PROGRESS_LOG_BASENAME)) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ''
    return '\n'.join(lines[-max_lines:])


def write_failure_page(job_dir, job_id, request, error_text):
    """Permanent results page for a failed job: the failure report. Never
    raises (called from exception handlers and the sweep)."""
    try:
        parts = [results_page_head('Archival forced photometry: job failed')]
        parts.append(job_summary_html(job_id, request or {}))
        parts.append("<div class='notice'>This job FAILED: {}</div>".format(
            html_escape(error_text)))
        parts.append("<p>Failed jobs are never retried automatically. To "
                     "try again, submit the same request from the input "
                     "form (the 'Re-run this request' link below has it "
                     "prefilled).</p>")
        tail = progress_tail_text(job_dir)
        if tail:
            parts.append("<p class='secondary'>Progress before the "
                         "failure:</p><pre>{}</pre>".format(
                             html_escape(tail)))
        parts.append(job_links_html(job_id, request or {}))
        parts.append("</body></html>\n")
        write_index_html_atomic(job_dir, '\n'.join(parts))
    except OSError:
        pass


def kick_worker(script_dir):
    """Spawn the queue worker as a detached background process (the same
    fork-and-forget approach wrapper.sh uses for autoprocess.sh). Always
    safe to call: a spawned worker that finds no free slot or no queued
    job exits immediately, so duplicate kicks are cheap.

    GATEWAY_INTERFACE and the other CGI request markers are stripped from
    the child environment -- the worker refuses to run when they are
    present (defense against being executed as a CGI), and the kicking
    process IS a CGI, so the inherited environment would otherwise trip
    that check.

    stdout/stderr go to uploads/archive_phot_worker.log (append).
    Best-effort: never raises."""
    worker = os.path.join(script_dir, WORKER_SCRIPT_BASENAME)
    log_path = os.path.join(script_dir, TEMP_PARENT, WORKER_LOG_BASENAME)
    env = dict(os.environ)
    for var in ('GATEWAY_INTERFACE', 'REQUEST_METHOD', 'QUERY_STRING',
                'CONTENT_LENGTH', 'CONTENT_TYPE'):
        env.pop(var, None)
    log_fh = None
    try:
        log_fh = open(log_path, 'ab')
    except OSError:
        pass
    try:
        subprocess.Popen(
            [sys.executable, worker], cwd=script_dir, env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh if log_fh is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh is not None
            else subprocess.DEVNULL,
            start_new_session=True)
    except OSError:
        pass
    finally:
        if log_fh is not None:
            # The child holds its own duplicate of the fd; closing ours is
            # safe and required to avoid leaking it in the CGI process.
            log_fh.close()

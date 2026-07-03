#!/usr/bin/env python3
"""Queue worker for archival forced photometry. NOT a CGI.

Spawned (fork-and-forget) by archive_forced_photometry.py after a job is
enqueued and by archive_phot_status.py when it notices queued jobs with no
live worker. Any number of kicks is safe: each worker instance first tries
to take one of the ARCHIVE_PHOT_MAX_RUNNING flock slots and exits silently
when none is free, so duplicate kicks cost one short-lived process.

A worker that holds a slot loops: claim the oldest queued job (under a
short flock so two workers never claim the same job), measure every image
in the job's frozen list with the exact engine coord_forced_photometry.py
uses (nmw_forced_phot_lib), write the persistent results page
uploads/archive_phot_<id>/index.html, and repeat until the queue is empty.

Failure policy: FAILED IS TERMINAL. A job that raises, or whose worker
process died (detected by the dead-pid sweep before each claim), is marked
failed, gets a failure-report results page, and is never retried by the
queue. Re-measuring the same target requires a fresh submission -- this is
deliberate, so a job that cannot complete can never wedge the queue in a
retry loop.

Load friendliness: before starting each job the worker waits (bounded) for
the 1-minute load average to drop below ARCHIVE_PHOT_MAX_SYSTEM_LOAD (or
MAX_SYSTEM_LOAD when the former is unset; no gating when neither is set),
and re-checks briefly every few images, so long archive jobs yield to the
live transient pipeline.

Defense against being run as a CGI: .htaccess denies web access to this
file, and additionally the worker refuses to start when GATEWAY_INTERFACE
is present in the environment (kick_worker strips it from the environment
of a legitimately spawned worker)."""

import os
import shutil
import sys
import time
import traceback

from nmw_coord_lib import (
    read_config_vars, acquire_concurrency_slot, get_image_metadata,
    make_zoomout_thumbnail, make_zoomin_thumbnail, field_name_from_fits,
    html_escape, HIRES_THUMBNAIL_MULTIPLIER,
)
from nmw_forced_phot_lib import (
    FORCED_PHOT_PARALLEL_SOLVE_WORKERS, DEFAULT_THUMBNAIL_PIXELS,
    MIN_THUMBNAIL_PIXELS, MAX_THUMBNAIL_PIXELS, DEFAULT_ZOOMIN_PIXELS,
    _read_factory_text, derive_band, derive_sextractor_config,
    get_jd_and_atel_date, _phase1_parallel_solve_plate,
    run_forced_photometry_c, setup_vast_working_copy,
    _write_lightcurve_data_files, _render_lightcurve_png, ascii_table,
    fits_url, _is_float, _fmt_mag, _fmt_err, _fmt_duration, _html_row,
    _html_skipped_row,
)
import nmw_archive_phot_lib as apl

WORK_DIR_PREFIX = 'vast_archive_phot_'

# Worker-slot acquisition retries. A kick that lands while the previous
# worker is on its way out (queue-empty check done, slot not yet released)
# would see the slot busy on the first try; a short retry window closes
# that race so the freshly enqueued job is not stranded until the next
# page view.
SLOT_ACQUIRE_ATTEMPTS = 4
SLOT_ACQUIRE_RETRY_SECONDS = 5

# Load gating: bounded waits, then proceed no matter what (mirrors the
# autoprocess.sh philosophy -- shape the load, never deadlock on it).
LOAD_POLL_SECONDS = 60
LOAD_MAX_WAIT_BEFORE_JOB_SECONDS = 3600
LOAD_CHECK_EVERY_N_IMAGES = 10
LOAD_MAX_WAIT_MID_JOB_SECONDS = 300



def _utc_now_str():
    return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())


def _log(msg):
    """Worker-level log line (lands in uploads/archive_phot_worker.log)."""
    print('[{}] [pid {}] {}'.format(_utc_now_str(), os.getpid(), msg),
          flush=True)


def _load_1min():
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def _load_threshold(cfg):
    """Configured load threshold as float, or None (no gating)."""
    for var in ('ARCHIVE_PHOT_MAX_SYSTEM_LOAD', 'MAX_SYSTEM_LOAD'):
        raw = (cfg.get(var) or '').strip()
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    return None


def _wait_for_low_load(threshold, max_wait_seconds, progress=None):
    """Sleep in LOAD_POLL_SECONDS steps while the 1-minute load average is
    above threshold, for at most max_wait_seconds, then proceed anyway."""
    if threshold is None:
        return
    waited = 0
    while waited < max_wait_seconds:
        load = _load_1min()
        if load is None or load < threshold:
            return
        if progress is not None:
            progress('waiting for system load {:.1f} to drop below {:.1f} '
                     '({} s waited)'.format(load, threshold, waited))
        time.sleep(LOAD_POLL_SECONDS)
        waited += LOAD_POLL_SECONDS


# ---------- claim (serialized by the claim flock) ----------

def _claim_next_job():
    """Atomically claim the oldest queued job: transition it to running
    with this worker's pid. The dead-worker sweep runs first under the
    same lock. Returns (job_id, job_dir, request) or None."""
    try:
        lock = apl.acquire_claim_lock()
    except OSError as exc:
        _log('ERROR: cannot open the claim lock: {} -- exiting'.format(exc))
        return None
    try:
        apl._sweep_dead_running_jobs_locked(log=_log)
        for job_id, job_dir, st, rq in apl.list_pending_jobs():
            if st.get('state') != apl.STATE_QUEUED:
                continue
            apl.update_state(job_dir, state=apl.STATE_RUNNING,
                             pid=os.getpid(), started=_utc_now_str(),
                             started_epoch=time.time(),
                             stage='starting')
            return (job_id, job_dir, rq)
        return None
    finally:
        lock.close()


# ---------- per-job processing ----------

def _thumb_pixels_from_cfg(cfg):
    raw = (cfg.get('COORD_SEARCH_THUMBNAIL_PIXELS') or '').strip()
    try:
        value = int(raw) if raw else DEFAULT_THUMBNAIL_PIXELS
    except ValueError:
        value = DEFAULT_THUMBNAIL_PIXELS
    if value < MIN_THUMBNAIL_PIXELS or value > MAX_THUMBNAIL_PIXELS:
        value = DEFAULT_THUMBNAIL_PIXELS
    return value


def _zoomin_pixels_from_cfg(cfg):
    raw = (cfg.get('COORD_FORCED_PHOT_ZOOMIN_PIXELS') or '').strip()
    try:
        value = int(raw) if raw else DEFAULT_ZOOMIN_PIXELS
    except ValueError:
        value = DEFAULT_ZOOMIN_PIXELS
    if value < 5:
        value = DEFAULT_ZOOMIN_PIXELS
    return value


def process_job(job_id, job_dir, request, cfg, local_config_path,
                load_threshold):
    """Measure every image of one claimed job and write its persistent
    results page. Raises on unrecoverable trouble (caller marks the job
    failed); per-image problems only produce skipped rows."""
    start_time = time.time()
    vast_dir = cfg['VAST_REFERENCE_COPY'].strip()
    url_prefix = cfg['URL_OF_DATA_PROCESSING_ROOT'].strip().rstrip('/')
    archive_dir = cfg['IMAGE_ARCHIVE_DIR'].strip()
    archive_url = (cfg.get('IMAGE_ARCHIVE_URL') or '').strip().rstrip('/')
    thumb_pixels = _thumb_pixels_from_cfg(cfg)
    zoomin_pixels = _zoomin_pixels_from_cfg(cfg)
    hires_pixels = min(MAX_THUMBNAIL_PIXELS,
                       thumb_pixels * HIRES_THUMBNAIL_MULTIPLIER)
    archive_dir_abs = os.path.abspath(archive_dir) if archive_dir else ''

    ra = request['ra']
    dec = request['dec']
    band_override = request.get('band_override') or ''
    images = [entry['path'] for entry in request.get('images', [])]
    sub_name = os.path.basename(job_dir)

    progress_path = os.path.join(job_dir, apl.PROGRESS_LOG_BASENAME)
    progress_fh = open(progress_path, 'a')

    def progress(msg):
        progress_fh.write('[{}] {}\n'.format(_utc_now_str(), msg))
        progress_fh.flush()

    work_dir = None
    try:
        progress('job claimed by worker pid {}'.format(os.getpid()))
        apl.update_state(job_dir, stage='waiting for low system load')
        _wait_for_low_load(load_threshold, LOAD_MAX_WAIT_BEFORE_JOB_SECONDS,
                           progress=progress)

        apl.update_state(job_dir, stage='preparing VaST working copy')
        progress('preparing the disposable VaST working copy')
        work_dir = setup_vast_working_copy(vast_dir, apl.TEMP_PARENT,
                                           prefix=WORK_DIR_PREFIX)
        if work_dir is None:
            raise RuntimeError('could not set up the VaST working copy '
                               '(rsync of {} failed)'.format(vast_dir))

        skip_log = os.path.join(job_dir, 'forced_phot_skipped.log')
        n_images = len(images)
        phase1_workers = min(n_images or 1, os.cpu_count() or 4,
                             FORCED_PHOT_PARALLEL_SOLVE_WORKERS)

        apl.update_state(job_dir, stage='plate-solving', n_done=0,
                         n_total=n_images)
        progress('plate-solving and photometric catalog-matching {} '
                 'image(s) with {} parallel workers'.format(
                     n_images, phase1_workers))

        def _solve_progress(done, total, fits_path, rc):
            status = 'solved' if rc == 0 else 'failed (rc={})'.format(rc)
            progress('solve {}/{} {}: {}'.format(
                done, total, status, os.path.basename(fits_path)))

        n_solved, sextractor_cache_hits, n_funpacked, compute_path_map, \
            phase1_elapsed = _phase1_parallel_solve_plate(
                work_dir, local_config_path, images, phase1_workers,
                skip_log, progress_callback=_solve_progress)

        factory_text = _read_factory_text(vast_dir)
        work_dir_default_sex = os.path.join(work_dir, 'default.sex')
        results = []
        rows_html = []
        n_skipped = 0

        apl.update_state(job_dir, stage='measuring')
        for idx, img in enumerate(images, start=1):
            if load_threshold is not None and \
                    idx % LOAD_CHECK_EVERY_N_IMAGES == 0:
                _wait_for_low_load(load_threshold,
                                   LOAD_MAX_WAIT_MID_JOB_SECONDS,
                                   progress=progress)
            band = derive_band(factory_text, img, band_override)
            sex_config_name = derive_sextractor_config(factory_text, img)
            if sex_config_name:
                src_sex = os.path.join(work_dir, sex_config_name)
                if os.path.isfile(src_sex):
                    try:
                        # copy2 keeps the rsync-era mtime; see the matching
                        # comment in coord_forced_photometry.py main().
                        shutil.copy2(src_sex, work_dir_default_sex)
                    except OSError:
                        pass
            file_fits_url = ''
            if archive_url:
                file_fits_url = fits_url(archive_url, img, archive_dir_abs)
            compute_path = compute_path_map.get(img)
            fp = None
            if compute_path is not None:
                fp = run_forced_photometry_c(
                    work_dir, local_config_path, img, compute_path, ra, dec,
                    band, debug_log=skip_log)
            if fp is None:
                rows_html.append(_html_skipped_row(
                    img, field_name_from_fits(img), file_fits_url))
                n_skipped += 1
                progress('skipped {}/{}: {}'.format(
                    idx, n_images, os.path.basename(img)))
                apl.update_state(job_dir, n_done=idx)
                continue
            fp['basename'] = os.path.basename(img)
            jd, atel = get_jd_and_atel_date(vast_dir, img)
            if jd is None:
                jd = '{:.4f}'.format(float(fp['jd'])) \
                    if _is_float(fp['jd']) else fp['jd']
            if atel is None:
                atel = '-'
            meta = get_image_metadata(img, vast_dir)
            nx = meta.get('nx') if meta else None
            ny = meta.get('ny') if meta else None
            png_preview = None
            png_preview_hires = None
            if nx and ny:
                png_preview = make_zoomout_thumbnail(
                    img, fp['x'], fp['y'], nx, ny, job_dir, vast_dir,
                    thumb_pixels)
                png_preview_hires = make_zoomout_thumbnail(
                    img, fp['x'], fp['y'], nx, ny, job_dir, vast_dir,
                    hires_pixels, suffix='zoomout_hires')
            png_cutout = make_zoomin_thumbnail(
                img, fp['x'], fp['y'], job_dir, vast_dir, thumb_pixels,
                zoomin_pixels, aperture_circle_diameter=fp['aperture'])
            png_cutout_hires = make_zoomin_thumbnail(
                img, fp['x'], fp['y'], job_dir, vast_dir, hires_pixels,
                zoomin_pixels, suffix='zoomin_hires',
                aperture_circle_diameter=fp['aperture'])
            r = {
                'jd': jd, 'atel': atel,
                'mag': _fmt_mag(fp['mag'], fp['status']),
                'err': _fmt_err(fp['err']),
                'status': fp['status'], 'band': band,
                'field': field_name_from_fits(img),
                'basename': fp['basename'],
                'fits_url': file_fits_url,
                'png_preview': png_preview,
                'png_preview_hires': png_preview_hires,
                'png_cutout': png_cutout,
                'png_cutout_hires': png_cutout_hires,
            }
            results.append(r)
            rows_html.append(_html_row(r, url_prefix, sub_name))
            progress('measured {}/{}: {} {} mag={} ({})'.format(
                idx, n_images, os.path.basename(img), band, r['mag'],
                r['status']))
            apl.update_state(job_dir, n_done=idx)

        # ---- Lightcurve files and plot (into the job dir, which
        # outlives the disposable VaST working copy). ----
        apl.update_state(job_dir, stage='rendering results page')
        lightcurve_html = []
        if results:
            lc_path, ul_path = _write_lightcurve_data_files(job_dir,
                                                            results)
            if lc_path is not None:
                png_basename = _render_lightcurve_png(
                    work_dir, job_dir, ra, dec, lc_path, ul_path)
                if png_basename is not None:
                    lightcurve_html.append(
                        "<p style='text-align: center;'>"
                        "<img src='{}/{}/{}' alt='Lightcurve plot' "
                        "style='max-width: 100%;'></p>".format(
                            html_escape(url_prefix), html_escape(sub_name),
                            html_escape(png_basename)))
                links = ["<a href='{}/{}/{}'>{}</a> (detections)".format(
                    html_escape(url_prefix), html_escape(sub_name),
                    html_escape(os.path.basename(lc_path)),
                    html_escape(os.path.basename(lc_path)))]
                if ul_path is not None:
                    links.append(
                        "<a href='{}/{}/{}'>{}</a> (upper limits)".format(
                            html_escape(url_prefix), html_escape(sub_name),
                            html_escape(os.path.basename(ul_path)),
                            html_escape(os.path.basename(ul_path))))
                lightcurve_html.append(
                    "<p class='secondary' style='text-align: center;'>"
                    "Data files: {}</p>".format(', '.join(links)))

        # ---- The persistent results page. ----
        elapsed = time.time() - start_time
        parts = [apl.results_page_head('Archival forced photometry results')]
        parts.append(apl.job_summary_html(job_id, request))
        parts.append("<p>{} of {} image(s) yielded a measurement; {} "
                     "skipped (target off-frame or calibration failed; "
                     "see <a href='{}/{}/forced_phot_skipped.log'>the skip "
                     "log</a>).</p>".format(
                         len(results), n_images, n_skipped,
                         html_escape(url_prefix), html_escape(sub_name)))
        if request.get('n_date_failed'):
            parts.append("<p class='secondary'>{} archive image(s) were "
                         "excluded at submission time because their "
                         "observation date could not be read.</p>".format(
                             request['n_date_failed']))
        if rows_html:
            parts.append("<table class='main'>")
            parts.append("<tr><th>Date (UTC)</th><th>JD (UTC)</th>"
                         "<th>mag</th><th>err</th><th>Status</th>"
                         "<th>Band</th><th>Field</th><th>Cutout</th>"
                         "<th>Image</th></tr>")
            parts.extend(rows_html)
            parts.append("</table>")
        parts.extend(lightcurve_html)
        if results:
            parts.append("<h3>Photometry table</h3>")
            parts.append("<pre>{}</pre>".format(
                html_escape(ascii_table(results))))
        else:
            parts.append("<div class='notice'>None of the {} image(s) "
                         "yielded a measurement (target off-frame or "
                         "calibration failed).</div>".format(n_images))
        parts.append("<p class='secondary'>Total computation time: {}"
                     "{}.</p>".format(
                         _fmt_duration(elapsed),
                         " (average {} per image over {} processed)".format(
                             _fmt_duration(elapsed / n_images), n_images)
                         if n_images else ''))
        parts.append("<p class='secondary'>UCAC5 plate-solve: {} of {} "
                     "image(s) solved in parallel in {} (workers: {}); "
                     "SExtractor catalogs reused: {}; funpacked: {}.</p>"
                     .format(n_solved, n_images,
                             _fmt_duration(phase1_elapsed), phase1_workers,
                             sextractor_cache_hits, n_funpacked))
        parts.append("<p class='secondary'>Measured on {}.</p>".format(
            html_escape(_utc_now_str())))
        parts.append("<p class='secondary'>Please download the data you "
                     "want to keep (the photometry table and the "
                     "lightcurve data files): this results page stays at "
                     "this URL, but may eventually be deleted to free up "
                     "server disk space.</p>")
        parts.append(apl.job_links_html(job_id, request))
        parts.append("</body></html>\n")
        apl.write_index_html_atomic(job_dir, '\n'.join(parts))

        apl.update_state(job_dir, state=apl.STATE_DONE, stage='',
                         finished=_utc_now_str(),
                         finished_epoch=time.time(),
                         n_measured=len(results), n_skipped=n_skipped)
        progress('done: {} measured, {} skipped, {} total, {}'.format(
            len(results), n_skipped, n_images, _fmt_duration(elapsed)))
        return len(results), n_skipped
    finally:
        progress_fh.close()
        if work_dir is not None:
            if os.environ.get('DEBUG_KEEP_WORK_DIR'):
                _log('DEBUG: keeping work_dir {}'.format(work_dir))
            else:
                shutil.rmtree(work_dir, ignore_errors=True)


def main():
    if os.environ.get('GATEWAY_INTERFACE'):
        # Looks like a web request. .htaccess should already deny this
        # file; refuse regardless.
        print("Content-Type: text/plain\n")
        print("This program is not a CGI.")
        return 1

    script_dir = os.path.dirname(os.path.realpath(__file__))
    os.chdir(script_dir)
    local_config_path = os.path.join(script_dir, 'local_config.sh')

    cfg = read_config_vars(
        'VAST_REFERENCE_COPY', 'URL_OF_DATA_PROCESSING_ROOT',
        'IMAGE_ARCHIVE_DIR', 'IMAGE_ARCHIVE_URL',
        'COORD_SEARCH_THUMBNAIL_PIXELS', 'COORD_FORCED_PHOT_ZOOMIN_PIXELS',
        'ARCHIVE_PHOT_MAX_RUNNING', 'ARCHIVE_PHOT_MAX_SYSTEM_LOAD',
        'MAX_SYSTEM_LOAD')
    vast_dir = cfg['VAST_REFERENCE_COPY'].strip()
    if not vast_dir or not os.path.isdir(vast_dir):
        _log('ERROR: VAST_REFERENCE_COPY is not set or not a directory; '
             'cannot process jobs')
        return 1
    if not cfg['URL_OF_DATA_PROCESSING_ROOT'].strip():
        _log('ERROR: URL_OF_DATA_PROCESSING_ROOT is not set; '
             'cannot process jobs')
        return 1
    max_running = min(apl.MAX_RUNNING_HARD_CAP,
                      apl.parse_positive_int(cfg['ARCHIVE_PHOT_MAX_RUNNING'],
                                             apl.DEFAULT_MAX_RUNNING))
    load_threshold = _load_threshold(cfg)

    slot = None
    for attempt in range(SLOT_ACQUIRE_ATTEMPTS):
        slot = acquire_concurrency_slot(prefix=apl.WORKER_SLOT_PREFIX,
                                        max_concurrent=max_running,
                                        lock_dir=apl.archive_lock_dir())
        if slot is not None:
            break
        if attempt < SLOT_ACQUIRE_ATTEMPTS - 1:
            time.sleep(SLOT_ACQUIRE_RETRY_SECONDS)
    if slot is None:
        # Enough workers are already running; this kick is redundant.
        _log('all {} worker slot(s) busy; exiting'.format(max_running))
        return 0

    _log('worker started (slot pool size {}, load threshold {})'.format(
        max_running, load_threshold))
    try:
        while True:
            claimed = _claim_next_job()
            if claimed is None:
                break
            job_id, job_dir, request = claimed
            _log('processing job {}'.format(job_id))
            try:
                n_measured, n_skipped = process_job(
                    job_id, job_dir, request, cfg, local_config_path,
                    load_threshold)
                _log('job {} done: {} measured, {} skipped'.format(
                    job_id, n_measured, n_skipped))
            except Exception as exc:
                _log('job {} FAILED: {}\n{}'.format(
                    job_id, exc, traceback.format_exc()))
                error = '{}: {}'.format(type(exc).__name__, exc)
                try:
                    apl.update_state(job_dir, state=apl.STATE_FAILED,
                                     error=error, stage='',
                                     finished=_utc_now_str(),
                                     finished_epoch=time.time())
                    apl.write_failure_page(job_dir, job_id, request, error)
                except Exception:
                    _log('job {}: could not record the failure: {}'.format(
                        job_id, traceback.format_exc()))
        _log('queue empty, exiting')
    finally:
        slot.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

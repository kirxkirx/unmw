#!/usr/bin/env python3
"""Source monitoring updater - command-line entry points.

Modes (see source_monitoring_design.md):
  --prepare <img_dir>
      Pre-factory prep called by autoprocess.sh: decide which ACTIVATED
      monitored sources fall on the field being processed and write the
      positions file for the factory's monitoring block. Prints the
      positions file path on stdout (empty output = nothing to do).
      Silent no-op when uploads/monitoring/ does not exist - its existence
      is the per-machine "monitoring is deployed here" switch.
  --ingest <raw_measurements_file>
      Post-factory ingest called (detached) by autoprocess.sh on SUCCESSFUL
      runs: append the factory's hand-off rows to the per-source ledgers
      and rebuild the derived products. Pure text processing + plots; no
      VaST working copy.
  --reconcile
      Manual activation + one-time backfill of new monitoring_list.txt
      entries (archive pass + ALL uploads/img_* recent pass). Resumable:
      sources without the backfill_done marker are re-enumerated and the
      ledger basename dedup skips already-measured images.
  --rescan-recent [<source_id>|--all]
  --rescan-archive [<source_id>|--all]
      Manual gap fillers over the recent / archive image populations for
      already-activated sources. No window or count caps.
  --rebuild-pages
      Re-render all products (HTML pages, plots, ASCII + AAVSO files) for
      every activated source from their existing ledgers, without measuring
      anything. Run this to apply changes to the page/plot templates to
      already-generated pages (--reconcile only re-renders sources that
      gained new measurements).

All manual modes refuse to run in a CGI environment, take the global
monitoring lock and EXIT (never queue) when another instance holds it.
Monitoring measurements always run with FORCED_PHOTOMETRY_AIRMASS_ZEROPOINT=yes.
"""

import os
import shutil
import subprocess
import sys

import nmw_coord_lib as ncl
import nmw_monitoring_lib as nml


def log(message):
    sys.stderr.write('monitoring: {}\n'.format(message))
    sys.stderr.flush()


def load_context():
    """chdir to the script directory (local_config.sh lives there, and the
    'uploads' symlink is relative to it) and read the configuration."""
    script_dir = os.path.dirname(os.path.realpath(__file__))
    os.chdir(script_dir)
    cfg = ncl.read_config_vars(
        'IMAGE_DATA_ROOT', 'VAST_REFERENCE_COPY', 'IMAGE_ARCHIVE_DIR',
        'REFERENCE_IMAGES', 'URL_OF_DATA_PROCESSING_ROOT', 'AAVSO_OBSCODE')
    uploads_dir = (cfg.get('IMAGE_DATA_ROOT') or '').strip() or 'uploads'
    local_config_path = os.path.join(script_dir, 'local_config.sh')
    return script_dir, cfg, uploads_dir, local_config_path


def read_factory_text(cfg):
    from nmw_forced_phot_lib import _read_factory_text
    vast_dir = (cfg.get('VAST_REFERENCE_COPY') or '').strip()
    if not vast_dir:
        return ''
    return _read_factory_text(vast_dir) or ''


def sky2xy_on_image(vast_dir, fits_path, ra, dec):
    """One sky2xy call: (x, y) pixel coordinates or None when the position
    is off the image or the call fails."""
    sky2xy = os.path.join(vast_dir, 'lib', 'bin', 'sky2xy')
    try:
        result = subprocess.run([sky2xy, fits_path, ra, dec],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or '') + (result.stderr or '')
    if 'off image' in output or 'offscale' in output:
        return None
    tokens = output.split()
    if len(tokens) < 2:
        return None
    try:
        return float(tokens[-2]), float(tokens[-1])
    except ValueError:
        return None


def list_entries_or_exit():
    list_path = nml.monitoring_list_path()
    if not list_path:
        log('monitoring_list.txt not found in the calibration directory')
        return None, []
    entries, problems = nml.parse_monitoring_list(list_path)
    for problem in problems:
        log('{}: {}'.format(os.path.basename(list_path), problem))
    return list_path, entries


# ---------- --prepare ----------

def mode_prepare(img_dir):
    script_dir, cfg, uploads_dir, _ = load_context()
    if not os.path.isdir(nml.monitoring_root(uploads_dir)):
        return 0  # monitoring not deployed on this machine - stay silent
    vast_dir = (cfg.get('VAST_REFERENCE_COPY') or '').strip()
    ref_dir = (cfg.get('REFERENCE_IMAGES') or '').strip()
    if not vast_dir or not ref_dir:
        log('--prepare: VAST_REFERENCE_COPY or REFERENCE_IMAGES not '
            'configured')
        return 0
    list_path, entries = list_entries_or_exit()
    if not entries:
        return 0
    activated = [e for e in entries if os.path.isdir(
        nml.source_dir_path(uploads_dir, e['source_id']))]
    n_pending = len(entries) - len(activated)
    if n_pending:
        log('{} source(s) in monitoring_list.txt are not activated on this '
            'machine - run monitoring_update.py --reconcile'.format(
                n_pending))
    if not activated:
        return 0
    # The run's field(s) from the unpacked image basenames
    fields = set()
    try:
        for basename in os.listdir(img_dir):
            if nml.looks_like_fits(basename):
                fields.add(ncl.field_name_from_fits(basename))
    except OSError as exc:
        log('--prepare: cannot list {}: {}'.format(img_dir, exc))
        return 0
    fields.discard('')
    if not fields:
        return 0
    # One solved reference image per field for the on-field test
    ref_images = []
    try:
        ref_names = sorted(os.listdir(ref_dir))
    except OSError as exc:
        log('--prepare: cannot list reference images: {}'.format(exc))
        return 0
    for field in sorted(fields):
        for name in ref_names:
            if ncl.field_name_from_fits(name) == field \
                    and nml.looks_like_fits(name):
                ref_images.append(os.path.join(ref_dir, name))
                break
    if not ref_images:
        log('--prepare: no reference image found for field(s) {}'.format(
            ' '.join(sorted(fields))))
        return 0
    positions_lines = []
    for entry in activated:
        for ref_image in ref_images:
            if sky2xy_on_image(vast_dir, ref_image, entry['ra'],
                               entry['dec']) is not None:
                positions_lines.append('{} {} {}'.format(
                    entry['ra'], entry['dec'], entry['source_id']))
                break
    if not positions_lines:
        return 0
    positions_path = os.path.join(os.path.abspath(img_dir),
                                  'monitoring_positions.txt')
    with open(positions_path, 'w') as fh:
        fh.write('\n'.join(positions_lines) + '\n')
    log('--prepare: {} monitored source(s) on field(s) {}'.format(
        len(positions_lines), ' '.join(sorted(fields))))
    print(positions_path)
    return 0


# ---------- --ingest ----------

def mode_ingest(raw_path):
    script_dir, cfg, uploads_dir, _ = load_context()
    if not os.path.isdir(nml.monitoring_root(uploads_dir)):
        return 0
    try:
        with open(raw_path) as fh:
            raw_lines = fh.read().splitlines()
    except OSError as exc:
        log('--ingest: cannot read {}: {}'.format(raw_path, exc))
        return 1
    rows_by_source = {}
    for line in raw_lines:
        parts = line.split()
        if len(parts) < 7:
            continue
        source_id, basename, jd, mag, err, status, camera = parts[:7]
        rows_by_source.setdefault(source_id, []).append(
            {'basename': basename, 'jd': jd, 'mag': mag, 'err': err,
             'status': status, 'camera': camera})
    if not rows_by_source:
        log('--ingest: no measurement rows in {}'.format(raw_path))
        return 0
    list_path, entries = list_entries_or_exit()
    # If the list is temporarily unreadable (NFS blip, env mismatch in the
    # detached ingest) do NOT proceed: every row would be dropped as "no
    # longer in the list" and rebuild_central_index(...[]) would overwrite the
    # central page with "no sources activated". Bail and let the next run - or
    # a manual --rescan-recent - recover the measurements.
    if list_path is None or not entries:
        log('--ingest: monitoring_list.txt is missing or empty; not '
            'ingesting {} (measurements preserved in the raw file)'.format(
                raw_path))
        return 1
    entries_by_id = {e['source_id']: e for e in entries}
    factory_text = read_factory_text(cfg)
    n_total = 0
    for source_id, rows in sorted(rows_by_source.items()):
        source_dir = nml.source_dir_path(uploads_dir, source_id)
        if not os.path.isdir(source_dir):
            log('--ingest: skipping {} rows for unknown source {}'.format(
                len(rows), source_id))
            continue
        entry = entries_by_id.get(source_id)
        if entry is None:
            # Source no longer in the list: keep it frozen (no-retirement
            # decision) - do not append
            log('--ingest: {} is no longer in monitoring_list.txt - '
                'not appending'.format(source_id))
            continue
        n_added = nml.append_ledger_rows(uploads_dir, source_id, rows)
        n_total += n_added
        log('--ingest: {}: {} new row(s), {} duplicate(s) skipped'.format(
            source_id, n_added, len(rows) - n_added))
        nml.rebuild_source_products(uploads_dir, entry, cfg, factory_text)
    nml.rebuild_central_index(uploads_dir, entries,
                              (cfg.get('VAST_REFERENCE_COPY') or '').strip())
    log('--ingest: done, {} row(s) appended'.format(n_total))
    return 0


# ---------- measurement machinery for the manual modes ----------

def measure_images_for_source(cfg, local_config_path, entry, images,
                              uploads_dir):
    """Measure one source on a list of already-solved images inside a
    disposable VaST working copy (the manual backfill/rescan path), appending
    ledger rows in chunks so an interrupted run keeps its progress."""
    from nmw_forced_phot_lib import (
        setup_vast_working_copy, _phase1_parallel_solve_plate,
        run_forced_photometry_c, derive_band, derive_sextractor_config,
        camera_settings_for_path, get_jd_and_atel_date)
    vast_dir = (cfg.get('VAST_REFERENCE_COPY') or '').strip()
    factory_text = read_factory_text(cfg)
    source_id = entry['source_id']
    source_dir = nml.source_dir_path(uploads_dir, source_id)
    _, already_measured = nml.read_ledger(source_dir)
    # Dedup by ledger key while building the todo list: the archive and recent
    # passes overlap for a recently-archived observation that is still in
    # uploads/ (archive/wcs_fd_X.fts.fz and uploads/img_*/wcs_fd_X.fts share a
    # ledger key), and without this the same image would be funpacked,
    # plate-solved and measured twice - only the append would dedup it.
    todo = []
    todo_seen = set()
    for img in images:
        key = nml.ledger_key(os.path.basename(img))
        if key in already_measured or key in todo_seen:
            continue
        todo_seen.add(key)
        todo.append(img)
    log('{}: {} image(s) to measure ({} already in the ledger or '
        'duplicate)'.format(source_id, len(todo), len(images) - len(todo)))
    if not todo:
        return 0
    work_dir = setup_vast_working_copy(vast_dir, 'uploads',
                                       prefix='vast_monitoring_')
    if work_dir is None:
        log('{}: could not set up the VaST working copy'.format(source_id))
        return 0
    n_appended = 0
    try:
        skip_log = os.path.join(source_dir, 'measurement_skipped.log')
        workers = min(len(todo) or 1, os.cpu_count() or 4, 4)
        log('{}: plate-solve/catalog pass with {} worker(s)'.format(
            source_id, workers))
        _, _, _, compute_path_map, _ = _phase1_parallel_solve_plate(
            work_dir, local_config_path, todo, workers, skip_log)
        work_dir_default_sex = os.path.join(work_dir, 'default.sex')
        pending_rows = []
        for idx, img in enumerate(todo, start=1):
            band = derive_band(factory_text, img, '')
            sex_config_name = derive_sextractor_config(factory_text, img)
            if sex_config_name:
                src_sex = os.path.join(work_dir, sex_config_name)
                if os.path.isfile(src_sex):
                    try:
                        shutil.copy2(src_sex, work_dir_default_sex)
                    except OSError:
                        pass
            camera = camera_settings_for_path(factory_text, img) or 'unknown'
            compute_path = compute_path_map.get(img)
            fp = None
            if compute_path is not None:
                fp = run_forced_photometry_c(
                    work_dir, local_config_path, img, compute_path,
                    entry['ra'], entry['dec'], band, debug_log=skip_log)
            if fp is None:
                # A None result means the measurement failed for a reason we
                # cannot classify here: a failed plate solve, a forced-photometry
                # error, or - importantly - a transient UCAC5/APASS/VizieR
                # network failure or timeout. Do NOT write a permanent ledger
                # row: a 'fail' row would be dedup-permanent and no rescan would
                # ever retry it, so one remote-service blip during a backfill
                # would truncate the lightcurve forever. Leaving the image out
                # of the ledger lets the next --reconcile / --rescan retry it.
                # (Genuinely off-frame positions come back as a normal 'edge'
                # dict and are recorded through the else branch below.)
                log('{}: {}/{} not measured (will retry on next rescan): '
                    '{}'.format(source_id, idx, len(todo),
                                os.path.basename(img)))
            else:
                jd = fp.get('jd')
                if not jd:
                    jd, _atel = get_jd_and_atel_date(vast_dir, img)
                pending_rows.append(
                    {'basename': os.path.basename(img),
                     'jd': '{}'.format(jd if jd else 'na'),
                     'mag': '{}'.format(fp.get('mag', '99.0000')),
                     'err': '{}'.format(fp.get('err', '99.0000')),
                     'status': '{}'.format(fp.get('status', 'fail')),
                     'camera': camera})
                log('{}: {}/{} {} {} {} {}'.format(
                    source_id, idx, len(todo), os.path.basename(img),
                    pending_rows[-1]['mag'], pending_rows[-1]['err'],
                    pending_rows[-1]['status']))
            if len(pending_rows) >= 10:
                n_appended += nml.append_ledger_rows(uploads_dir, source_id,
                                                     pending_rows)
                pending_rows = []
        if pending_rows:
            n_appended += nml.append_ledger_rows(uploads_dir, source_id,
                                                 pending_rows)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    return n_appended


def covering_fields_for_entry(cfg, entry):
    ref_dir = (cfg.get('REFERENCE_IMAGES') or '').strip()
    vast_dir = (cfg.get('VAST_REFERENCE_COPY') or '').strip()
    if not ref_dir or not vast_dir:
        return set()
    matches, truncated = ncl.run_sky2xy_scan(ref_dir, entry['ra'],
                                             entry['dec'], vast_dir)
    if truncated:
        log('{}: WARNING reference image scan timed out - the covering '
            'field list may be incomplete'.format(entry['source_id']))
    fields = set()
    for path, _x, _y in matches:
        field = ncl.field_name_from_fits(path)
        if field:
            fields.add(field)
    return fields


def enumerate_archive_images(cfg, covering_fields):
    import nmw_archive_phot_lib as apl
    archive_dir = (cfg.get('IMAGE_ARCHIVE_DIR') or '').strip()
    if not archive_dir or not os.path.isdir(archive_dir):
        log('archival pass skipped: IMAGE_ARCHIVE_DIR is not configured or '
            'does not exist on this machine')
        return []
    images = apl.discover_archive_images(archive_dir, covering_fields)
    if not images:
        log('archival pass: no archive images for field(s) {}'.format(
            ' '.join(sorted(covering_fields)) or '(none)'))
    return images


# ---------- --reconcile / --rescan ----------

def mode_reconcile():
    return _run_manual_mode('reconcile', None)


def mode_rescan(population, source_selector):
    return _run_manual_mode(population, source_selector)


def mode_rebuild_pages():
    """Re-derive all products (pages, plots, ASCII files, AAVSO) for every
    activated source from their existing ledgers, without measuring anything.
    Use this to apply changes to the HTML/plot templates to already-generated
    pages: --reconcile only rebuilds sources that gained new measurements, so
    a template change would otherwise not reach up-to-date sources."""
    script_dir, cfg, uploads_dir, local_config_path = load_context()
    if not os.path.isdir(nml.monitoring_root(uploads_dir)):
        log('monitoring is not deployed on this machine (no {} directory)'
            .format(nml.monitoring_root(uploads_dir)))
        return 1
    lock_fh = nml.acquire_global_lock(uploads_dir)
    if lock_fh is None:
        log('another monitoring reconcile/rescan is already running - '
            'exiting (NOT queuing)')
        return 1
    try:
        _, entries = list_entries_or_exit()
        factory_text = read_factory_text(cfg)
        vast_dir = (cfg.get('VAST_REFERENCE_COPY') or '').strip()
        n = 0
        for entry in entries:
            if not os.path.isdir(nml.source_dir_path(uploads_dir,
                                                     entry['source_id'])):
                continue
            nml.rebuild_source_products(uploads_dir, entry, cfg, factory_text)
            n += 1
            log('{}: products rebuilt'.format(entry['source_id']))
        nml.rebuild_central_index(uploads_dir, entries, vast_dir)
        log('rebuilt {} source page(s) + the central index'.format(n))
        return 0
    finally:
        lock_fh.close()


def _run_manual_mode(mode, source_selector):
    script_dir, cfg, uploads_dir, local_config_path = load_context()
    list_path, entries = list_entries_or_exit()
    if not entries:
        log('nothing to do: the monitoring list is missing or empty')
        return 1
    root = nml.monitoring_root(uploads_dir)
    if mode == 'reconcile':
        os.makedirs(root, mode=0o755, exist_ok=True)
    elif not os.path.isdir(root):
        log('monitoring is not deployed on this machine (no {} directory); '
            'run --reconcile first'.format(root))
        return 1
    lock_fh = nml.acquire_global_lock(uploads_dir)
    if lock_fh is None:
        log('another monitoring reconcile/rescan is already running - '
            'exiting (NOT queuing)')
        return 1
    factory_text = read_factory_text(cfg)
    try:
        if mode == 'reconcile':
            selected = entries
        else:
            if source_selector == '--all':
                selected = [e for e in entries if os.path.isdir(
                    nml.source_dir_path(uploads_dir, e['source_id']))]
            else:
                selected = [e for e in entries
                            if e['source_id'] == source_selector]
                if not selected:
                    log('source id "{}" is not in monitoring_list.txt'.format(
                        source_selector))
                    return 1
                if not os.path.isdir(nml.source_dir_path(
                        uploads_dir, selected[0]['source_id'])):
                    log('source "{}" is not activated - run --reconcile '
                        'first'.format(source_selector))
                    return 1
        for entry in selected:
            source_id = entry['source_id']
            source_dir = nml.source_dir_path(uploads_dir, source_id)
            marker = os.path.join(source_dir, nml.BACKFILL_MARKER_BASENAME)
            if mode == 'reconcile':
                if os.path.isdir(source_dir) and os.path.exists(marker):
                    # Already activated and backfilled: perform an INCREMENTAL
                    # sweep - images that migrated into the archive (or new
                    # uploads processed while monitoring was down) since the
                    # last run are measured; everything already in the ledger
                    # is skipped by the basename dedup, so this costs only a
                    # directory walk when nothing is new.
                    log('{}: already activated - checking for new '
                        'images'.format(source_id))
                else:
                    os.makedirs(source_dir, mode=0o755, exist_ok=True)
                    log('{}: activating ({} {} "{}")'.format(
                        source_id, entry['ra'], entry['dec'], entry['name']))
            covering_fields = covering_fields_for_entry(cfg, entry)
            log('{}: covering field(s): {}'.format(
                source_id, ' '.join(sorted(covering_fields)) or '(none)'))
            images = []
            if covering_fields:
                if mode in ('reconcile', 'rescan-archive'):
                    images.extend(enumerate_archive_images(cfg,
                                                           covering_fields))
                if mode in ('reconcile', 'rescan-recent'):
                    images.extend(nml.list_all_recent_field_images(
                        uploads_dir, covering_fields))
            n_new = 0
            if images:
                n_new = measure_images_for_source(cfg, local_config_path,
                                                  entry, images, uploads_dir)
            if mode == 'reconcile':
                with open(marker, 'w'):
                    pass
            if n_new > 0 or not os.path.exists(
                    os.path.join(source_dir, 'index.html')):
                nml.rebuild_source_products(uploads_dir, entry, cfg,
                                            factory_text)
                log('{}: {} new measurement(s), products rebuilt'.format(
                    source_id, n_new))
            else:
                log('{}: up to date'.format(source_id))
        nml.rebuild_central_index(uploads_dir, entries,
                              (cfg.get('VAST_REFERENCE_COPY') or '').strip())
        log('central index rebuilt')
        return 0
    finally:
        lock_fh.close()


# ---------- entry point ----------

def main(argv):
    if os.environ.get('GATEWAY_INTERFACE'):
        sys.stderr.write('monitoring_update.py refuses to run as a CGI\n')
        return 1
    # Monitoring measurements always use the airmass-aware zero-point
    os.environ['FORCED_PHOTOMETRY_AIRMASS_ZEROPOINT'] = 'yes'
    if len(argv) >= 3 and argv[1] == '--prepare':
        return mode_prepare(argv[2])
    if len(argv) >= 3 and argv[1] == '--ingest':
        return mode_ingest(argv[2])
    if len(argv) >= 2 and argv[1] == '--reconcile':
        return mode_reconcile()
    if len(argv) >= 3 and argv[1] == '--rescan-recent':
        return mode_rescan('rescan-recent', argv[2])
    if len(argv) >= 3 and argv[1] == '--rescan-archive':
        return mode_rescan('rescan-archive', argv[2])
    if len(argv) >= 2 and argv[1] == '--rebuild-pages':
        return mode_rebuild_pages()
    sys.stderr.write(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))

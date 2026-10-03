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
  --frame-quality <raw_measurements_file> <vast_working_dir>
      Frame quality (cloud) check called synchronously by autoprocess.sh
      right before --ingest, while the VaST working copy (holding the
      frame and reference-frame .wcscat catalogs) is still alive. Rewrites
      the status of measurements from cloud-affected frames to 'cloudy' in
      the raw file; the ingest then records them in the ledger (so the
      frames are never re-measured) but they are excluded from the
      published products. Never blocks the ingest: any trouble is logged
      and the raw file is left untouched. New uploads only: the manual
      modes never run the check, they reuse the verdicts it left in the
      ledgers (see below).
  --ingest <raw_measurements_file>
      Post-factory ingest called synchronously by autoprocess.sh on
      SUCCESSFUL runs: append the factory's hand-off rows to the
      per-source ledgers and rebuild the derived products. A row replaces
      the run_error row an earlier rejected run left for the same image.
      Pure text processing + plots; no VaST working copy.
  --ingest-rejected <raw_measurements_file>
      The same for a run whose transient search raised a processing ERROR
      or failed: every row except the off-frame 'edge' ones is recorded
      with the status run_error (the measured values kept), so the images
      are in the ledgers as processed and rejected - never published and
      never measured by a manual mode. Existing rows are never replaced.
  --reconcile
      Manual activation + one-time backfill of new monitoring_list.txt
      entries (archive pass + ALL uploads/img_* recent pass + quarantine
      pass). Resumable: sources without the backfill_done marker are
      re-enumerated and the ledger basename dedup skips already-measured
      images.
  --rescan-recent [<source_id>|--all]
  --rescan-archive [<source_id>|--all]
      Manual gap fillers over the recent (uploads + quarantine) / archive
      image populations for already-activated sources. No window or count
      caps.
  The manual modes honour the verdict of the transient-search run that
  processed each image (transient_search_verdicts.txt written by
  autoprocess.sh, or the run's report for older images): the images of a
  rejected run get run_error ledger rows without being measured (no row
  when the position is not on the image), the images of a run still in
  progress are left for the next rescan. They also honour the verdict of
  the ingest's cloud check, kept in the ledgers: a frame with a 'cloudy'
  row in any source's ledger is measured and a detection or upper limit on
  it is recorded as 'cloudy' (the values kept), as the ingest does. The
  images are processed camera by camera with the bad-region list the
  transient factory uses for the camera (BAD_REGION_FILE of the camera
  block) in place for the plate-solve pass and the measurement.
  --rebuild-pages
      Re-render all products (HTML pages, plots, ASCII + AAVSO files) for
      every activated source from their existing ledgers, without measuring
      anything. Run this to apply changes to the page/plot templates to
      already-generated pages (--reconcile only re-renders sources that
      gained new measurements).
  --exclude-measurement <image_basename> [--source <source_id>]
      Manually exclude the measurement(s) coming from one image: flip the
      matching ledger row to the 'manual' status so the point moves from
      the published lightcurve/plot/AAVSO file to the "excluded by the
      quality checks" table (reason manual_exclusion). By default every
      activated source whose ledger contains the image is updated (a bad
      frame is bad for all sources on it); --source restricts to one.
      The image is matched by basename with the trailing .fz ignored.
      The ledger row keeps the original magnitude, so the exclusion is
      reversible; the row also keeps deduplicating rescans, so the image
      is never re-measured. Note that a full registry wipe + reconcile
      re-measures everything and forgets manual exclusions.
  --restore-measurement <image_basename> [--source <source_id>]
      Undo --exclude-measurement: manually excluded rows go back to
      'detection' (or 'upperlimit' for fainter-than rows) and the point is
      published again on the rebuilt products.
  --set-detection-threshold <source> <mag>
      Set a manual per-source detection threshold: detections FAINTER than
      <mag> are published as upper limits at the measured magnitude (use
      case: the photometric aperture is contaminated by an unrelated
      nearby source, so only a target outshining the contaminant yields a
      true detection). Stored machine-locally in the source's registry
      directory (detection_threshold.txt) - thresholds are camera-specific
      and do not belong in the shared monitoring list. Applied at product
      rebuild, so it is retroactive and fully reversible; the measurement
      ledger is untouched. <source> accepts the source_id, the full name,
      or the short AAVSO name (the part before ' - '/' = ', e.g.
      "AT 2026xyz" for "AT 2026xyz - Nova in Cyg 2026").
  --clear-detection-threshold <source>
      Remove the manual threshold and rebuild the products.

All manual modes refuse to run in a CGI environment, take the global
monitoring lock and EXIT (never queue) when another instance holds it.
Monitoring measurements always run with FORCED_PHOTOMETRY_AIRMASS_ZEROPOINT=yes.
"""

import os
import shutil
import subprocess
import sys
import time

import nmw_coord_lib as ncl
import nmw_monitoring_lib as nml


def log(message):
    sys.stderr.write('monitoring: {} {}\n'.format(
        time.strftime('%Y-%m-%d %H:%M:%S'), message))
    sys.stderr.flush()


def _rescan_worker_cap(cfg=None):
    """Parallel worker cap for the plate-solve/catalog pass of the manual
    backfill/rescan modes. Resolution order: the MONITORING_RESCAN_WORKERS
    environment variable (explicit per-run override), then the same-name
    local_config.sh setting, then 4. Keep an eye on RAM (each worker
    SExtracts a full frame) and on the transient pipeline: autoprocess
    defers uploads when the system load is high, so a wide reconcile at
    night delays transient processing."""
    for raw in (os.environ.get('MONITORING_RESCAN_WORKERS'),
                (cfg or {}).get('MONITORING_RESCAN_WORKERS')):
        try:
            cap = int(raw)
        except (TypeError, ValueError):
            continue
        if cap >= 1:
            return cap
    return 4


def _edge_margin_pix(cfg=None):
    """Minimum distance (pixels) from a frame edge for a monitored position
    to be measured by the backfill/rescan modes; closer positions get a
    terminal 'edge' ledger row (util/forced_photometry applies the rule
    through FORCED_PHOTOMETRY_EDGE_MARGIN_PIX). Resolution order as for
    _rescan_worker_cap: the MONITORING_EDGE_MARGIN_PIX environment variable,
    then the same-name local_config.sh setting, then 100 - the default the
    transient factory uses for the per-upload measurements."""
    for raw in (os.environ.get('MONITORING_EDGE_MARGIN_PIX'),
                (cfg or {}).get('MONITORING_EDGE_MARGIN_PIX')):
        try:
            margin = float(raw)
        except (TypeError, ValueError):
            continue
        if margin >= 0.0:
            return margin
    return 100.0


def load_context():
    """chdir to the script directory (local_config.sh lives there, and the
    'uploads' symlink is relative to it) and read the configuration."""
    script_dir = os.path.dirname(os.path.realpath(__file__))
    os.chdir(script_dir)
    cfg = ncl.read_config_vars(
        'IMAGE_DATA_ROOT', 'VAST_REFERENCE_COPY', 'IMAGE_ARCHIVE_DIR',
        'IMAGE_QUARANTINE_DIR', 'REFERENCE_IMAGES',
        'URL_OF_DATA_PROCESSING_ROOT', 'AAVSO_OBSCODE',
        'MONITORING_RESCAN_WORKERS', 'MONITORING_PAGE_MESSAGE',
        'MONITORING_EDGE_MARGIN_PIX')
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
    is off the image or the call fails.

    The raw sky2xy verdict is verified with the geometric coverage check
    (nmw_coord_lib._image_covers_position): the inverse SIP distortion
    polynomial, evaluated for a position far outside the frame, can fold
    that position onto valid-looking pixel coordinates - GK Per got fake
    monitoring upper limits from Lac-01 frames 50 deg away because the
    --prepare coverage test here trusted sky2xy alone. A None verdict from
    the verification (tooling failure) keeps the match (fail open)."""
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
        x, y = float(tokens[-2]), float(tokens[-1])
    except ValueError:
        return None
    try:
        ra_deg, dec_deg = ncl._hms_to_deg(ra, dec)
    except (ValueError, TypeError):
        return (x, y)
    if ncl._image_covers_position(fits_path, ra_deg, dec_deg,
                                  vast_dir) is False:
        return None
    return (x, y)


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


# ---------- --frame-quality ----------

def mode_frame_quality(raw_path, work_dir):
    """Cloud check for the frames of one upload; see the module docstring.
    Always returns 0: a failed check must never block the ingest."""
    import nmw_frame_quality_lib as nfq
    try:
        n_cloudy = nfq.check_and_mark_raw_file(raw_path, work_dir, log)
        if n_cloudy:
            log('--frame-quality: {} frame(s) marked {}'.format(
                n_cloudy, nfq.CLOUDY_STATUS))
    except Exception as exc:
        log('--frame-quality: check failed ({}); raw file left '
            'untouched'.format(exc))
    return 0


# ---------- --ingest ----------

def mode_ingest(raw_path, rejected=False):
    """--ingest (rejected=False): record the rows of a successful run; a row
    replaces the run_error row an earlier rejected run left for the same
    image (nml.SUPERSEDABLE_STATUSES). --ingest-rejected (rejected=True):
    the run raised a processing ERROR or failed, so every row except the
    'edge' ones (the position is not on the frame) is recorded with
    nml.RUN_ERROR_STATUS, keeping the measured values; the images are then
    in the ledger as processed-and-rejected and no manual run measures them
    again. A rejected run never replaces an existing ledger row."""
    mode_name = '--ingest-rejected' if rejected else '--ingest'
    script_dir, cfg, uploads_dir, _ = load_context()
    if not os.path.isdir(nml.monitoring_root(uploads_dir)):
        return 0
    try:
        with open(raw_path) as fh:
            raw_lines = fh.read().splitlines()
    except OSError as exc:
        log('{}: cannot read {}: {}'.format(mode_name, raw_path, exc))
        return 1
    rows_by_source = {}
    for line in raw_lines:
        parts = line.split()
        if len(parts) < 7:
            continue
        source_id, basename, jd, mag, err, status, camera = parts[:7]
        if rejected and status != 'edge':
            status = nml.RUN_ERROR_STATUS
        rows_by_source.setdefault(source_id, []).append(
            {'basename': basename, 'jd': jd, 'mag': mag, 'err': err,
             'status': status, 'camera': camera})
    if not rows_by_source:
        log('{}: no measurement rows in {}'.format(mode_name, raw_path))
        return 0
    list_path, entries = list_entries_or_exit()
    # If the list is temporarily unreadable (NFS blip, env mismatch in the
    # detached ingest) do NOT proceed: every row would be dropped as "no
    # longer in the list" and rebuild_central_index(...[]) would overwrite the
    # central page with "no sources activated". Bail and let the next run - or
    # a manual --rescan-recent - recover the measurements.
    if list_path is None or not entries:
        log('{}: monitoring_list.txt is missing or empty; not '
            'ingesting {} (measurements preserved in the raw file)'.format(
                mode_name, raw_path))
        return 1
    entries_by_id = {e['source_id']: e for e in entries}
    factory_text = read_factory_text(cfg)
    n_total = 0
    for source_id, rows in sorted(rows_by_source.items()):
        source_dir = nml.source_dir_path(uploads_dir, source_id)
        if not os.path.isdir(source_dir):
            log('{}: skipping {} rows for unknown source {}'.format(
                mode_name, len(rows), source_id))
            continue
        entry = entries_by_id.get(source_id)
        if entry is None:
            # Source no longer in the list: keep it frozen (no-retirement
            # decision) - do not append
            log('{}: {} is no longer in monitoring_list.txt - '
                'not appending'.format(mode_name, source_id))
            continue
        n_added = nml.append_ledger_rows(
            uploads_dir, source_id, rows,
            supersede_statuses=(() if rejected
                                else nml.SUPERSEDABLE_STATUSES))
        n_total += n_added
        log('{}: {}: {} new row(s), {} duplicate(s) skipped'.format(
            mode_name, source_id, n_added, len(rows) - n_added))
        nml.rebuild_source_products(uploads_dir, entry, cfg, factory_text)
    nml.rebuild_central_index(uploads_dir, entries,
                              (cfg.get('VAST_REFERENCE_COPY') or '').strip())
    log('{}: done, {} row(s) appended'.format(mode_name, n_total))
    return 0


# ---------- measurement machinery for the manual modes ----------

def record_without_measuring(cfg, entry, images, uploads_dir, factory_text,
                             status):
    """Record images in the ledger of one source without measuring them -
    the images of rejected transient-search runs (status run_error). A row
    is written only when the position is on the image; a position that
    sky2xy puts off the image gets no row at all rather than a permanent
    'edge' one - the plate solution of a rejected run may be what failed,
    while a run_error row is replaced when a later reprocessing of the
    upload succeeds - and the image is simply checked again by the next
    rescan. sky2xy reports every position "off image" on an fpack-compressed
    file, so such a copy is recorded without the test. images holds
    (image_path, why) pairs, why being the explanation for the log. Returns
    the number of rows written."""
    from nmw_forced_phot_lib import (camera_settings_for_path,
                                     get_jd_and_atel_date)
    vast_dir = (cfg.get('VAST_REFERENCE_COPY') or '').strip()
    source_id = entry['source_id']
    rows = []
    for img, why in images:
        if not img.endswith('.fz') and sky2xy_on_image(
                vast_dir, img, entry['ra'], entry['dec']) is None:
            log('{}: {} not recorded - the position is not on the image '
                '({})'.format(source_id, os.path.basename(img), why))
            continue
        jd, _atel = get_jd_and_atel_date(vast_dir, img)
        camera = camera_settings_for_path(factory_text, img) or 'unknown'
        rows.append({'basename': os.path.basename(img),
                     'jd': '{}'.format(jd if jd else 'na'),
                     'mag': '99.0000', 'err': '99.0000', 'status': status,
                     'camera': camera})
        log('{}: {} recorded as {} without measuring - {}'.format(
            source_id, os.path.basename(img), status, why))
    if not rows:
        return 0
    return nml.append_ledger_rows(uploads_dir, source_id, rows)


def install_bad_region_list(work_dir, factory_text, camera, default_text):
    """Put the bad-region list the transient factory uses for this camera
    into the working copy as bad_region.lst, or the working copy's own
    default list when the camera has none. The plate-solve/catalog pass
    (solve_plate_with_UCAC5, the aperture estimate) and util/forced_photometry
    (positions in a listed region come back as 'bad_region') read it there.
    Written through a temporary file and an atomic rename, so a failure
    never leaves another camera's list - or a truncated one - in place.
    Returns True on success."""
    from nmw_forced_phot_lib import bad_region_file_for_camera
    source = bad_region_file_for_camera(
        factory_text, camera, nml.resolve_nmw_calibration_dir(), work_dir)
    target = os.path.join(work_dir, 'bad_region.lst')
    tmp = '{}.tmp{}'.format(target, os.getpid())
    try:
        if source and os.path.isfile(source):
            shutil.copyfile(source, tmp)
        else:
            with open(tmp, 'w') as fh:
                fh.write(default_text)
        os.replace(tmp, target)
    except OSError as exc:
        log('WARNING: cannot install the bad region list for camera {} '
            '({})'.format(camera, exc))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    if source and os.path.isfile(source):
        log('camera {}: bad regions from {}'.format(camera, source))
    else:
        log('camera {}: no bad region list of its own ({}) - using the '
            'default bad_region.lst of the VaST copy'.format(
                camera, source or 'not set in the factory'))
    return True


def measure_images_for_source(cfg, local_config_path, entry, images,
                              uploads_dir, verdicts=None):
    """Measure one source on a list of already-solved images inside a
    disposable VaST working copy (the manual backfill/rescan path), appending
    ledger rows in chunks so an interrupted run keeps its progress.

    The verdict of the transient-search run that processed each image is
    honoured (verdicts: an nml.RunVerdictResolver, shared by the sources of
    one manual run): the images of a rejected run are recorded as run_error
    without being measured, and the images of a run still in progress are
    left for the next rescan. So is the verdict of the ingest's cloud check,
    kept in the ledgers: a frame with a 'cloudy' row in any source's ledger
    is measured, and a detection or upper limit on it is recorded with the
    status 'cloudy' (the measured values kept), exactly as the ingest
    records its own rows. Images already in the ledger are skipped whatever
    their status - a run_error row is replaced only by the ingest of a
    successful reprocessing run. The images are processed camera by camera,
    each camera's bad-region list in place for both the plate-solve pass and
    the measurement, as in the factory."""
    from nmw_forced_phot_lib import (
        setup_vast_working_copy, _phase1_parallel_solve_plate,
        run_forced_photometry_c, derive_band, derive_sextractor_config,
        camera_settings_for_path, get_jd_and_atel_date)
    vast_dir = (cfg.get('VAST_REFERENCE_COPY') or '').strip()
    factory_text = read_factory_text(cfg)
    if verdicts is None:
        verdicts = nml.RunVerdictResolver(uploads_dir, log=log)
    source_id = entry['source_id']
    source_dir = nml.source_dir_path(uploads_dir, source_id)
    _, already_measured = nml.read_ledger(source_dir)
    # Dedup by ledger key while building the todo list: the archive and recent
    # passes overlap for a recently-archived observation that is still in
    # uploads/ (archive/wcs_fd_X.fts.fz and uploads/img_*/wcs_fd_X.fts share a
    # ledger key), and without this the same image would be funpacked,
    # plate-solved and measured twice - only the append would dedup it. All
    # copies of an image are kept for the verdict: the archive copy has none,
    # the img_* copy has the verdict of its run.
    copies = {}
    keys_in_order = []
    for img in images:
        key = nml.ledger_key(os.path.basename(img))
        if key in already_measured:
            continue
        if key not in copies:
            copies[key] = []
            keys_in_order.append(key)
        copies[key].append(img)
    cloudy_frames = nml.frames_judged_cloudy(uploads_dir, log)
    todo = []
    rejected = []
    condemned_keys = set()
    n_in_progress = 0
    for key in keys_in_order:
        verdict, reason, origin, path = verdicts.verdict_for_copies(
            copies[key])
        # the on-image test of record_without_measuring needs a copy that
        # sky2xy can read
        plain_copies = [p for p in copies[key] if not p.endswith('.fz')]
        if verdict in nml.REJECTING_RUN_VERDICTS:
            rejected.append((
                plain_copies[0] if plain_copies else path,
                'the transient search run of its upload was rejected ({}: {}; '
                'from {})'.format(verdict, (reason or 'no reason given')[:200],
                                  origin)))
        elif verdict == nml.RUN_VERDICT_PENDING:
            n_in_progress += 1
            log('{}: {} skipped - the transient search run of its upload is '
                'still in progress ({})'.format(
                    source_id, os.path.basename(path), origin))
        else:
            todo.append(copies[key][0])
            if nml.image_core_name(key) in cloudy_frames:
                condemned_keys.add(key)
    log('{}: {} image(s) to measure ({} of them condemned by the cloud check '
        'of the ingest: recorded as cloudy), {} of rejected transient-search '
        'runs to record without measuring, {} of runs in progress skipped '
        '({} already in the ledger or duplicate)'.format(
            source_id, len(todo), len(condemned_keys), len(rejected),
            n_in_progress, len(images) - len(keys_in_order)))
    n_appended = 0
    if rejected:
        n_appended += record_without_measuring(
            cfg, entry, rejected, uploads_dir, factory_text,
            nml.RUN_ERROR_STATUS)
    if not todo:
        return n_appended
    work_dir = setup_vast_working_copy(vast_dir, 'uploads',
                                       prefix='vast_monitoring_')
    if work_dir is None:
        log('{}: could not set up the VaST working copy'.format(source_id))
        return n_appended
    try:
        skip_log = os.path.join(source_dir, 'measurement_skipped.log')
        work_dir_default_sex = os.path.join(work_dir, 'default.sex')
        # The working copy's own bad_region.lst (the VaST default) is what a
        # camera without a list of its own gets
        try:
            with open(os.path.join(work_dir, 'bad_region.lst'),
                      errors='replace') as fh:
                default_bad_region_text = fh.read()
        except OSError:
            default_bad_region_text = ''
        edge_margin_pix = _edge_margin_pix(cfg)
        log('{}: positions closer than {:g} pix to a frame edge are recorded '
            'as edge'.format(source_id, edge_margin_pix))
        # Camera by camera, so that each camera's bad-region list is in place
        # for the plate-solve/catalog pass of its images as well as for their
        # measurement (the pass runs in parallel in one working copy)
        groups = []
        group_of_camera = {}
        for img in todo:
            camera = camera_settings_for_path(factory_text, img) or 'unknown'
            if camera not in group_of_camera:
                group_of_camera[camera] = len(groups)
                groups.append((camera, []))
            groups[group_of_camera[camera]][1].append(img)
        idx = 0
        pending_rows = []
        for camera, group in groups:
            if not install_bad_region_list(work_dir, factory_text, camera,
                                           default_bad_region_text):
                idx += len(group)
                log('{}: {} image(s) of camera {} not measured (will retry on '
                    'next rescan): the bad region list could not be '
                    'installed'.format(source_id, len(group), camera))
                continue
            workers = min(len(group), os.cpu_count() or 4,
                          _rescan_worker_cap(cfg))
            log('{}: camera {}: plate-solve/catalog pass over {} image(s) with '
                '{} worker(s)'.format(source_id, camera, len(group), workers))
            _, _, _, compute_path_map, _ = _phase1_parallel_solve_plate(
                work_dir, local_config_path, group, workers, skip_log)
            for img in group:
                idx += 1
                band = derive_band(factory_text, img, '')
                sex_config_name = derive_sextractor_config(factory_text, img)
                if sex_config_name:
                    src_sex = os.path.join(work_dir, sex_config_name)
                    if os.path.isfile(src_sex):
                        try:
                            shutil.copy2(src_sex, work_dir_default_sex)
                        except OSError:
                            pass
                compute_path = compute_path_map.get(img)
                fp = None
                if compute_path is not None:
                    fp = run_forced_photometry_c(
                        work_dir, local_config_path, img, compute_path,
                        entry['ra'], entry['dec'], band, debug_log=skip_log,
                        off_image_as_edge=True,
                        edge_margin_pix=edge_margin_pix)
                if fp is None:
                    # A None result means the measurement failed for a reason
                    # we cannot classify here: a failed plate solve, a
                    # forced-photometry error, or - importantly - a transient
                    # UCAC5/APASS/VizieR network failure or timeout. Do NOT
                    # write a permanent ledger row: a 'fail' row would be
                    # dedup-permanent and no rescan would ever retry it, so
                    # one remote-service blip during a backfill would truncate
                    # the lightcurve forever. Leaving the image out of the
                    # ledger lets the next --reconcile / --rescan retry it.
                    # (Positions that sky2xy puts off this particular frame
                    # come back as an 'edge' dict thanks to
                    # off_image_as_edge=True and are recorded through the else
                    # branch below as a terminal edge row, like the factory
                    # does; without that mapping such images were retried on
                    # every rescan.)
                    log('{}: {}/{} not measured (will retry on next rescan): '
                        '{}'.format(source_id, idx, len(todo),
                                    os.path.basename(img)))
                    continue
                jd = fp.get('jd')
                if not jd:
                    jd, _atel = get_jd_and_atel_date(vast_dir, img)
                status = '{}'.format(fp.get('status', 'fail'))
                key = nml.ledger_key(os.path.basename(img))
                if key in condemned_keys and status in nml.CLOUD_RESTATUSED_STATUSES:
                    # the ingest's cloud verdict on this frame, applied the
                    # way the ingest applies it to its own rows
                    status = nml.CLOUDY_STATUS
                pending_rows.append(
                    {'basename': os.path.basename(img),
                     'jd': '{}'.format(jd if jd else 'na'),
                     'mag': '{}'.format(fp.get('mag', '99.0000')),
                     'err': '{}'.format(fp.get('err', '99.0000')),
                     'status': status,
                     'camera': camera})
                log('{}: {}/{} {} {} {} {}'.format(
                    source_id, idx, len(todo), os.path.basename(img),
                    pending_rows[-1]['mag'], pending_rows[-1]['err'],
                    pending_rows[-1]['status']))
                if len(pending_rows) >= 10:
                    n_appended += nml.append_ledger_rows(
                        uploads_dir, source_id, pending_rows)
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


def enumerate_quarantine_images(cfg, covering_fields):
    """Images of the covering fields in the quarantine directory: a second
    uploads-style tree (img_* subdirectories) on a big disk that holds
    processed fields moved off the SSD workdir but not yet quality-controlled
    into the long-term archive. Optional: silently skipped when
    IMAGE_QUARANTINE_DIR is not configured, skipped with a note when the
    configured directory does not exist. The ledger basename dedup makes the
    overlap with the workdir and archive populations harmless."""
    quarantine_dir = (cfg.get('IMAGE_QUARANTINE_DIR') or '').strip()
    if not quarantine_dir:
        return []
    if not os.path.isdir(quarantine_dir):
        log('quarantine pass skipped: IMAGE_QUARANTINE_DIR={} does not '
            'exist'.format(quarantine_dir))
        return []
    images = nml.list_all_recent_field_images(quarantine_dir,
                                              covering_fields)
    log('quarantine pass: {} image(s) for field(s) {} under {}'.format(
        len(images), ' '.join(sorted(covering_fields)) or '(none)',
        quarantine_dir))
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


def mode_flag_measurement(image_basename, source_filter, restore):
    """Manually exclude (or restore) the measurement(s) of one image in the
    per-source ledgers and rebuild the affected products. See the module
    docstring for the CLI semantics."""
    action = 'restore' if restore else 'exclude'
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
        if not entries:
            log('nothing to do: the monitoring list is missing or empty')
            return 1
        targets = []
        for entry in entries:
            if source_filter and entry['source_id'] != source_filter:
                continue
            if os.path.isdir(nml.source_dir_path(uploads_dir,
                                                 entry['source_id'])):
                targets.append(entry)
        if source_filter and not targets:
            log('{}-measurement: source {} is not in monitoring_list.txt or '
                'not activated on this machine'.format(action, source_filter))
            return 1
        factory_text = read_factory_text(cfg)
        vast_dir = (cfg.get('VAST_REFERENCE_COPY') or '').strip()
        n_total = 0
        for entry in targets:
            n = nml.rewrite_measurement_status(
                uploads_dir, entry['source_id'], image_basename,
                restore=restore)
            if n:
                log('{}-measurement: {}: {} ledger row(s) updated for '
                    '{}'.format(action, entry['source_id'], n,
                                image_basename))
                nml.rebuild_source_products(uploads_dir, entry, cfg,
                                            factory_text)
                n_total += n
        if n_total:
            nml.rebuild_central_index(uploads_dir, entries, vast_dir)
            log('{}-measurement: done, {} row(s) updated'.format(
                action, n_total))
            return 0
        log('{}-measurement: no matching ledger row for {} in {} source(s)'
            ' - nothing changed (check the image basename{})'.format(
                action, image_basename, len(targets),
                '' if restore else '; rows already excluded or with a'
                ' non-detection status are left as they are'))
        return 1
    finally:
        lock_fh.close()


def resolve_source_selector(entries, selector):
    """Match a user-supplied source selector against the monitoring list:
    the source_id, the full display name, the sanitized form of the
    selector, or the short AAVSO name (the part of the display name before
    ' - '/' = ', whitespace-trimmed) all match. Returns
    (entry, None) on a unique match, (None, message) otherwise."""
    selector_stripped = selector.strip()
    selector_id = nml.sanitize_source_id(selector_stripped)
    matches = []
    for entry in entries:
        if entry['source_id'] == selector_stripped \
                or entry['name'] == selector_stripped \
                or (selector_id and entry['source_id'] == selector_id) \
                or nml.aavso_star_name(entry['name']) == selector_stripped:
            matches.append(entry)
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, ('no monitoring list entry matches "{}" (accepted: '
                      'source_id, full name, or short AAVSO name)'
                      .format(selector))
    return None, ('selector "{}" is ambiguous, matches: {}'.format(
        selector, ', '.join(e['source_id'] for e in matches)))


def mode_set_threshold(source_selector, threshold):
    """Set (float) or clear (None) the manual detection threshold of one
    source and rebuild its products. See the module docstring."""
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
        if not entries:
            log('nothing to do: the monitoring list is missing or empty')
            return 1
        entry, problem = resolve_source_selector(entries, source_selector)
        if entry is None:
            log('detection-threshold: {}'.format(problem))
            return 1
        source_dir = nml.source_dir_path(uploads_dir, entry['source_id'])
        if not os.path.isdir(source_dir):
            log('detection-threshold: source {} is not activated on this '
                'machine'.format(entry['source_id']))
            return 1
        action = nml.write_detection_threshold(source_dir, threshold)
        log('detection-threshold: {}: {}'.format(entry['source_id'],
                                                 action))
        factory_text = read_factory_text(cfg)
        vast_dir = (cfg.get('VAST_REFERENCE_COPY') or '').strip()
        nml.rebuild_source_products(uploads_dir, entry, cfg, factory_text)
        nml.rebuild_central_index(uploads_dir, entries, vast_dir)
        log('detection-threshold: {}: products rebuilt'.format(
            entry['source_id']))
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
    # One verdict cache for all sources: the results_* index of
    # IMAGE_DATA_ROOT is listed once
    verdicts = nml.RunVerdictResolver(uploads_dir, log=log)
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
                    images.extend(enumerate_quarantine_images(
                        cfg, covering_fields))
            n_new = 0
            if images:
                n_new = measure_images_for_source(cfg, local_config_path,
                                                  entry, images, uploads_dir,
                                                  verdicts)
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
    if len(argv) >= 4 and argv[1] == '--frame-quality':
        return mode_frame_quality(argv[2], argv[3])
    if len(argv) >= 3 and argv[1] == '--ingest':
        return mode_ingest(argv[2])
    if len(argv) >= 3 and argv[1] == '--ingest-rejected':
        return mode_ingest(argv[2], rejected=True)
    if len(argv) >= 2 and argv[1] == '--reconcile':
        return mode_reconcile()
    if len(argv) >= 3 and argv[1] == '--rescan-recent':
        return mode_rescan('rescan-recent', argv[2])
    if len(argv) >= 3 and argv[1] == '--rescan-archive':
        return mode_rescan('rescan-archive', argv[2])
    if len(argv) >= 2 and argv[1] == '--rebuild-pages':
        return mode_rebuild_pages()
    if len(argv) >= 4 and argv[1] == '--set-detection-threshold':
        try:
            threshold = float(argv[3])
        except ValueError:
            sys.stderr.write('bad threshold magnitude: {}\n'.format(argv[3]))
            return 1
        if not 0.0 < threshold < 30.0:
            sys.stderr.write('threshold magnitude {} out of range\n'.format(
                argv[3]))
            return 1
        return mode_set_threshold(argv[2], threshold)
    if len(argv) >= 3 and argv[1] == '--clear-detection-threshold':
        return mode_set_threshold(argv[2], None)
    if len(argv) >= 3 and argv[1] in ('--exclude-measurement',
                                      '--restore-measurement'):
        source_filter = None
        if len(argv) >= 5 and argv[3] == '--source':
            source_filter = argv[4]
        elif len(argv) >= 4:
            sys.stderr.write('unrecognized extra arguments for {} (expected'
                             ' --source <source_id>)\n'.format(argv[1]))
            return 1
        return mode_flag_measurement(
            argv[2], source_filter,
            restore=(argv[1] == '--restore-measurement'))
    sys.stderr.write(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))

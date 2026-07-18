"""Frame quality (cloud) check for the source monitoring ingest.

Compares the bright-star photometry of each second-epoch frame against the
same field's REFERENCE frame (archived reference frames are vetted to be
cloud-free) and condemns frames whose stars disagree with the reference in
a way uniform transparency loss cannot explain. Uniform haze only shifts
the photometric zero point and is absorbed by the per-image magnitude
calibration, so the median magnitude offset is reported but is NOT a
trigger; the cloud signatures are:

  - missing:  fraction of bright reference stars with no counterpart
  - scatter:  robust (MAD-based) scatter of the per-star magnitude offsets
  - ftail:    fraction of stars >0.3 mag fainter than the median offset
              (clouds only ever make stars fainter - a one-sided tail)
  - patch:    spread of the median offset across a 4x3 grid of image
              blocks AFTER removing the best-fit plane (patchy clouds dim
              different parts of the frame by different amounts; a smooth
              differential-extinction gradient across the wide field at
              low altitude is a plane and is NOT a cloud signature)

The check is differential against the same field's own baseline, so
Milky Way fields are largely safe by construction: diffuse background
never enters the statistics and the star density is the same in both
frames. Crowding does, however, inflate 'missing' and 'scatter' (epoch
seeing differences change the deblending of close pairs), so their
thresholds must leave room for the densest galactic-plane fields.

The thresholds were first calibrated on the 2026-07-10..14 T CrB frames
(TTU cameras) and recalibrated 2026-07-18 on the dense Milky Way fields
covering Nova Sct 2026: cloud-free dense frames (Aql-03: 76k reference
stars, dzp=-0.01, ftail=0.01) show missing up to 0.19 and scatter up to
0.18 from crowding alone, while genuinely cloudy frames (Per-02
2026-07-18: dzp=+1.17) show missing 0.69, scatter 0.20, ftail 0.20.
ftail is the primary (one-sided, physical) cloud signature and keeps a
tight threshold; missing and scatter act on gross violations only.
Override per camera via the MONITORING_FRAME_QUALITY_* environment
variables if needed.

Input catalogs are the 10-column .wcscat files written by VaST
(NUMBER RA Dec X Y FLUX FLUXERR MAG MAGERR FLAGS).
"""

import os
from bisect import bisect_left

# Verdict thresholds (calibrated, see module docstring)
MAX_MISSING_FRACTION = 0.30
MAX_SCATTER_MAG = 0.25
MAX_FAINT_TAIL_FRACTION = 0.08
MAX_PATCHINESS_MAG = 0.3

# Bright unsaturated star selection (instrumental magnitudes) and matching
BRIGHT_MAG_MIN = -15.0
BRIGHT_MAG_MAX = -8.5
MATCH_RADIUS_ARCSEC = 10.0
FAINT_TAIL_OFFSET_MAG = 0.3

# Patchiness grid and small-number guards
PATCH_GRID_X = 4
PATCH_GRID_Y = 3
PATCH_MIN_STARS_PER_BLOCK = 30
PATCH_MIN_BLOCKS = 3
MIN_REF_STARS_FOR_CHECK = 200
MIN_MATCHES_FOR_SHAPE_STATS = 10

CLOUDY_STATUS = 'cloudy'
RESTATUSED_STATUSES = ('detection', 'upperlimit')


def _env_float(name, default):
    try:
        return float(os.environ.get(name, ''))
    except (TypeError, ValueError):
        return default


def thresholds_from_env():
    return {
        'missing': _env_float('MONITORING_FRAME_QUALITY_MAX_MISSING',
                              MAX_MISSING_FRACTION),
        'scatter': _env_float('MONITORING_FRAME_QUALITY_MAX_SCATTER',
                              MAX_SCATTER_MAG),
        'ftail': _env_float('MONITORING_FRAME_QUALITY_MAX_FAINT_TAIL',
                            MAX_FAINT_TAIL_FRACTION),
        'patch': _env_float('MONITORING_FRAME_QUALITY_MAX_PATCHINESS',
                            MAX_PATCHINESS_MAG),
    }


def read_bright_stars(wcscat_path):
    """Bright unsaturated stars from a 10-column VaST .wcscat file as a
    list of (ra, dec, x, y, mag) tuples; [] when the file is unreadable."""
    stars = []
    try:
        with open(wcscat_path) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 10:
                    continue
                try:
                    ra = float(parts[1])
                    dec = float(parts[2])
                    x = float(parts[3])
                    y = float(parts[4])
                    mag = float(parts[7])
                    flags = int(parts[9])
                except ValueError:
                    continue
                if flags != 0:
                    continue
                if mag <= BRIGHT_MAG_MIN or mag >= BRIGHT_MAG_MAX:
                    continue
                stars.append((ra, dec, x, y, mag))
    except OSError:
        return []
    return stars


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def frame_quality_metrics(frame_stars, ref_stars):
    """Match reference stars to frame stars and compute the cloud metrics.
    Returns a dict with keys n_ref, n_match, missing, dzp, scatter, ftail,
    patch; or None when the reference list is too small for a meaningful
    check."""
    import math
    if len(ref_stars) < MIN_REF_STARS_FOR_CHECK:
        return None
    r_deg = MATCH_RADIUS_ARCSEC / 3600.0
    by_dec = sorted(frame_stars, key=lambda s: s[1])
    dec_keys = [s[1] for s in by_dec]
    offsets = []
    offset_positions = []
    for ra, dec, x, y, mag in ref_stars:
        lo = bisect_left(dec_keys, dec - r_deg)
        hi = bisect_left(dec_keys, dec + r_deg)
        best = None
        best_d2 = r_deg * r_deg
        cosd = math.cos(math.radians(dec))
        for cand in by_dec[lo:hi]:
            dra = (cand[0] - ra) * cosd
            ddec = cand[1] - dec
            d2 = dra * dra + ddec * ddec
            if d2 < best_d2:
                best_d2 = d2
                best = cand
        if best is not None:
            offsets.append(best[4] - mag)
            offset_positions.append((x, y))
    n_ref = len(ref_stars)
    n_match = len(offsets)
    metrics = {'n_ref': n_ref, 'n_match': n_match,
               'missing': 1.0 - float(n_match) / n_ref,
               'dzp': 0.0, 'scatter': 0.0, 'ftail': 0.0, 'patch': 0.0}
    if n_match < MIN_MATCHES_FOR_SHAPE_STATS:
        # nothing matched to speak of: the missing fraction alone decides
        return metrics
    med = _median(offsets)
    metrics['dzp'] = med
    metrics['scatter'] = 1.4826 * _median([abs(v - med) for v in offsets])
    metrics['ftail'] = (float(len([v for v in offsets
                                   if v > med + FAINT_TAIL_OFFSET_MAG]))
                        / n_match)
    max_x = max(p[0] for p in offset_positions) + 1.0
    max_y = max(p[1] for p in offset_positions) + 1.0
    block_offsets = {}
    for (x, y), v in zip(offset_positions, offsets):
        bx = min(int(x * PATCH_GRID_X / max_x), PATCH_GRID_X - 1)
        by = min(int(y * PATCH_GRID_Y / max_y), PATCH_GRID_Y - 1)
        block_offsets.setdefault((bx, by), []).append(v)
    blocks = [(bx, by, _median(v)) for (bx, by), v in block_offsets.items()
              if len(v) >= PATCH_MIN_STARS_PER_BLOCK]
    if len(blocks) >= PATCH_MIN_BLOCKS:
        # Remove the best-fit plane from the block medians before measuring
        # their spread: a smooth transparency gradient across the wide field
        # (differential atmospheric extinction at low altitude) is a plane
        # and must not count as cloud patchiness; real patchy clouds leave
        # residuals the plane cannot absorb.
        residuals = _plane_residuals(blocks)
        metrics['patch'] = max(residuals) - min(residuals)
    return metrics


def _plane_residuals(blocks):
    """Least-squares fit of v = a*bx + b*by + c to the (bx, by, v) block
    medians; returns the list of fit residuals. Falls back to zero-mean
    offsets (equivalent to the plain range) when the fit is degenerate
    (e.g. all blocks in one row)."""
    n = float(len(blocks))
    sx = sum(b[0] for b in blocks)
    sy = sum(b[1] for b in blocks)
    sv = sum(b[2] for b in blocks)
    sxx = sum(b[0] * b[0] for b in blocks)
    syy = sum(b[1] * b[1] for b in blocks)
    sxy = sum(b[0] * b[1] for b in blocks)
    sxv = sum(b[0] * b[2] for b in blocks)
    syv = sum(b[1] * b[2] for b in blocks)
    # normal equations for [a, b, c]
    det = (sxx * (syy * n - sy * sy) - sxy * (sxy * n - sy * sx)
           + sx * (sxy * sy - syy * sx))
    if abs(det) < 1e-12:
        mean_v = sv / n
        return [b[2] - mean_v for b in blocks]
    a = ((syy * n - sy * sy) * sxv + (sy * sx - sxy * n) * syv
         + (sxy * sy - syy * sx) * sv) / det
    bcoef = ((sy * sx - sxy * n) * sxv + (sxx * n - sx * sx) * syv
             + (sxy * sx - sxx * sy) * sv) / det
    c = ((sxy * sy - syy * sx) * sxv + (sxy * sx - sxx * sy) * syv
         + (sxx * syy - sxy * sxy) * sv) / det
    return [b[2] - (a * b[0] + bcoef * b[1] + c) for b in blocks]


def frame_verdict(metrics, thresholds=None):
    """Return (is_cloudy, list of tripped threshold names). A None metrics
    (too few reference stars) never condemns the frame."""
    if metrics is None:
        return False, []
    if thresholds is None:
        thresholds = thresholds_from_env()
    tripped = []
    if metrics['missing'] > thresholds['missing']:
        tripped.append('missing')
    if metrics['scatter'] > thresholds['scatter']:
        tripped.append('scatter')
    if metrics['ftail'] > thresholds['ftail']:
        tripped.append('ftail')
    if metrics['patch'] > thresholds['patch']:
        tripped.append('patch')
    return bool(tripped), tripped


def reference_wcscat_path(work_dir):
    """The reference-frame .wcscat of the field processed in work_dir,
    located via the 'Ref.  image:' line of vast_summary.log (the same
    convention as util/transients/calibrate_current_field_with_tycho2.sh).
    Returns None when the summary or the catalog cannot be found."""
    summary = os.path.join(work_dir, 'vast_summary.log')
    ref_path = None
    try:
        with open(summary) as fh:
            for line in fh:
                if line.startswith('Ref.') and 'image:' in line:
                    ref_path = line.split()[-1]
                    break
    except OSError:
        return None
    if not ref_path:
        return None
    basename = os.path.basename(ref_path)
    if not basename.startswith('wcs_'):
        basename = 'wcs_' + basename
    candidate = os.path.join(work_dir, basename + '.wcscat')
    return candidate if os.path.isfile(candidate) else None


def format_metrics(metrics):
    if metrics is None:
        return 'too few reference stars for the check'
    return ('missing={:.2f} dzp={:+.2f} scatter={:.2f} ftail={:.2f} '
            'patch={:.2f} matched={}/{}'.format(
                metrics['missing'], metrics['dzp'], metrics['scatter'],
                metrics['ftail'], metrics['patch'], metrics['n_match'],
                metrics['n_ref']))


def check_and_mark_raw_file(raw_path, work_dir, log):
    """Run the frame quality check for every frame referenced in the raw
    measurements file and rewrite (atomically, in place) the status of the
    rows belonging to condemned frames from detection/upperlimit to
    'cloudy'. Any problem (missing catalogs etc.) is logged and leaves the
    raw file untouched: the check must never block the ingest.
    Returns the number of condemned frames."""
    try:
        with open(raw_path) as fh:
            raw_lines = fh.read().splitlines()
    except OSError as exc:
        log('frame-quality: cannot read {}: {}'.format(raw_path, exc))
        return 0
    basenames = []
    for line in raw_lines:
        parts = line.split()
        if len(parts) >= 7 and parts[1] not in basenames:
            basenames.append(parts[1])
    if not basenames:
        return 0
    ref_wcscat = reference_wcscat_path(work_dir)
    if ref_wcscat is None:
        log('frame-quality: no reference-frame catalog in {} - '
            'check skipped'.format(work_dir))
        return 0
    ref_stars = read_bright_stars(ref_wcscat)
    if len(ref_stars) < MIN_REF_STARS_FOR_CHECK:
        log('frame-quality: only {} bright reference stars in {} - '
            'check skipped'.format(len(ref_stars),
                                   os.path.basename(ref_wcscat)))
        return 0
    thresholds = thresholds_from_env()
    cloudy_basenames = set()
    for basename in basenames:
        frame_wcscat = os.path.join(work_dir, basename + '.wcscat')
        if not os.path.isfile(frame_wcscat):
            log('frame-quality: {} has no .wcscat - check skipped for this '
                'frame'.format(basename))
            continue
        metrics = frame_quality_metrics(read_bright_stars(frame_wcscat),
                                        ref_stars)
        is_cloudy, tripped = frame_verdict(metrics, thresholds)
        log('frame-quality: {} {} verdict={}{}'.format(
            basename, format_metrics(metrics),
            CLOUDY_STATUS if is_cloudy else 'ok',
            ' ({})'.format(','.join(tripped)) if tripped else ''))
        if is_cloudy:
            cloudy_basenames.add(basename)
    if not cloudy_basenames:
        return 0
    out_lines = []
    for line in raw_lines:
        parts = line.split()
        if (len(parts) >= 7 and parts[1] in cloudy_basenames
                and parts[5] in RESTATUSED_STATUSES):
            parts[5] = CLOUDY_STATUS
            out_lines.append(' '.join(parts))
        else:
            out_lines.append(line)
    tmp = '{}.tmp{}'.format(raw_path, os.getpid())
    with open(tmp, 'w') as fh:
        fh.write('\n'.join(out_lines) + '\n')
    os.replace(tmp, raw_path)
    return len(cloudy_basenames)

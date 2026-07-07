# Source monitoring: design + implementation plan

## 1. Overview

A source is added to the monitoring list by its equatorial coordinates
(`HH:MM:SS.SS +DD:MM:SS.S`) and a name (spaces, upper/lowercase English
letters, digits, `+` and `-`). On the first update run after the addition the
source's lightcurve is backfilled from the long-term image archive and from
the recent `uploads/img_*` directories; afterwards, every successful
`autoprocess.sh` run of a field containing the source appends new
measurements. Each source has:

- a plain three-plus-one-column ASCII lightcurve (`JD(UTC) mag err camera`),
- an upper-limits file (`JD limit_mag camera`),
- an AAVSO Extended Format file including the upper limits as fainter-than
  records,
- an HTML page with the lightcurve plot (same rendering machinery and style
  as the archive forced photometry pages) and links to the data files,

plus one central page listing all monitored sources.

NO JSON anywhere in this feature: the master list, the per-source info file
and the measurement ledger are all plain ASCII.

## 2. Decisions fixed by the user (2026-07-06)

1. Master list is an ASCII file in the calibration folder:
   `$NMW_CALIBRATION/monitoring_list.txt` (next to `neverexclude_list.txt`).
2. Adding sources is server-side only: edit `monitoring_list.txt` by hand,
   then run `monitoring_update.py --reconcile`. That is the whole workflow -
   there is NO add-wrapper command (rejected as redundant by the user) and
   no web form, no auth. `monitoring_list.txt` is the SINGLE source of truth
   for every source's name and coordinates; the registry never duplicates
   them.
3. Updates run from `autoprocess.sh`, on successful (report OK) runs only.
4. Multi-camera sources produce ONE combined lightcurve; the camera name is
   the fourth column of the ASCII lightcurve file and appears in the AAVSO
   records.
5. Upper limits are included (ASCII upper-limits file, plotted, and reported
   in the AAVSO file as fainter-than records per the AAVSO documentation);
   `edge` / `saturated` / `bad_region` measurements are excluded from all
   published products.
6. No nightly averaging: every image yields its own point.
7. Archive backfill happens only on the source's first update run.
8. One AAVSO OBSCODE for everything, overridable via `local_config.sh`
   (`AAVSO_OBSCODE`); `util/format_lightcurve_AAVSO.sh` in VaST must honor
   the same variable.
9. Monitoring measurements always run with
   `FORCED_PHOTOMETRY_AIRMASS_ZEROPOINT=yes`.
10. No retirement mechanism for now: a source removed from
    `monitoring_list.txt` simply stops being updated; its directory and page
    remain frozen in place.
11. NO lazy/opportunistic reconcile (user, 2026-07-06): the archive backfill
    and any recent-images reprocessing run ONLY when triggered manually from
    the command line. The per-run hook operates exclusively on sources that
    already have a registry directory; on a fresh install with a populated
    `monitoring_list.txt`, uploads trigger nothing for monitoring until the
    admin runs the reconcile once. This avoids the fresh-install hot mess of
    every incoming upload racing to start (or queue behind) heavy backfills.
12. The recent-images rescan must be able to process ALL `img_*` directories
    regardless of their number or age - no window cap, no image-count cap.
    It is a command-line tool, not a CGI, so there is no rush and no need
    for limits.

## 3. Master list format

`$NMW_CALIBRATION/monitoring_list.txt`, one source per line:

```
HH:MM:SS.SS +DD:MM:SS.S Source Name With Spaces
# comment lines and blank lines are ignored
20:00:05.12 +19:59:52.3 V0615 Vul
17:55:00.01 -21:22:42.4 N Sgr 2020 N4
```

- First two whitespace-separated tokens are RA and Dec (sexagesimal, signed
  Dec); everything after them is the display name.
- Name charset validation: `[A-Za-z0-9+._ -]` - letters, digits, spaces,
  `+`, `-`, `.` and `_` (dot and underscore added by user request; the dot
  lets survey names with decimal coordinates, e.g.
  `ASASSN-V J010901.57+471816.4`, be pasted verbatim). Leading/trailing
  whitespace and trailing dots are trimmed. Lines failing coordinate or name
  validation are reported to the update log and skipped, never fatal.
  Note that since whitespace maps to `_` in the id, the names `V0615 Vul`
  and `V0615_Vul` produce the SAME id: if both appear as separate list
  lines, the second is refused by the id-collision check (logged) - which is
  the intended behavior, they would be the same directory.

Sanitization examples (whitespace runs collapse to a single `_`; everything
else passes through - the charset is already filesystem- and URL-safe, and a
literal `+` or `.` is legal in a URL path):

```
N Sgr 2020 N4                   -> monitoring/N_Sgr_2020_N4/
V0615 Vul                       -> monitoring/V0615_Vul/
TCP J15030519+2202041           -> monitoring/TCP_J15030519+2202041/
OGLE-BLG-RRLYR-00001            -> monitoring/OGLE-BLG-RRLYR-00001/
ASASSN-V J010901.57+471816.4    -> monitoring/ASASSN-V_J010901.57+471816.4/
Nova  Sgr   2026 (double space) -> monitoring/Nova_Sgr_2026/
```

Page URL: `$URL_OF_DATA_PROCESSING_ROOT/monitoring/<source_id>/index.html`
with `lightcurve.dat`, `upperlimits.dat`, `lightcurve_aavso.txt` and
`lightcurve.png` next to it; the central page is
`$URL_OF_DATA_PROCESSING_ROOT/monitoring/index.html`. Note that ids are
case-sensitive (Linux directories): `V0615 Vul` and `v0615 VUL` would be two
different sources; the reconcile warns on case-insensitive near-duplicates.
- `NMW_CALIBRATION` is resolved exactly like `transient_factory_test31.sh`
  does (`$HOME/nmw_calibration`, `/dataX/cgi-bin/unmw/uploads/nmw_calibration`,
  `/home/apache/nmw_calibration`, `/var/www/nmw_calibration`, first existing;
  env `NMW_CALIBRATION` wins).

## 4. On-disk registry (all ASCII)

```
uploads/monitoring/                          registry root
uploads/monitoring/index.html                central page (derived)
uploads/monitoring/<source_id>/
    measured_images.txt                      the measurement ledger (the ONLY primary data)
    backfill_done                            empty marker file (initial backfill completed)
    lightcurve.dat                           derived: JD mag err camera
    upperlimits.dat                          derived: JD limit_mag camera
    lightcurve_aavso.txt                     derived: AAVSO Extended incl. fainter-thans
    lightcurve.png, lightcurve.eps           derived plot
    index.html                               derived page
uploads/.monitoring_locks/                   flock files (per source + one global)
```

`<source_id>` = display name with whitespace runs replaced by `_`, restricted
to `[A-Za-z0-9+._-]` (dot allowed, see section 3); the id is the join key
back to the `monitoring_list.txt` line, which remains the only home of the
name and coordinates. The reconciler refuses a new list line whose id
collides with an existing different source (logged, skipped) and warns on
case-insensitive near-duplicates.

There is deliberately NO per-source metadata file: name and coordinates are
always read from `monitoring_list.txt` (so a coordinate typo is fixed by
editing the list - the next measurements simply use the corrected position),
and the existence of the registry directory itself means "activated on this
machine". A source whose line disappears from the list keeps its directory
and page frozen (no-retirement decision), with the page noting that the
source is no longer in the list.

The `backfill_done` empty marker replaces any completion flag: `--reconcile`
skips sources whose marker exists, and an interrupted backfill is resumable -
rerun `--reconcile`, the marker is absent, enumeration repeats and the
ledger's basename dedup skips the already-measured images.

There is also NO covering-fields cache: the pre-factory prep decides whether
an activated source falls on the field being processed with one direct
`sky2xy` call per source against a freshly unpacked frame (a second or two
for tens of sources), which cannot go stale when the reference set changes.

`measured_images.txt` — the append-only ledger, one line per measured image:

```
# image_basename JD mag err status camera
wcs_fd_068_2024-10-30_16-24-33_002.fts 2460614.18507 12.345 0.021 detection STL-11000M
wcs_fd_068_2024-10-30_16-25-26_003.fts 2460614.18571 >17.50 na upperlimit STL-11000M
wcs_068_2022-8-29_19-6-38_001.fts 2459821.29xxx na na edge STL-11000M
```

- The ledger records EVERY attempted image (including excluded statuses) so
  an image is never re-measured: dedup key = image basename. This makes
  archive/recent overlaps and upload reprocessing self-deduplicating.
- All published files (`lightcurve.dat`, `upperlimits.dat`,
  `lightcurve_aavso.txt`, plot, pages) are REBUILT from the ledger after
  every append, sorted by JD, via temp-file + atomic rename. `detection`
  rows go to `lightcurve.dat` and AAVSO magnitudes; `upperlimit` rows go to
  `upperlimits.dat` and AAVSO fainter-thans; `edge`/`saturated`/`bad_region`
  rows appear nowhere outside the ledger.
- Camera token = `CAMERA_SETTINGS` value (`Stas`, `STL-11000M`, `TTUQ1b1x1`,
  ...), derived per image via `camera_settings_for_path()` with an
  INSTRUME-header fallback for archival paths that do not carry the token.

## 5. Data products

### 5.1 ASCII lightcurve

`lightcurve.dat`: `# JD(UTC) mag err camera` header, rows
`%.5f %.4f %.4f %s` sorted by JD. The camera token contains no spaces, so
the file remains trivially awk-parseable. `upperlimits.dat`:
`# JD(UTC) limit_mag camera`, rows `%.5f %.4f %s`.

The plot renderers read these files directly: both the matplotlib reader
(`_read_numeric_columns` parses only the leading numeric columns and
ignores trailing tokens - verified) and VaST's `lib/lightcurve_png`
tolerate the trailing camera column, so no projection files are needed.
Rendering itself is the existing `render_lightcurve_plots()` from
`nmw_forced_phot_lib.py` - identical style to the archive forced
photometry pages, upper limits included.

### 5.2 AAVSO Extended Format file

`lightcurve_aavso.txt`, following the conventions of VaST's
`util/format_lightcurve_AAVSO.sh` and the AAVSO Extended Format
specification (to be re-verified against the online AAVSO documentation at
implementation time, specifically the fainter-than convention):

```
#TYPE=EXTENDED
#OBSCODE=<from AAVSO_OBSCODE, see 5.3>
#SOFTWARE=VaST <version> + unmw source monitoring
#DELIM=,
#DATE=JD
#OBSTYPE=CCD
<NAME>,<JD .4f>,<mag .3f>,<err .3f>,CV,NO,STD,ENSEMBLE,na,na,na,na,na,na,<camera comment string>
```

- Fainter-than records: magnitude field is the limit preceded by `<`
  (e.g. `<17.500`) and the uncertainty field is `na` - the standard AAVSO
  fainter-than convention (verify the current wording of the spec online
  before finalizing).
- FILT=CV for all NMW cameras (unfiltered CCD with V zero point), MTYPE=STD,
  CNAME=ENSEMBLE (the zero point is fitted to many catalog stars), the
  remaining comparison/check fields `na`.
- NOTES = the per-camera `AAVSO_COMMENT_STRING`, extracted by PARSING
  `util/transients/transient_factory_test31.sh` (a small Python helper greps
  the per-camera blocks for `CAMERA_SETTINGS` -> `AAVSO_COMMENT_STRING`), so
  the camera description table is never duplicated.

### 5.3 OBSCODE

- unmw side: `AAVSO_OBSCODE` exported from `local_config.sh` (added to
  `local_config.sh_example` with a placeholder). The monitoring AAVSO writer
  uses it; if unset, falls back to the value in
  `AAVSO_previously_used_header.txt` if present in the VaST copy, else `XXX`
  with a warning in the log.
- VaST side (the one VaST change besides nothing): in
  `util/format_lightcurve_AAVSO.sh` the OBSCODE resolution becomes
  env-overridable - a preset `AAVSO_OBSCODE` environment variable wins over
  the `AAVSO_previously_used_header.txt` lookup and the `SKA` default.

## 6. Update flows

One entry point, `monitoring_update.py` (importable helpers in
`nmw_monitoring_lib.py`), invoked in two modes. All measurement work uses the
existing engine: `setup_vast_working_copy()`, `_phase1_parallel_solve_plate()`,
`run_forced_photometry_c()` / `util/forced_photometry.sh --list`, with
`FORCED_PHOTOMETRY_AIRMASS_ZEROPOINT=yes` always in the environment.

### 6.1 Manual modes (command line ONLY - never invoked by uploads)

All manual modes refuse to run under a CGI environment (`GATEWAY_INTERFACE`
check, like `archive_phot_worker.py`), take the global monitoring flock in
`uploads/.monitoring_locks/` (a second instance EXITS with a clear message
instead of queuing), and have no window, image-count or runtime caps.

- `monitoring_update.py --reconcile`
  Run by hand after editing `monitoring_list.txt` - this is the only way a
  source gets activated. It validates every list line (malformed lines are
  reported and skipped, never fatal) and, for every valid line whose id has
  no registry directory OR whose `backfill_done` marker is missing
  (interrupted-backfill resume):
  1. create `uploads/monitoring/<source_id>/` if needed;
  2. determine covering fields with `run_sky2xy_scan()` over
     `$REFERENCE_IMAGES` (used only within this backfill run - not cached);
  3. ONE-TIME BACKFILL:
     a. archival pass - `discover_archive_images($IMAGE_ARCHIVE_DIR,
        covering_fields)`, measure the source on every archive image,
        append ledger. If `IMAGE_ARCHIVE_DIR` is unset, or the directory is
        missing or empty (machines with no long-term archive, only the
        `uploads/img_*` backlog), the pass is SKIPPED with a clear log note
        - never an error;
     b. recent pass - enumerate ALL `uploads/img_*` directories (NOT the
        windowed `list_recent_field_images()` - no age cap; on archiveless
        machines this months-long backlog IS the whole backfill), measure on
        every field-matching solved image, append ledger (basename dedup
        makes the archive/recent overlap harmless);
     c. touch `backfill_done` (meaning: the initial enumeration completed
        over whatever populations existed at that time; an archive attached
        LATER is picked up by the manual `--rescan-archive`);
  4. rebuild derived files, plot, per-source page, central page.
  Sources with an existing `backfill_done` marker are untouched
  (report-only). The backfill measures per image at archive-photometry cost
  (~0.5-2 min/image), so a well-covered field means hours - run under
  nohup/screen; per-image progress is printed.

- `monitoring_update.py --rescan-recent [<source_id> | --all]`
  Re-enumerate ALL `uploads/img_*` directories for the given (or every)
  registered source and measure only the images missing from the ledger.
  Incremental by construction; safe to run any time; intended for filling
  gaps after monitoring outages or after adding the hook to an already-busy
  server.

- `monitoring_update.py --rescan-archive [<source_id> | --all]`
  Same, over `$IMAGE_ARCHIVE_DIR` - the manual gap-filler for images that
  migrated into the archive without ever being measured. Does NOT depend on
  `BACKFILL_DONE` (that flag only prevents the automatic first-time backfill
  from repeating).

### 6.2 The per-upload path: measure INSIDE the factory run, ingest after

No working copy is ever created on this path (user requirement): the
measurement happens inside the factory's own disposable VaST copy, which
already holds every expensive per-image product.

**(a) Pre-factory prep, in `autoprocess.sh` after unpacking, before the
factory run** (pure text work, no VaST tools except sky2xy):
- If the registry root `uploads/monitoring/` DOES NOT EXIST: exit silently.
  The root is only ever created by the first manual `--reconcile` run on
  that machine, so its existence is the per-machine "monitoring is deployed
  here" switch. Machines that merely received `monitoring_list.txt` through
  the shared calibration tree (the list is distributed via git across
  processing machines) behave exactly as before monitoring existed - no
  warnings, no log noise, no cost.
- If the root exists but `monitoring_list.txt` contains valid sources that
  are NOT activated (no registry dir), log loudly:
  `monitoring: N sources in monitoring_list.txt are not activated on this
  machine - run monitoring_update.py --reconcile`.
- For each ACTIVATED source (registry dir exists; coordinates read from the
  list line matching the dir id): one `sky2xy` call against a freshly
  unpacked frame decides on-field membership (a second or two for tens of
  sources; no covering-fields cache to go stale).
- If any sources are on-field: write an ASCII positions file
  (`RA Dec source_id` per line) and `export MONITORING_POSITIONS_FILE=<path>`
  for the factory. Otherwise leave the variable unset.

**(b) In-factory measurement block, in `transient_factory_test31.sh`**,
gated by `MONITORING_POSITIONS_FILE` being set and non-empty (unset = the
block does not exist; fully backward compatible). Runs after the per-ref
forced-photometry calibration block, operating on the SECOND-EPOCH images
only (the reference frames belong to the archive backfill; the ledger would
dedup them anyway):
1. For each second-epoch image: build its zero-point calibration reusing the
   run's own products - the solved `wcs_fd_*` frame and its `.wcscat`
   catalog already exist in CWD, so this is only the catalog match +
   `fit_zeropoint` (+ the airmass zero-point fit; monitoring always runs
   with `FORCED_PHOTOMETRY_AIRMASS_ZEROPOINT=yes`) - seconds per image, via
   the same per-camera calibrator dispatch as the per-ref block (local
   Tycho-2 for TYCHO2_V fields, never a per-image VizieR round trip).
2. `sky2xy` the monitored positions onto each image; run ONE
   `util/forced_photometry <image> --list <pixlist> <aper> --calib <that
   image's param>` call per image; apply the airmass term the same way
   `report_transient.sh` does.
3. Append the results to an ASCII hand-off file next to the report:
   `monitoring_raw_measurements.txt`, one line per (source, image):
   `source_id image_basename JD mag err status camera`.
   Off-frame positions yield `edge` rows (recorded in the ledger so they are
   never retried, published nowhere).
Added latency to the upload processing: ~5-15 s total when sources are
in-field, zero otherwise.

**(c) Post-factory ingest**, from the `autoprocess.sh` SUCCESS branch only
(where `transient_report/index.html` is confirmed), detached so transient
alerts are never delayed:

```sh
# autoprocess.sh, success branch, before cleanup:
setsid python3 "$SCRIPT_DIR/monitoring_update.py" --ingest "$MONITORING_RAW_FILE" \
    >> "$IMAGE_DATA_ROOT/monitoring_update.log" 2>&1 < /dev/null &
```

`--ingest` is pure text processing plus one plot render per touched source:
check-then-append each raw line into the source's ledger under its flock
(basename dedup - reprocessed uploads no-op here), then rebuild the derived
files, plot, source page and the central page atomically. No VaST copy, no
solving, no calibration; a few seconds. On a failed factory run the ingest
is never invoked, so failed runs append nothing.

### 6.3 Trigger timeline - when each image population is measured

| population | measured when | triggered by |
|---|---|---|
| archive images | once, during the source's first `--reconcile` backfill; gaps later only via manual `--rescan-archive` | command line only |
| recent images (`uploads/img_*`, all ages) | once, during the same backfill; gaps later only via manual `--rescan-recent` | command line only |
| latest images (the just-processed upload) | inside the factory run itself (block 6.2b), for activated sources only; ledger append by the detached `--ingest` on the SUCCESS branch | the per-upload path |

Interactions: a source added to the list is invisible to the per-upload path
until `--reconcile` activates it; uploads processed between activation and
any rescans are caught by the per-upload path; an upload processed while a
backfill runs is handled by the per-source flock + basename dedup (worst
case the same image is measured twice, stored once).

### 6.4 Pages

- Per-source `index.html`: `results_page_head()`-style header, display name,
  coordinates, point/limit counts, first/last JD, the `lightcurve.png` plot,
  links to `lightcurve.dat`, `upperlimits.dat`, `lightcurve_aavso.txt`, and
  a table of the most recent ~50 measurements (`_html_row` style). Rebuilt
  atomically (`write_index_html_atomic` pattern).
- Central `uploads/monitoring/index.html`: one row per source (name, coords,
  N points, N limits, last JD, camera set, link), sorted by name; rebuilt on
  every change; linked from `move_to_htdocs/index.html` next to the forced
  photometry links.

## 7. Files changed / added

unmw:
1. `nmw_monitoring_lib.py` - NEW: list parsing/validation, id sanitizer,
   registry paths, ledger append/read, derived-file rebuild (ASCII, AAVSO,
   plot projections), page rendering, factory-parser for
   `AAVSO_COMMENT_STRING`, locks.
2. `monitoring_update.py` - NEW: `--reconcile`, `--rescan-recent`,
   `--rescan-archive` and `--ingest` entry points (refuses to run as CGI,
   like `archive_phot_worker.py`).
3. `autoprocess.sh` - the pre-factory positions-file prep (reads the list
   for coordinates of ACTIVATED sources only, logs loudly when the list has
   sources not activated on this machine) and the detached success-branch
   ingest kick (~15 lines total).
4. `move_to_htdocs/index.html` - link to the central monitoring page.
5. `local_config.sh_example` - `AAVSO_OBSCODE`.
6. `.htaccess` - deny web execution of `monitoring_update.py` and
   `nmw_monitoring_lib.py` (same list as the archive worker).
7. `archive_phot_prune.sh` - no change needed (it only touches
   `archive_phot_*`/`forced_phot_*` prefixes), but add a comment + assertion
   that `monitoring/` is out of scope.

VaST:
8. `util/transients/transient_factory_test31.sh` - the env-gated monitoring
   measurement block of section 6.2b (second-epoch per-image zero-point
   calibrations reusing the run's own `.wcscat` products, sky2xy, one
   `util/forced_photometry --list` call per image, airmass term, hand-off
   file). Unset `MONITORING_POSITIONS_FILE` = block skipped entirely.
9. `util/format_lightcurve_AAVSO.sh` - honor a preset `AAVSO_OBSCODE`
   environment variable over the header-file lookup and the built-in default.
   (Everything else on the VaST side is reused as-is; monitoring measurements
   inherit the airmass-aware zero-point through the factory machinery.)

## 8. Implementation order

1. VaST `AAVSO_OBSCODE` override (tiny, independently testable).
2. `nmw_monitoring_lib.py` core: list parser, sanitizer, ledger, derived-file
   rebuild incl. the AAVSO writer (verify the fainter-than convention against
   the online AAVSO documentation at this step); unit-style checks with a
   synthetic ledger.
3. `monitoring_update.py --reconcile` + backfill against a test uploads tree
   (small archive dir + one `img_*` dir from an existing test dataset; a
   known variable such as V0615 Vul in the NMW-STL NovaVul24 test data is the
   natural end-to-end target). `--rescan-recent`/`--rescan-archive` fall out
   of the same code path.
4. Pages + central index + landing-page link.
5. The VaST factory measurement block (6.2b), verified standalone by
   exporting `MONITORING_POSITIONS_FILE` by hand for a factory run on a test
   dataset and checking `monitoring_raw_measurements.txt`; then the
   `autoprocess.sh` prep + detached `--ingest` kick; end-to-end test:
   process a test upload of a field hosting an activated source, verify the
   ledger gains exactly the new second-epoch rows and the derived
   files/pages update, and that a reprocessed upload appends nothing.
6. shellcheck / py_compile everything; document config knobs in README.

## 9. Compatibility and safety notes

- No change to any existing unmw flow when `monitoring_list.txt` is absent or
  empty: the hook exits immediately after the prefilter.
- The registry lives under `uploads/` (web-served): result pages and data
  files are directly linkable, same as archive photometry jobs; no secrets
  are stored (the list is admin-curated).
- All writes are temp+rename atomic; ledger appends under per-source flock;
  the backfill is serialized globally. A crash mid-update loses at most the
  in-flight append and self-heals on the next run (idempotent rebuilds,
  basename dedup).
- astrocam-go is unaffected; the only VaST change is the OBSCODE override,
  which is backward compatible (unset variable = current behavior).

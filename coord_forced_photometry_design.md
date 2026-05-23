# Coordinate forced-photometry lightcurve page -- design

Status: implemented (validated end-to-end on real data).
Spans two repositories: `unmw` (the CGI + form + shared module) and
`vast` (`util/forced_photometry.sh`, `src/pgfv/pgfv.c`).

## 1. Motivation

Provide, as a web page, a more capable version of the manual one-liner:

```
for i in $(ls -dt /data_ssd/NMW-TexasTech/workdir/img_2026-05-??_CI_Aql-02-Q*/wcs_fd_*) ;do
  util/forced_photometry.sh "$i" 19:20:11.64 +01:40:40.6 V | grep -A1 'C implementation:' | tail -n1
done
```

A user enters sky coordinates and receives forced aperture photometry at that
position for every image covering it taken in the last week that is found
in the `uploads/` directory, in reverse chronological order, with image
previews, cutouts, links to the FITS files, and a copy-paste plain-text
photometry table.

## 2. User-facing behaviour

- Input: one sky position (sexagesimal or decimal), parsed exactly like
  `coord_search.py`, plus an optional band override (default: the band derived
  from the camera; see section 7).
- Output page, newest first, one row per `wcs_fd_` image. Rows are streamed
  one at a time as each measurement completes so the user can see progress
  rather than waiting for the whole batch. Columns:
  - `Date (UTC)` (ATel-style fractional day) and `JD (UTC)`;
  - `mag` and `err` (rounded to two decimal places; `mag` is prefixed with
    `>` for upper-limit detections), and `Status` (verbatim string from the
    forced-photometry C tool: `detection`, `upperlimit`, or `saturated`);
  - `Band` and `Field` (the camera/field is read from the image name);
  - `Cutout` -- a zoom-in centred on the measured pixel with a red circle
    drawn at the photometric aperture used for that image;
  - `Image` -- a full-frame thumbnail of the image, with a `FITS` link
    placed directly under it for the (world-viewable) `wcs_fd_` file.
  Both thumbnails open a higher-resolution PNG (HIRES_THUMBNAIL_MULTIPLIER
  times larger, capped at `MAX_THUMBNAIL_PIXELS`) when clicked, the same
  pattern coord_search.py uses.
- Faint placeholder rows are emitted for images that yield no measurement
  (off-frame or calibration failure) so the streamed table keeps advancing.
- A separate ASCII-only table that can be copied verbatim follows the HTML
  table (rendered once at the end, since its column widths depend on the
  full result set). Mag values are rounded and prefixed identically.
- Time window: last `WINDOW_DAYS` (default 7), fixed, by the
  `img_YYYY-MM-DD` directory date.
- Testing knob `MAX_IMAGES_FOR_TESTING` (set to 5 while iterating) caps how
  many of the matching images are actually measured per request; when active
  a visible "Testing mode: ..." line says so. Set to `None` for production.

## 3. Architecture and data flow

```
coordinates (RA, Dec)
  -> parse_coordinates()                              [shared module]
  -> run_sky2xy_scan() over $REFERENCE_IMAGES         [shared module]
       -> covering reference FITS files
  -> field_name_from_fits() -> SET of covering fields [shared module]
       (all cameras: the reference set is co-pointed and multi-camera,
        so e.g. both Aql-02-Q1b1x1 and Aql-02-Q2b1x1 appear naturally)
  -> for each of the last 14 calendar dates:
       glob uploads/img_<date>_*/wcs_fd_*.fits
       keep images whose field_name_from_fits is in the covering set
  -> per image, SEQUENTIALLY (no cap):
       derive band by parsing transient_factory_test31.sh
       FORCED_PHOTOMETRY_ONLY_C=yes \
         util/forced_photometry.sh <img> <RA> <Dec> <BAND>   (cwd = VaST dir; see section 10)
       parse: JD, mag/limit, err, status, aperture diameter, pixel x,y
       date + JD from util/get_image_date
       render preview  (util/fits2png <img> x y)
       render cutout   (util/make_finding_chart --targetaperturecircle <APER> ...)
  -> sort by JD descending
  -> emit HTML table + photometry table
```

The reference-image coverage scan and the cutout rendering are exactly the
mechanisms `coord_search.py` already uses; they are moved into a shared module
(section 5) so the two pages stay consistent.

## 4. Inputs, locations, conventions

- Uploads root: `uploads/` (served), a symlink to
  `/data_ssd/NMW-TexasTech/workdir`. Per-upload directories are named
  `img_<YYYY-MM-DD>_<...field+camera+id...>` and contain the calibrated,
  plate-solved images `wcs_fd_<Field>-..._LIGHTs_NNNN.fits` (the only ones we
  measure -- they carry WCS for RA/Dec -> pixel and are dark+flat corrected).
- Reference images: `$REFERENCE_IMAGES`
  (`/data_ssd/NMW-TexasTech/reference_imgs`), one or more reference frames per
  field per camera.
- Field name: the image-basename token before the first `_`, after stripping a
  `wcs_fd_` / `wcs_` / `fd_` prefix (the existing `field_name_from_fits()`
  convention; mirrors `transient_factory_test31.sh`).
- Served URL of uploads: `$URL_OF_DATA_PROCESSING_ROOT`
  (`http://tau.kirx.net/unmw/uploads`).

## 5. Shared module (consistent updates between the two pages)

New module `nmw_coord_lib.py` containing the functions common to
`coord_search.py` and the new page. `coord_search.py` is refactored to import
them (behaviour identical, verified); the new CGI imports the same module. A
change to any shared helper then updates both pages at once.

Functions/constants to move into the module:

- Page chrome: `html_escape`, `_PAGE_CSS`, `back_link_url`, `form_page_url`,
  `emit_redirect`, `emit_message_page`.
- Input parsing: `parse_coordinates`.
- Config: `read_config_vars`.
- Concurrency limit: `acquire_concurrency_slot`.
- Coverage scan: the bash scan loop + `run_sky2xy_scan`.
- Image metadata: `get_image_metadata`, `_reformat_sexagesimal`.
- Thumbnails: `_run_pgfv_tool`, `zoomout_png_dims`, `make_zoomout_thumbnail`,
  `make_zoomin_thumbnail`.
- File/field helpers: `field_name_from_fits`, `list_fits_files`.

Stays in `coord_search.py`: its `main()` and the helpers specific to its result
table (`emit_match_row`, `emit_listall_row`, `render_*` cells).

## 6. Generality across cameras

Nothing is hardwired to the Q1/Q2 (NMW-TexasTech) cameras. The page must work
for any camera known to `transient_factory_test31.sh` and `combine_reports.sh`
(today: `Stas`, `STL-11000M`, `TICA_TESS`, `ED80__Black`, `TTUQ1b1x1`,
`TTUQ2b1x1`; these are the cameras listed in `combine_reports.sh:101`).

- Covering fields: come only from the reference-image scan, so any camera with
  references in `$REFERENCE_IMAGES` is supported, and co-pointed multi-camera
  setups are found without special-casing.
- Camera detection: parse the `*"PATTERN"* -> CAMERA_SETTINGS` rules near the
  top of `transient_factory_test31.sh` and apply them to each field/image name.
- Date window: consider only directories whose names start with
  `img_<YYYY-MM-DD>`; parse that date and keep the last `WINDOW_DAYS` days
  (default 7). Any other
  directories in `uploads/` are unrelated and ignored (their names do not start
  with `img_<YYYY-MM-DD>`).
- SExtractor configuration: per-image, the CGI selects the same camera-
  specific `default.sex.<...>` file that `transient_factory_test31.sh` would
  use for that camera, by parsing the factory script the same way the band
  is derived (see `sextractor_config_for_camera`). When the per-camera
  block lists two SEXTRACTOR_CONFIG_FILES, the **second** is picked,
  matching the script's documented convention that "the first run is
  optimized to detect bright targets while the second one is optimized
  for faint targets". When the per-camera block does not set it, the
  script's global default (top-of-file `if [ -z "$SEXTRACTOR_CONFIG_FILES"
  ];then ... fi`) is used; nested `if/fi` inside camera blocks are
  ignored via an indent-aware block matcher. The chosen file (e.g.
  `default.sex.telephoto_lens_vSTL` for Q1/Q2) is copied over the working
  copy's `default.sex` right before each forced-photometry call. If the
  chosen file is missing the generic `default.sex` is left in place --
  measurement still proceeds, just with the unrefined settings.

## 7. Band derivation (by parsing the transient factory script)

For each field/image:

1. Determine `CAMERA_SETTINGS` using the parsed camera-detection rules.
2. Determine `PHOTOMETRIC_CALIBRATION`: if the camera's block in
   `transient_factory_test31.sh` sets it explicitly (e.g. `TICA_TESS` ->
   `APASS_I`), use that; otherwise the factory's field-of-view default
   (`APASS_V` for narrow fields, `TYCHO2_V` otherwise).
3. Map `PHOTOMETRIC_CALIBRATION` to a band letter by taking the token after the
   underscore, so `APASS_V` and `TYCHO2_V` both -> `V`, `APASS_R` -> `R`,
   `APASS_I` -> `I`, `APASS_B` -> `B`, `APASS_g` -> `g`, and so on.

For all cameras currently in use this yields `V` (matching the one-liner); the
one exception is `TICA_TESS`, whose block sets `PHOTOMETRIC_CALIBRATION=APASS_I`
explicitly, giving `I`. The form always offers an optional band-override field;
when the user sets it, it takes precedence over the derived band.

Note on the field-of-view default: the factory's non-explicit default chooses
`APASS_V` (narrow field) vs `TYCHO2_V` (wide field) at runtime, but both map to
`V`, so we do not need to evaluate the field of view -- the default band is `V`
regardless. The only non-`V` band among current cameras (`TICA_TESS` -> `I`)
comes from an explicit per-camera setting that the parse reads directly.

## 8. VaST changes (must not break existing functionality)

### 8.1 `src/pgfv/pgfv.c`: new `--targetaperturecircle <diameter_pix>`

A new, isolated finder-chart option. In finder-chart mode, when set (> 0),
draw a red circle (`cpgsci(2); cpgcirc(markX, markY, diameter/2.0); cpgsci(1)`)
at the target. The existing `--targetmark` cross (used by
`util/make_finding_chart_script.sh` and the fastplot finder charts in transient
reports) and all other modes remain byte-identical. Requires a full `make`.

### 8.2 `util/forced_photometry.sh`: new `FORCED_PHOTOMETRY_ONLY_C` flag

Environment flag, default unset -> current behaviour (runs both C and Python),
so the transient pipeline and the two `util/examples/test_vast.sh`
forced-photometry tests are unaffected. When `=yes`:

- skip the Python implementation step;
- additionally print, on stdout before the `# C implementation:` block, two
  machine-readable lines for the caller:
  - `# aperture_diameter_pix: <APERTURE>`
  - `# target_pixel: <x> <y>`

The `# C implementation:` line format is unchanged.

## 9. Output

### 9.1 HTML results table (reverse chronological by JD)

One row per `wcs_fd_` image, streamed (one `<tr>` per finished measurement,
flushed) so the table fills in live. Nine columns, in this order:

| Date (UTC) | JD (UTC) | mag | err | Status | Band | Field | Cutout | Image |

The order is approximate JD-descending: rows are emitted in the order of the
embedded filename timestamp (a cheap proxy for JD that is known before the
image is measured), so the streamed and final orderings match.

- `mag` and `err` are formatted to two decimal places; `mag` carries a `>`
  prefix when `Status` is `upperlimit`. Non-numeric values pass through.
- `Cutout`: `util/make_finding_chart --width N --nolabels
  --targetaperturecircle <APER> -- <img> x y`. With `--targetaperturecircle`
  in effect, `pgfv.c` suppresses the default `+` marker so the aperture
  circle is the only annotation drawn.
- `Image`: `util/fits2png <img> x y` thumbnail with a `FITS` link rendered
  directly below it (the same `wcs_fd_` file is served from
  `$URL_OF_DATA_PROCESSING_ROOT` + the image's path relative to `uploads/`).
- Both thumbnails are clickable; the anchor target is a higher-resolution
  PNG of the same view, rendered alongside the thumbnail
  (`HIRES_THUMBNAIL_MULTIPLIER` times bigger, capped at
  `MAX_THUMBNAIL_PIXELS`). Shared with coord_search.py via
  `nmw_coord_lib.render_thumbnail_link`.
- Images where sky2xy reports the target off the frame, or where
  `forced_photometry.sh` exits non-zero, yield a faint `tr.skipped` row that
  still names the image, the field and a `FITS` link, so each skipped image
  visibly advances the streamed table.

### 9.2 Photometry table (plain-text, in a `<pre>` for easy copy)

```
date             JD            mag/limit  err   status      field          image
2026-05-20.7822  2461181.2822  13.21      0.02  detection   Aql-02-Q1b1x1  wcs_fd_Aql-02-Q1b1x1_2026-05-20_..._0214.fits
2026-05-20.7799  2461181.2799  >18.10     -     upperlimit  Aql-02-Q2b1x1  wcs_fd_Aql-02-Q2b1x1_2026-05-20_..._0210.fits
```

`mag/limit` and `err` use the same formatting as the HTML table: rounded to
two decimal places, with a `>` prefix on the magnitude when `status` is
`upperlimit`. `status` is passed through verbatim from the forced-photometry
C tool (`detection`, `upperlimit`, `saturated`).

Columns are left-aligned and space-padded to a fixed width so they line up. The
status column is padded to the longest expected value (`upperlimit`, 10
characters); the magnitude/limit and error columns are likewise padded to their
widest expected values. This keeps the table readable when pasted into a
monospaced editor or an ATel.

### 9.3 Date and JD

Both the JD and the ATel-style calendar date come from `util/get_image_date`
run on the image, for consistency with the rest of the codebase (it is
well-tested), rather than from a private Python conversion. Its output already
contains the two lines we parse:

```
         JD 2461177.86532463
 ATel style 2026-05-17.36532
```

We take the `JD` and `ATel style` values and present them to 4 decimal places
(e.g. `2026-05-17.3653  2461177.8653`).

## 10. Per-request scratch and cleanup

The served PNG thumbnails (full-frame preview and zoom-in cutout) for each
request go in `uploads/forced_phot_<pid><rand>/`, mirroring `coord_search.py`'s
per-request directory. The CGI creates this directory and leaves it in place;
it must not delete or prune anything. Cleanup of `uploads/forced_phot_*` (and
`uploads/coord_search_*`) is handled by separate, external housekeeping programs
and is out of scope for this feature.

`util/forced_photometry.sh` (via `calibrate_single_image.sh` ->
`solve_plate_with_UCAC5`) uses some paths relative to the VaST tree and writes
its scratch (plate-solve products, SExtractor catalogs, calib.txt) into the
current directory. To keep that scratch out of `$VAST_REFERENCE_COPY` (and away
from the main transient pipeline that also runs there), the CGI makes a
**disposable per-request working copy of the VaST tree the same way
`autoprocess.sh` does** and runs forced photometry inside it:

- `rsync -a --whole-file --no-times --omit-dir-times` of `$VAST_REFERENCE_COPY`
  into `uploads/vast_forced_phot_<pid><rand>/`, excluding the large/static data
  (`astorb.dat`, `lib/catalogs`, `src`, `.git`, `.github`);
- that excluded data is then symlinked back (`astorb.dat`, `lib/catalogs`),
  exactly as `autoprocess.sh` does;
- `forced_photometry.sh` is run with its cwd inside this copy, so all its
  scratch lands there and is fully isolated per request;
- `local_config.sh` is sourced before running it -- exactly as `autoprocess.sh`
  does before `transient_factory_test31.sh` -- so the calibration runs with the
  production environment (Python venv, `VAST_SEXTRACTOR_CACHE_DIR`, data-root
  exports). Without this the bare Apache CGI environment makes the calibration
  fail and every image yields no measurement;
- the CGI `rm -rf`s the working copy when the request finishes (like
  `autoprocess.sh`), so it does not accumulate.

Because each request is isolated in its own copy, `FORCED_PHOT_MAX_CONCURRENT`
(default 5) only caps server load, not correctness. The served PNG thumbnails
still go to `uploads/forced_phot_<pid><rand>/` and are left for external
housekeeping; only the VaST working copy is deleted by the CGI.

## 11. Backward compatibility

- `pgfv.c`: additive option only; no existing mode changes.
- `forced_photometry.sh`: new behaviour is gated behind a default-off flag;
  existing callers, the transient pipeline, and the forced-photometry tests are
  bit-for-bit unaffected.
- `coord_search.py`: shared helpers are extracted verbatim into
  `nmw_coord_lib.py` and imported back; the page's rendered output is unchanged
  (to be verified by running it after the refactor).

## 12. Testing

- vast: full `make`; verify the red aperture circle renders and that
  `--targetmark` output (fastplot finder charts) is unchanged; `shellcheck`
  `forced_photometry.sh`; smoke-test `FORCED_PHOTOMETRY_ONLY_C=yes` on one
  image; confirm the two forced-photometry tests in `test_vast.sh` still pass.
- unmw: run `coord_search.py` after the refactor to confirm identical output;
  run the new CGI via `python3` against the CI Aql example coordinates on the
  real `uploads/` directory and check the HTML table, photometry table, FITS
  links, previews, and red-circle cutouts.

## 13. Deployment note

Deployment is handled by the maintainer; this section only records the
architecture (what goes where). The changes live in two repositories:

- `unmw` (`/tmp/unmw`, = `origin/main`, currently ahead of the deployed copy
  under `/var/www/tau.kirx.net/cgi-bin/unmw/`): `nmw_coord_lib.py` (new),
  `coord_search.py` (refactored to import it), `coord_forced_photometry.py`
  (new CGI), `coord_forced_photometry.html` (new form). The new CGI and the
  refactored `coord_search.py` both import `nmw_coord_lib.py`, so these four
  files form one consistent set.
- `vast`: `util/forced_photometry.sh` (new `FORCED_PHOTOMETRY_ONLY_C` flag) and
  `src/pgfv/pgfv.c` (new `--targetaperturecircle` option; requires rebuild).

## 14. Decisions and open items

Decided:
- New standalone CGI (not a mode of `coord_search.py`).
- Common code shared via a new module imported by both pages.
- Only-C enforced via a `forced_photometry.sh` flag.
- New dedicated `pgfv.c` flag for the red aperture circle.
- Sequential processing, no cap.
- 14-day window by directory date, fixed; only `img_<YYYY-MM-DD>` directories
  are considered.
- HTML rows carry the photometry result alongside preview/cutout/FITS link;
  the plain-text photometry table repeats the same numbers (intentional).
- Plain-text photometry table columns include field and image name, left-
  aligned and padded to fixed widths.
- Dates and JD via `util/get_image_date` (not a private Python conversion).
- Band derived by parsing `transient_factory_test31.sh`; the band letter is the
  token after the underscore in `PHOTOMETRIC_CALIBRATION` (`APASS_V`/`TYCHO2_V`
  -> `V`, `APASS_R` -> `R`, `APASS_I` -> `I`, ...). `TICA_TESS` -> `I`; all
  other current cameras -> `V`. An optional form field overrides it.
- Camera-agnostic (works for any camera known to the factory / `combine_reports.sh`).
- Names: module `nmw_coord_lib.py`; CGI/form `coord_forced_photometry.{py,html}`;
  pgfv flag `--targetaperturecircle`; env `FORCED_PHOTOMETRY_ONLY_C`.
- Optional band-override field on the form: included.
- Each row links only the measured `wcs_fd_` FITS image (no directory link).
- The CGI performs no cleanup; external housekeeping prunes the scratch dirs.
- Deployment handled by the maintainer (section 13).

Open: none outstanding; ready to implement on approval.

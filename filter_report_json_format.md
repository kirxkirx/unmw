# `_filtered.json` format

`filter_report.py` writes a JSON document alongside the `_filtered.html` it produces. Same basename, `.json` extension:

```
20260512_evening_TTUQ1b1x1.html              (input combined report)
20260512_evening_TTUQ1b1x1_filtered.html     (existing filtered output)
20260512_evening_TTUQ1b1x1_filtered.json     (this file)
```

The JSON is intended for downstream tools that want to render the candidate list in a different UI from the bundled HTML. The schema (permissive) lives next to the script as `filter_report_json_schema.json`.

The schema is intentionally permissive. Most fields are nullable, additional properties are allowed, and only `schema_version`, `generated_at_utc`, `candidates`, plus per-candidate `id` and `classification`, are strictly required.

## Schema history

| `schema_version` | Notes |
|---|---|
| 1 | Initial version. |
| 2 | Added `known_transient` to the `classification` enum and the `totals` object; added `crossmatches.{tocp, asassn_transients, tns_transients, neverexclude_list, moons, planets, spacecraft}` for exclusion-list matches. |

## Versioning

| Change | `schema_version` action |
|---|---|
| Add a new optional field | unchanged |
| Add a new optional value to an enum | unchanged |
| Remove or rename a field, tighten a type, change semantics | bump |

Consumers should ignore fields they do not understand.

## Top level

| Field | Type | Description |
|---|---|---|
| `schema_version` | integer | Currently `1`. Bumped on breaking changes. |
| `generated_at_utc` | string (ISO 8601, e.g., `2026-05-12T08:14:33Z`) | When the JSON was produced. |
| `source_report` | string \| null | Filename of the input combined HTML report. |
| `url_of_data_processing_root` | string \| null | Base URL used to absolutize relative image paths (`$URL_OF_DATA_PROCESSING_ROOT` from the unmw config). |
| `error` | string \| null | Present and non-null only when parsing the input failed. When set, `candidates` is `[]`. |
| `parse_warnings` | string[] | Top-level warnings (e.g., "VAST_REFERENCE_COPY not set, falling back to Python date conversion"). |
| `session` | object \| null | Session metadata parsed from the input filename. |
| `totals` | object \| null | Candidate counts by classification. |
| `candidates` | object[] | Per-candidate records. |

### `session`

| Field | Type | Example |
|---|---|---|
| `date_utc` | string \| null | `"2026-05-12"` |
| `session`  | string \| null | `"morning"` or `"evening"` |
| `camera`   | string \| null | `"TTUQ1b1x1"`, `"TTUQ2b1x1"` |

### `totals`

| Field | Type | Description |
|---|---|---|
| `total`           | integer \| null | Total candidates included. |
| `new`             | integer \| null | Candidates classified `new`. |
| `known_asteroid`  | integer \| null | Candidates classified `known_asteroid`. |
| `known_variable`  | integer \| null | Candidates classified `known_variable`. |
| `known_transient` | integer \| null | Candidates classified `known_transient` (TOCP / ASAS-SN-list / TNS-list matches). |

## Per-candidate object

Required: `id`, `classification`. Everything else may be `null` or omitted.

| Field | Type | Notes |
|---|---|---|
| `id` | string | Same string as the `<a name>` anchor in the HTML, e.g., `20369_Cyg5_2026-5-9_23-4-47_003`. Stable across re-runs. |
| `field` | string \| null | Field name, e.g., `Cyg5`, `Oph-05-Q1b1x1`. Parsed from the "FIELD field processing log" link if present; otherwise from the `id` via regex. |
| `classification` | enum: `new`, `known_asteroid`, `known_variable`, `known_transient` | See "Classification precedence" below. Consumers should treat unknown classification values as `new`-equivalent so future additions stay forward-compatible. |
| `mean` | object \| null | Mean magnitude and position on the discovery images. |
| `second_epoch_separation` | object \| null | Separation between detections on the two second-epoch images. |
| `cutouts` | object \| null | Absolute URLs of small cutout PNGs. |
| `discovery_images` | object[] \| null | One row per row in the per-candidate HTML table. |
| `crossmatches` | object \| null | One sub-object per crossmatch catalog. |
| `forced_photometry` | object \| null | Forced photometry on reference images. |
| `external_links` | object \| null | Absolute URLs of external catalog/service searches. |
| `report_stubs` | object \| null | Pre-formatted text blocks (MPC, TOCP, AAVSO, VSNET). |
| `parse_warnings` | string[] | Per-candidate parse warnings. Empty array when everything parsed cleanly. |

### `mean`

| Field | Type | Notes |
|---|---|---|
| `date_utc_iso` | string \| null | ISO 8601 UTC, e.g., `2026-05-09T23:04:50Z`. Computed via `$VAST_REFERENCE_COPY/util/get_image_date` when available. |
| `date_utc_dayfraction` | string \| null | `"YYYY MM DD.fffff"` as printed verbatim in the source report. |
| `jd_utc` | number \| null | Julian Date (UTC). |
| `mag` | number \| null | Mean magnitude on the discovery images. |
| `ra_hms` | string \| null | `"HH:MM:SS.SS"`. |
| `dec_dms` | string \| null | `"+DD:MM:SS.S"` or `"-DD:MM:SS.S"`. |
| `ra_deg` | number \| null | Decimal degrees, 0 .. 360. |
| `dec_deg` | number \| null | Decimal degrees, -90 .. 90. |
| `galactic_l_deg` | number \| null | Galactic longitude, decimal degrees. |
| `galactic_b_deg` | number \| null | Galactic latitude, decimal degrees. |
| `constellation` | string \| null | Three-letter constellation code, e.g., `Cyg`. |

### `second_epoch_separation`

| Field | Type | Notes |
|---|---|---|
| `arcsec` | number \| null | Separation in arcseconds. |
| `pix` | number \| null | Separation in pixels. |

### `cutouts`

Object whose keys are `reference`, `discovery1`, `discovery2`, optionally `discovery3`, etc. Values are absolute URLs to the cutout PNGs. Keys whose corresponding image was not present are omitted (rather than `null`).

### `discovery_images[]`

One element per row of the HTML discovery-image table. The first row in the table may be the *reference* image (in which case `label` is `"Reference image"`); the rest are `"Discovery image N"`.

| Field | Type | Notes |
|---|---|---|
| `label` | string \| null | Verbatim first-column label (e.g., `"Reference image"`, `"Discovery image 1"`). |
| `date_utc_iso` | string \| null | ISO 8601 UTC. |
| `date_utc_dayfraction` | string \| null | `"YYYY MM DD.fffff"`. |
| `jd_utc` | number \| null | |
| `mag` | number \| null | |
| `ra_hms` | string \| null | |
| `dec_dms` | string \| null | |
| `x_pix` | number \| null | Image-pixel coordinate (column). |
| `y_pix` | number \| null | Image-pixel coordinate (row). |
| `fits_path` | string \| null | Local FITS path as listed in the HTML table (the producer's filesystem path). |
| `fits_url` | string \| null | Absolute URL to the FITS, if a matching link is present in the `fullframepreview_*` section. |

### `crossmatches`

Object whose keys are catalog or exclusion-list identifiers. The currently emitted ones split into two groups:

**Catalog cross-matches** (from "The object was {found|not found} in X" lines):

| Key | Source |
|---|---|
| `vsx` | VSX |
| `asassn_v` | ASASSN-V |
| `astcheck` | astcheck (Solar System body / asteroid catalog) |

**Exclusion-list matches** (from "This object is listed in `<file>`" lines). At most one of these keys is present per candidate (VaST emits at most one such marker):

| Key | List file |
|---|---|
| `tocp` | `tocp_transients_list.txt` -- objects on the CBAT TOCP page |
| `asassn_transients` | `asassn_transients_list.txt` -- ASAS-SN-listed transients |
| `tns_transients` | `tns_transients_list.txt` -- TNS-registered transients |
| `neverexclude_list` | `neverexclude_list.txt` -- manually whitelisted targets |
| `moons` | `moons.txt` -- Solar System moons |
| `planets` | `planets.txt` -- planets |
| `spacecraft` | `spacecraft.txt` -- artificial satellites / known spacecraft |

Each value is:

| Field | Type | Notes |
|---|---|---|
| `found` | boolean | Catalog crossmatches always include this; exclusion-list keys are only present when `found` is true. |
| `raw` | string \| null | Catalogs: multi-line block of follow-up details (separation, name, type, period, etc.). Exclusion lists: trailing detail on the same line (e.g., `162.2"  Titan (8.7mag)`). Captured verbatim, no further parsing. |

#### Classification precedence

A candidate may match several of these simultaneously (e.g. a TOCP transient that's also in VSX). `classification` is set by the first rule that fires, in order:

1. astcheck match -> `known_asteroid`
2. VSX or ASASSN-V match within the variable-star tolerance -> `known_variable`
3. `tocp_transients_list.txt`, `asassn_transients_list.txt`, or `tns_transients_list.txt` match -> `known_transient`
4. Otherwise -> `new`

A candidate matched only against `neverexclude_list.txt`, `moons.txt`, `planets.txt`, or `spacecraft.txt` (without any of the rules above firing) is classified as `new` -- those lists do not by themselves indicate a known transient. The match is still recorded in `crossmatches` so downstream tools can show or filter on it.

### `forced_photometry`

| Field | Type | Notes |
|---|---|---|
| `per_image[]` | object[] \| null | One element per `Forced photometry on wcs_*.fits at HMS DMS:  MAG +/- ERR  FLAG` line. |
| `reference_weighted_avg` | object \| null | From `Forced photometry reference-image weighted average:  MAG +/- ERR`. |

Each `per_image[]` element:

| Field | Type | Notes |
|---|---|---|
| `wcs_fits` | string \| null | Basename of the wcs_*.fits file. |
| `position_hms` | string \| null | `"HH:MM:SS.SS +DD:MM:SS.S"`. |
| `mag` | number \| null | |
| `mag_err` | number \| null | |
| `flag` | string \| null | Free string. Seen values include `detection`, `upperlimit`, `calib_fail`, `edge`, `tool_fail`. Consumers should not assume a closed enum. |

### `external_links`

Object whose keys identify the destination service. Currently:

| Key | Destination |
|---|---|
| `tns` | wis-tns.org search |
| `asassn_list` | ASAS-SN transients page |
| `simbad` | SIMBAD search |
| `vizier` | VizieR search |
| `wise` | IRSA WISE atlas |
| `aladin_lite` | Aladin Lite |
| `snad_ztf` | SNAD ZTF viewer |
| `asas3` | ASAS-3 lightcurve query (if the form is present) |
| `nmw_archive` | NMW sky archive query (if the form is present) |

Values are absolute URLs. Keys for missing destinations are omitted.

### `report_stubs`

| Field | Type | Notes |
|---|---|---|
| `mpc_mean` | string \| null | One MPC-format line at the mean position. |
| `mpc_per_image` | string[] \| null | One MPC-format line per discovery image. |
| `tocp` | string \| null | One TOCP-format line. |
| `aavso` | string \| null | Full text block in AAVSO extended format. |
| `vsnet` | string \| null | Full text block in VSNET format. |

## Special-case JSON documents

### No candidates found in the input

```json
{
  "schema_version": 1,
  "generated_at_utc": "...",
  "source_report": "...",
  "session": { "date_utc": "...", "session": "...", "camera": "..." },
  "totals": { "total": 0, "new": 0, "known_asteroid": 0, "known_variable": 0 },
  "candidates": []
}
```

### Parsing error

```json
{
  "schema_version": 1,
  "generated_at_utc": "...",
  "source_report": "...",
  "error": "<short description of what failed>",
  "candidates": []
}
```

## Parse-warning keys

The producer never drops a candidate. When a sub-field cannot be extracted, the candidate object still appears with that field set to `null` and a string is appended to `parse_warnings`. The keys are intentionally informal (free strings) but the producer tries to keep them stable. Examples:

- `"mean: regex no match"`
- `"discovery_images: table not found"`
- `"crossmatches: VSX block parse failed"`
- `"forced_photometry: regex no match"`
- `"report_stubs: mpc div not found"`
- `"field: could not derive from link or id"`
- `"date: get_image_date subprocess failed, used Python datetime fallback"`

Consumers should treat `parse_warnings` as informational, not a hard contract.

## Date handling

The combined HTML report already prints two date formats next to each timestamp: a "year month day.fraction" dayfraction (e.g., `2026 05 09.96127`) and the corresponding JD (e.g., `2461170.46127`). Both are captured verbatim into `date_utc_dayfraction` and `jd_utc`.

To produce the ISO 8601 `date_utc_iso`, `filter_report.py` invokes:

```
$VAST_REFERENCE_COPY/util/get_image_date <JD>
```

once per unique JD encountered (memoized). The "(mid. exp) YYYY-MM-DDTHH:MM:SS.000" line is parsed and the `T...` portion plus a trailing `Z` becomes `date_utc_iso`. If `$VAST_REFERENCE_COPY` is unset or the binary is not executable, the script falls back to Python's `datetime` (still correct) and records a top-level `parse_warnings` entry to flag the fallback.

## URL absolutization

Relative `src=` / `href=` paths in the combined HTML are turned into absolute URLs in the JSON by prepending `$URL_OF_DATA_PROCESSING_ROOT` (read from the environment, the same source `combine_reports.sh` uses). If `$URL_OF_DATA_PROCESSING_ROOT` is unset, the value falls back to `http://vast.sai.msu.ru/unmw/uploads` (matching `combine_reports.sh`'s default). Paths that are already absolute (start with `http://` or `https://`) are kept as-is.

# HANDOFF: nightly transient-search statistics

Updated 2026-11-18 after review of the first prototype. This is the current
scope and design; it replaces the earlier, much broader page/digest proposal.
The previous handoff is preserved outside the repo at
`/tmp/unmw-night-summary-prototype-v2/HANDOFF_previous.md` for historical
pipeline research, not as an implementation specification.

## 1. Status and authorization

- **Discuss the revised design with the user before implementing statistics
  code in either repository.** The statistics feature is still unimplemented.
- The user authorized updating this document and making a local prototype.
  The new prototype is outside both repos:
  `/tmp/unmw-night-summary-prototype-v2/index.html`.
- **Only authorized production-code exception: azimuth.** Display Az next
  to Altitude/El in the existing summary log and make VaST print it when a
  header supplies it. This work is described in section 8.
- **Postpone Slack entirely.** Do not implement a digest, webhook setting,
  posting schedule, sent-message state, or Slack preview. The previous
  acceptance of a daily digest is superseded by the latest feedback. Existing
  unrelated pipeline notifications are outside this change.
- Stage the task's changed files, including this document, but **never commit
  or push**. Preserve unrelated staged and unstaged work. Both repos were
  already dirty; VaST's factory had unrelated cloud-selection edits.
- No FITS header modifications. Read headers/logs only.
- ASCII data files use ordinary spaces, never tab-separated formatting.

The source review used unmw HEAD `4f5daf1`, VaST HEAD `7c8fdf41` plus their
existing working changes, and the saved production sample described below.
A checkout's current commit is not evidence of a historical processing build.

## 2. Page decisions from the user's review

| Previous item | Revised decision |
|---|---|
| Nights | **Nightly search coverage**. Page/table title describes the observer's question. |
| Three-week table | **28 observing nights / four weeks**, including rows with no received images. |
| UTC display | Server local time, with timezone shown. Tau uses America/Chicago, CST/CDT; do not use the viewer's timezone or this workstation's America/Denver. |
| Uploads and Fields counters | Keep **Uploads** only: one received field visit. Repeat visits remain separate observations; no distinct-field counter. |
| Clean | Use the exact summary-log status **OK**. Do not introduce a new success definition. |
| Sky / Telescope / System / Workload columns | Remove. These assignments imply causes the logs do not establish. |
| Too many candidates | Treat the recorded excess as an **image-quality symptom**, not human workload. Do not infer a particular physical cause. |
| Searched / left out / not searched | One operational outcome: **In combined list / Not included**, with Unknown where evidence is insufficient. |
| Lim. mag and FWHM | Retain; show units and available-data coverage. |
| Data 01:04–04:01 UTC 2.9 h | **Observation span: Nov 13 19:04–22:01 CST**; explain “first to last recorded exposure, including gaps”. It is not summed exposure time or observing efficiency. The revised sample uses first-frame header times where available, so exact endpoints can differ from the old prototype. |
| Reference age | Remove. |
| Depth difference | Immediately after Limiting magnitude. Define sign and aggregation explicitly. |
| Sky level and Zero point | Remove: heterogeneous images make nightly medians unhelpful. |
| Plate solution: matched stars + RMS | Retain **Plate-solution RMS (arcsec)** only. |
| Cooling: 0.0 °C | Replace with median **CCD temperature (CCD-TEMP)**. Temperature mismatch events are a separate warning, not hidden by the median. |
| Workload section | Remove; retain **Unidentified candidates: N in M uploads**, preferably from uploads represented in the combined lists. |
| Software | Show **VaST build and unmw processing build**. List multiple builds when a night straddles an update. Historical absence is “not recorded”. |
| Runs that need a look | **Error statistics for this night**: counts and evidence, not a queue of human intervention tasks. |
| Long-term plots | Scatter only, one point per camera-night. Remove running medians and connecting lines. |
| Blame column | Remove, including the derived “lost to system” headline. |
| Fields section / field-health page | Remove from scope, including phase 2. Field names may remain in example logs. |

Also remove the low-value focus-position counts, pier-side counts, extinction
summary, candidate-before-filtering totals, exclusion-list update totals,
and reference/sky/zero-point panels from the default page. Keep server timing
breakdowns behind details. Do not replace the removed clutter with a new
collection of cards and alerts.

Previously decided constraints still apply: no “night complete” detection;
no observing-plan comparison, uploader heartbeat or upload ledger; no Moon
column; no asteroid-recovery or known-variable/transient-count performance
metrics; no human-vetting records. The former “no VaST changes” rule now has
the explicit azimuth exception only.

## 3. What the observer should see

### 3.1 Four-week overview

Columns:

`Night  Camera  Uploads  OK  ERROR  In combined list  Lim. mag  FWHM (pix)`

Use evening-date night labels and local times wherever times are displayed.
A camera selector changes overview rows, night panels and plot points.
`CAMERA STUCK`, when present, retains that exact status beside ERROR rather
than being silently relabelled. OK + ERROR + CAMERA STUCK equals the upload
count for records with a known summary status. Missing status is unknown.

“In combined list” displays included/total with the not-included count
beneath it. For historical summary-only ERROR rows, show an explicit
unknown count; do not imply that every ERROR was excluded. No-data rows say
“No images received” for a camera active on at least 3 of the preceding 14
nights. This does not distinguish weather, a camera outage or an uploader
outage. The snapshot date and source coverage must be visible.

The prototype includes 2026-10-21 through 2026-11-17: 28 dates and two cameras,
56 rows. A live implementation should include a current night as its data
arrive, within the same 28-date window; no final/complete state is needed.

### 3.2 Selected night report

Show one selected night, with a panel per selected camera:

1. Included/total and not-included counts, plus exact summary-log statuses
   and links to the combined lists that contain that night's results.
2. Observation span in server local time, with dates when crossing midnight.
3. Limiting magnitude, **then depth difference**, FWHM, plate-solution RMS.
4. Actual median CCD-TEMP and temperature warnings.
5. Unidentified candidates in the combined lists, in N uploads. This is
   detections, not unique astrophysical sources or confirmed transients.
6. Exposure-to-finished-report delay (median and 90th percentile).
7. Both software builds, with missing historical values explicitly labelled.
8. Expandable delay/temperature details and **Error statistics for this
   night** with affected-upload counts and links to example logs.

Temperature details should appear automatically when abnormal. Show frame
skips only when the log explicitly confirms a skip. A report that recovered
with other frames must still retain its temperature-warning events.

### 3.3 History and errors

Scatter plots: uploads per night, fraction with summary status OK, limiting
magnitude, FWHM. These are descriptive; they are not causal diagnoses. No
running median, fitted line or percentile clipping of outlier points. Expose
values through tooltips and a table. Keep camera colours consistent.

Error statistics list the recorded class, counts per camera, number whose
results are not included (and unknown counts), and an example log. No Blame
column. Count an upload at most once per class; classes can overlap, so their
counts need not sum to the number of erroneous uploads. Historic summary-only
rows contain only the first ERROR text; full reports can reveal more classes.
Avoid “needs attention” labels for completed processing outcomes.

## 4. Definitions that matter

### 4.1 OK is not the same as inclusion

In the old prototype, “clean” meant a completed search without recognised
ERRORs. It happened to match summary status OK in all 672 sampled full
reports. That observed agreement should not become a separate public
status definition: **read OK from the summary row**.

`combine_reports.sh` marks ERROR for ERROR text anywhere in a report. Some
errors are recoverable; a later SExtractor pass can complete. Therefore:

- The `Prepared the HTML report for the field` marker establishes a finished
  search pass, but does not by itself establish publication in a combined list.
- A summary row plus a completed search and no explicit exclusion provides
  evidence of inclusion in the sampled pipeline. OK rows provide that evidence
  directly. A successfully searched upload with zero candidates is included.
- A skipped/failed search has no search results to represent even if its
  empty report shell was copied to the combined HTML.
- An explicit combine exclusion establishes not-included.
- `combine_reports.log` alone is insufficient: it records excluded reports
  as handled too. Do not treat ledger membership as publication success.
- Without enough evidence, use Unknown. For future live records, preserve
  search completion, publication outcome and exact summary status as
  independent facts; render their simple included/not-included view.

The old generic comment “too many candidates (...) to include in the combined
list” is also used for failed candidate-count parsing and missing report
markers. `99999 with no ID` is an error sentinel, not 99,999 candidates.
Do not classify every such historical comment as bad images. Preserve an
ambiguous exclusion class when the underlying cause cannot be verified.

Verified sample, night 2026-11-13:

| Camera | Uploads | OK | ERROR | Included | Not included |
|---|---:|---:|---:|---:|---:|
| TTUQ1b1x1 | 67 | 56 | 11 | 62 | 5 |
| TTUQ2b1x1 | 89 | 73 | 16 | 86 | 3 |

This illustrates why ERROR cannot substitute for not-included. Combined:
148/156 included; 8 not included. The 672 full reports across both nights
contain 623 OK and 49 ERROR rows; 29 of the ERROR rows completed a search,
including one explicitly excluded result.

### 4.2 Units, medians and missing values

- Summary limiting magnitude is missing from many ERROR rows. Show how many
  uploads contribute; do not present it as an unbiased all-image average.
  For historical scatter plots use a consistent summary-only definition.
- FWHM per upload is the larger of the two selected new-frame FWHMs; nightly
  value is the median over measured uploads. Missing is not zero.
- Depth difference per upload = **limiting magnitude of the worse (shallower)
  new frame minus the mean limiting magnitude of its two reference frames**.
  Then take the nightly median. Positive is deeper; negative is shallower.
  The first prototype incorrectly implemented a median of the two new frames
  while the handoff specified the worse frame. The revised prototype uses the
  worse frame. A production parser must match a single completed pass/config
  and identify new/reference frames by the logged names, not filename prefixes.
- Plate-solution RMS = `sigma_overall_arcsec` from the final solution of each
  selected new frame; nightly median across measured frames. Retain frame
  identities so repeated retries do not inflate the count.
- CCD temperature = median actual `CCD-TEMP` across distinct recorded new
  frames. Pair `CCD-TEMP` with `SET-TEMP` from **the same frame**, not by zipping
  separately filtered arrays. Report measured-frame counts.
- Observation span includes gaps and is not integrated exposure or duty cycle.
  Prefer first start to last exposure end from headers; state reduced coverage
  if only the summary's last-frame times are known.
- Exposure-to-finished-report is not combined-list publication latency. The
  combiner runs roughly every eight minutes; publication can occur later.
  Do not label a finish timestamp “available to observer” without evidence.
- Keep internal times as UTC instants, format using the server's actual
  timezone rules (including DST), and show the zone with displayed times.
  Naive historical local timestamps around a DST fold can be ambiguous.

### 4.3 Temperature warning and skipped-frame evidence

Verified in this VaST checkout:

- `src/ccd/ms.c` sets `MAX_CCD_TEMP_DIFF` to 3.0 °C and rejects dark subtraction
  when `abs(CCD-TEMP - SET-TEMP) > 3.0`, unless temperature checks are disabled.
  It prints, for example:
  `ERROR: mismatch between CCD-TEMP= ... and SET-TEMP= ... in ...! Looks like the the camera didn't have time to cool down.`
- That error occurs **before the complete `Record N:` header dump**. A parser
  using only successful header dumps will miss the affected frames. Parse the
  explicit event and preceding per-frame temperature lines as well.
- `try_to_calibrate_the_input_frame` returns failure, but its callers leave
  the original image path in place and can continue searching the raw image.
  Thus dark-calibration rejection is **not proof of an unprocessed frame**.
- `src/ccd/mk.c` and `mk_fast.c` also have explicit warnings ending in
  `skipping this frame` or `skipping this frame as reference`. Those establish
  a skip in that particular stacking operation, not necessarily a lost new
  science frame. Preserve the operation and frame role.

Required future behaviour: retain an event with frame identity, measured and
set temperatures when known, operation, explicit skip/rejection evidence and
resulting upload outcome. Surface **Temperature warnings** even when the
remaining processing succeeds; report confirmed skipped science frames
separately. Also warn on a measured positive excess above the configured
processing tolerance, with unknown coverage explicitly identified. Do not
silently change a temperature threshold, or hard-code this checkout's 3 °C
as a universal historical threshold. Use the actual log/processing build.

The current sample has no explicit temperature-mismatch events. “0 uploads
with messages in these reports” is supported; “0 input frames ever rejected”
is not. This reporting requirement is recorded here and demonstrated in the
prototype; production statistics/warning code awaits design approval.

## 5. Additional operational statistics worth considering

Prioritize these instead of restoring removed columns:

1. **Time until results are available**, median and p90. Split delivery,
   server wait and computation only when the total delay is abnormal.
   Existing sample: 45/156 uploads on Nov 13 waited more than 30 min for
   server load; maxima were about 169 min (Q1) and 155 min (Q2), while VaST
   itself usually took a few minutes. This suggests investigating processing
   capacity/concurrency before optimizing candidate vetting. It does not prove
   a specific CPU-temperature or hardware cause. The prototype uses the
   available finish time and labels it accordingly.
2. **Fraction of received visits absent from the combined list**, with
   recorded reasons and unknowns. This directly measures lost search coverage.
   A count of completed-but-unpublished reports would expose combiner outages
   and the existing 720-minute scan window problem. Collection from local
   result directories is required; summary pages alone omit these failures.
3. **Confirmed temperature-related frame skips / affected uploads**, retaining
   failures before usable-frame selection. This prevents a reassuring median
   CCD temperature from hiding cooling losses.
4. **Quality versus altitude/azimuth**, as an optional diagnostic scatter plot
   for FWHM, depth difference or RMS. It can reveal a repeatable poor horizon
   sector or mounting/focus behaviour. Compare like camera/configuration and
   control for the field; do not assign a cause automatically. The azimuth
   exception supplies the missing coordinate for future data.

Possible later details, only if the user wants them: count/age of currently
pending results and fraction with missing dark/flat calibration. No speculative
“blame” score. Do not add all proposed metrics to the overview. Items 1–3 are
the recommended first operational priorities; item 4 is a drill-down proposal.

## 6. Pipeline facts and implementation proposal (not authorized yet)

### 6.1 Existing sources and limits

- `upload.py3` validates, creates `web_upload_*`, starts `wrapper.sh`, waits
  briefly for a result URL. Rejected uploads are deleted; no upload ledger is
  requested. Frames never delivered are outside the observable denominator.
- `autoprocess.sh` creates `results_*`, waits for load/CPU/I/O, unpacks images,
  runs VaST, appends verdicts and timing, then deletes upload/VaST work dirs.
  Raw/calibrated images and per-run reports usually survive until later cleanup.
- `combine_reports.sh` runs about every eight minutes. It scans recently
  modified results (720 minutes), combines completed reports per camera,
  writes shared summary rows and records handled paths. Existing page names
  follow server combine time and an evening/morning split, not observing night.
- `results_<YYYYMMDD_HHMMSS>_...` encodes server-local start/arrival before load
  wait. Dataset times encode camera packing time; on TTU this follows DST.
  Do not treat an inferred camera offset as an exact authoritative timestamp.
- `Last  image: <JD> <date> <time> UTC`: JD is mid-exposure, printed civil time
  is start of the last image's exposure. Do not add half an exposure twice.
- A per-run `index.html` contains Processing log, Filtering log, profiling,
  `Processing complete!`, and often new-frame header dumps from dark subtraction.
  Dumps can be absent with no darks, reused calibration, or early failures.
- `autoprocess.txt` gives total, VaST, load-wait and unpack minutes, exit code,
  CPU temperature and results URL. The downloaded tail only covers recent runs.
- Existing server files cannot explain no-data nights, prove which candidates
  are real, or reconstruct missing historical unmw commits.

### 6.2 Night grouping and data model

Retain automatic observing-night grouping without a completion state:
find the middle of the longest daily gap over a camera's last 60 days, with
15-minute circular bins; require 20 samples and a four-hour gap, otherwise
use server local noon. Optional override can be retained. The tau sample's
boundary was 18:38 UTC, about local noon. Test date labels for both signs of
UTC offset and across DST. Space data may still use UTC date bins internally,
clearly identified as such; visible timestamps remain local as requested.

Persist one record per processing attempt with input identity, exact summary
status, completed search marker, publication outcome, error events, frame
measurements, times and **both processing builds**. Resolve reprocessing to
the latest conclusive attempt per input before calculating visit totals, and
identify duplicate upload retries by dataset/frame identity. Do not count
reprocessing as new sky coverage. Test data stay excluded.

Incomplete/running work is not automatically a failed result. Record its
known state and elapsed time. Do not infer a killed job only from a missing
`vast_*` directory without validating lifecycle evidence.

### 6.3 Minimal future implementation

After approval only:

- `nmw_night_stats_lib.py`: pure parsers, grouping, aggregation, rendering.
- `night_stats.py`: init/update/backfill/rebuild-pages. No Slack command.
- Data under `<DATA_PROCESSING_ROOT>/night_stats/`: versioned run ledger,
  grouping state, HTML/JSON snapshots, update log. Directory existence is the
  enable switch, analogous to existing monitoring.
- Update hook after `combine_reports.sh` releases its lock. Own nonblocking
  lock, bounded runtime, failures logged without blocking combined lists.
- Search both `IMAGE_DATA_ROOT` and `DATA_PROCESSING_ROOT` where distinct.
- Atomic writes; escape all file names, field names and log text in HTML.
- Use existing page chrome and dependency-free inline SVG. No new dependencies.
- Capture unmw processing commit at processing time; never substitute the
  current rendering commit for historical runs. Display generator version
  separately. Missing legacy builds remain unknown. This instrumentation is
  part of the future implementation and is not included in the Az-only edit.
- No field-health page, no digest state, no extra upload logging.

Test the report grammar, both summary layouts, soft ERROR + included result,
explicit exclusion, missing report/unknown inclusion, zero candidates,
reprocessing/deduplication, no-data rows, local times/DST, partial measurements,
multiple builds, temperature errors before header dumps and recovery after a
rejected calibration. Use offline fixtures. Do not contact production services
or send notifications from tests.

New root scripts must have CGI guards and `.htaccess` denial, as documented in
README. Production auto-updates tracked files from main after CI; the feature
must remain inert until enabled locally. Once the design is agreed, consolidate
it into the repo's usual tracked `night_stats_design.md`.

## 7. Revised prototype and review limitations

Local page: `/tmp/unmw-night-summary-prototype-v2/index.html`.
Builder: `/tmp/unmw-night-summary-prototype-v2/build_prototype.py`.
Audit counts: `/tmp/unmw-night-summary-prototype-v2/audit.json`.

It reuses parsers from the previous external prototype, with a new renderer
and corrected temperature pairing/depth aggregation. A local parsed-data cache
speeds repeat rendering; remove `parsed_sample.pickle` if input samples or
parser definitions change. The page is a complete, self-contained HTML document
with camera/night selectors, theme toggle, source links and SVG scatter plots.
No deployment, registration, production hook or Slack posting occurs.

Input sample (read-only, already downloaded):
`/tmp/claude-10220/-tmp-unmw/421bec67-93a7-425d-bbeb-f6757ca339c3/scratchpad/real/`.
It contains 354 summary pages / 61,792 parsed result rows and 672 full reports
for observing nights 2026-11-12 and 2026-11-13, plus recent timing logs.
Rebuild:

```sh
python3 /tmp/unmw-night-summary-prototype-v2/build_prototype.py \
  /tmp/claude-10220/-tmp-unmw/421bec67-93a7-425d-bbeb-f6757ca339c3/scratchpad/real \
  /tmp/unmw-night-summary-prototype-v2/index.html
```

The old published artifact is obsolete:
https://claude.ai/artifact/LFdmuVakBtLWFmjF4XHtxx . This task does not edit it.

Prototype limitations: snapshot only; summary-only historical inclusion can
be unknown; no access to upload/verdict ledgers; no validated deduplication or
reprocessing sample; early failures can lack metrics and build strings; legacy
new/reference/pass association needs stronger production fixtures; no inferred
unmw builds; all examples come from TTU on tau. `/tmp` artifacts are not durable
repository assets. Keep a copy before reboot/cleanup if review continues.

## 8. Azimuth implementation, staging and verification

### unmw

The already staged Azimuth column in `combine_reports.sh` remains immediately
after Altitude. Older half-night pages keep their original column layout when
new rows are appended; subsequent pages get Azimuth. This task extends
`nmw_second_epoch_azimuth` in `report_helpers.sh` to prefer the explicit
`Field azimuth during imaging:` line, with the old `CENTAZ`/`OBJCTAZ` header-dump
fallback for older VaST reports.

Azimuth always belongs to the image furnishing the lowest reported altitude.
If that image lacks azimuth, the cell is blank; the helper no longer borrows
another frame's azimuth. Without altitude, use the first available azimuth.
Keep 0° valid. README documents the added cross-repo string contract.
`test_python.py` tests new/legacy reports, missing values, precedence,
range checks and same-frame pairing. Existing staged `unmw_selftest.sh`
column checks are retained.

### VaST

`util/transients/transient_factory_test31.sh` now reports both coordinates
from each selected new frame through `report_second_epoch_image_altaz`:

```text
Field altitude during imaging: 22.6 deg (FITS keyword CENTALT)  image.fits
Field azimuth during imaging: 239.8 deg (FITS keyword CENTAZ)  image.fits
```

Preserve the altitude line contract. Prefer CENTALT/CENTAZ, fall back to
OBJCTALT/OBJCTAZ, accept decimal and sexagesimal values, validate coordinate
ranges, and emit an informational no-value note when unavailable. The read
is independent of dark subtraction and does not modify FITS headers. Do not
use the separate `pixel_flux_airmass_correction` computed azimuth: the old
handoff identified a geometry bug there, outside this authorized change.

`util/examples/test_transient_altaz.sh` checks ten cases through real
`util/listhead` on temporary minimal FITS images and verifies unchanged header
hashes and identical stdout/report-log output. Only Az-related factory hunks
and this test are to be staged; preserve the unrelated VaST edits unstaged.

Validation performed during this revision:

- unmw report-helper selection: 26 passed.
- Full `test_python.py`: 288 passed; 39 unrelated archive tests skipped because
  the RAR binary is absent. These are not claimed as full CI success.
- VaST coordinate regression: 10 cases passed.
- Bash syntax and fatal ShellCheck checks on changed scripts; fatal Python
  flake8 classes on `test_python.py` passed.
- Prototype rendered in local headless Chromium; data totals checked against
  the saved sample. Eleven browser assertions passed, including camera/night
  selection, overview links, theme, mobile overflow and scatter-only plots.
- Real `combine_reports.sh` exercised in disposable directories with five
  direct/legacy/missing/error/stuck report cases. All rows aligned under new
  (13-column), altitude-only (12-column), and older (11-column) headers.

The network-dependent `unmw_selftest.sh` science workflow was not run. Do not
claim it passed based on helper tests. No commits or pushes were made.

Suggested one-line commit messages:

- unmw: `Show azimuth beside altitude and document the revised nightly summary design`
- VaST: `Report FITS azimuth alongside altitude for transient-search images`

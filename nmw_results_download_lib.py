"""
Downloadable copies of forced-photometry results: ZIP archives (with and
without FITS cutouts) and a full-frame FITS download script.

Shared by the recent-images page (coord_forced_photometry.py), the archival
photometry queue worker (archive_phot_worker.py) and the download CGI
(forced_phot_download.py). Not a CGI entry point; import-safe (no work at
import time). .htaccess denies web access to this file.

At the end of every run the measuring code writes, into its per-request
directory uploads/<name>/ next to the result PNGs and lightcurve files:

  download_manifest.json  the run parameters and every frame of the run
                          (server path at measurement time, target pixel,
                          frame size, photometry), written by write_manifest()
  index.html              the results page (the archival worker always wrote
                          it; coord_forced_photometry.py now keeps a copy of
                          the page it streams, see StreamedPageCopy)

and ends the page with the three links of download_links_html(). Everything
else is made when somebody clicks a link (forced_phot_download.py):

  <name>_results.zip                every file the page shows or links (the
                                    page itself with relative links, PNGs,
                                    lightcurve files, skip log), the
                                    photometry table as CSV and plain text, a
                                    README and the full-frame download script
  <name>_results_with_cutouts.zip   the same plus cutouts/: a FITS cutout of
                                    FORCED_PHOT_CUTOUT_PIXELS (default 501)
                                    pixels square centred on the target from
                                    every measured frame, made with VaST's
                                    util/fitscopy (cfitsio image sections
                                    shift CRPIX; the TAN-SIP solution stays
                                    valid)
  download_fits.sh                  bash script that downloads the full frames
                                    with wget -c or curl -C - (resumable)

Each ZIP is built once, published atomically into the request directory and
then served by the web server as a static file (resumable downloads, pruned
together with the directory by archive_phot_prune.sh). Frames are looked up
where they are at click time: sort_workdir_images.sh moves them into
good_wcs_fd_images/, archive_frame_quality.sh into frame_quality/, and the
operator may offload them to IMAGE_QUARANTINE_DIR. Quarantine frames are not
web-served: they get cutouts but no full-frame link.
"""

import csv
import datetime
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.parse
import zipfile

from nmw_coord_lib import html_escape

# ---------- names ----------

DOWNLOAD_CGI_NAME = 'forced_phot_download.py'
MANIFEST_BASENAME = 'download_manifest.json'
PAGE_BASENAME = 'index.html'
CSV_BASENAME = 'photometry.csv'
TXT_BASENAME = 'photometry.txt'
SKIP_LOG_BASENAME = 'forced_phot_skipped.log'
SCRIPT_BASENAME = 'download_fits.sh'
URL_LIST_BASENAME = 'fits_urls.txt'
README_BASENAME = 'README.txt'
CUTOUT_SUBDIR = 'cutouts'
BUILD_LOCK_BASENAME = '.download_build.lock'
BUILD_TMP_PREFIX = '.download_build_'
MANIFEST_VERSION = 1

TOOL_RECENT = 'forced_phot'
TOOL_ARCHIVE = 'archive_phot'

WHAT_ZIP = 'zip'
WHAT_ZIP_CUTOUTS = 'zip_cutouts'
WHAT_SCRIPT = 'script'
WHAT_VALUES = (WHAT_ZIP, WHAT_ZIP_CUTOUTS, WHAT_SCRIPT)
_ZIP_SUFFIXES = {WHAT_ZIP: '_results.zip',
                 WHAT_ZIP_CUTOUTS: '_results_with_cutouts.zip'}

# Per-request directory names of the two tools: forced_phot_<pid><8 letters>
# (coord_forced_photometry.py) and archive_phot_<job id> (the archival queue,
# job id = UTC YYYYMMDD-HHMMSS_<8 letters>). Nothing else under uploads/ is
# ever served by the download CGI.
REQUEST_NAME_RE = re.compile(
    r'^(?:forced_phot_[0-9]{1,10}[A-Za-z]{8}'
    r'|archive_phot_[0-9]{8}-[0-9]{6}_[A-Za-z]{8})$')

# Pipeline directory layout (see sort_workdir_images.sh and
# archive_frame_quality.sh).
SORTED_GOOD_SUBDIR = 'good_wcs_fd_images'
SORTED_SUBDIRS = (SORTED_GOOD_SUBDIR, 'no_reference_image', 'other_images_to_delete')
FRAME_QUALITY_SUBDIR = 'frame_quality'
QUALITY_VERDICTS = ('review', 'rejected')
_IMG_DIR_RE = re.compile(r'^img_[0-9]{4}-[0-9]{2}-[0-9]{2}_[A-Za-z0-9_.+-]+$')

# ---------- limits ----------

DEFAULT_CUTOUT_PIXELS = 501
MIN_CUTOUT_PIXELS = 11
MAX_CUTOUT_PIXELS = 4001
# Archival jobs have no per-job image cap; the cutout ZIP has one, stated in
# its README and on the build page whenever it applies.
MAX_CUTOUTS_PER_ZIP = 2000
FITSCOPY_TIMEOUT_SECONDS = 120
# A build must leave the transient pipeline's own free-space floor intact:
# autoprocess.sh stops processing below WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB
# (default 5 GB), and downloads must never push the disk there. This much
# more stays free on top of it.
DISK_MARGIN_BYTES = 1024 ** 3
PIPELINE_HARDLIMIT_KB_DEFAULT = 5242880
MAX_BUILDS_AT_ONCE = 2
# Seconds between keep-alive lines while an archive is written, so Apache's
# Timeout (time without output from the CGI) never cuts a long build off.
KEEPALIVE_SECONDS = 5
# A build gives up after this long (a stalled disk would otherwise make every
# fitscopy run into its own timeout and hold a build slot for days).
BUILD_DEADLINE_SECONDS = 1800
_MAX_HEADER_BLOCKS = 1000           # 2.9 MB of FITS header is surely garbage

# Files of the request directory that go into the archives: the page and
# everything it shows or links. Nothing else (manifest, request/state JSON,
# progress log, locks, earlier archives) is ever copied.
_INCLUDED_FIXED_NAMES = (PAGE_BASENAME, SKIP_LOG_BASENAME)
_INCLUDED_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+-]*\.(?:png|eps|dat)$')
_STORED_EXTENSIONS = ('.png',)      # already compressed

# cfitsio's extended file-name syntax gives brackets, parentheses, '+N',
# '!', spaces and URL-like prefixes a meaning. Only plain absolute paths of
# these characters are ever handed to util/fitscopy.
_SAFE_FITS_PATH_RE = re.compile(r'^/[A-Za-z0-9_./-]+$')
_FITS_ENDINGS = ('.fits.fz', '.fit.fz', '.fts.fz', '.fits', '.fit', '.fts')

# The streamed page pads rows with whitespace comments to push them through
# Apache's CGI buffer; the saved copy drops them.
_FLUSH_PAD_RE = re.compile(r'<!-- {64,} -->\n?')

CSV_COLUMNS = ('date_utc', 'jd', 'mag', 'mag_err', 'status', 'band', 'field',
               'x_pix', 'y_pix', 'aperture_pix', 'image')


def zip_basename(name, what):
    """File name of the ZIP for request directory `name` and kind `what`."""
    return name + _ZIP_SUFFIXES[what]


def utc_now_str():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%d %H:%M:%S UTC')


def cutout_pixels_from_config(raw):
    """FORCED_PHOT_CUTOUT_PIXELS from local_config.sh, or the default when
    unset or outside [MIN_CUTOUT_PIXELS, MAX_CUTOUT_PIXELS]."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_CUTOUT_PIXELS
    if value < MIN_CUTOUT_PIXELS or value > MAX_CUTOUT_PIXELS:
        return DEFAULT_CUTOUT_PIXELS
    return value


# ---------- written by the measuring code at the end of a run ----------

def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def frame_entry(path, field, fits_link='', fp=None, row=None, band='',
                meta=None):
    """One frame of a run, as recorded in the manifest.

    fp is the run_forced_photometry_c() result (None when the frame gave no
    measurement), row the rendered results-table row (formatted date, JD,
    magnitude), meta the get_image_metadata() result (frame size)."""
    entry = {
        'path': os.path.abspath(path),
        'basename': os.path.basename(path),
        'field': field or '',
        'fits_url': fits_link or '',
        'status': 'skipped',
        'band': band or '',
        'date_utc': '', 'jd': '',
        'mag': '', 'mag_err': '', 'mag_display': '', 'err_display': '',
        'x': None, 'y': None, 'aperture_pix': None,
        'nx': None, 'ny': None,
    }
    if fp is not None:
        entry.update(
            status=str(fp.get('status') or ''),
            mag=str(fp.get('mag') if fp.get('mag') is not None else ''),
            mag_err=str(fp.get('err') if fp.get('err') is not None else ''),
            x=_float_or_none(fp.get('x')), y=_float_or_none(fp.get('y')),
            aperture_pix=_float_or_none(fp.get('aperture')))
    if row is not None:
        entry.update(date_utc=str(row.get('atel') or ''),
                     jd=str(row.get('jd') or ''),
                     mag_display=str(row.get('mag') or ''),
                     err_display=str(row.get('err') or ''))
    if meta:
        entry.update(nx=_int_or_none(meta.get('nx')),
                     ny=_int_or_none(meta.get('ny')))
    return entry


def write_manifest(out_dir, tool, ra, dec, coords_raw, parameters, frames,
                   url_prefix):
    """Record the run for the download CGI (atomically). Returns True on
    success; on failure the caller leaves the download links off the page."""
    manifest = {
        'version': MANIFEST_VERSION,
        'tool': tool,
        'name': os.path.basename(os.path.normpath(out_dir)),
        'ra': str(ra), 'dec': str(dec), 'coords_raw': str(coords_raw or ''),
        'parameters': dict(parameters or {}),
        'created_utc': utc_now_str(),
        'url_prefix': str(url_prefix or ''),
        'frames': list(frames),
    }
    path = os.path.join(out_dir, MANIFEST_BASENAME)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as fh:
            json.dump(manifest, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def download_links_html(cgi_base, name):
    """The three download links at the bottom of a results page."""
    base = '{}/{}?request={}&what='.format(
        (cgi_base or '/cgi-bin/unmw').rstrip('/'), DOWNLOAD_CGI_NAME,
        urllib.parse.quote(name, safe=''))
    links = [
        (WHAT_ZIP, 'Download results (ZIP)'),
        (WHAT_ZIP_CUTOUTS, 'Download results + FITS cutouts (ZIP)'),
        (WHAT_SCRIPT, 'FITS download script'),
    ]
    return ("<p class='downloads'>{}</p>\n"
            "<p class='secondary'>The ZIP archives hold everything shown on "
            "this page (the page itself, all images, the lightcurve and "
            "photometry tables) and the full-frame download script; the "
            "second one adds a FITS cutout centred on the target from every "
            "measured image. Each archive is prepared on the first click, "
            "which may take a minute. The script downloads the full-frame "
            "FITS images with wget or curl and can be re-run to resume.</p>"
            .format(' &middot; '.join(
                "<a href='{}' rel='nofollow'>{}</a>".format(
                    html_escape(base + what), html_escape(text))
                for what, text in links)))


class StreamedPageCopy(object):
    """sys.stdout stand-in that also writes the streamed page into a file.

    The copy goes to <path>.part and is renamed to <path> by finish(), so
    the file exists only for a page that was streamed to the end (the
    download CGI takes its presence as "the run finished"). Write errors on
    the copy abandon the copy; they never disturb the page itself."""

    def __init__(self, stream, path):
        self._stream = stream
        self._path = path
        self._part = path + '.part'
        self._fh = open(self._part, 'w', encoding='utf-8', errors='replace')

    def write(self, text):
        written = self._stream.write(text)
        if self._fh is not None:
            try:
                self._fh.write(_FLUSH_PAD_RE.sub('', text))
            except (OSError, ValueError):
                self.abandon()
        return written

    def flush(self):
        self._stream.flush()
        if self._fh is not None:
            try:
                self._fh.flush()
            except (OSError, ValueError):
                self.abandon()

    def finish(self):
        """Publish the copy; returns True when it is in place."""
        if self._fh is None:
            return False
        try:
            self._fh.close()
            self._fh = None
            os.replace(self._part, self._path)
            return True
        except OSError:
            self.abandon()
            return False

    def abandon(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except (OSError, ValueError):
                pass
            self._fh = None
        try:
            os.remove(self._part)
        except OSError:
            pass

    @property
    def stream(self):
        return self._stream

    def __getattr__(self, name):
        return getattr(self._stream, name)


# ---------- request directory checks (download CGI) ----------

def request_dir_path(uploads_dir, name):
    """Absolute path of the per-request directory `name` under uploads_dir,
    or None unless `name` has one of the two allowed shapes and is a real
    directory (not a symlink) directly inside uploads_dir."""
    if not isinstance(name, str) or not REQUEST_NAME_RE.fullmatch(name):
        return None
    base = os.path.abspath(uploads_dir)
    path = os.path.normpath(os.path.join(base, name))
    if os.path.dirname(path) != base:
        return None
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    return path


def read_manifest(request_dir):
    """Parsed manifest of a request directory, or None when it is missing,
    unreadable or not a manifest of this format."""
    path = os.path.join(request_dir, MANIFEST_BASENAME)
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return None
        with open(path) as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or \
            manifest.get('version') != MANIFEST_VERSION or \
            manifest.get('tool') not in (TOOL_RECENT, TOOL_ARCHIVE) or \
            not isinstance(manifest.get('frames'), list):
        return None
    frames = []
    for frame in manifest['frames']:
        if isinstance(frame, dict) and isinstance(frame.get('path'), str) \
                and isinstance(frame.get('basename'), str):
            frames.append(frame)
    manifest['frames'] = frames
    return manifest


def run_is_finished(request_dir, name, read_state=None):
    """True when the run of this request directory is complete: its page and
    manifest exist and, for an archival job, its state is 'done'.
    read_state(job_dir) is nmw_archive_phot_lib.read_state."""
    for basename in (PAGE_BASENAME, MANIFEST_BASENAME):
        try:
            if not stat.S_ISREG(os.lstat(
                    os.path.join(request_dir, basename)).st_mode):
                return False
        except OSError:
            return False
    if name.startswith(TOOL_ARCHIVE + '_'):
        state = read_state(request_dir) if read_state else None
        return bool(state) and state.get('state') == 'done'
    return True


# ---------- where the frames are now ----------

def _within(path, roots):
    """True when the real path of `path` is inside one of `roots`."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    for root in roots:
        if not root:
            continue
        real_root = os.path.realpath(root)
        if real == real_root or real.startswith(real_root.rstrip(os.sep) + os.sep):
            return True
    return False


def _lexically_within(path, roots):
    """True when the normalized path of `path` is inside one of `roots`
    (symlinks inside the root are allowed to point elsewhere)."""
    norm = os.path.normpath(path)
    for root in roots:
        if root and norm.startswith(os.path.normpath(root).rstrip(os.sep) + os.sep):
            return True
    return False


def _is_regular_file(path):
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def quality_frame_dirs(root, statuses=('', 'good', 'bad'), unreadable=None):
    """Existing flat frame directories produced by archive_frame_quality.sh."""
    if not root:
        return
    for verdict in QUALITY_VERDICTS:
        verdict_dir = os.path.join(root, FRAME_QUALITY_SUBDIR, verdict)
        try:
            reasons = sorted(os.listdir(verdict_dir))
        except FileNotFoundError:
            continue
        except OSError as err:
            if unreadable is not None:
                unreadable.append((verdict_dir, err))
            continue
        for reason in reasons:
            if reason.startswith('.'):
                continue
            reason_dir = os.path.join(verdict_dir, reason)
            for status in statuses:
                directory = os.path.join(reason_dir, status) if status else reason_dir
                if os.path.isdir(directory):
                    yield directory


def quality_upload_dirs(root, basenames):
    """Get the original img_<S> names of selected quality-sorted frames."""
    uploads = {}
    if not root or not basenames:
        return uploads
    pending = set(basenames)
    try:
        with open(os.path.join(root, FRAME_QUALITY_SUBDIR, 'measurements.txt'),
                  encoding='utf-8', errors='replace') as measurements:
            for line in measurements:
                fields = line.split(maxsplit=2)
                if len(fields) < 2:
                    continue
                img, frame = fields[:2]
                if not img.startswith('img_dir=') or not frame.startswith('frame='):
                    continue
                img, frame = img[8:], frame[6:]
                if frame in pending and _IMG_DIR_RE.fullmatch(img):
                    uploads[frame] = img
                    pending.remove(frame)
                    if not pending:
                        break
    except OSError:
        pass
    return uploads


def recent_frame_within(path, root):
    """Check a frame against its root, resolving operator archive directories."""
    if not root:
        return False
    parts = os.path.relpath(path, root).split(os.sep)
    sorted_frame = (len(parts) == 3 and parts[0] in SORTED_SUBDIRS
                    and _IMG_DIR_RE.fullmatch(parts[1]))
    quality_frame = (len(parts) in (4, 5) and parts[0] == FRAME_QUALITY_SUBDIR
                     and parts[1] in QUALITY_VERDICTS
                     and (len(parts) == 4 or parts[3] in ('good', 'bad')))
    # The archive directories are operator-controlled and may be symlinks to
    # another disk. An individual FITS symlink must stay within its directory.
    return _within(path, [os.path.dirname(path) if sorted_frame or quality_frame
                          else root])


def _frame_candidates(frame_path, roots, img_dir):
    """Current and possible archived paths of a recent-images frame."""
    base = os.path.basename(frame_path)
    img_dir = img_dir or os.path.basename(os.path.dirname(frame_path))
    yield os.path.normpath(frame_path)
    if _IMG_DIR_RE.fullmatch(img_dir):
        for root in roots:
            yield os.path.join(root, img_dir, base)
            for subdir in SORTED_SUBDIRS:
                yield os.path.join(root, subdir, img_dir, base)
    for root in roots:
        for directory in quality_frame_dirs(root):
            yield os.path.join(directory, base)


def locate_frame(frame, tool, workdir, quarantine_dir, archive_dir,
                 image_data_root='', img_dir=''):
    """Current path of a manifest frame, or None when it is gone (or its
    recorded path is not inside the trees this tool reads)."""
    base = frame.get('basename') or ''
    path = frame.get('path') or ''
    if not base or os.path.basename(path) != base or '/' in base or \
            not os.path.isabs(path):
        return None
    if tool == TOOL_ARCHIVE:
        # The archive is the operator's tree (month folders may be symlinks
        # to other disks): its frames only have to be inside it by name.
        candidates = [os.path.normpath(path)]

        def inside(candidate):
            return _lexically_within(candidate, [archive_dir])
    else:
        roots = [root for root in (workdir, image_data_root, quarantine_dir) if root]
        candidates = _frame_candidates(path, roots, img_dir)

        def inside(candidate):
            return any(recent_frame_within(candidate, root) for root in roots)
    for candidate in candidates:
        if os.path.basename(candidate) == base and \
                _is_regular_file(candidate) and inside(candidate):
            return candidate
    return None


def _served_url(prefix, path, root):
    """URL of `path` under a web-served `root`, percent-encoded, or ''."""
    if not prefix or not root:
        return ''
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return ''
    if rel == '.' or rel.startswith('..') or os.path.isabs(rel):
        return ''
    return '{}/{}'.format(prefix.rstrip('/'),
                          urllib.parse.quote(rel.replace(os.sep, '/'),
                                             safe='/'))


def current_fits_url(path, tool, workdir, url_prefix, archive_dir,
                     archive_url):
    """Full-frame download URL of a frame's current path ('' when it is not
    web-served: the quarantine tree, or the archive without
    IMAGE_ARCHIVE_URL)."""
    if not path:
        return ''
    if tool == TOOL_ARCHIVE:
        if archive_url and archive_dir and \
                _lexically_within(path, [archive_dir]):
            return _served_url(archive_url, os.path.normpath(path),
                               os.path.normpath(archive_dir))
        return ''
    if recent_frame_within(path, workdir):
        return _served_url(url_prefix, path, workdir)
    return ''


def resolve_frames(manifest, cfg_paths):
    """[(frame, current_path or None, current_url)] for every manifest
    frame. cfg_paths includes the work, uploads, quarantine and archive trees
    and the public URL prefixes."""
    tool = manifest['tool']
    workdir = cfg_paths.get('workdir')
    image_data_root = cfg_paths.get('image_data_root')
    quarantine_dir = cfg_paths.get('quarantine_dir')
    uploads = {}
    if tool == TOOL_RECENT:
        # A manually approved frame may have gone back to img_<S> since
        # photometry. Read each quality log once to recover its upload name.
        returned = {f.get('basename') for f in manifest['frames']
                    if FRAME_QUALITY_SUBDIR in (f.get('path') or '').split(os.sep)
                    and not os.path.isfile(f.get('path') or '')}
        for root in (workdir, image_data_root, quarantine_dir):
            uploads.update(quality_upload_dirs(root, returned - uploads.keys()))
    resolved = []
    for frame in manifest['frames']:
        current = locate_frame(frame, tool, workdir, quarantine_dir,
                               cfg_paths.get('archive_dir'), image_data_root,
                               uploads.get(frame.get('basename'), ''))
        url = current_fits_url(current, tool, workdir,
                               cfg_paths.get('url_prefix'),
                               cfg_paths.get('archive_dir'),
                               cfg_paths.get('archive_url'))
        resolved.append((frame, current, url))
    return resolved


# ---------- texts generated at click time ----------

_SAFE_URL_RE = re.compile(r'^https?://[A-Za-z0-9._~:/?#@!$&()*+,;=%-]+$')


def _shell_safe_url(url):
    return bool(url) and "'" not in url and bool(_SAFE_URL_RE.match(url))


def _comment_safe(text):
    """Text for a shell comment: printable ASCII without anything that could
    end the comment line."""
    return re.sub(r'[^A-Za-z0-9 _.:+-]', '?', str(text))


def fits_download_script(manifest, resolved):
    """bash script that downloads every full frame that has a URL."""
    name = manifest.get('name', '')
    if not REQUEST_NAME_RE.fullmatch(name):
        name = 'forced_phot'
    lines = [
        '#!/usr/bin/env bash',
        '# Full-frame FITS images measured by forced photometry',
        '# {} at RA {} Dec {} (J2000), {} frame(s); generated {}.'.format(
            name, _comment_safe(manifest.get('ra', '')),
            _comment_safe(manifest.get('dec', '')), len(resolved),
            utc_now_str()),
        '#',
        '# Usage: bash {} [DIRECTORY]   (default: ./fits_{})'.format(
            SCRIPT_BASENAME, name),
        '# Uses wget -c or curl -C -: run it again to resume interrupted '
        'downloads.',
        'set -u',
        "dest=\"${{1:-fits_{}}}\"".format(name),
        'mkdir -p -- "$dest" && cd -- "$dest" || exit 1',
        'if command -v wget >/dev/null 2>&1; then',
        '  fetch() { wget -c -nv "$1"; }',
        'elif command -v curl >/dev/null 2>&1; then',
        '  fetch() { curl -fSL -C - -O "$1"; }',
        'else',
        '  echo "Neither wget nor curl was found." >&2',
        '  exit 1',
        'fi',
        'failed=0',
    ]
    missing = []
    for frame, current, url in resolved:
        if _shell_safe_url(url):
            lines.append("fetch '{}' || failed=$((failed + 1))".format(url))
        else:
            missing.append((frame.get('basename', ''), current))
    if missing:
        lines.append('# Not downloadable (not served by the web server, or '
                     'no longer on the server):')
        for basename, current in missing:
            why = 'no longer on the server' if current is None else \
                'not web-served'
            lines.append('#   {} ({})'.format(_comment_safe(basename), why))
    lines.extend([
        'if [ "$failed" -ne 0 ]; then',
        '  echo "$failed download(s) failed; run the script again to '
        'retry." >&2',
        '  exit 1',
        'fi',
        '',
    ])
    return '\n'.join(lines)


def fits_url_list(resolved):
    return ''.join(url + '\n' for _frame, _current, url in resolved
                   if _shell_safe_url(url))


def photometry_csv(manifest):
    """One row per frame, the engine's values at full precision."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(CSV_COLUMNS)
    for frame in manifest['frames']:
        row = [frame.get(key) for key in
               ('date_utc', 'jd', 'mag', 'mag_err', 'status', 'band',
                'field', 'x', 'y', 'aperture_pix', 'basename')]
        writer.writerow(['' if value is None else str(value)
                         for value in row])
    return out.getvalue()


def photometry_txt(manifest, ascii_table):
    """The plain-text table of the results page (measured frames only)."""
    rows = [{'atel': f.get('date_utc', ''), 'jd': f.get('jd', ''),
             'mag': f.get('mag_display', ''), 'err': f.get('err_display', ''),
             'status': f.get('status', ''), 'field': f.get('field', ''),
             'basename': f.get('basename', '')}
            for f in manifest['frames'] if f.get('status') != 'skipped']
    if not rows:
        return 'No image yielded a measurement.\n'
    return ascii_table(rows) + '\n'


def offline_page(page_html, manifest, resolved):
    """The results page with links into its own directory made relative
    (so it works from the unpacked archive) and FITS links updated to where
    the frames are now."""
    name = manifest.get('name', '')
    prefix = (manifest.get('url_prefix') or '').rstrip('/')
    text = page_html
    if prefix and name:
        own = '{}/{}/'.format(prefix, name)
        text = text.replace(html_escape(own), '').replace(own, '')
    for frame, _current, url in resolved:
        old = frame.get('fits_url') or ''
        if old and url and old != url:
            text = text.replace(html_escape(old), html_escape(url))
    return text


def readme_text(manifest, resolved, what, cutout_pixels, cutout_notes,
                n_cutouts, cutout_limit_applied):
    tool = manifest['tool']
    frames = manifest['frames']
    statuses = {}
    for frame in frames:
        statuses[frame.get('status') or '?'] = \
            statuses.get(frame.get('status') or '?', 0) + 1
    params = manifest.get('parameters') or {}
    lines = [
        'Forced photometry results: {}'.format(manifest.get('name', '')),
        '',
        'Target (J2000): RA {}  Dec {}'.format(manifest.get('ra', ''),
                                               manifest.get('dec', '')),
        'Coordinates as entered: {}'.format(manifest.get('coords_raw', '')),
        'Tool: {}'.format(
            'archival photometry queue (archive_forced_photometry.py)'
            if tool == TOOL_ARCHIVE else
            'forced photometry on recent images (coord_forced_photometry.py)'),
    ]
    for key in sorted(params):
        lines.append('  {}: {}'.format(key, params[key]))
    lines.extend([
        'Measured: {}'.format(manifest.get('created_utc', '')),
        'Archive made: {}'.format(utc_now_str()),
        'Frames: {} ({})'.format(len(frames), ', '.join(
            '{} {}'.format(n, s) for s, n in sorted(statuses.items()))),
        '',
        'Contents',
        '  index.html          the results page; open it in a browser (the '
        'images',
        '                      are relative links; the FITS and download '
        'links',
        '                      point to the server)',
        '  {}      one row per frame: date, JD, magnitude or upper limit, '
        'error,'.format(CSV_BASENAME),
        '                      status, band, field, target pixel, aperture '
        '(pixels),',
        '                      image; full precision as measured (skipped '
        'frames:',
        '                      status "skipped")',
        '  {}      the plain-text photometry table of the page'.format(
            TXT_BASENAME),
        '  lightcurve.dat, upperlimits.dat, lightcurve.png, lightcurve.eps',
        '                      the lightcurve data and plot (when the page '
        'had them)',
        '  *.png               the cutout and full-frame thumbnails of the '
        'page',
        '  {} why some images gave no measurement'.format(
            SKIP_LOG_BASENAME),
        '  {}     downloads the full-frame FITS images (bash, wget or '
        'curl);'.format(SCRIPT_BASENAME),
        '                      run it again to resume; {} has the same '
        'URLs'.format(URL_LIST_BASENAME),
    ])
    if what == WHAT_ZIP_CUTOUTS:
        lines.extend([
            '  {}/            {} x {} pixel FITS cutouts centred on the '
            'target, one'.format(CUTOUT_SUBDIR, cutout_pixels, cutout_pixels),
            '                      per measured frame (smaller at the frame '
            'edges): the',
            '                      pixels and header of the calibrated frame '
            'with CRPIX',
            '                      shifted, so the frame\'s WCS (TAN-SIP) '
            'stays valid;',
            '                      LTV1/LTV2 make DS9 "physical" coordinates '
            'full-frame',
            '                      pixels; HISTORY names the source frame '
            'and section',
            '',
            'Cutouts: {} made.'.format(n_cutouts),
        ])
        if cutout_limit_applied:
            lines.append('Only the first {} measured frames got cutouts '
                         '(the limit per archive).'.format(
                             MAX_CUTOUTS_PER_ZIP))
        for note in cutout_notes:
            lines.append('  ' + note)
    unavailable = [(f.get('basename', ''), current)
                   for f, current, url in resolved if not url]
    lines.append('')
    if unavailable:
        lines.append('Frames without a full-frame download link:')
        for basename, current in unavailable:
            lines.append('  {} ({})'.format(
                basename, 'no longer on the server' if current is None
                else 'not served by the web server'))
    else:
        lines.append('Every frame has a full-frame download link.')
    lines.append('')
    return '\n'.join(lines)


# ---------- FITS headers (cutout post-processing) ----------

def _read_header(fh):
    """(cards, header_bytes) of the HDU starting at fh's position; cards are
    80-character strings up to and excluding END. Raises ValueError."""
    cards = []
    nbytes = 0
    for _ in range(_MAX_HEADER_BLOCKS):
        block = fh.read(2880)
        if len(block) < 2880:
            raise ValueError('truncated FITS header')
        nbytes += 2880
        for i in range(0, 2880, 80):
            card = block[i:i + 80].decode('ascii', 'replace')
            if card[:8] == 'END     ':
                return cards, nbytes
            cards.append(card)
    raise ValueError('FITS header too long')


def _card_key(card):
    return card[:8].rstrip()


def _card_value(card):
    """Value of a fixed-format FITS value card (str, int, float, bool), or
    None when the card has no value."""
    if card[8:10] != '= ':
        return None
    text = card[10:].strip()
    if text.startswith("'"):
        value = []
        i = 1
        while i < len(text):
            if text[i] == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    value.append("'")
                    i += 2
                    continue
                break
            value.append(text[i])
            i += 1
        return ''.join(value).rstrip()
    text = text.split('/', 1)[0].strip()
    if text == 'T':
        return True
    if text == 'F':
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text.replace('D', 'E'))
    except ValueError:
        return None


def _header_dict(cards):
    values = {}
    for card in cards:
        key = _card_key(card)
        if key and key not in values:
            values[key] = _card_value(card)
    return values


def _data_bytes(values):
    """Size of the HDU data (unpadded) from its header values."""
    naxis = values.get('NAXIS') or 0
    if not isinstance(naxis, int) or naxis < 0 or naxis > 999:
        raise ValueError('bad NAXIS')
    if naxis == 0:
        return 0
    count = 1
    for i in range(1, naxis + 1):
        n = values.get('NAXIS{}'.format(i))
        if not isinstance(n, int) or n < 0:
            raise ValueError('bad NAXIS{}'.format(i))
        count *= n
    bitpix = values.get('BITPIX')
    if bitpix not in (8, 16, 32, 64, -32, -64):
        raise ValueError('bad BITPIX')
    pcount = values.get('PCOUNT') or 0
    gcount = values.get('GCOUNT') or 1
    return abs(bitpix) // 8 * gcount * (pcount + count)


def _padded(n):
    return (n + 2879) // 2880 * 2880


def image_size(path):
    """(NAXIS1, NAXIS2) of the first image in a FITS file (for a tile-
    compressed .fz file the ZNAXIS1/ZNAXIS2 of its compressed image), or
    None when it cannot be read."""
    try:
        with open(path, 'rb') as fh:
            for _hdu in range(3):
                cards, _nbytes = _read_header(fh)
                values = _header_dict(cards)
                if values.get('ZIMAGE') is True:
                    nx, ny = values.get('ZNAXIS1'), values.get('ZNAXIS2')
                else:
                    nx, ny = values.get('NAXIS1'), values.get('NAXIS2')
                if (values.get('NAXIS') or 0) >= 2 and isinstance(nx, int) \
                        and isinstance(ny, int) and nx > 0 and ny > 0 and \
                        (values.get('ZIMAGE') is True or
                         values.get('XTENSION') in (None, 'IMAGE')):
                    return nx, ny
                fh.seek(_padded(_data_bytes(values)), os.SEEK_CUR)
    except (OSError, ValueError):
        return None
    return None


def _value_card(key, value, comment=''):
    if isinstance(value, bool):
        text = '{:<8}= {:>20}'.format(key, 'T' if value else 'F')
    elif isinstance(value, int):
        text = '{:<8}= {:>20d}'.format(key, value)
    elif isinstance(value, float):
        text = '{:<8}= {:>20}'.format(key, repr(value).upper())
    else:
        quoted = "'" + str(value).replace("'", "''").ljust(8) + "'"
        text = '{:<8}= {:<20}'.format(key, quoted)
    if comment:
        text += ' / ' + comment
    return text[:80].ljust(80)


def _history_cards(text):
    text = ''.join(c if 32 <= ord(c) < 127 else '?' for c in text)
    return [('HISTORY ' + text[i:i + 72]).ljust(80)
            for i in range(0, max(len(text), 1), 72)]


def _set_card(cards, key, value, comment=''):
    card = _value_card(key, value, comment)
    for i, existing in enumerate(cards):
        if _card_key(existing) == key:
            cards[i] = card
            return
    cards.append(card)


def cutout_section(x, y, nx, ny, size):
    """1-based inclusive FITS section (x1, x2, y1, y2) of a size x size
    cutout centred on pixel (x, y), clipped to an nx x ny frame; None when
    the centre is off the frame."""
    cx, cy = int(round(x)), int(round(y))
    if not (1 <= cx <= nx and 1 <= cy <= ny):
        return None
    half = (size - 1) // 2
    x1, y1 = max(1, cx - half), max(1, cy - half)
    x2, y2 = min(nx, cx - half + size - 1), min(ny, cy - half + size - 1)
    return x1, x2, y1, y2


def finish_cutout(path, x1, x2, y1, y2, source_name, note):
    """Turn fitscopy's output into a single-HDU FITS image and record where
    it came from. fitscopy writes a section of a tile-compressed image as an
    image extension behind an empty primary HDU; that extension becomes the
    primary HDU. IMAGEW/IMAGEH (astrometry.net's image size) follow the
    cutout, LTV1/LTV2 map cutout pixels to full-frame ("physical") pixels,
    HISTORY names the source. Raises ValueError on an unexpected layout."""
    with open(path, 'rb') as fh:
        cards, header_bytes = _read_header(fh)
        values = _header_dict(cards)
        size = _data_bytes(values)
        if (values.get('NAXIS') or 0) == 0:
            fh.seek(header_bytes + _padded(size))
            cards, _ = _read_header(fh)
            values = _header_dict(cards)
            if values.get('XTENSION') != 'IMAGE':
                raise ValueError('no image in the cutout')
            size = _data_bytes(values)
            # The extension becomes the primary HDU: XTENSION -> SIMPLE, and
            # PCOUNT/GCOUNT (extension-only keywords) go.
            kept = [c for c in cards[1:]
                    if _card_key(c) not in ('PCOUNT', 'GCOUNT')]
            cards = [_value_card('SIMPLE', True,
                                 'conforms to FITS standard')] + kept
        data = fh.read(size)
    if len(data) != size:
        raise ValueError('truncated cutout data')
    if values.get('NAXIS') != 2 or values.get('NAXIS1') != x2 - x1 + 1 or \
            values.get('NAXIS2') != y2 - y1 + 1:
        raise ValueError('unexpected cutout size')
    while cards and not cards[-1].strip():
        cards.pop()                 # blank padding cards before END
    for key, axis in (('IMAGEW', 'NAXIS1'), ('IMAGEH', 'NAXIS2')):
        if key in values:
            _set_card(cards, key, values[axis], 'image size, pixels')
    for key, offset in (('LTV1', x1 - 1), ('LTV2', y1 - 1)):
        old = values.get(key)
        if not isinstance(old, (int, float)) or isinstance(old, bool):
            old = 0
        _set_card(cards, key, old - offset,
                  'physical = cutout + {} (full frame)'.format(offset))
    cards.extend(_history_cards(
        'Cutout [{}:{},{}:{}] of {}'.format(x1, x2, y1, y2, source_name)))
    cards.extend(_history_cards(note))
    header = ''.join(cards) + 'END'.ljust(80)
    header += ' ' * (_padded(len(header)) - len(header))
    tmp = path + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(header.encode('ascii'))
        fh.write(data)
        fh.write(b'\0' * (_padded(len(data)) - len(data)))
    os.replace(tmp, path)


def cutout_basename(frame_basename, size):
    base = frame_basename
    if base.endswith('.fz'):
        base = base[:-3]
    for ending in ('.fits', '.fit', '.fts'):
        if base.lower().endswith(ending):
            base = base[:-len(ending)]
            break
    return '{}_cutout{}.fits'.format(base, size)


def make_cutout(fitscopy, frame_path, frame, size, out_path, note):
    """Cut a size x size section centred on the frame's measured target
    pixel. Returns None on success, else the reason there is no cutout."""
    x, y = frame.get('x'), frame.get('y')
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return 'no measured target position'
    if not _SAFE_FITS_PATH_RE.match(frame_path) or \
            not _SAFE_FITS_PATH_RE.match(out_path) or \
            not frame_path.lower().endswith(_FITS_ENDINGS):
        return 'file name not supported for cutouts'
    nx, ny = frame.get('nx'), frame.get('ny')
    if not (isinstance(nx, int) and isinstance(ny, int) and nx > 0 and ny > 0):
        dims = image_size(frame_path)
        if dims is None:
            return 'cannot read the image size'
        nx, ny = dims
    section = cutout_section(x, y, nx, ny, size)
    if section is None:
        return 'target off the frame'
    x1, x2, y1, y2 = section
    spec = '{}{}[{}:{},{}:{}]'.format(
        frame_path, '[1]' if frame_path.endswith('.fz') else '',
        x1, x2, y1, y2)
    try:
        result = subprocess.run([fitscopy, spec, out_path],
                                capture_output=True, text=True,
                                timeout=FITSCOPY_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as err:
        return 'fitscopy failed ({})'.format(type(err).__name__)
    if result.returncode != 0 or not os.path.isfile(out_path):
        return 'fitscopy failed (exit code {})'.format(result.returncode)
    try:
        finish_cutout(out_path, x1, x2, y1, y2, frame.get('basename', ''),
                      note)
    except (OSError, ValueError) as err:
        try:
            os.remove(out_path)
        except OSError:
            pass
        return 'unexpected fitscopy output ({})'.format(err)
    return None


# ---------- the archive ----------

def included_files(request_dir):
    """Regular files of the request directory that go into the archives."""
    names = []
    try:
        entries = sorted(os.listdir(request_dir))
    except OSError:
        return names
    for name in entries:
        if name not in _INCLUDED_FIXED_NAMES and \
                not _INCLUDED_NAME_RE.match(name):
            continue
        try:
            info = os.lstat(os.path.join(request_dir, name))
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            names.append((name, info.st_size))
    return names


def required_free_bytes(estimate, hardlimit_kb_raw):
    """Free bytes a build needs: the archive estimate, the pipeline's
    WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB (as autoprocess.sh reads it) and
    DISK_MARGIN_BYTES."""
    try:
        hardlimit_kb = int(str(hardlimit_kb_raw).strip())
    except (TypeError, ValueError):
        hardlimit_kb = 0
    if hardlimit_kb <= 0:
        hardlimit_kb = PIPELINE_HARDLIMIT_KB_DEFAULT
    return estimate + hardlimit_kb * 1024 + DISK_MARGIN_BYTES


def estimated_bytes(request_dir, manifest, what, cutout_pixels):
    """Rough size of the archive, for the free-space check."""
    total = sum(size for _name, size in included_files(request_dir))
    if what == WHAT_ZIP_CUTOUTS:
        n = min(sum(1 for f in manifest['frames']
                    if f.get('status') != 'skipped'), MAX_CUTOUTS_PER_ZIP)
        # 4 bytes per pixel covers BITPIX 16 and 32 with room for headers
        total += n * (cutout_pixels * cutout_pixels * 4 + 2 * 2880)
    return total + 2 * 1024 * 1024


def remove_stale_builds(request_dir):
    """Delete the work directories of earlier builds that never finished.

    Apache stops a CGI whose browser went away (SIGTERM, then SIGKILL), so a
    build can die without its cleanup running. Call this only while holding
    the request directory's build lock: then no other build is running there
    and every remaining work directory is debris."""
    try:
        names = os.listdir(request_dir)
    except OSError:
        return
    for name in names:
        if name.startswith(BUILD_TMP_PREFIX):
            path = os.path.join(request_dir, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)


def _unique_cutout_name(base, size, used):
    """Cutout file name for a frame; frames uploaded twice under the same
    name (identical re-uploads are allowed) get _2, _3, ... suffixes."""
    name = cutout_basename(base, size)
    stem = name[:-len('.fits')]
    n = 2
    while name in used:
        name = '{}_{}.fits'.format(stem, n)
        n += 1
    used.add(name)
    return name


def build_archive(request_dir, manifest, what, cfg_paths, fitscopy,
                  cutout_pixels, ascii_table, progress=None, keepalive=None):
    """Build and publish <name>_results[_with_cutouts].zip in request_dir.
    The caller must hold the directory's build lock. progress(index, total,
    frame basename, problem or None) reports each cutout; keepalive() is
    called every KEEPALIVE_SECONDS while files are written. Returns
    (zip_path, n_cutouts, notes). Raises OSError/ValueError; a failed build
    leaves no archive behind."""
    name = manifest['name']
    final = os.path.join(request_dir, zip_basename(name, what))
    remove_stale_builds(request_dir)
    resolved = resolve_frames(manifest, cfg_paths)
    with open(os.path.join(request_dir, PAGE_BASENAME), encoding='utf-8',
              errors='replace') as fh:
        page = fh.read()
    last_tick = [time.time()]

    def tick():
        if keepalive and time.time() - last_tick[0] >= KEEPALIVE_SECONDS:
            last_tick[0] = time.time()
            keepalive()

    work = tempfile.mkdtemp(prefix=BUILD_TMP_PREFIX, dir=request_dir)
    notes = []
    n_cutouts = 0
    limit_applied = False
    try:
        cutouts = []
        if what == WHAT_ZIP_CUTOUTS:
            cutout_dir = os.path.join(work, CUTOUT_SUBDIR)
            os.mkdir(cutout_dir)
            measured = [(f, cur) for f, cur, _u in resolved
                        if f.get('status') != 'skipped']
            if len(measured) > MAX_CUTOUTS_PER_ZIP:
                limit_applied = True
                notes.append('only the first {} of {} measured frames get '
                             'cutouts'.format(MAX_CUTOUTS_PER_ZIP,
                                              len(measured)))
                measured = measured[:MAX_CUTOUTS_PER_ZIP]
            note = 'unmw forced-photometry cutout centred on RA {} Dec {}, ' \
                'made {}'.format(manifest.get('ra', ''),
                                 manifest.get('dec', ''), utc_now_str())
            used_names = set()
            deadline = time.time() + BUILD_DEADLINE_SECONDS
            for index, (frame, current) in enumerate(measured, start=1):
                if time.time() > deadline:
                    raise TimeoutError('cutouts took longer than {} s'.format(
                        BUILD_DEADLINE_SECONDS))
                base = frame.get('basename', '')
                out_name = out_path = None
                if current is None:
                    problem = 'no longer on the server'
                else:
                    out_name = _unique_cutout_name(base, cutout_pixels,
                                                   used_names)
                    out_path = os.path.join(cutout_dir, out_name)
                    problem = make_cutout(fitscopy, current, frame,
                                          cutout_pixels, out_path, note)
                if problem is None:
                    cutouts.append((out_name, out_path))
                    n_cutouts += 1
                else:
                    notes.append('{}: no cutout ({})'.format(base, problem))
                if progress:
                    progress(index, len(measured), base, problem)
                tick()
        texts = [
            (PAGE_BASENAME, offline_page(page, manifest, resolved), 0o644),
            (CSV_BASENAME, photometry_csv(manifest), 0o644),
            (TXT_BASENAME, photometry_txt(manifest, ascii_table), 0o644),
            (SCRIPT_BASENAME, fits_download_script(manifest, resolved),
             0o755),
            (URL_LIST_BASENAME, fits_url_list(resolved), 0o644),
            (README_BASENAME, readme_text(manifest, resolved, what,
                                          cutout_pixels, notes, n_cutouts,
                                          limit_applied), 0o644),
        ]
        tmp_zip = os.path.join(work, 'archive.zip')
        stamp = time.localtime()[:6]
        with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED,
                             allowZip64=True) as archive:
            written = set()
            for arcname, text, mode in texts:
                info = zipfile.ZipInfo('{}/{}'.format(name, arcname), stamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, text.encode('utf-8'))
                written.add(arcname)
            for file_name, _size in included_files(request_dir):
                if file_name in written:
                    continue
                path = os.path.join(request_dir, file_name)
                compress = zipfile.ZIP_STORED \
                    if file_name.lower().endswith(_STORED_EXTENSIONS) \
                    else zipfile.ZIP_DEFLATED
                archive.write(path, '{}/{}'.format(name, file_name),
                              compress_type=compress)
                tick()
            for out_name, out_path in cutouts:
                archive.write(out_path, '{}/{}/{}'.format(
                    name, CUTOUT_SUBDIR, out_name),
                    compress_type=zipfile.ZIP_DEFLATED)
                tick()
        os.chmod(tmp_zip, 0o644)
        os.replace(tmp_zip, final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return final, n_cutouts, notes

#!/usr/bin/env python3
"""
CGI: downloadable copies of forced-photometry results.

  forced_phot_download.py?request=<result directory>&what=zip|zip_cutouts|script

<result directory> is the per-request directory under uploads/ of a finished
run of the recent-images page (coord_forced_photometry.py, forced_phot_<...>)
or of the archival photometry queue (archive_phot_<job id>); the bottom of
each results page links here.

  what=zip          every file the results page shows or links, the
                    photometry table as CSV and text, a README and the
                    full-frame download script, as <name>_results.zip
  what=zip_cutouts  the same plus a FITS cutout centred on the target from
                    every measured frame (FORCED_PHOT_CUTOUT_PIXELS pixels
                    square, default 501), as <name>_results_with_cutouts.zip
  what=script       download_fits.sh: fetches the full-frame FITS images with
                    wget -c or curl -C - (resumable)

A ZIP is built on the first request for it (a progress page, then a redirect)
and published into the result directory, where the web server serves it as a
static file from then on. See nmw_results_download_lib.py for the details.

Configuration (read from local_config.sh next to this script):
  URL_OF_DATA_PROCESSING_ROOT   URL prefix for the served uploads/ directory
  VAST_REFERENCE_COPY           VaST tree; util/fitscopy makes the cutouts
  FORCED_PHOT_CUTOUT_PIXELS     cutout size in pixels (optional, default 501)
  IMAGE_QUARANTINE_DIR          optional quarantine tree (cutouts, no links)
  IMAGE_ARCHIVE_DIR             archive of the archival photometry queue
  IMAGE_ARCHIVE_URL             optional URL prefix serving IMAGE_ARCHIVE_DIR
"""

# Handle cgi module removal in Python 3.13+
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
try:
    import cgi
except ImportError:
    import sys
    sys.exit("Error: 'cgi' module not found. "
             "For Python 3.13+, install: pip install legacy-cgi")

import fcntl
import os
import shutil
import sys
import urllib.parse

import nmw_coord_lib as ncl
from nmw_coord_lib import (
    html_escape, _PAGE_CSS, main_page_url, page_top_html, page_title_html,
    page_end_html, emit_redirect, emit_message_page, read_config_vars,
    acquire_concurrency_slot, open_lock_file,
)
import nmw_archive_phot_lib as apl
from nmw_forced_phot_lib import ascii_table
import nmw_results_download_lib as rdl

TEMP_PARENT = 'uploads'                 # mirrors upload.py's upload_dir
DEFAULT_CGI_PATH = '/cgi-bin/unmw/forced_phot_download.py'
# Apache's CGI buffer is ~4 KB; pad progress lines past it (see
# coord_forced_photometry.py).
_ROW_FLUSH_PAD = "<!-- " + (" " * 1500) + " -->\n"


def _this_url(name, what):
    return '{}?{}'.format(
        os.environ.get('SCRIPT_NAME', '').strip() or DEFAULT_CGI_PATH,
        urllib.parse.urlencode({'request': name, 'what': what}))


def _message(title, text, status, retry_url=None):
    emit_message_page(title, '<p>{}</p>'.format(text), status_line=status,
                      search_again_url=retry_url)


def _serve_script(name, manifest, cfg_paths):
    text = rdl.fits_download_script(
        manifest, rdl.resolve_frames(manifest, cfg_paths))
    sys.stdout.write('Content-Type: text/x-shellscript; charset=utf-8\n')
    sys.stdout.write('Content-Disposition: attachment; '
                     'filename="download_fits_{}.sh"\n\n'.format(name))
    sys.stdout.write(text)
    sys.stdout.flush()


class _Page(object):
    """Output of the progress page. A browser that goes away must not stop
    the build (Apache may still let it finish, and then the next click gets
    the archive), so write errors only silence the page."""

    def __init__(self):
        self.gone = False

    def emit(self, text):
        if self.gone:
            return
        try:
            sys.stdout.write(text + '\n')
            sys.stdout.flush()
        except (OSError, ValueError):
            self.gone = True


def _build_page(name, what, request_dir, manifest, cfg_paths, fitscopy,
                cutout_pixels, zip_url):
    """Stream a progress page while the archive is built, then send the
    browser to the published archive."""
    page = _Page()
    title = 'Preparing the ZIP archive'
    page.emit("Content-Type: text/html\n")
    page.emit("<html><head><title>{}</title>".format(html_escape(title)))
    page.emit(_PAGE_CSS)
    page.emit("</head><body>")
    page.emit("<!-- {} -->".format(' ' * 4000))  # past Apache's CGI buffer
    page.emit(page_top_html(main_page_url(), [('Main page', main_page_url())],
                            wide=False))
    page.emit(page_title_html(title))
    n_frames = len(manifest['frames'])
    measured = sum(1 for f in manifest['frames']
                   if f.get('status') != 'skipped')
    page.emit("<p>Forced-photometry results <span class='code'>{}</span> "
              "(RA {} Dec {}, {} image(s)). Please keep this page open "
              "until the download starts.</p>".format(
                  html_escape(name), html_escape(manifest.get('ra', '')),
                  html_escape(manifest.get('dec', '')), n_frames))
    if what == rdl.WHAT_ZIP_CUTOUTS:
        page.emit("<p class='secondary'>Cutting {n} x {n} pixel FITS cutouts "
                  "from {m} measured image(s){cap}; each line below appears "
                  "as one is done...</p>".format(
                      n=cutout_pixels, m=measured,
                      cap=' (at most {})'.format(rdl.MAX_CUTOUTS_PER_ZIP)
                      if measured > rdl.MAX_CUTOUTS_PER_ZIP else ''))
    every = 1 if measured <= 200 else max(1, measured // 100)

    def progress(index, total, basename, problem):
        if index % every and index != total and problem is None:
            return
        page.emit("<p class='secondary'>&nbsp;&nbsp;{}/{} {}: {}</p>".format(
            index, total, 'done' if problem is None else
            html_escape('no cutout ({})'.format(problem)),
            html_escape(basename)) + _ROW_FLUSH_PAD)

    def keepalive():
        page.emit(_ROW_FLUSH_PAD)

    try:
        zip_path, n_cutouts, _notes = rdl.build_archive(
            request_dir, manifest, what, cfg_paths, fitscopy, cutout_pixels,
            ascii_table, progress=progress, keepalive=keepalive)
    except Exception as err:  # the page is already streaming: report, log
        sys.stderr.write('forced_phot_download: {} build failed: {!r}\n'.format(
            name, err))
        page.emit("<div class='notice'>ERROR: the archive could not be made "
                  "({}). Please try again later.</div>".format(
                      html_escape(type(err).__name__)))
        page.emit("<br><a href='{}'>Try again</a>".format(
            html_escape(_this_url(name, what))))
        page.emit(page_end_html())
        return
    size_mb = os.path.getsize(zip_path) / 1048576.0
    page.emit("<p>The archive is ready: <a href='{u}'>{f}</a> ({s:.1f} MB{c}). "
              "The download starts by itself; if it does not, click the "
              "link.</p>".format(
                  u=html_escape(zip_url),
                  f=html_escape(os.path.basename(zip_path)), s=size_mb,
                  c=', {} cutout(s)'.format(n_cutouts)
                  if what == rdl.WHAT_ZIP_CUTOUTS else ''))
    page.emit("<meta http-equiv='refresh' content='1; url={}'>".format(
        html_escape(zip_url)))
    page.emit(page_end_html())


def main():
    ncl.enable_cgi_error_logging()
    script_dir = os.path.dirname(os.path.realpath(__file__))
    try:
        os.chdir(script_dir)
    except OSError:
        _message("Internal error", "Cannot open the results directory.",
                 "Status: 500 Internal Server Error")
        return

    form = ncl.parse_cgi_form(cgi)
    name = (form.getfirst('request', '') or '').strip()
    what = (form.getfirst('what', '') or rdl.WHAT_ZIP).strip()
    if what not in rdl.WHAT_VALUES:
        _message("Invalid request", "Unknown download type.",
                 "Status: 400 Bad Request")
        return
    request_dir = rdl.request_dir_path(TEMP_PARENT, name)
    if request_dir is None:
        _message("Not found", "No forced-photometry results by this name.",
                 "Status: 404 Not Found")
        return
    if not rdl.run_is_finished(request_dir, name, apl.read_state):
        _message("Not available yet",
                 "These results cannot be downloaded: the run is still in "
                 "progress, did not finish, or predates the download "
                 "feature.", "Status: 409 Conflict",
                 retry_url=_this_url(name, what))
        return
    manifest = rdl.read_manifest(request_dir)
    expected_tool = rdl.TOOL_ARCHIVE if name.startswith('archive_phot_') \
        else rdl.TOOL_RECENT
    if manifest is None or manifest.get('name') != name or \
            manifest.get('tool') != expected_tool:
        _message("Not found", "No downloadable forced-photometry results by "
                 "this name.", "Status: 404 Not Found")
        return

    cfg = read_config_vars('URL_OF_DATA_PROCESSING_ROOT',
                           'VAST_REFERENCE_COPY', 'IMAGE_DATA_ROOT', 'IMAGE_QUARANTINE_DIR',
                           'IMAGE_ARCHIVE_DIR', 'IMAGE_ARCHIVE_URL',
                           'FORCED_PHOT_CUTOUT_PIXELS',
                           'WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB')
    url_prefix = cfg['URL_OF_DATA_PROCESSING_ROOT'].strip().rstrip('/')
    archive_dir = cfg['IMAGE_ARCHIVE_DIR'].strip()
    quarantine_dir = cfg['IMAGE_QUARANTINE_DIR'].strip()
    image_data_root = cfg['IMAGE_DATA_ROOT'].strip()
    cfg_paths = {
        'workdir': os.path.abspath(TEMP_PARENT),
        'image_data_root': os.path.abspath(image_data_root) if image_data_root else '',
        'quarantine_dir': os.path.abspath(quarantine_dir)
        if quarantine_dir else '',
        'archive_dir': os.path.abspath(archive_dir) if archive_dir else '',
        'url_prefix': url_prefix,
        'archive_url': cfg['IMAGE_ARCHIVE_URL'].strip().rstrip('/'),
    }

    if what == rdl.WHAT_SCRIPT:
        _serve_script(name, manifest, cfg_paths)
        return

    if not url_prefix:
        _message("Configuration error", "URL_OF_DATA_PROCESSING_ROOT is not "
                 "set in local_config.sh.",
                 "Status: 500 Internal Server Error")
        return
    zip_name = rdl.zip_basename(name, what)
    zip_path = os.path.join(request_dir, zip_name)
    zip_url = '{}/{}/{}'.format(url_prefix, name, zip_name)
    if os.path.isfile(zip_path):
        emit_redirect(zip_url)
        return
    if os.environ.get('REQUEST_METHOD', 'GET') == 'HEAD':
        # Link checkers and prefetchers must not start a build.
        print("Content-Type: text/html\n")
        return

    fitscopy = ''
    if what == rdl.WHAT_ZIP_CUTOUTS:
        vast_dir = cfg['VAST_REFERENCE_COPY'].strip()
        fitscopy = os.path.join(vast_dir, 'util', 'fitscopy') \
            if vast_dir else ''
        if not fitscopy or not os.access(fitscopy, os.X_OK):
            _message("Configuration error", "VaST's util/fitscopy was not "
                     "found, so no cutouts can be made.",
                     "Status: 500 Internal Server Error")
            return
    cutout_pixels = rdl.cutout_pixels_from_config(
        cfg['FORCED_PHOT_CUTOUT_PIXELS'])

    # The slot locks live in uploads/ (owned by the web server user), not in
    # world-writable /tmp where another user's stale lock file would block a
    # slot for good (see acquire_concurrency_slot). Their prefix matches no
    # result-directory pattern (forced_phot_*, archive_phot_*, results_*).
    slot = acquire_concurrency_slot(prefix='download_build',
                                    max_concurrent=rdl.MAX_BUILDS_AT_ONCE,
                                    lock_dir=os.path.abspath(TEMP_PARENT))
    if slot is None:
        _message("Server busy", "{0} of {0} archives are being prepared right "
                 "now. Please try again in a minute: the link below repeats "
                 "this request.".format(rdl.MAX_BUILDS_AT_ONCE),
                 "Status: 503 Service Unavailable",
                 retry_url=_this_url(name, what))
        return
    lock = None
    try:
        try:
            lock = open_lock_file(os.path.join(request_dir,
                                               rdl.BUILD_LOCK_BASENAME))
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _message("Being prepared", "This archive is being prepared for "
                     "another request. Please try again in a minute.",
                     "Status: 503 Service Unavailable",
                     retry_url=_this_url(name, what))
            return
        if os.path.isfile(zip_path):
            emit_redirect(zip_url)
            return
        # Never take the disk below the transient pipeline's own floor.
        need = rdl.required_free_bytes(
            rdl.estimated_bytes(request_dir, manifest, what, cutout_pixels),
            cfg['WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB'])
        try:
            free = shutil.disk_usage(request_dir).free
        except OSError:
            free = 0
        if free < need:
            _message("Not enough disk space", "The server does not have "
                     "enough free disk space for this archive right now. "
                     "Please try again later.",
                     "Status: 507 Insufficient Storage",
                     retry_url=_this_url(name, what))
            return
        _build_page(name, what, request_dir, manifest, cfg_paths, fitscopy,
                    cutout_pixels, zip_url)
    finally:
        if lock is not None:
            lock.close()
        slot.close()


if __name__ == "__main__":
    main()

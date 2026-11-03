"""Exercise CGI entry points, not copies of their validation logic."""
import datetime
import fcntl
import html
import io
import os
import re
import shlex
import sys
import types
import urllib.parse

import pytest

import archive_forced_photometry
import archive_phot_status
import coord_forced_photometry
import coord_search
import fastplot
import nmw_coord_lib as ncl


@pytest.mark.parametrize('module', [coord_search, coord_forced_photometry,
                                   archive_forced_photometry, archive_phot_status])
def test_each_cgi_rejects_oversized_body_before_reading(app_sandbox, monkeypatch, capsys, module):
    app_sandbox.configure(module)
    monkeypatch.setenv('REQUEST_METHOD', 'POST')
    monkeypatch.setenv('CONTENT_LENGTH', '65537')
    body = io.BytesIO(b'not read')
    monkeypatch.setattr(sys, 'stdin', types.SimpleNamespace(buffer=body))
    with pytest.raises(SystemExit):
        module.main()
    assert body.tell() == 0
    assert 'Status: 400' in capsys.readouterr().out
    assert app_sandbox.kicks == []


@pytest.mark.parametrize('module', [coord_search, coord_forced_photometry])
def test_coordinate_cgi_rejects_and_escapes_bad_input(app_sandbox, capsys, module):
    app_sandbox.configure(module, {'coords': '<img src=x onerror=alert(1)>'})
    module.main()
    output = capsys.readouterr().out
    assert 'Invalid coordinates' in output
    assert '<img src=x' not in output and '&lt;img' in output
    assert list(app_sandbox.paths['uploads'].iterdir()) == []


@pytest.mark.parametrize('module', [coord_search, coord_forced_photometry])
def test_coordinate_cgi_without_input_redirects_to_form(app_sandbox, capsys, module):
    app_sandbox.configure(module)
    module.main()
    output = capsys.readouterr().out
    assert 'Status: 302' in output
    assert 'Location:' in output and '.html' in output


@pytest.mark.parametrize('module', [coord_search, coord_forced_photometry])
def test_coordinate_cgi_reports_no_covering_images(app_sandbox, monkeypatch, capsys, module):
    app_sandbox.configure(module, {'coords': '300 20'})
    (app_sandbox.root / 'locks').mkdir()
    monkeypatch.setattr(module, 'acquire_concurrency_slot',
                        lambda **kw: ncl.acquire_concurrency_slot(
                            lock_dir=str(app_sandbox.root / 'locks'), **kw))
    scans = []

    def scan(*args, **kwargs):
        scans.append(args)
        return [], False

    monkeypatch.setattr(module, 'run_sky2xy_scan', scan)
    module.main()
    output = capsys.readouterr().out
    assert len(scans) == 1
    assert 'no ' in output.lower() and '</html>' in output
    assert app_sandbox.kicks == []


def _search_again_hrefs(output):
    return [html.unescape(href) for href in
            re.findall(r"<a href='([^']*)'>Search again</a>", output)]


def test_message_page_link_prefers_explicit_target_over_referer(monkeypatch, capsys):
    monkeypatch.setenv('HTTP_REFERER', 'http://referer.invalid/previous.html')
    ncl.emit_message_page('Title', '<p>body</p>')
    assert _search_again_hrefs(capsys.readouterr().out) == ['http://referer.invalid/previous.html']
    ncl.emit_message_page('Title', '<p>body</p>', search_again_url='/cgi-bin/x.py?a=1&b=2')
    output = capsys.readouterr().out
    assert _search_again_hrefs(output) == ['/cgi-bin/x.py?a=1&b=2']
    assert 'referer.invalid' not in output


@pytest.fixture
def forced_phot_slots(app_sandbox, monkeypatch):
    """The CGI's real flock slot pool inside the sandbox. Yields hold(n), which
    occupies n slots of that pool (kept until the test ends) so main() finds
    them busy."""
    module = coord_forced_photometry
    locks = app_sandbox.root / 'locks'
    locks.mkdir()
    monkeypatch.setattr(module, 'acquire_concurrency_slot',
                        lambda **kw: ncl.acquire_concurrency_slot(lock_dir=str(locks), **kw))
    held = []

    def hold(count):
        for _ in range(count):
            handle = ncl.acquire_concurrency_slot(
                prefix='forced_phot', max_concurrent=module.FORCED_PHOT_MAX_CONCURRENT,
                lock_dir=str(locks))
            assert handle is not None
            held.append(handle)

    yield hold
    for handle in held:
        handle.close()


FORCED_PHOT_QUERY = {'coords': '300 20', 'window_days': '3', 'max_images': '5'}


@pytest.mark.parametrize('band', ['', 'R'])
def test_forced_photometry_busy_page_search_again_repeats_the_request(
        app_sandbox, forced_phot_slots, monkeypatch, capsys, band):
    module = coord_forced_photometry
    query = dict(FORCED_PHOT_QUERY, band=band)
    app_sandbox.configure(module, query)
    referer = 'http://referer.invalid/unmw/coord_forced_photometry.html'
    monkeypatch.setenv('HTTP_REFERER', referer)
    scans = []
    monkeypatch.setattr(module, 'run_sky2xy_scan', lambda *a, **kw: scans.append(a) or ([], False))
    forced_phot_slots(module.FORCED_PHOT_MAX_CONCURRENT)
    module.main()
    output = capsys.readouterr().out
    assert 'Status: 503' in output and 'Server busy' in output
    assert '({0} of {0})'.format(module.FORCED_PHOT_MAX_CONCURRENT) in output
    assert scans == [] and list(app_sandbox.paths['uploads'].iterdir()) == []
    # One click re-runs the same request: a GET to the CGI itself carrying the
    # request's own parameters (an empty band is left out so the form default
    # applies), not a return to the referring form page.
    repeated = {name: value for name, value in query.items() if value}
    assert _search_again_hrefs(output) == [
        '/cgi-bin/coord_forced_photometry.py?' + urllib.parse.urlencode(repeated)]
    assert 'referer.invalid' not in output


@pytest.mark.parametrize('outcome', ['no_covering_field', 'no_recent_images'])
def test_forced_photometry_no_data_page_returns_to_prefilled_form(
        app_sandbox, forced_phot_slots, monkeypatch, capsys, outcome):
    module = coord_forced_photometry
    query = dict(FORCED_PHOT_QUERY, band='R')
    app_sandbox.configure(module, query)
    monkeypatch.setenv('HTTP_REFERER', 'http://referer.invalid/unmw/coord_forced_photometry.html')
    matches = ([] if outcome == 'no_covering_field'
               else [('/reference/Vul8_2025-1-7_15-30-31_001.fts', 512., 512.)])
    monkeypatch.setattr(module, 'run_sky2xy_scan', lambda *a, **kw: (matches, False))
    monkeypatch.setattr(module, 'list_recent_field_images', lambda *a: [])
    # Every slot but one is taken: the last free slot still admits the request.
    forced_phot_slots(module.FORCED_PHOT_MAX_CONCURRENT - 1)
    module.main()
    output = capsys.readouterr().out
    assert 'Status: 503' not in output and 'Server busy' not in output
    assert 'ERROR: no ' in output and '</html>' in output
    if outcome == 'no_recent_images':
        assert 'Vul8' in output
    # No data: go back to the input form, pre-filled with this request, so the
    # user can widen the window or change the position.
    assert _search_again_hrefs(output) == [
        '/unmw/coord_forced_photometry.html?' + urllib.parse.urlencode(query)]
    assert 'referer.invalid' not in output


@pytest.mark.parametrize('raw,expected', [('300 20', ('300', '20')),
                                       ('20:00:00 +20:00:00', ('20:00:00', '+20:00:00')),
                                       ('20 00 00 +20 00 00', ('20:00:00', '+20:00:00'))])
def test_coordinate_formats(raw, expected):
    assert ncl.parse_coordinates(raw) == expected


@pytest.fixture
def coordinate_science(app_sandbox, camera_images, monkeypatch):
    """Keep CGI control flow, locking and output real; stub external binaries."""
    locks = app_sandbox.root / 'locks'
    locks.mkdir()

    def configure(module, query):
        app_sandbox.configure(module, query)
        monkeypatch.setattr(module, 'acquire_concurrency_slot',
                            lambda **kw: ncl.acquire_concurrency_slot(lock_dir=str(locks), **kw))
        monkeypatch.setattr(module, 'run_sky2xy_scan',
                            lambda *a, **kw: ([(str(p), 512., 512.) for p in camera_images], False))
        monkeypatch.setattr(module, 'get_image_metadata', lambda *a: dict(
            nx=1024, ny=1024, arcmin_str='60 x 60', deg_str='1 x 1',
            scale_x=3.5, scale_y=3.5, center_radec='300 +20'))
        monkeypatch.setattr(module, 'make_zoomout_thumbnail', lambda *a, **kw: None)
        monkeypatch.setattr(module, 'make_zoomin_thumbnail', lambda *a, **kw: None)

    return configure


@pytest.mark.parametrize('query', [{'coords': '300 20'}, {'action': 'list_all'}])
def test_reference_cgi_renders_real_result_rows(app_sandbox, camera_images, coordinate_science,
                                             monkeypatch, capsys, query):
    coordinate_science(coord_search, query)
    monkeypatch.setattr(coord_search, 'list_fits_files', lambda *a: [str(p) for p in camera_images])
    coord_search.main()
    output = capsys.readouterr().out
    assert '</table>' in output and '</html>' in output
    assert 'render failed' not in output and 'Internal error' not in output
    for image in camera_images:
        assert image.name in output


@pytest.mark.parametrize('cap,successful', [(2, True), (1, True), (2, False)])
def test_forced_photometry_cgi_keeps_distinct_visits_and_cleans_working_copy(
        app_sandbox, camera_images, coordinate_science, monkeypatch, capsys, cap, successful):
    module = coord_forced_photometry
    coordinate_science(module, {'coords': '300 20', 'max_images': str(cap)})
    monkeypatch.setattr(module, 'list_recent_field_images', lambda *a: [str(p) for p in camera_images])
    work = app_sandbox.root / 'science-copy'
    work.mkdir()
    monkeypatch.delenv('DEBUG_KEEP_WORK_DIR', raising=False)
    monkeypatch.setattr(module, 'setup_vast_working_copy', lambda *a: str(work))
    monkeypatch.setattr(module, '_read_factory_text', lambda *a: '')
    monkeypatch.setattr(module, 'derive_band', lambda *a: 'V')
    monkeypatch.setattr(module, 'derive_sextractor_config', lambda *a: None)
    monkeypatch.setattr(module, '_phase1_parallel_solve_plate',
                        lambda *a, **kw: (cap, 0, 0, {str(p): str(p) for p in camera_images}, .1))
    monkeypatch.setattr(module, 'get_jd_and_atel_date', lambda *a: ('2460683.15', '2025-01-07'))
    monkeypatch.setattr(module, 'render_lightcurve_plots', lambda *a: (None, None))
    measured = []

    def measure(*args, **kwargs):
        measured.append(args[2])
        return (dict(status='detection', mag='14.25', err='.03', jd='2460683.15',
                     x='512', y='512', aperture='5') if successful else None)

    monkeypatch.setattr(module, 'run_forced_photometry_c', measure)
    module.main()
    output = capsys.readouterr().out
    assert len(measured) == cap and len(set(measured)) == cap
    assert '</html>' in output and 'Internal error' not in output
    assert not work.exists()
    if successful:
        assert 'Photometry table' in output and '14.25' in output
    else:
        assert 'None of the 2 image(s) yielded' in output
    if cap == 1:
        assert 'Limited to the first 1 of 2' in output


@pytest.fixture
def forced_phot_engine(app_sandbox, coordinate_science, monkeypatch):
    """Stub the measurement engine of coord_forced_photometry (VaST working
    copy, plate solve, C photometry, plots) while the directory walk, the
    concurrency slot and the page rendering stay real. Returns the list of
    frames handed to the (stubbed) C engine."""
    module = coord_forced_photometry
    coordinate_science(module, {'coords': '300 20'})
    work = app_sandbox.root / 'science-copy'
    work.mkdir()
    monkeypatch.delenv('DEBUG_KEEP_WORK_DIR', raising=False)
    monkeypatch.setattr(module, 'setup_vast_working_copy', lambda *a: str(work))
    monkeypatch.setattr(module, '_read_factory_text', lambda *a: '')
    monkeypatch.setattr(module, 'derive_band', lambda *a: 'V')
    monkeypatch.setattr(module, 'derive_sextractor_config', lambda *a: None)
    monkeypatch.setattr(module, '_phase1_parallel_solve_plate',
                        lambda work_dir, cfg, images, *a, **kw: (
                            len(images), 0, 0, {img: img for img in images}, .1))
    monkeypatch.setattr(module, 'get_jd_and_atel_date', lambda *a: ('2460683.15', '2025-01-07'))
    monkeypatch.setattr(module, 'render_lightcurve_plots', lambda *a: (None, None))
    measured = []

    def measure(*args, **kwargs):
        measured.append(args[2])
        return dict(status='detection', mag='14.25', err='.03', jd='2460683.15',
                    x='512', y='512', aperture='5')

    monkeypatch.setattr(module, 'run_forced_photometry_c', measure)
    return measured


def _make_frames(trees):
    """Create empty placeholder frames: {directory: [file names]}."""
    for directory, names in trees.items():
        directory.mkdir(parents=True)
        for name in names:
            (directory / name).write_bytes(b'')


def test_forced_photometry_cgi_measures_sorted_good_and_quarantine_trees(
        app_sandbox, forced_phot_engine, capsys):
    """Real directory walk over the four image trees: uploads/img_*, the
    good_wcs_fd_images/ directory that sort_workdir_images.sh fills under
    uploads/, IMAGE_QUARANTINE_DIR/img_* and its own good_wcs_fd_images/.
    A frame listed by two trees is measured once; only frames under uploads/
    get a FITS download link."""
    today = datetime.date.today().isoformat()
    uploads = app_sandbox.paths['uploads']
    quarantine = app_sandbox.root / 'quarantine'
    app_sandbox.config['IMAGE_QUARANTINE_DIR'] = str(quarantine)
    _make_frames({
        uploads / ('img_%s_a' % today): [
            'wcs_fd_Vul8_2025-1-7_15-30-31_001.fts',
            'fd_Vul8_2025-1-7_15-30-31_001.fts'],          # not plate-solved: ignored
        uploads / 'good_wcs_fd_images' / ('img_%s_b' % today): [
            'wcs_fd_Vul8_2025-1-7_15-31-1_002.fts'],
        quarantine / ('img_%s_c' % today): [
            'wcs_fd_Vul8_2025-1-7_15-31-1_002.fts',        # mid-move duplicate: measured once
            'wcs_fd_Vul8_2025-1-7_15-32-1_003.fts'],
        quarantine / 'good_wcs_fd_images' / ('img_%s_d' % today): [
            'wcs_fd_Vul8_2025-1-7_15-33-1_004.fts',
            'wcs_fd_Cyg1_2025-1-7_15-33-1_005.fts'],       # other field: ignored
    })
    coord_forced_photometry.main()
    output = capsys.readouterr().out
    assert '</html>' in output and 'Internal error' not in output
    expected = {
        str(uploads / ('img_%s_a' % today) / 'wcs_fd_Vul8_2025-1-7_15-30-31_001.fts'),
        str(uploads / 'good_wcs_fd_images' / ('img_%s_b' % today)
            / 'wcs_fd_Vul8_2025-1-7_15-31-1_002.fts'),
        str(quarantine / ('img_%s_c' % today) / 'wcs_fd_Vul8_2025-1-7_15-32-1_003.fts'),
        str(quarantine / 'good_wcs_fd_images' / ('img_%s_d' % today)
            / 'wcs_fd_Vul8_2025-1-7_15-33-1_004.fts'),
    }
    assert set(forced_phot_engine) == expected and len(forced_phot_engine) == len(expected)
    assert 'Performing forced photometry on 4 images' in output
    assert '1 image(s) come from the sorted good_wcs_fd_images/ directory.' in output
    assert ('1 image(s) come from the quarantine directory '
            '(no FITS download links for these).') in output
    assert ('1 image(s) come from the good_wcs_fd_images/ directory of the '
            'quarantine tree (no FITS download links for these).') in output
    assert 'WARNING' not in output
    # Rows are streamed newest first (by the timestamp in the file name), and
    # only the two frames under uploads/ carry a FITS link.
    links = re.findall(r"<a href='([^']*)' target='_blank'>FITS</a>", output)
    assert links == [
        'http://127.0.0.1/uploads/good_wcs_fd_images/img_%s_b/wcs_fd_Vul8_2025-1-7_15-31-1_002.fts' % today,
        'http://127.0.0.1/uploads/img_%s_a/wcs_fd_Vul8_2025-1-7_15-30-31_001.fts' % today,
    ]
    assert not (app_sandbox.root / 'science-copy').exists()


def test_forced_photometry_cgi_skips_unreadable_upload_directories_with_a_warning(
        app_sandbox, forced_phot_engine, monkeypatch, capsys):
    """One img_* directory that cannot be listed costs only that directory:
    the other frames of the same tree are still measured, and the page says
    which directories were skipped."""
    today = datetime.date.today().isoformat()
    uploads = app_sandbox.paths['uploads']
    _make_frames({
        uploads / ('img_%s_a' % today): ['wcs_fd_Vul8_2025-1-7_15-30-31_001.fts'],
        uploads / ('img_%s_y' % today): ['wcs_fd_Vul8_2025-1-7_15-31-1_002.fts'],
        uploads / 'good_wcs_fd_images' / ('img_%s_b' % today): [
            'wcs_fd_Vul8_2025-1-7_15-32-1_003.fts'],
        uploads / 'good_wcs_fd_images' / ('img_%s_z' % today): [
            'wcs_fd_Vul8_2025-1-7_15-33-1_004.fts'],
    })
    # Deny listing of img_*_y and img_*_z regardless of the test user's
    # privileges (chmod 000 would be a no-op for root).
    unreadable = {'img_%s_y' % today, 'img_%s_z' % today}
    real_listdir = os.listdir

    def listdir(path='.'):
        if os.path.basename(str(path)) in unreadable:
            raise PermissionError(13, 'Permission denied', str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, 'listdir', listdir)
    coord_forced_photometry.main()
    output = capsys.readouterr().out
    assert '</html>' in output and 'Internal error' not in output
    assert set(forced_phot_engine) == {
        str(uploads / ('img_%s_a' % today) / 'wcs_fd_Vul8_2025-1-7_15-30-31_001.fts'),
        str(uploads / 'good_wcs_fd_images' / ('img_%s_b' % today)
            / 'wcs_fd_Vul8_2025-1-7_15-32-1_003.fts'),
    }
    assert ('WARNING: 1 directory(ies) in the uploads directory could not be '
            'listed and were skipped: img_%s_y.' % today) in output
    assert ('WARNING: 1 directory(ies) in the sorted good_wcs_fd_images/ directory '
            'could not be listed and were skipped: img_%s_z.' % today) in output
    assert '1 image(s) come from the sorted good_wcs_fd_images/ directory.' in output
    assert 'Performing forced photometry on 2 images' in output


@pytest.fixture
def plot_sandbox(tmp_path, monkeypatch):
    root = tmp_path / "plot dir with ' quotes"
    root.mkdir()
    monkeypatch.setattr(fastplot, '__file__', str(root / 'fastplot.py'))
    config = {'DATA_PROCESSING_ROOT': str(root), 'VAST_REFERENCE_COPY': str(root / 'vast'),
              'URL_OF_DATA_PROCESSING_ROOT': 'http://127.0.0.1/uploads'}
    monkeypatch.setattr(fastplot, 'get_config', lambda: config)
    calls = []
    monkeypatch.setattr(fastplot.os, 'system', lambda command: calls.append(command) or 0)
    monkeypatch.setattr(fastplot, 'check_disk_space', lambda path: (True, 10**8))
    wrapper = root / 'fastplot_wrapper.sh'
    wrapper.write_text('#!/bin/sh\nexit 99\n')
    wrapper.chmod(0o700)
    monkeypatch.setenv('QUERY_STRING', urllib.parse.urlencode(
        {'candidate_url': 'http://untrusted.invalid/uploads/results_1/index.html#candidate1'}))
    return types.SimpleNamespace(root=root, data=root / 'fastplot', calls=calls, config=config)


def test_fastplot_uses_configured_host_not_submitted_host(plot_sandbox):
    url, candidate = fastplot.validate_candidate_url(
        'https://untrusted.invalid/uploads/results_1/index.html#candidate1', plot_sandbox.config)
    assert url == 'http://127.0.0.1/uploads/results_1/index.html#candidate1'
    assert candidate == 'candidate1'


@pytest.mark.parametrize('path', ['../secret.html', '%2e%2e/secret.html',
                                  'x;echo/index.html', '<img>/index.html', 'bad|name/index.html'])
def test_fastplot_bad_paths_never_launch(plot_sandbox, monkeypatch, capsys, path):
    monkeypatch.setenv('QUERY_STRING', urllib.parse.urlencode(
        {'candidate_url': 'http://untrusted.invalid/uploads/' + path + '#candidate1'}))
    fastplot.main()
    output = capsys.readouterr().out
    assert 'Status: 400' in output and '<img>' not in output
    assert plot_sandbox.calls == []


def test_fastplot_cache_bypasses_rate_limit_without_new_work(plot_sandbox, capsys):
    plot_sandbox.data.mkdir()
    (plot_sandbox.data / 'fastplot__today__candidate1.tar.gz').write_bytes(b'cached')
    (plot_sandbox.data / '.fastplot_rate_limit').write_text('9999999999\n' * 10)
    fastplot.main()
    output = capsys.readouterr().out
    assert 'Status: 302' in output and '/fastplot/fastplot__today__candidate1.tar.gz' in output
    assert plot_sandbox.calls == []


@pytest.mark.parametrize('candidate,expected', [('candidate1', 200), ('another', 503)])
def test_fastplot_actual_flock_blocks_duplicate_launch(plot_sandbox, capsys, candidate, expected):
    plot_sandbox.data.mkdir()
    with (plot_sandbox.data / '.fastplot.lock').open('w') as lock:
        lock.write(candidate)
        lock.flush()
        fcntl.flock(lock, fcntl.LOCK_EX)
        fastplot.main()
    assert 'Status: {}'.format(expected) in capsys.readouterr().out
    assert plot_sandbox.calls == []


def test_fastplot_rate_limit_and_expiration(plot_sandbox, monkeypatch, capsys):
    plot_sandbox.data.mkdir()
    monkeypatch.setattr(fastplot.time, 'time', lambda: 10000.)
    rate = plot_sandbox.data / '.fastplot_rate_limit'
    rate.write_text('bad\n1\n' + '9999\n' * fastplot.MAX_REQUESTS_PER_HOUR)
    fastplot.main()
    assert 'Status: 429' in capsys.readouterr().out
    assert plot_sandbox.calls == []
    rate.write_text('1\ninvalid\n')
    assert fastplot.check_rate_limit(str(plot_sandbox.data)) is True
    fastplot.record_request(str(plot_sandbox.data))
    assert rate.read_text() == '10000.0\n'


def test_fastplot_launch_quotes_arguments_and_strips_cgi(plot_sandbox, capsys):
    fastplot.main()
    assert 'Status: 200' in capsys.readouterr().out
    assert len(plot_sandbox.calls) == 1
    command = plot_sandbox.calls[0]
    assert command.startswith('unset GATEWAY_INTERFACE REQUEST_METHOD QUERY_STRING CONTENT_LENGTH CONTENT_TYPE; ')
    args = shlex.split(command.split('; ', 1)[1])
    assert args[:4] == ['nohup', str(plot_sandbox.root / 'fastplot_wrapper.sh'),
                        'http://127.0.0.1/uploads/results_1/index.html#candidate1', 'candidate1']
    assert args[4:] == ['>', str(plot_sandbox.data / 'fastplot_candidate1.log'), '2>&1', '&']


def test_fastplot_failure_log_is_private_and_retry_after_cooldown_works(plot_sandbox, monkeypatch, capsys):
    plot_sandbox.data.mkdir()
    log = plot_sandbox.data / 'fastplot_candidate1.log'
    log.write_text('SECRET /private/path <script>')
    fastplot.main()
    output = capsys.readouterr().out
    assert 'Status: 500' in output and 'SECRET' not in output and '/private/path' not in output
    assert plot_sandbox.calls == []
    os.utime(log, (1, 1))
    fastplot.main()
    assert 'Status: 200' in capsys.readouterr().out
    assert len(plot_sandbox.calls) == 1


def test_fastplot_low_disk_does_not_start_work(plot_sandbox, monkeypatch, capsys):
    monkeypatch.setattr(fastplot, 'check_disk_space', lambda path: (False, 10))
    fastplot.main()
    assert 'Status: 507' in capsys.readouterr().out
    assert plot_sandbox.calls == []

"""Exercise CGI entry points, not copies of their validation logic."""
import fcntl
import io
import os
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

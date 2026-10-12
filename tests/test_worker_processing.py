"""Real worker orchestration/output; only external science binaries are stubbed."""
import shutil

import pytest

import archive_phot_worker as worker
import nmw_archive_phot_lib as apl


@pytest.fixture
def science_standins(app_sandbox, camera_images, monkeypatch):
    images = []
    for source in camera_images:
        target = app_sandbox.paths['archive'] / source.name
        shutil.copy2(source, target)
        images.append(str(target))
    work = app_sandbox.root / 'disposable-vast'
    work.mkdir()
    monkeypatch.delenv('DEBUG_KEEP_WORK_DIR', raising=False)
    monkeypatch.setattr(worker, 'setup_vast_working_copy', lambda *a, **kw: str(work))
    monkeypatch.setattr(worker, '_read_factory_text', lambda *a: '')
    monkeypatch.setattr(worker, 'derive_band', lambda *a: 'V')
    monkeypatch.setattr(worker, 'derive_sextractor_config', lambda *a: None)
    monkeypatch.setattr(worker, '_phase1_parallel_solve_plate',
                        lambda *a, **kw: (2, 0, 0, {p: p for p in images}, .1))
    monkeypatch.setattr(worker, 'get_jd_and_atel_date', lambda *a: ('2460683.15', '2025-01-07'))
    monkeypatch.setattr(worker, 'get_image_metadata', lambda *a: {'nx': 1024, 'ny': 1024})
    monkeypatch.setattr(worker, 'make_zoomout_thumbnail', lambda *a, **kw: None)
    monkeypatch.setattr(worker, 'make_zoomin_thumbnail', lambda *a, **kw: None)
    monkeypatch.setattr(worker, 'render_lightcurve_plots', lambda *a: (None, None))
    return images, work


@pytest.mark.parametrize('states,expected', [(('detection', 'upperlimit'), (2, 0)),
                                            (('detection', None), (1, 1)),
                                            ((None, None), (0, 2))])
def test_worker_publishes_measurements_skips_and_persistent_results(
        app_sandbox, science_standins, monkeypatch, states, expected):
    images, work = science_standins
    measurements = dict(zip(images, states))
    calls = []

    def measure(*args, **kwargs):
        image = args[2]
        calls.append(image)
        state = measurements[image]
        return (dict(status=state, mag='14.25', err='.03', jd='2460683.15',
                     x='512', y='512', aperture='5') if state else None)

    monkeypatch.setattr(worker, 'run_forced_photometry_c', measure)
    jid, directory = app_sandbox.make_job(images=[{'path': p} for p in images])
    request = apl.read_request(directory)
    assert worker.process_job(jid, str(directory), request, app_sandbox.config, '/unused', None) == expected
    assert calls == images  # same field, distinct exposure filenames: both measured
    state = apl.read_state(directory)
    assert state['state'] == apl.STATE_DONE and state['n_done'] == 2
    assert (state['n_measured'], state['n_skipped']) == expected
    report = (directory / 'index.html').read_text()
    assert '{} of 2 image(s) yielded'.format(expected[0]) in report
    assert not work.exists() and directory.is_dir()
    if 'detection' in states:
        assert '14.250' in (directory / 'lightcurve.dat').read_text()
    if 'upperlimit' in states:
        assert '14.250' in (directory / 'upperlimits.dat').read_text()


def test_worker_cleans_disposable_copy_after_science_exception(app_sandbox, science_standins, monkeypatch):
    images, work = science_standins
    jid, directory = app_sandbox.make_job(images=[{'path': p} for p in images])

    def fail(*args, **kwargs):
        raise RuntimeError('simulated calibration failure')

    monkeypatch.setattr(worker, '_phase1_parallel_solve_plate', fail)
    with pytest.raises(RuntimeError, match='calibration'):
        worker.process_job(jid, str(directory), apl.read_request(directory),
                           app_sandbox.config, '/unused', None)
    assert not work.exists() and directory.is_dir()

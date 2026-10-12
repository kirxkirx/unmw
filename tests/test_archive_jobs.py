"""Real queue/CGI behavior, isolated from production and external submission."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import archive_forced_photometry as submit
import archive_phot_status as status
import archive_phot_worker as worker
import nmw_archive_phot_lib as apl


@pytest.mark.parametrize('bad', ['../x', '/tmp/x', '20260101-000000_abcdefgh/..',
                                 '20260101-000000_abcdefgh\n', '<script>', ''])
def test_job_paths_cannot_escape_queue(app_sandbox, bad):
    assert apl.job_dir_path(bad) is None


def test_job_is_published_only_after_metadata_is_complete(app_sandbox, monkeypatch):
    original = apl.os.rename
    observed = []

    def checked_rename(source, destination):
        assert apl.list_job_dirs() == []
        assert apl.read_request(source)['ra'] == '300'
        assert apl.read_state(source)['state'] == apl.STATE_QUEUED
        observed.append(destination)
        return original(source, destination)

    monkeypatch.setattr(apl.os, 'rename', checked_rename)
    jid, directory = app_sandbox.make_job()
    assert observed == [str(directory)]
    assert apl.queue_position(jid) == 1
    assert not list(app_sandbox.paths['uploads'].glob('.*tmp*'))


def test_concurrent_workers_claim_each_job_exactly_once(app_sandbox):
    expected = [app_sandbox.make_job()[0] for _ in range(8)]
    source = str(Path(worker.__file__).resolve().parent)
    code = '''import json, sys
sys.path.insert(0, sys.argv[1])
import archive_phot_worker as worker
worker.apl.TEMP_PARENT = sys.argv[2]
claims = []
while True:
    job = worker._claim_next_job()
    if job is None:
        break
    claims.append(job[0])
print(json.dumps(claims))
'''
    processes = []
    try:
        for _ in range(4):
            processes.append(subprocess.Popen(
                [sys.executable, '-c', code, source, str(app_sandbox.paths['uploads'])],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        claimed = []
        for proc in processes:
            out, err = proc.communicate(timeout=15)
            assert proc.returncode == 0, err
            claimed.extend(json.loads(out))
        assert sorted(claimed) == expected
        assert len(set(claimed)) == len(expected)
        assert all(entry[2]['state'] == apl.STATE_RUNNING for entry in apl.list_pending_jobs())
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()


@pytest.mark.parametrize('alive,recent,expected', [(False, False, 1), (True, False, 0),
                                                  (False, True, 0)])
def test_dead_worker_recovery_is_terminal(app_sandbox, monkeypatch, alive, recent, expected):
    jid, directory = app_sandbox.make_job(apl.STATE_RUNNING)
    apl.update_state(str(directory), pid=987654,
                     started_epoch=time.time() if recent else 1)
    monkeypatch.setattr(apl, 'pid_alive', lambda pid: alive)
    assert apl.sweep_dead_running_jobs() == expected
    if expected:
        assert apl.read_state(directory)['state'] == apl.STATE_FAILED
        assert 'submit the same request again' in (directory / 'index.html').read_text()
        assert worker._claim_next_job() is None
        retry, _ = app_sandbox.make_job()
        assert retry != jid
        assert worker._claim_next_job()[0] == retry
    else:
        assert apl.read_state(directory)['state'] == apl.STATE_RUNNING


def test_worker_failure_does_not_wedge_following_jobs(app_sandbox, monkeypatch):
    first, first_dir = app_sandbox.make_job()
    second, second_dir = app_sandbox.make_job()
    app_sandbox.configure(worker)
    monkeypatch.delenv('GATEWAY_INTERFACE')
    monkeypatch.delenv('REQUEST_METHOD')
    seen = []

    def process(jid, directory, *args):
        seen.append(jid)
        if jid == first:
            raise RuntimeError('<script>failure</script>')
        apl.update_state(directory, state=apl.STATE_DONE)
        return 2, 0

    monkeypatch.setattr(worker, 'process_job', process)
    assert worker.main() == 0
    assert seen == [first, second]
    assert apl.read_state(first_dir)['state'] == apl.STATE_FAILED
    assert '<script>failure</script>' not in (first_dir / 'index.html').read_text()
    assert apl.read_state(second_dir)['state'] == apl.STATE_DONE


def prepare_submission(app_sandbox, camera_images, monkeypatch):
    from shutil import copyfile
    images = []
    for path in camera_images:
        target = app_sandbox.paths['archive'] / ('wcs_fd_' + path.name)
        copyfile(path, target)
        images.append(str(target))
    reference = str(app_sandbox.paths['reference'] / ('wcs_fd_' + camera_images[0].name))
    monkeypatch.setattr(submit, 'run_sky2xy_scan', lambda *args: ([(reference, 512, 512)], False))
    # Native date parsing is covered separately; avoid a science process here.
    monkeypatch.setattr(apl, 'get_jd_batch', lambda vast, paths, **kwargs:
                        {p: 2460683.146 + i / 2880 for i, p in enumerate(paths)})
    app_sandbox.configure(submit, {'coords': '300 20'})
    return images


@pytest.mark.parametrize('old_state', [apl.STATE_DONE, apl.STATE_FAILED])
def test_completed_and_failed_requests_can_be_reprocessed(app_sandbox, camera_images,
                                                         monkeypatch, capsys, old_state):
    images = prepare_submission(app_sandbox, camera_images, monkeypatch)
    old_id, old_dir = app_sandbox.make_job(old_state, snapshot_paths=images)
    before = (old_dir / 'request.json').read_bytes()
    submit.main()
    output = capsys.readouterr().out
    pending = apl.list_pending_jobs()
    assert len(pending) == 1 and pending[0][0] != old_id
    assert len(pending[0][3]['images']) == 2
    assert set(pending[0][3]['snapshot_paths']) == set(images)
    assert 'queued' in output and old_id in output
    assert (old_dir / 'request.json').read_bytes() == before
    assert len(app_sandbox.kicks) == 1


def test_same_field_different_exposures_are_all_discovered(app_sandbox, camera_images):
    from shutil import copyfile
    archive = app_sandbox.paths['archive']
    expected = []
    for image in camera_images:
        target = archive / ('wcs_fd_' + image.name)
        copyfile(image, target)
        expected.append(str(target))
    for hidden in ('bad', '.private'):
        (archive / hidden).mkdir()
        copyfile(camera_images[0], archive / hidden / ('wcs_fd_' + camera_images[0].name))
    copyfile(camera_images[0], archive / 'wcs_fd_Vul3_other.fts')
    assert apl.discover_archive_images(str(archive), {'Vul8'}) == sorted(expected)


@pytest.mark.parametrize('query,expected', [({'coords': '<script>alert(1)</script>'}, 'Invalid coordinates'),
                                          ({'coords': '300 20', 'band': '<b>'}, 'Invalid band'),
                                          ({'coords': '300 20', 'date_from': '2026-02-30'}, 'Invalid date'),
                                          ({'coords': '300 20', 'date_from': '2026-02-02',
                                            'date_to': '2026-01-01'}, 'Invalid date range')])
def test_submission_rejects_bad_forms_without_starting_jobs(app_sandbox, capsys, query, expected):
    app_sandbox.configure(submit, query)
    submit.main()
    output = capsys.readouterr().out
    assert expected in output and '<script>alert' not in output
    assert apl.list_job_dirs() == [] and app_sandbox.kicks == []


@pytest.mark.parametrize('kind', ['total', 'per_ip'])
def test_queue_caps_remain_resource_limits_not_duplicate_detection(app_sandbox, capsys, kind):
    app_sandbox.make_job(ra_deg=100.)
    app_sandbox.config['ARCHIVE_PHOT_MAX_QUEUE' if kind == 'total' else
                       'ARCHIVE_PHOT_MAX_QUEUE_PER_IP'] = '1'
    app_sandbox.configure(submit, {'coords': '300 20'})
    submit.main()
    assert 'Status: 503' in capsys.readouterr().out
    assert len(apl.list_job_dirs()) == 1 and app_sandbox.kicks == []


@pytest.mark.parametrize('state', [apl.STATE_QUEUED, apl.STATE_RUNNING, apl.STATE_DONE, apl.STATE_FAILED])
def test_status_endpoint_for_every_job_state(app_sandbox, capsys, state):
    jid, directory = app_sandbox.make_job(state)
    (directory / 'progress.log').write_text('<script>private</script>\n')
    (directory / 'index.html').write_text('result')
    app_sandbox.configure(status, {'job': jid})
    status.main()
    output = capsys.readouterr().out
    assert '<script>private</script>' not in output
    if state in (apl.STATE_DONE, apl.STATE_FAILED):
        assert 'Status: 303' in output or 'Status: 302' in output
        assert jid + '/index.html' in output
    else:
        assert state in output
    assert bool(app_sandbox.kicks) == (state == apl.STATE_QUEUED)


@pytest.mark.parametrize('job', ['../../etc/passwd', '20260101-000000_aaaaaaaa'])
def test_unknown_status_job_is_404(app_sandbox, capsys, job):
    app_sandbox.configure(status, {'job': job})
    status.main()
    assert 'Status: 404' in capsys.readouterr().out


def test_update_check_counts_new_visits_not_just_field_names(app_sandbox, camera_images, capsys):
    from shutil import copyfile
    paths = []
    for image in camera_images:
        target = app_sandbox.paths['archive'] / ('wcs_fd_' + image.name)
        copyfile(image, target)
        paths.append(str(target))
    jid, _ = app_sandbox.make_job(apl.STATE_DONE, snapshot_paths=paths[:1])
    app_sandbox.configure(status, {'job': jid, 'check_updates': '1'})
    status.main()
    output = capsys.readouterr().out
    assert 'gained <b>1</b> image(s)' in output
    assert 'submit the same request again' in output

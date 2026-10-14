"""Regression checks for the test harness itself (only a loopback stub HTTP server, no external services)."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading

import jsonschema
import pytest
import yaml

from tests import collect_artifacts, run_science_test, wait_for_processing


REPO = Path(__file__).resolve().parents[1]


def test_external_connections_are_blocked():
    with socket.socket() as sock, pytest.raises(AssertionError, match='external services'):
        sock.connect(('www.aavso.org', 443))


def test_science_environment_never_inherits_credentials(monkeypatch):
    for key in ('AAVSO_PASSWORD', 'AAVSO_OBSCODE', 'CURL_USERNAME_URL_TO_EMAIL_TEAM',
                'SLACK_WEBHOOK_URL_TEAM', 'MONITORING_POSITIONS_FILE', 'DATA_PROCESSING_ROOT'):
        monkeypatch.setenv(key, 'must-not-leak')
    env = run_science_test.clean_environment()
    assert set(env) == {'PATH', 'LANG', 'LC_ALL', 'AAVSO_OBSCODE', 'PYTHONDONTWRITEBYTECODE'}
    assert env['AAVSO_OBSCODE'] == 'XXX'


def test_selftest_refuses_to_overwrite_checkout(tmp_path):
    result = subprocess.run(['bash', str(REPO / 'unmw_selftest.sh')], cwd=tmp_path,
                            env={'PATH': os.environ['PATH']}, capture_output=True,
                            text=True, timeout=5)
    assert result.returncode != 0 and 'run_science_test.py' in result.stdout
    assert not list(tmp_path.iterdir())


def test_wait_ignores_unrelated_processing(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    owned = tmp_path / 'owned'
    owned.mkdir()
    other = tmp_path / 'other'
    other.mkdir()
    for pid, cwd, args in ((123, owned, b'bash\0./autoprocess.sh\0x\0'),
                           (124, other, b'bash\0./autoprocess.sh\0x\0'),
                           (125, owned, b'bash\0not_autoprocess.sh\0')):
        entry = proc / str(pid)
        entry.mkdir()
        (entry / 'cwd').symlink_to(cwd)
        (entry / 'cmdline').write_bytes(args)
    assert wait_for_processing.processing_pids(owned, proc) == [123]


def test_processing_wait_has_a_real_deadline(monkeypatch):
    monkeypatch.setattr(wait_for_processing, 'processing_pids', lambda root: [123])
    with pytest.raises(TimeoutError, match='123'):
        wait_for_processing.wait('/unused', timeout=0)


def test_artifacts_exclude_credentials_and_image_archives(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    for name in ('unit/pytest/case/private/error.log', 'unit/pytest/case/private/passwords',
                 'unit/pytest/case/upload.zip', 'unit/junit.xml', 'science/selftest.log'):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('test')
    destination = tmp_path / 'artifacts'
    collect_artifacts.collect(source, destination)
    files = {str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file()}
    assert files == {'unit/junit.xml', 'science/selftest.log', 'unit/pytest/case/private/error.log'}


def test_schema_validation_is_real_and_rejects_wrong_candidate_type():
    schema = json.loads((REPO / 'filter_report_json_schema.json').read_text())
    valid = {'schema_version': 1, 'generated_at_utc': '2026-01-01', 'candidates': []}
    jsonschema.validate(valid, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(dict(valid, candidates='not a list'), schema)


def test_workflow_matrix_and_policy():
    # BaseLoader preserves "on" rather than interpreting it as YAML 1.1 true.
    ubuntu = yaml.load((REPO / '.github/workflows/test_ubuntu.yml').read_text(), Loader=yaml.BaseLoader)
    wsl = yaml.load((REPO / '.github/workflows/test_wsl.yml').read_text(), Loader=yaml.BaseLoader)
    for workflow in (ubuntu, wsl):
        assert set(workflow['on']) == {'push', 'workflow_dispatch'}
        assert workflow['on']['push']['branches'] == ['main']
    matrix = ubuntu['jobs']['unit-tests']['strategy']['matrix']
    assert set(matrix['python-version']) == {'3.9', '3.10', '3.11', '3.12', '3.13', '3.14'}
    assert any(row['rarfile'] == 'absent' and row['advisory'] == 'false' for row in matrix['include'])
    assert any(row['python-version'] == '3.15' and row['advisory'] == 'true' for row in matrix['include'])


FETCH_FIXTURE = REPO / 'tests' / 'fetch_fixture.sh'
PAYLOAD = bytes(range(256)) * 400
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
HALF = len(PAYLOAD) // 2
# The byte-exact body the proxy in front of tau.kirx.net returned to every CI
# job on 2026-10-13 (SHA256 5764fed3...): it was saved as the archive and "unpacked".
SERVICE_UNAVAILABLE = (
    b'<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">\n<html><head>\n'
    b'<title>503 Service Unavailable</title>\n</head><body>\n<h1>Service Unavailable</h1>\n'
    b'<p>The server is temporarily unable to service your\nrequest due to maintenance downtime '
    b'or capacity\nproblems. Please try again later.</p>\n</body></html>\n')


class _FixtureMirror(http.server.BaseHTTPRequestHandler):
    """Loopback stand-in for a fixture mirror; each path replays scripted answers.

    An answer is a dict with 'status' and 'body', optionally 'cut' (close the
    connection after that many bytes of the promised body), 'location' (redirect
    target) and 'ranges': False (ignore a Range request, as a server without
    byte-range support would).
    """
    protocol_version = 'HTTP/1.0'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get('Range')))
        answers = self.server.plan[self.path]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        status, body, start = answer['status'], answer.get('body', b''), 0
        if status == 200 and self.headers.get('Range') and answer.get('ranges', True):
            start, status = int(self.headers['Range'].split('=')[1].rstrip('-')), 206
        self.send_response(status)
        if 'location' in answer:
            self.send_header('Location', answer['location'])
        self.send_header('Content-Length', str(len(body) - start))
        if status == 206:
            self.send_header('Content-Range', 'bytes {}-{}/{}'.format(start, len(body) - 1, len(body)))
        self.end_headers()
        self.wfile.write(body[start:answer.get('cut', len(body))])


@pytest.fixture
def mirror():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _FixtureMirror)
    server.plan = {}
    server.requests = []
    server.url = 'http://127.0.0.1:{}'.format(server.server_port)
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def fetch_fixture(cwd, dest, digest, *urls, attempts=1, **settings):
    env = {'PATH': os.environ['PATH'], 'UNMW_FETCH_ATTEMPTS': str(attempts), 'UNMW_FETCH_RETRY_PAUSE': '0'}
    env.update(settings)
    return subprocess.run(['bash', str(FETCH_FIXTURE), str(dest), digest, *urls], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def test_fixture_download_skips_error_pages_and_wrong_content(tmp_path, mirror):
    mirror.plan = {'/down.tar.bz2': [{'status': 503, 'body': SERVICE_UNAVAILABLE}],
                   '/wrong.tar.bz2': [{'status': 200, 'body': SERVICE_UNAVAILABLE}],
                   '/empty.tar.bz2': [{'status': 200}],
                   '/good.tar.bz2': [{'status': 200, 'body': PAYLOAD}]}
    dest = tmp_path / 'fixture.tar.bz2'
    result = fetch_fixture(tmp_path, dest, PAYLOAD_SHA256, *(mirror.url + path for path in mirror.plan))
    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == PAYLOAD
    assert list(tmp_path.iterdir()) == [dest]
    assert [path for path, _ in mirror.requests] == list(mirror.plan)
    # curl's own --fail diagnostic for the 503, then the impostor with an excerpt of its content.
    assert 'returned error: 503' in result.stderr
    assert 'Rejected' in result.stderr and '<title>503 Service Unavailable</title>' in result.stderr
    assert 'verified' in result.stdout


def test_fixture_download_fails_closed_without_a_good_mirror(tmp_path, mirror):
    mirror.plan = {'/down.tar.bz2': [{'status': 503, 'body': SERVICE_UNAVAILABLE}],
                   '/wrong.tar.bz2': [{'status': 200, 'body': SERVICE_UNAVAILABLE}],
                   '/never.tar.bz2': [{'status': 200, 'body': PAYLOAD, 'cut': HALF}]}
    dest = tmp_path / 'fixture.tar.bz2'
    result = fetch_fixture(tmp_path, dest, PAYLOAD_SHA256,
                           *(mirror.url + path for path in mirror.plan), attempts=2)
    assert result.returncode == 1 and 'no mirror served' in result.stderr
    assert not list(tmp_path.iterdir())
    # A failed transfer is retried (a truncated one resumed); a complete file with the wrong checksum is not.
    assert mirror.requests == [('/down.tar.bz2', None), ('/down.tar.bz2', None), ('/wrong.tar.bz2', None),
                               ('/never.tar.bz2', None), ('/never.tar.bz2', 'bytes={}-'.format(HALF))]


def test_fixture_download_resumes_an_interrupted_transfer(tmp_path, mirror):
    mirror.plan = {'/flaky.tar.bz2': [{'status': 200, 'body': PAYLOAD, 'cut': HALF},
                                      {'status': 200, 'body': PAYLOAD}]}
    dest = tmp_path / 'fixture.tar.bz2'
    result = fetch_fixture(tmp_path, dest, PAYLOAD_SHA256, mirror.url + '/flaky.tar.bz2', attempts=2)
    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == PAYLOAD
    assert mirror.requests == [('/flaky.tar.bz2', None), ('/flaky.tar.bz2', 'bytes={}-'.format(HALF))]


def test_fixture_download_never_resumes_a_file_from_elsewhere(tmp_path, mirror):
    """Neither a file left by an earlier run nor a partial file from another mirror is resumed."""
    mirror.plan = {'/flaky.tar.bz2': [{'status': 200, 'body': PAYLOAD[::-1], 'cut': HALF}],
                   '/good.tar.bz2': [{'status': 200, 'body': PAYLOAD}]}
    dest = tmp_path / 'fixture.tar.bz2'
    dest.write_bytes(SERVICE_UNAVAILABLE)
    result = fetch_fixture(tmp_path, dest, PAYLOAD_SHA256, mirror.url + '/flaky.tar.bz2', mirror.url + '/good.tar.bz2')
    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == PAYLOAD
    assert mirror.requests == [('/flaky.tar.bz2', None), ('/good.tar.bz2', None)]


def test_fixture_download_restarts_when_a_mirror_cannot_resume(tmp_path, mirror):
    mirror.plan = {'/norange.tar.bz2': [{'status': 200, 'body': PAYLOAD, 'cut': HALF},
                                        {'status': 200, 'body': PAYLOAD, 'ranges': False}]}
    dest = tmp_path / 'fixture.tar.bz2'
    result = fetch_fixture(tmp_path, dest, PAYLOAD_SHA256, mirror.url + '/norange.tar.bz2', attempts=3)
    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == PAYLOAD
    assert 'curl exit 33' in result.stderr
    assert mirror.requests == [('/norange.tar.bz2', None), ('/norange.tar.bz2', 'bytes={}-'.format(HALF)),
                               ('/norange.tar.bz2', None)]


def test_fixture_download_follows_a_redirect(tmp_path, mirror):
    mirror.plan = {'/moved.tar.bz2': [{'status': 301, 'location': mirror.url + '/good.tar.bz2'}],
                   '/good.tar.bz2': [{'status': 200, 'body': PAYLOAD}]}
    dest = tmp_path / 'fixture.tar.bz2'
    result = fetch_fixture(tmp_path, dest, PAYLOAD_SHA256, mirror.url + '/moved.tar.bz2')
    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == PAYLOAD
    assert [path for path, _ in mirror.requests] == ['/moved.tar.bz2', '/good.tar.bz2']


@pytest.mark.parametrize('digest,settings', [
    ('', {}), ('not-a-sha256', {}), (PAYLOAD_SHA256, {'UNMW_FETCH_ATTEMPTS': '0'}),
    (PAYLOAD_SHA256, {'UNMW_FETCH_RETRY_PAUSE': 'soon'}),
])
def test_fixture_download_rejects_a_malformed_pin_or_setting(tmp_path, mirror, digest, settings):
    mirror.plan = {'/good.tar.bz2': [{'status': 200, 'body': PAYLOAD}]}
    result = fetch_fixture(tmp_path, tmp_path / 'fixture.tar.bz2', digest, mirror.url + '/good.tar.bz2', **settings)
    assert result.returncode == 2 and 'Usage' in result.stderr
    assert not mirror.requests and not list(tmp_path.iterdir())


def test_fetch_fixture_refuses_cgi_invocation(tmp_path):
    result = subprocess.run(['bash', str(FETCH_FIXTURE), 'fixture', '0' * 64, 'http://127.0.0.1:9/'],
                            cwd=tmp_path, env={'PATH': os.environ['PATH'], 'REQUEST_METHOD': 'GET'},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and 'not a CGI' in result.stderr
    assert not list(tmp_path.iterdir())


def test_science_fixtures_are_pinned_and_fetched_through_the_helper():
    script = (REPO / 'unmw_selftest.sh').read_text()
    pins = dict(line.split('=', 1) for line in (REPO / 'tests/dependencies.env').read_text().splitlines()
                if line and not line.startswith('#'))
    for name in ('UNMW_TEST_NOVA_FIXTURE_SHA256', 'UNMW_TEST_CAMERA_RAR_SHA256'):
        assert re.fullmatch('[0-9a-f]{64}', pins[name])
        assert re.search(r'fetch_fixture\.sh" \S+ "\$' + name + '"', script)
    # Every archive download goes through the verified helper, never a bare 'curl -O' in any form.
    assert not re.search(r'\bcurl\b[^\n]*(\s-O\b|--remote-name\b)', script)

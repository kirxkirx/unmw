"""Real loopback HTTP -> Apache -> upload CGI -> wrapper -> native extraction.

Only the final science processor is a test stand-in. No production Apache
configuration, service, filesystem tree, credentials or ports are used.
"""
import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import types
import urllib.parse
import zipfile

import pytest


pytestmark = pytest.mark.apache
REPO = Path(__file__).resolve().parents[1]
AUTH = 'Basic ' + base64.b64encode(b'tester:only-a-test-password').decode()


def tool(name):
    found = shutil.which(name)
    if not found:
        if os.environ.get('UNMW_TEST_STRICT') == '1':
            pytest.fail('Required HTTP test tool is missing: ' + name)
        pytest.skip('HTTP test requires ' + name)
    return found


@pytest.fixture
def apache(tmp_path):
    if os.geteuid() == 0:
        pytest.fail('Run Apache integration tests as an unprivileged test user, not root')
    server_path = os.environ['PATH'] + os.pathsep + '/usr/sbin' + os.pathsep + '/sbin'
    binary = shutil.which('apache2', path=server_path) or shutil.which('httpd', path=server_path) or tool('httpd')
    htpasswd = tool('htpasswd')
    tool('rar')
    tool('unzip')
    modules = next((path for path in (Path('/usr/lib/apache2/modules'),
                                      Path('/usr/lib64/apache2/modules'),
                                      Path('/usr/lib64/httpd/modules'),
                                      Path('/usr/lib/httpd/modules')) if path.is_dir()), None)
    if modules is None:
        pytest.fail('Apache shared modules were not found')
    compiled = subprocess.run([binary, '-l'], check=True, capture_output=True, text=True).stdout
    root = tmp_path / 'unmw-test-site'
    app = root / 'app'
    app.mkdir(parents=True)
    private = root / 'private'
    private.mkdir()
    (root / 'public').mkdir()
    (root / '.unmw-http-test').write_text('isolated test only\n')
    (app / 'uploads').mkdir()
    (app / 'tests').mkdir()
    (app / '.git').mkdir()
    (app / '.git' / 'config').write_text('PRIVATE REPOSITORY CONTENT\n')
    for name in ('.htaccess', 'wrapper.sh', 'upload.py3', 'upload.py2',
                 'nmw_fs_check.py', 'nmw_coord_lib.py'):
        shutil.copy2(REPO / name, app / name)
    shutil.copy2(REPO / 'tests' / '.htaccess', app / 'tests' / '.htaccess')
    (app / 'tests' / 'private.txt').write_text('PRIVATE TEST DATA')
    (app / 'upload.py').symlink_to('upload.py3')
    for name in ('local_config.sh', 'HANDOFF.md', '.env'):
        (app / name).write_text('# PRIVATE CONFIGURATION CONTENT\n')
    (app / 'autoprocess.sh').write_text('#!/bin/sh\nexec {} -B {} "$1"\n'.format(
        shlex.quote(sys.executable), shlex.quote(str(REPO / 'tests' / 'http_extract_stub.py'))))
    (app / 'autoprocess.sh').chmod(0o700)
    # An explicitly unsuccessful processing attempt must remain retryable.
    (private / 'passwords').write_bytes(subprocess.run(
        [htpasswd, '-nb', 'tester', 'only-a-test-password'], check=True,
        capture_output=True).stdout)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = 'http://127.0.0.1:' + str(port)
    loads = []
    for name in ('mpm_prefork', 'authn_core', 'authn_file', 'authz_core', 'authz_host',
                 'authz_user', 'auth_basic', 'access_compat', 'unixd', 'alias', 'dir',
                 'mime', 'env', 'log_config', 'cgi'):
        path = modules / ('mod_' + name + '.so')
        if path.is_file() and ('mod_' + name + '.c') not in compiled:
            loads.append('LoadModule {}_module "{}"'.format(name, path))
    # mpm module's identifier differs from its filename on shared-MPM builds.
    config = '''ServerRoot "{root}"
{loads}
Listen 127.0.0.1:{port}
ServerName 127.0.0.1
PidFile "{private}/httpd.pid"
ErrorLog "{private}/error.log"
LogLevel warn
User #{uid}
Group #{gid}
DefaultRuntimeDir "{private}"
Timeout 15
StartServers 1
MinSpareServers 1
MaxSpareServers 2
ServerLimit 4
MaxRequestWorkers 4
DocumentRoot "{root}/public"
TypesConfig /dev/null
ScriptAlias /cgi-bin/unmw/ "{app}/"
Alias /uploads/ "{app}/uploads/"
<Directory "{root}">
    Require all denied
</Directory>
<Directory "{app}">
    Options ExecCGI FollowSymLinks
    AllowOverride AuthConfig Options=ExecCGI
    SetHandler cgi-script
    AuthType Basic
    AuthName "isolated CI"
    AuthUserFile "{private}/passwords"
    Require valid-user
    LimitRequestBody 8388608
</Directory>
<Directory "{app}/uploads">
    AllowOverride None
    Options -ExecCGI -Indexes
    SetHandler None
    Require all granted
</Directory>
<DirectoryMatch "{app}/(?:\\.git|tests)(?:/|$)">
    Require all denied
</DirectoryMatch>
SetEnv PATH "{path}"
SetEnv PYTHONDONTWRITEBYTECODE 1
SetEnv WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB 1
SetEnv WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB 1
SetEnv SUSPICIOUS_FILENAMES_LOG "{private}/suspicious.log"
SetEnv UNMW_HTTP_TEST_ROOT "{root}"
SetEnv UNMW_TEST_BASE_URL "{base}"
SetEnv AAVSO_OBSCODE XXX
'''.format(root=root, app=app, private=private, port=port, uid=os.geteuid(),
           gid=os.getegid(), loads='\n'.join(loads), base=base,
           path=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
    # Pass only coverage settings, never arbitrary caller credentials, to CGI.
    for key in ('COVERAGE_PROCESS_START', 'COVERAGE_PROCESS_CONFIG', 'COVERAGE_RCFILE',
                'COVERAGE_FILE', 'UNMW_COVERAGE_SOURCE'):
        if key in os.environ:
            config += 'SetEnv {} "{}"\n'.format(key, os.environ[key])
    conf = private / 'httpd.conf'
    conf.write_text(config)
    check = subprocess.run([binary, '-f', str(conf), '-t'], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    log = (private / 'console.log').open('w')
    process = subprocess.Popen([binary, '-f', str(conf), '-DFOREGROUND'],
                               stdout=log, stderr=log, start_new_session=True)

    def request(method, path, body=None, headers=None, authenticated=True):
        headers = dict(headers or {})
        if authenticated:
            headers['Authorization'] = AUTH
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    try:
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                pytest.fail('Test Apache exited: ' + (private / 'console.log').read_text())
            try:
                request('GET', '/cgi-bin/unmw/upload.py', authenticated=False)
                break
            except ConnectionRefusedError:
                if time.monotonic() > deadline:
                    pytest.fail('Test Apache did not become ready')
                time.sleep(.05)
        yield types.SimpleNamespace(root=root, app=app, private=private, request=request, base=base)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        log.close()
        # pytest keeps these under --basetemp for the always-upload artifact step.


def archive_bytes(tmp_path, images, suffix, bad=None):
    if suffix == 'zip':
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
            for path in images:
                archive.write(path, path.name)
            if bad:
                archive.writestr(bad, b'bad')
        return output.getvalue()
    archive = tmp_path / 'camera.rar'
    subprocess.run([tool('rar'), 'a', '-cfg-', '-idq', '-m0', '-ep', '--', str(archive)] +
                   [str(path) for path in images], check=True, capture_output=True, timeout=30)
    return archive.read_bytes()


def upload(apache, data, filename):
    body = (b'--unmw-ci\r\nContent-Disposition: form-data; name="file"; filename="' +
            filename.encode() + b'"\r\nContent-Type: application/octet-stream\r\n\r\n' +
            data + b'\r\n--unmw-ci--\r\n')
    return apache.request('POST', '/cgi-bin/unmw/upload.py', body,
                          {'Content-Type': 'multipart/form-data; boundary=unmw-ci'})


@pytest.mark.parametrize('suffix', ['zip', 'rar'])
def test_real_camera_upload_and_repeat_are_independent(apache, tmp_path, camera_images, suffix):
    data = archive_bytes(tmp_path, camera_images, suffix)
    expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in camera_images}
    results = []
    for _ in range(2):
        code, body, _ = upload(apache, data, 'same-night.' + suffix)
        assert code == 200 and b'UNMW_STATUS:OK' in body, body
        url = re.search(rb'url=([^"<>]+)', body).group(1).decode()
        results.append(url)
        code, body, _ = apache.request('GET', urllib.parse.urlsplit(url).path)
        assert code == 200 and json.loads(body) == expected
    assert results[0] != results[1]
    assert len(list((apache.app / 'uploads').glob('web_upload_*'))) == 2


def test_failed_processing_does_not_block_resubmission(apache, tmp_path, camera_images):
    script = apache.app / 'autoprocess.sh'
    working = script.read_text()
    # Fail the wrapper's validation launch, so the CGI observes a real failure
    # and cleans up; the next identical upload must still be accepted.
    wrapper = apache.app / 'wrapper.sh'
    original = wrapper.read_text()
    wrapper.write_text('#!/bin/sh\nexit 1\n')
    data = archive_bytes(tmp_path, camera_images, 'zip')
    _, body, _ = upload(apache, data, 'retry.zip')
    assert b'UNMW_STATUS:ERROR' in body
    assert list((apache.app / 'uploads').iterdir()) == []
    wrapper.write_text(original)
    assert script.read_text() == working
    code, body, _ = upload(apache, data, 'retry.zip')
    assert code == 200 and b'UNMW_STATUS:OK' in body


@pytest.mark.parametrize('prefix', ['../', '/absolute/', 'C:/absolute/'])
def test_http_rejects_entire_rar_with_unsafe_paths(apache, tmp_path, camera_images, prefix):
    archive = tmp_path / 'unsafe.rar'
    subprocess.run([tool('rar'), 'a', '-cfg-', '-idq', '-m0', '-ep', '-ap' + prefix,
                    str(archive)] + [str(p) for p in camera_images],
                   check=True, capture_output=True, timeout=30)
    _, body, _ = upload(apache, archive.read_bytes(), 'unsafe.rar')
    assert b'UNMW_STATUS:ERROR' in body
    assert list((apache.app / 'uploads').iterdir()) == []
    assert (apache.private / 'suspicious.log').is_file()


@pytest.mark.parametrize('bad', ['../empty/', '/absolute/', 'C:/absolute/',
                                '<img src=x onerror=alert(1)>.fits'])
def test_http_rejects_entire_unsafe_archive_and_escapes_html(apache, tmp_path, camera_images, bad):
    data = archive_bytes(tmp_path, camera_images, 'zip', bad)
    _, body, _ = upload(apache, data, 'unsafe.zip')
    assert b'UNMW_STATUS:ERROR' in body
    assert b'<img src=x' not in body
    assert list((apache.app / 'uploads').iterdir()) == []
    assert (apache.private / 'suspicious.log').is_file()


def test_apache_authentication_and_private_file_denials(apache):
    code, _, _ = apache.request('GET', '/cgi-bin/unmw/upload.py', authenticated=False)
    assert code == 401
    for path in ('local_config.sh', 'HANDOFF.md', '.env', '.git/config', 'wrapper.sh',
                 'nmw_coord_lib.py', 'upload.py2', 'tests/private.txt'):
        code, body, _ = apache.request('GET', '/cgi-bin/unmw/' + path)
        assert code == 403, (path, code, body)
        assert b'PRIVATE' not in body


def test_apache_body_limit_and_upload_tree_not_executable(apache):
    code, body, _ = apache.request('POST', '/cgi-bin/unmw/upload.py', b'x',
                                  {'Content-Type': 'multipart/form-data; boundary=unmw-ci',
                                   'Content-Length': str(8 * 1024 * 1024 + 1)})
    assert code == 413, (code, body)
    assert list((apache.app / 'uploads').iterdir()) == []
    script = apache.app / 'uploads' / 'probe.py'
    script.write_text('#!/bin/sh\nprintf "Content-Type: text/plain\\n\\nEXECUTED"\n')
    script.chmod(0o700)
    code, body, _ = apache.request('GET', '/uploads/probe.py')
    assert code == 200 and body.startswith(b'#!/bin/sh')

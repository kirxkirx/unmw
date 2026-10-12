"""Regression tests for the scientific test server's CGI dependency boundary."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from tests import science_cgi


def test_configure_sthttpd_cgi_path_preserves_private_tools(tmp_path, monkeypatch):
    header = tmp_path / 'thttpd.h'
    header.write_bytes(b'/* Copyright \xa9 */\n#define CGI_PATH "/usr/bin:/bin"\n#define OTHER 1\n')
    # Spaces, quotes and backslashes must remain data in the generated C string.
    test_path = '/private/rar tools:/private/a"b\\c:/usr/bin:/bin'
    monkeypatch.setenv('PATH', test_path)
    science_cgi.configure(header)
    lines = header.read_bytes().splitlines()
    assert lines[0] == b'/* Copyright \xa9 */' and lines[2] == b'#define OTHER 1'
    assert json.loads(lines[1].removeprefix(b'#define CGI_PATH ')) == (
        str(Path(sys.executable).parent) + os.pathsep + test_path)


@pytest.mark.parametrize('definitions', [0, 2])
def test_configure_rejects_changed_upstream_header(tmp_path, definitions):
    header = tmp_path / 'thttpd.h'
    original = '#define CGI_PATH "/usr/bin:/bin"\n' * definitions
    header.write_text(original)
    with pytest.raises(RuntimeError, match='exactly one CGI_PATH'):
        science_cgi.configure(header)
    assert header.read_text() == original


@pytest.fixture
def probe_report(monkeypatch):
    monkeypatch.setattr(science_cgi.shutil, 'which', lambda name: '/private/tools/' + name)
    return {'executable': sys.executable, 'prefix': sys.prefix,
            'version': list(sys.version_info[:3]), 'rar': '/private/tools/rar', 'problems': []}


def test_preflight_accepts_selected_dependencies(probe_report):
    science_cgi.validate_probe(probe_report)


@pytest.mark.parametrize('field,value', [
    ('prefix', '/different/venv'), ('version', [3, 8, 0]),
    ('executable', '/different/bin/python3'), ('rar', None),
    ('rar', '/system/bin/rar'), ('problems', ['Cannot import jsonschema']),
])
def test_preflight_rejects_cgi_environment_mismatch(probe_report, field, value):
    probe_report[field] = value
    with pytest.raises(RuntimeError, match='CGI dependency preflight failed'):
        science_cgi.validate_probe(probe_report)


@pytest.mark.parametrize('missing', [None, 'jsonschema'])
def test_probe_checks_imports_but_does_not_require_rarfile(monkeypatch, capsys, missing):
    original_import = importlib.import_module

    def import_module(name):
        assert name != 'rarfile', 'The CGI preflight must not make rarfile mandatory'
        if name == missing:
            raise ImportError(name)
        return original_import(name)

    monkeypatch.setattr(importlib, 'import_module', import_module)
    monkeypatch.setattr(science_cgi.shutil, 'which', lambda name: '/private/tools/' + name)
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: None)
    exec(science_cgi.PROBE, {'__name__': '__main__'})
    report = json.loads(capsys.readouterr().out.split('\n\n', 1)[1])
    assert report['problems'] == ([] if missing is None else ['Cannot import jsonschema'])


@pytest.mark.parametrize('returncode', [None, 2])
def test_probe_detects_unavailable_or_broken_rar(monkeypatch, capsys, returncode):
    monkeypatch.setattr(science_cgi.shutil, 'which',
                        lambda name: None if returncode is None else '/broken/' + name)

    def broken_tool(*args, **kwargs):
        raise subprocess.CalledProcessError(returncode, args[0])

    monkeypatch.setattr(subprocess, 'run', broken_tool)
    exec(science_cgi.PROBE, {'__name__': '__main__'})
    report = json.loads(capsys.readouterr().out.split('\n\n', 1)[1])
    assert any('rar/unrar' in problem for problem in report['problems'])


@pytest.mark.parametrize('bad_response', [False, True])
def test_http_probe_is_temporary_and_uses_loopback(tmp_path, monkeypatch, probe_report, bad_response):
    (tmp_path / '.unmw-science-test').touch()
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    requests = []

    class Connection:
        def __init__(self, host, port, timeout):
            assert host == '127.0.0.1' and port == 8080 and timeout <= 20

        def request(self, method, path):
            assert method == 'GET'
            script = tmp_path / path.lstrip('/')
            assert script.read_text().startswith('#!/usr/bin/env python3\n')
            assert script.stat().st_mode & 0o777 == 0o755
            assert script.parent.stat().st_mode & 0o777 == 0o700
            requests.append(path)

        def getresponse(self):
            return types.SimpleNamespace(status=200, read=lambda limit: (
                b'not JSON' if bad_response else json.dumps(probe_report).encode()))

        def close(self):
            pass

    monkeypatch.setattr(science_cgi.http.client, 'HTTPConnection', Connection)
    if bad_response:
        with pytest.raises(RuntimeError, match='did not return JSON'):
            science_cgi.check(tmp_path, 8080)
    else:
        assert science_cgi.check(tmp_path, 8080) == probe_report
    assert len(requests) == 1
    assert list(tmp_path.iterdir()) == [tmp_path / '.unmw-science-test']


def test_http_probe_refuses_deployment_directory(tmp_path):
    with pytest.raises(RuntimeError, match='isolated science test'):
        science_cgi.check(tmp_path, 8080)
    assert not list(tmp_path.iterdir())


def test_science_cgi_preflight_precedes_expensive_processing():
    script = (Path(__file__).resolve().parents[1] / 'unmw_selftest.sh').read_text()
    assert script.index('science_cgi.py" configure') < script.index('./autogen.sh')
    assert script.index('science_cgi.py" check') < script.index('git init vast')


def test_science_helper_refuses_cgi_invocation(tmp_path):
    result = subprocess.run([sys.executable, science_cgi.__file__], cwd=tmp_path,
                            env={'PATH': os.environ['PATH'], 'REQUEST_METHOD': 'GET'},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and 'not a CGI' in result.stderr
    assert not list(tmp_path.iterdir())

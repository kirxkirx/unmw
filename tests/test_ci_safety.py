"""Regression checks for the test harness itself (no services launched)."""
import json
import os
from pathlib import Path
import socket
import subprocess

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

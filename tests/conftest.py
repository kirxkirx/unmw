import gzip
import hashlib
import os
from pathlib import Path
import sys
import types

import pytest


FIXTURES = Path(__file__).parent / 'fixtures'
CAMERA_FILES = {
    'Vul8_2025-1-7_15-30-31_001.fts.gz': 'e6713d36d553aa98ceee0072496aa26631a9cfec6a1d57754446139b818e95f2',
    'Vul8_2025-1-7_15-31-1_002.fts.gz': 'ea57625c0389da5a8d84c6ce886f3602310ea5162d0129141766ded95f970cc0',
}


@pytest.fixture
def camera_images(tmp_path):
    destination = tmp_path / 'camera-images'
    destination.mkdir()
    paths = []
    for name, digest in CAMERA_FILES.items():
        data = (FIXTURES / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, 'Camera fixture checksum mismatch'
        path = destination / name[:-3]
        path.write_bytes(gzip.decompress(data))
        assert path.read_bytes().startswith(b'SIMPLE  =')
        paths.append(path)
    return paths


@pytest.fixture
def app_sandbox(tmp_path, monkeypatch):
    """Real on-disk queue/configuration, but no live worker/science launch."""
    import nmw_archive_phot_lib as apl
    import nmw_coord_lib as ncl
    paths = {name: tmp_path / name for name in ('uploads', 'archive', 'reference', 'vast')}
    for path in paths.values():
        path.mkdir()
    config = {
        'IMAGE_ARCHIVE_DIR': str(paths['archive']),
        'REFERENCE_IMAGES': str(paths['reference']),
        'VAST_REFERENCE_COPY': str(paths['vast']),
        'URL_OF_DATA_PROCESSING_ROOT': 'http://127.0.0.1/uploads',
        'ARCHIVE_PHOT_MAX_QUEUE': '10',
        'ARCHIVE_PHOT_MAX_QUEUE_PER_IP': '10',
        'AAVSO_OBSCODE': 'XXX',
    }
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(apl, 'TEMP_PARENT', str(paths['uploads']))
    monkeypatch.setattr(sys, 'excepthook', sys.excepthook)
    monkeypatch.setattr(ncl, 'DEFAULT_FORM_PATH', ncl.DEFAULT_FORM_PATH)
    ids = iter('20260101-0000{:02d}_abcdefgh'.format(n) for n in range(60))
    monkeypatch.setattr(apl, 'new_job_id', lambda: next(ids))
    kicks = []
    monkeypatch.setattr(apl, 'kick_worker', lambda script_dir: kicks.append(script_dir))

    def configure(module, query=''):
        from urllib.parse import urlencode
        monkeypatch.setattr(module, '__file__', str(tmp_path / Path(module.__file__).name))
        monkeypatch.setattr(module, 'read_config_vars',
                            lambda *names: {name: config.get(name, '') for name in names})
        monkeypatch.setenv('REQUEST_METHOD', 'GET')
        monkeypatch.setenv('GATEWAY_INTERFACE', 'CGI/1.1')
        monkeypatch.setenv('SCRIPT_NAME', '/cgi-bin/' + Path(module.__file__).name)
        monkeypatch.setenv('REMOTE_ADDR', '127.0.0.1')
        monkeypatch.setenv('QUERY_STRING', urlencode(query) if isinstance(query, dict) else query)
        return module

    def make_job(state=apl.STATE_QUEUED, **request):
        values = dict(coords_raw='300 20', ra='300', dec='20', ra_deg=300., dec_deg=20.,
                      band_override='', date_from='', date_to='', covering_fields=['Vul8'],
                      images=[], snapshot_paths=[], remote_addr='127.0.0.1',
                      submit_time_utc='2026-01-01 00:00:00 UTC')
        values.update(request)
        jid, directory = apl.create_job(values)
        if state != apl.STATE_QUEUED:
            apl.update_state(directory, state=state, pid=os.getpid(), started_epoch=0)
        return jid, Path(directory)

    return types.SimpleNamespace(root=tmp_path, paths=paths, config=config,
                                 configure=configure, make_job=make_job, kicks=kicks)

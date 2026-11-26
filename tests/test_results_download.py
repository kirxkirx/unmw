"""Downloads of forced-photometry results: ZIP archives, FITS cutouts and the
full-frame download script (forced_phot_download.py and
nmw_results_download_lib.py). The CGI runs for real; only VaST's fitscopy is
a stand-in (a section copy that shifts CRPIX, like cfitsio)."""
import io
import os
import re
import struct
import sys
import types
import urllib.parse
import zipfile

import pytest

import forced_phot_download as download
import nmw_archive_phot_lib as apl
import nmw_coord_lib as ncl
import nmw_results_download_lib as rdl

NAME = 'forced_phot_4242abcdEFGH'
ARCHIVE_NAME = 'archive_phot_20261124-221010_abcdEFGH'
PREFIX = 'http://127.0.0.1/uploads'


# ---------- tiny FITS writer/reader ----------

def _header(cards):
    text = ''.join(card.ljust(80)[:80] for card in cards) + 'END'.ljust(80)
    return (text + ' ' * (-len(text) % 2880)).encode('ascii')


def fits_bytes(nx, ny, extra=(), mef=False):
    """16-bit image whose pixel (x, y) (1-based) holds x + 100 * y."""
    data = b''.join(struct.pack('>h', x + 100 * y)
                    for y in range(1, ny + 1) for x in range(1, nx + 1))
    data += b'\0' * (-len(data) % 2880)
    image = [rdl._value_card('BITPIX', 16), rdl._value_card('NAXIS', 2),
             rdl._value_card('NAXIS1', nx), rdl._value_card('NAXIS2', ny)]
    if not mef:
        return _header([rdl._value_card('SIMPLE', True)] + image + list(extra)) + data
    primary = _header([rdl._value_card('SIMPLE', True), rdl._value_card('BITPIX', 8),
                       rdl._value_card('NAXIS', 0), rdl._value_card('EXTEND', True)])
    extension = _header([rdl._value_card('XTENSION', 'IMAGE')] + image +
                        [rdl._value_card('PCOUNT', 0), rdl._value_card('GCOUNT', 1)] +
                        list(extra))
    return primary + extension + data


WCS_CARDS = [rdl._value_card('CTYPE1', 'RA---TAN-SIP'), rdl._value_card('CRPIX1', 20.5),
             rdl._value_card('CRPIX2', 15.5), rdl._value_card('CRVAL1', 72.3),
             rdl._value_card('IMAGEW', 40), rdl._value_card('IMAGEH', 30),
             rdl._value_card('A_ORDER', 2), rdl._value_card('A_0_2', 1.5e-07),
             ' ' * 80, ' ' * 80]


def read_fits(path):
    with open(path, 'rb') as fh:
        cards, _ = rdl._read_header(fh)
        values = rdl._header_dict(cards)
        nx, ny = values['NAXIS1'], values['NAXIS2']
        raw = fh.read(nx * ny * 2)
    pixels = struct.unpack('>{}h'.format(nx * ny), raw)
    return cards, values, [pixels[i * nx:(i + 1) * nx] for i in range(ny)]


FITSCOPY_STANDIN = r'''#!{python}
"""Stand-in for VaST's util/fitscopy: copy an image section, shift CRPIX."""
import re, sys
sys.path.insert(0, {repo!r})
import nmw_results_download_lib as rdl
spec, out = sys.argv[1], sys.argv[2]
with open({log!r}, 'a') as log:
    log.write(spec + '\n')
m = re.fullmatch(r'(.+?)(\[1\])?\[(\d+):(\d+),(\d+):(\d+)\]', spec)
path = m.group(1)
x1, x2, y1, y2 = (int(v) for v in m.groups()[2:])
with open(path, 'rb') as fh:
    primary, _ = rdl._read_header(fh)
    cards, values = primary, rdl._header_dict(primary)
    extension = values.get('NAXIS') == 0
    if extension:              # the image is extension 1 (as in a .fz file)
        cards, _ = rdl._read_header(fh)
        values = rdl._header_dict(cards)
    nx = values['NAXIS1']
    data = fh.read(nx * values['NAXIS2'] * 2)
rows = b''.join(data[((y - 1) * nx + x1 - 1) * 2:((y - 1) * nx + x2) * 2]
                for y in range(y1, y2 + 1))
new = []
for card in cards:
    key = card[:8].rstrip()
    if key == 'NAXIS1':
        card = rdl._value_card(key, x2 - x1 + 1)
    elif key == 'NAXIS2':
        card = rdl._value_card(key, y2 - y1 + 1)
    elif key in ('CRPIX1', 'CRPIX2'):
        card = rdl._value_card(key, values[key] - ((x1 if key == 'CRPIX1' else y1) - 1))
    new.append(card)
def block(cards):
    text = ''.join(cards) + 'END'.ljust(80)
    return (text + ' ' * (-len(text) % 2880)).encode('ascii')
rows += b'\0' * (-len(rows) % 2880)
with open(out, 'wb') as fh:
    # Like cfitsio: a section of an extension stays an extension behind an
    # empty primary HDU.
    fh.write((block(primary) if extension else b'') + block(new) + rows)
'''


# ---------- a finished run ----------

@pytest.fixture
def run(app_sandbox, monkeypatch):
    """A finished recent-images run of 5 frames: one still in its upload
    directory, one sorted into good_wcs_fd_images/ since the run, one in the
    quarantine tree, one deleted, one without a measurement."""
    root, uploads = app_sandbox.root, app_sandbox.paths['uploads']
    quarantine = root / 'quarantine'
    # Independent of the test machine's free disk (the floor has its own tests).
    app_sandbox.config.update({'IMAGE_QUARANTINE_DIR': str(quarantine),
                               'FORCED_PHOT_CUTOUT_PIXELS': '11',
                               'WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB': '1'})
    monkeypatch.setattr(rdl, 'DISK_MARGIN_BYTES', 0)
    frames = []

    def frame(tree, img_dir, base, x=15.2, y=12.6, nx=40, ny=30, status='detection',
              exists=True, recorded=None):
        path = tree / img_dir / base
        if exists:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fits_bytes(40, 30, WCS_CARDS))
        rec = recorded or path
        fp = None if status == 'skipped' else dict(
            status=status, mag='14.523', err='0.12', x=x, y=y, aperture='5')
        row = None if fp is None else dict(atel='2026-11-09.2289', jd='2461353.7289',
                                           mag='14.52', err='0.12')
        frames.append(rdl.frame_entry(str(rec), base.split('_')[2], '{}/{}'.format(
            PREFIX, os.path.relpath(str(rec), str(uploads))) if str(rec).startswith(
            str(uploads)) else '', fp=fp, row=row, band='V',
            meta={'nx': nx, 'ny': ny} if nx else None))

    img = 'img_2026-11-09_CI_Aur-05-Q2b1x1_000155_TTUQ2b1x1_42_abcdEFGH'
    frame(uploads, img, 'wcs_fd_Aur-05-Q2b1x1_2026-11-09_00-00-46_LIGHT_0001.fits')
    moved = uploads / 'good_wcs_fd_images' / img
    frame(moved.parent, img, 'wcs_fd_Aur-05-Q2b1x1_2026-11-09_00-01-46_LIGHT_0002.fits',
          nx=None, ny=None, status='upperlimit',
          recorded=uploads / img / 'wcs_fd_Aur-05-Q2b1x1_2026-11-09_00-01-46_LIGHT_0002.fits')
    frame(quarantine, img, 'wcs_fd_Aur-05-Q2b1x1_2026-11-09_00-02-46_LIGHT_0003.fits')
    frame(uploads, img, 'wcs_fd_Aur-05-Q2b1x1_2026-11-09_00-03-46_LIGHT_0004.fits',
          exists=False)
    frame(uploads, img, 'wcs_fd_Aur-05-Q2b1x1_2026-11-09_00-04-46_LIGHT_0005.fits',
          status='skipped')

    directory = uploads / NAME
    directory.mkdir()
    own = '{}/{}/'.format(PREFIX, NAME)
    page = ("<html><body><table><tr><td><a href='{own}a_zoomin_hires.png'>"
            "<img src='{own}a_zoomin.png'></a></td><td><a href='{fits}'>FITS</a>"
            "</td></tr></table><img src='{own}lightcurve.png'>"
            "<pre>photometry</pre></body></html>\n").format(
                own=own, fits=frames[1]['fits_url'])
    (directory / 'index.html').write_text(page)
    for name in ('a_zoomin.png', 'a_zoomin_hires.png', 'lightcurve.png'):
        (directory / name).write_bytes(b'\x89PNG fake ' + name.encode())
    (directory / 'lightcurve.dat').write_text('2461353.7289 14.523 0.12\n')
    (directory / 'forced_phot_skipped.log').write_text('frame 5: no measurement\n')
    # Never part of an archive:
    (directory / 'request.json').write_text('{"private": true}')
    (directory / 'notes.txt').write_text('PRIVATE')
    (directory / 'sub').mkdir()
    (directory / 'sub' / 'inner.png').write_bytes(b'PRIVATE')
    secret = root / 'secret.png'
    secret.write_bytes(b'PRIVATE SECRET')
    os.symlink(str(secret), str(directory / 'evil.png'))
    assert rdl.write_manifest(str(directory), rdl.TOOL_RECENT, '04:49:22.53',
                              '+29:26:01.6', '04:49:22.53 +29:26:01.6',
                              {'window_days': 17, 'max_images': 150, 'band': 'auto'},
                              frames, PREFIX)

    vast_util = app_sandbox.paths['vast'] / 'util'
    vast_util.mkdir()
    log = root / 'fitscopy.log'
    standin = vast_util / 'fitscopy'
    standin.write_text(FITSCOPY_STANDIN.format(
        python=sys.executable, repo=str(download.__file__).rsplit(os.sep, 1)[0],
        log=str(log)))
    standin.chmod(0o755)

    locks = root / 'locks'
    locks.mkdir()
    real_slot = ncl.acquire_concurrency_slot
    monkeypatch.setattr(download, 'acquire_concurrency_slot',
                        lambda **kw: real_slot(**dict(kw, lock_dir=str(locks))))

    def call(capsys, query, method='GET'):
        app_sandbox.configure(download, query)
        monkeypatch.setenv('REQUEST_METHOD', method)
        download.main()
        return capsys.readouterr().out

    return types.SimpleNamespace(dir=directory, frames=frames, call=call, img=img,
                                 uploads=uploads, quarantine=quarantine, log=log,
                                 secret=secret, sandbox=app_sandbox)


def _zip_url(name, what):
    return '{}/{}/{}'.format(PREFIX, name, rdl.zip_basename(name, what))


# ---------- validation ----------

@pytest.mark.parametrize('bad', ['', '..', '../uploads', NAME + '/', '/' + NAME,
                                 'forced_phot_12abc', 'vast_forced_phot_4242abcdEFGH',
                                 'archive_phot_bad', 'forced_phot_4242abcdEFGH\n',
                                 'forced_phot_4242abcdEFG%2F'])
def test_only_result_directory_names_are_accepted(run, bad):
    assert rdl.request_dir_path(str(run.uploads), bad) is None


def test_symlinked_or_missing_result_directories_are_refused(run, tmp_path):
    os.symlink(str(run.dir), str(run.uploads / 'forced_phot_1111abcdEFGH'))
    assert rdl.request_dir_path(str(run.uploads), 'forced_phot_1111abcdEFGH') is None
    assert rdl.request_dir_path(str(run.uploads), 'forced_phot_2222abcdEFGH') is None
    assert rdl.request_dir_path(str(run.uploads), NAME) == str(run.dir)


@pytest.mark.parametrize('query,status', [
    ({'request': NAME, 'what': 'tarball'}, 'Status: 400'),
    ({'request': '../' + NAME, 'what': 'zip'}, 'Status: 404'),
    ({'request': 'forced_phot_9999abcdEFGH', 'what': 'zip'}, 'Status: 404'),
])
def test_cgi_rejects_bad_requests(run, capsys, query, status):
    out = run.call(capsys, query)
    assert out.startswith(status) and '<script' not in out
    assert not list(run.dir.glob('*.zip'))


def test_unfinished_run_is_not_offered(run, capsys):
    (run.dir / 'index.html').unlink()
    out = run.call(capsys, {'request': NAME, 'what': 'zip'})
    assert out.startswith('Status: 409')
    assert not list(run.dir.glob('*.zip'))


def test_manifest_of_another_kind_or_name_is_refused(run, capsys):
    manifest = (run.dir / rdl.MANIFEST_BASENAME).read_text()
    (run.dir / rdl.MANIFEST_BASENAME).write_text(
        manifest.replace('"tool": "forced_phot"', '"tool": "archive_phot"'))
    assert run.call(capsys, {'request': NAME, 'what': 'script'}).startswith('Status: 404')
    (run.dir / rdl.MANIFEST_BASENAME).write_text(manifest.replace(NAME, 'forced_phot_1abcdEFGH'))
    assert run.call(capsys, {'request': NAME, 'what': 'script'}).startswith('Status: 404')


def test_archival_job_must_be_done(app_sandbox, run):
    directory = run.uploads / ARCHIVE_NAME
    directory.mkdir()
    (directory / 'index.html').write_text('<html></html>')
    assert rdl.write_manifest(str(directory), rdl.TOOL_ARCHIVE, '1', '2', '1 2', {}, [], PREFIX)
    apl.write_json_atomic(str(directory / 'state.json'), {'state': apl.STATE_RUNNING})
    assert not rdl.run_is_finished(str(directory), ARCHIVE_NAME, apl.read_state)
    apl.write_json_atomic(str(directory / 'state.json'), {'state': apl.STATE_DONE})
    assert rdl.run_is_finished(str(directory), ARCHIVE_NAME, apl.read_state)


# ---------- the results ZIP ----------

def test_results_zip_is_built_once_then_served_statically(run, capsys):
    out = run.call(capsys, {'request': NAME, 'what': 'zip'})
    url = _zip_url(NAME, 'zip')
    assert "url={}'".format(url) in out and 'ERROR' not in out
    path = run.dir / rdl.zip_basename(NAME, 'zip')
    assert oct(path.stat().st_mode & 0o777) == '0o644'
    assert not list(run.dir.glob(rdl.BUILD_TMP_PREFIX + '*'))
    with zipfile.ZipFile(str(path)) as archive:
        names = set(archive.namelist())
        assert names == {NAME + '/' + n for n in (
            'index.html', 'a_zoomin.png', 'a_zoomin_hires.png', 'lightcurve.png',
            'lightcurve.dat', 'forced_phot_skipped.log', 'photometry.csv',
            'photometry.txt', 'download_fits.sh', 'fits_urls.txt', 'README.txt')}
        for info in archive.infolist():
            assert b'PRIVATE' not in archive.read(info)
        page = archive.read(NAME + '/index.html').decode()
        script = archive.read(NAME + '/download_fits.sh')
        assert (archive.getinfo(NAME + '/download_fits.sh').external_attr >> 16) & 0o111
        csv_text = archive.read(NAME + '/photometry.csv').decode()
        readme = archive.read(NAME + '/README.txt').decode()
    # Links into the result directory work from the unpacked archive; the FITS
    # link of the frame sorted away since the run points to where it is now.
    assert "src='a_zoomin.png'" in page and "href='a_zoomin_hires.png'" in page
    assert PREFIX + '/' + NAME not in page
    moved = '{}/good_wcs_fd_images/{}/{}'.format(PREFIX, run.img, run.frames[1]['basename'])
    assert moved in page and run.frames[1]['fits_url'] not in page
    assert script.startswith(b'#!/usr/bin/env bash') and moved.encode() in script
    lines = csv_text.splitlines()
    assert lines[0].split(',') == list(rdl.CSV_COLUMNS) and len(lines) == 6
    assert '14.523' in lines[1] and lines[5].split(',')[4] == 'skipped'
    assert 'no longer on the server' in readme and 'not served by the web server' in readme
    # A second click goes straight to the published file.
    out = run.call(capsys, {'request': NAME, 'what': 'zip'})
    assert out.startswith('Status: 302') and 'Location: ' + url in out


def test_fits_script_lists_current_urls_and_quotes_them(run, capsys):
    out = run.call(capsys, {'request': NAME, 'what': 'script'})
    head, body = out.split('\n\n', 1)
    assert 'Content-Type: text/x-shellscript' in head
    assert 'attachment; filename="download_fits_{}.sh"'.format(NAME) in head
    fetches = re.findall(r"^fetch '([^']+)' \|\| failed", body, re.MULTILINE)
    sorted_url = '{}/good_wcs_fd_images/{}/{}'.format(PREFIX, run.img, run.frames[1]['basename'])
    assert fetches == ['{}/{}/{}'.format(PREFIX, run.img, run.frames[0]['basename']),
                       sorted_url,
                       '{}/{}/{}'.format(PREFIX, run.img, run.frames[4]['basename'])]
    assert '#   ' + run.frames[2]['basename'] + ' (not web-served)' in body
    assert '#   ' + run.frames[3]['basename'] + ' (no longer on the server)' in body
    assert not list(run.dir.glob('*.zip'))


def test_script_never_emits_a_url_that_could_break_quoting():
    manifest = {'name': NAME, 'ra': "1'; rm -rf ~ #", 'dec': '2\n3', 'frames': []}
    resolved = [({'basename': "a'b.fits"}, '/x', "http://h/a'b.fits"),
                ({'basename': 'c.fits'}, '/x', 'http://h/c d.fits'),
                ({'basename': 'e.fits'}, '/x', 'http://h/e.fits')]
    script = rdl.fits_download_script(manifest, resolved)
    assert script.count("\nfetch '") == 1 and "fetch 'http://h/e.fits'" in script
    assert "rm -rf ~ #" not in script and '\n3' not in script
    assert all(line.startswith('#') or "'" not in line or line.startswith("fetch 'http://h/e")
               for line in script.splitlines())


# ---------- the cutout ZIP ----------

def test_cutout_zip_has_wcs_preserving_cutouts_and_notes(run, capsys):
    out = run.call(capsys, {'request': NAME, 'what': 'zip_cutouts'})
    assert "url={}'".format(_zip_url(NAME, 'zip_cutouts')) in out
    specs = run.log.read_text().split()
    # 11 x 11 cutouts centred on the measured pixel (15.2, 12.6) -> (15, 13)
    assert specs == [str(run.uploads / run.img / run.frames[0]['basename']) + '[10:20,8:18]',
                     str(run.uploads / 'good_wcs_fd_images' / run.img /
                         run.frames[1]['basename']) + '[10:20,8:18]',
                     str(run.quarantine / run.img / run.frames[2]['basename']) + '[10:20,8:18]']
    with zipfile.ZipFile(str(run.dir / rdl.zip_basename(NAME, 'zip_cutouts'))) as archive:
        cutouts = sorted(n for n in archive.namelist() if '/cutouts/' in n)
        assert len(cutouts) == 3
        data = archive.read(cutouts[0])
        readme = archive.read(NAME + '/README.txt').decode()
    path = run.sandbox.root / 'cutout.fits'
    path.write_bytes(data)
    cards, values, pixels = read_fits(str(path))
    assert (values['NAXIS1'], values['NAXIS2']) == (11, 11)
    assert (values['CRPIX1'], values['CRPIX2']) == (20.5 - 9, 15.5 - 7)
    assert (values['IMAGEW'], values['IMAGEH'], values['LTV1'], values['LTV2']) == (11, 11, -9, -7)
    assert values['A_0_2'] == 1.5e-07 and values['CTYPE1'] == 'RA---TAN-SIP'
    assert pixels[0][0] == 10 + 100 * 8 and pixels[10][10] == 20 + 100 * 18
    history = ''.join(c[8:].strip() for c in cards if c.startswith('HISTORY'))
    assert 'Cutout [10:20,8:18] of ' + run.frames[0]['basename'] in history
    assert 'Cutouts: 3 made.' in readme
    assert run.frames[3]['basename'] + ': no cutout (no longer on the server)' in readme


def test_cutout_of_compressed_frame_reads_its_extension(run, tmp_path):
    frame = tmp_path / 'wcs_fd_X_2026-11-09_LIGHT_0001.fits.fz'
    # A stand-in "tile-compressed" file: the image as extension 1.
    frame.write_bytes(fits_bytes(40, 30, WCS_CARDS, mef=True))
    out = tmp_path / 'out.fits'
    fitscopy = str(run.sandbox.paths['vast'] / 'util' / 'fitscopy')
    measured = {'basename': frame.name, 'x': 3, 'y': 29, 'nx': 40, 'ny': 30}
    problem = rdl.make_cutout(fitscopy, str(frame), measured, 11, str(out), 'note')
    assert problem is None
    assert run.log.read_text().split() == [str(frame) + '[1][1:8,24:30]']
    cards, values, pixels = read_fits(str(out))
    assert cards[0].startswith('SIMPLE  =') and 'XTENSION' not in values
    assert (values['NAXIS1'], values['NAXIS2'], values['LTV1'], values['LTV2']) == (8, 7, 0, -23)
    assert pixels[0][0] == 1 + 100 * 24


def test_fitscopy_output_with_empty_primary_becomes_one_image(tmp_path):
    path = tmp_path / 'mef.fits'
    path.write_bytes(fits_bytes(5, 4, [rdl._value_card('CRPIX1', 3.0)], mef=True))
    rdl.finish_cutout(str(path), 11, 15, 21, 24, 'frame.fits', 'made by a test')
    cards, values, pixels = read_fits(str(path))
    assert cards[0].startswith('SIMPLE  =') and 'XTENSION' not in values
    assert 'PCOUNT' not in values and 'GCOUNT' not in values
    assert (values['LTV1'], values['LTV2'], values['CRPIX1']) == (-10, -20, 3.0)
    assert pixels[3][4] == 5 + 100 * 4
    assert path.stat().st_size % 2880 == 0


@pytest.mark.parametrize('x,y,expected', [
    (15.2, 12.6, (10, 20, 8, 18)), (1, 1, (1, 6, 1, 6)), (40, 30, (35, 40, 25, 30)),
    (0.4, 10, None), (41, 10, None)])
def test_cutout_section_is_centred_and_clipped(x, y, expected):
    assert rdl.cutout_section(x, y, 40, 30, 11) == expected


@pytest.mark.parametrize('raw,expected', [('', 501), ('301', 301), ('11', 11), ('10', 501),
                                          ('4002', 501), ('abc', 501), (' 1001 ', 1001)])
def test_cutout_size_setting(raw, expected):
    assert rdl.cutout_pixels_from_config(raw) == expected


def test_unsafe_frame_names_never_reach_fitscopy(tmp_path):
    frame = {'basename': 'a.fits', 'x': 5, 'y': 5, 'nx': 40, 'ny': 30}
    for path in ('/data/a[1:2,3:4].fits', '/data/a(b).fits', '/data/a b.fits',
                 'relative/a.fits', '/data/a.fits.gz', '/data/a+1.fits'):
        assert rdl.make_cutout('/bin/false', path, frame, 11, str(tmp_path / 'o.fits'),
                               'n') == 'file name not supported for cutouts'


def test_image_size_of_tile_compressed_header(tmp_path):
    path = tmp_path / 'x.fits.fz'
    primary = _header([rdl._value_card('SIMPLE', True), rdl._value_card('BITPIX', 8),
                       rdl._value_card('NAXIS', 0)])
    table = _header([rdl._value_card('XTENSION', 'BINTABLE'), rdl._value_card('BITPIX', 8),
                     rdl._value_card('NAXIS', 2), rdl._value_card('NAXIS1', 8),
                     rdl._value_card('NAXIS2', 3), rdl._value_card('PCOUNT', 100),
                     rdl._value_card('GCOUNT', 1), rdl._value_card('ZIMAGE', True),
                     rdl._value_card('ZNAXIS', 2), rdl._value_card('ZNAXIS1', 9576),
                     rdl._value_card('ZNAXIS2', 6388)])
    path.write_bytes(primary + table + b'\0' * 2880)
    assert rdl.image_size(str(path)) == (9576, 6388)
    assert rdl.image_size(str(tmp_path / 'missing.fits')) is None


# ---------- refusals while building ----------

def test_head_request_does_not_build(run, capsys):
    out = run.call(capsys, {'request': NAME, 'what': 'zip'}, method='HEAD')
    assert out.startswith('Content-Type: text/html') and not list(run.dir.glob('*.zip'))


def test_busy_server_and_busy_archive(run, capsys, monkeypatch):
    monkeypatch.setattr(download, 'acquire_concurrency_slot', lambda **kw: None)
    out = run.call(capsys, {'request': NAME, 'what': 'zip'})
    assert out.startswith('Status: 503') and 'what=zip' in out


def test_archive_already_being_prepared(run, capsys):
    import fcntl
    with open(str(run.dir / rdl.BUILD_LOCK_BASENAME), 'w') as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        out = run.call(capsys, {'request': NAME, 'what': 'zip'})
    assert out.startswith('Status: 503') and 'being prepared' in out
    assert not list(run.dir.glob('*.zip'))


def test_not_enough_disk_space(run, capsys, monkeypatch):
    monkeypatch.setattr(download.shutil, 'disk_usage',
                        lambda path: types.SimpleNamespace(free=10))
    out = run.call(capsys, {'request': NAME, 'what': 'zip_cutouts'})
    assert out.startswith('Status: 507') and not list(run.dir.glob('*.zip'))


@pytest.mark.parametrize('hardlimit_kb,builds', [('', False), ('1', True)])
def test_builds_never_take_the_disk_below_the_pipeline_floor(run, capsys, monkeypatch,
                                                             hardlimit_kb, builds):
    """autoprocess.sh stops below WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB (default
    5 GB): an archive must leave that much free."""
    run.sandbox.config['WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB'] = hardlimit_kb
    estimate = rdl.estimated_bytes(str(run.dir), rdl.read_manifest(str(run.dir)), 'zip', 11)
    monkeypatch.setattr(download.shutil, 'disk_usage', lambda path: types.SimpleNamespace(
        free=estimate + 1024 ** 3))
    out = run.call(capsys, {'request': NAME, 'what': 'zip'})
    assert out.startswith('Status: 507') != builds
    assert (run.dir / rdl.zip_basename(NAME, 'zip')).exists() == builds


@pytest.mark.parametrize('raw,expected', [('', 5242880), ('abc', 5242880), ('0', 5242880),
                                          ('-5', 5242880), ('1024', 1024)])
def test_required_free_space_follows_the_pipeline_setting(raw, expected):
    assert rdl.required_free_bytes(100, raw) == 100 + expected * 1024 + rdl.DISK_MARGIN_BYTES


def test_stale_work_directories_of_killed_builds_are_removed(run, capsys):
    stale = run.dir / (rdl.BUILD_TMP_PREFIX + 'killed')
    (stale / 'cutouts').mkdir(parents=True)
    (stale / 'archive.zip').write_bytes(b'partial')
    run.call(capsys, {'request': NAME, 'what': 'zip'})
    assert not stale.exists() and (run.dir / rdl.zip_basename(NAME, 'zip')).exists()


def test_browser_going_away_does_not_stop_the_build(run, monkeypatch, capsys):
    class Gone(io.StringIO):
        def write(self, text):
            if self.getvalue().count('\n') > 3:
                raise BrokenPipeError(32, 'Broken pipe')
            return io.StringIO.write(self, text)
    run.sandbox.configure(download, {'request': NAME, 'what': 'zip_cutouts'})
    monkeypatch.setattr(sys, 'stdout', Gone())
    download.main()
    assert (run.dir / rdl.zip_basename(NAME, 'zip_cutouts')).exists()


def test_a_build_that_overruns_its_deadline_publishes_nothing(run, capsys, monkeypatch):
    monkeypatch.setattr(rdl, 'BUILD_DEADLINE_SECONDS', -1)
    out = run.call(capsys, {'request': NAME, 'what': 'zip_cutouts'})
    assert 'ERROR: the archive could not be made (TimeoutError)' in out
    assert not list(run.dir.glob('*.zip')) and not list(run.dir.glob(rdl.BUILD_TMP_PREFIX + '*'))


def test_keepalive_lines_keep_a_long_build_talking(run, capsys, monkeypatch):
    monkeypatch.setattr(rdl, 'KEEPALIVE_SECONDS', 0)
    out = run.call(capsys, {'request': NAME, 'what': 'zip'})
    assert out.count('<!-- ' + ' ' * 1500) >= 5


def test_duplicate_frame_names_get_their_own_cutouts(run, capsys):
    manifest = rdl.read_manifest(str(run.dir))
    twin = dict(manifest['frames'][0])
    twin['path'] = str(run.uploads / 'img_2026-11-09_CI_Aur-05-Q2b1x1_000199_TTUQ2b1x1_43_abcdEFGH'
                       / twin['basename'])
    os.makedirs(os.path.dirname(twin['path']))
    with open(twin['path'], 'wb') as fh:
        fh.write(fits_bytes(40, 30, WCS_CARDS))
    assert rdl.write_manifest(str(run.dir), rdl.TOOL_RECENT, '1', '2', '1 2', {},
                              manifest['frames'] + [twin], PREFIX)
    run.call(capsys, {'request': NAME, 'what': 'zip_cutouts'})
    with zipfile.ZipFile(str(run.dir / rdl.zip_basename(NAME, 'zip_cutouts'))) as archive:
        names = [n.rsplit('/', 1)[1] for n in archive.namelist() if '/cutouts/' in n]
    stem = rdl.cutout_basename(twin['basename'], 11)[:-len('.fits')]
    assert stem + '.fits' in names and stem + '_2.fits' in names and len(names) == 4


def test_frame_containment_rules(run, tmp_path):
    # Upload trees: a symlink planted there must not lead out of them.
    planted = run.uploads / run.img / 'wcs_fd_Aur-05-Q2b1x1_2026-11-09_00-05-46_LIGHT_0006.fits'
    os.symlink(str(run.secret), str(planted))
    frame = {'path': str(planted), 'basename': planted.name}
    assert rdl.locate_frame(frame, rdl.TOOL_RECENT, str(run.uploads), '', '') is None
    # The archive is the operator's: a month folder may be a symlink to another disk.
    archive = run.sandbox.paths['archive']
    other_disk = tmp_path / 'other-disk'
    other_disk.mkdir()
    (other_disk / 'wcs_fd_X_LIGHT_0001.fits').write_bytes(fits_bytes(4, 3))
    os.symlink(str(other_disk), str(archive / '2026-11'))
    frame = {'path': str(archive / '2026-11' / 'wcs_fd_X_LIGHT_0001.fits'),
             'basename': 'wcs_fd_X_LIGHT_0001.fits'}
    found = rdl.locate_frame(frame, rdl.TOOL_ARCHIVE, '', '', str(archive))
    assert found == frame['path']
    assert rdl.current_fits_url(found, rdl.TOOL_ARCHIVE, '', '', str(archive),
                                'http://h/archive') == \
        'http://h/archive/2026-11/wcs_fd_X_LIGHT_0001.fits'
    assert rdl.current_fits_url(found, rdl.TOOL_ARCHIVE, '', '', str(archive), '') == ''
    outside = {'path': str(tmp_path / 'elsewhere.fits'), 'basename': 'elsewhere.fits'}
    (tmp_path / 'elsewhere.fits').write_bytes(b'x')
    assert rdl.locate_frame(outside, rdl.TOOL_ARCHIVE, '', '', str(archive)) is None


def test_download_tracks_frames_between_linked_archives(run, tmp_path, capsys):
    base = run.frames[1]['basename']
    disk = tmp_path / 'off_disk'
    sorted_dir = run.uploads / 'good_wcs_fd_images'
    sorted_dir.rename(disk)
    sorted_dir.symlink_to(disk, target_is_directory=True)
    old_path = sorted_dir / run.img / base

    output = run.call(capsys, {'request': NAME, 'what': 'script'})
    assert '{}/good_wcs_fd_images/{}/{}'.format(PREFIX, run.img, base) in output

    quality = run.quarantine / 'frame_quality'
    approved = quality / 'rejected' / 'moon' / 'good'
    approved.parent.mkdir(parents=True)
    approved.symlink_to(tmp_path / 'approved_disk', target_is_directory=True)
    approved.resolve().mkdir()
    moved = old_path.rename(approved / base)
    frame = run.frames[1]
    assert rdl.locate_frame(frame, rdl.TOOL_RECENT, str(run.uploads),
                            str(run.quarantine), '') == str(moved)
    assert rdl.current_fits_url(str(moved), rdl.TOOL_RECENT, str(run.uploads),
                                PREFIX, '', '') == ''

    (quality / 'measurements.txt').write_text('img_dir={} frame={}\n'.format(run.img, base))
    returned = run.quarantine / 'good_wcs_fd_images' / run.img / base
    returned.parent.mkdir(parents=True)
    moved.rename(returned)
    former_manifest = {'tool': rdl.TOOL_RECENT,
                       'frames': [{'path': str(moved), 'basename': base}]}
    found = rdl.resolve_frames(former_manifest, {'workdir': str(run.uploads),
                                                'quarantine_dir': str(run.quarantine)})
    assert found[0][1:] == (str(returned), '')


def test_missing_fitscopy_is_a_configuration_error(run, capsys):
    (run.sandbox.paths['vast'] / 'util' / 'fitscopy').unlink()
    out = run.call(capsys, {'request': NAME, 'what': 'zip_cutouts'})
    assert out.startswith('Status: 500') and not list(run.dir.glob('*.zip'))


def test_failed_build_publishes_nothing(run, capsys, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(download.rdl, 'build_archive', broken)
    out = run.call(capsys, {'request': NAME, 'what': 'zip'})
    assert 'ERROR: the archive could not be made (OSError)' in out
    assert 'disk full' not in out and not list(run.dir.glob('*.zip'))


# ---------- the page copy ----------

def test_streamed_page_copy_is_published_only_when_finished(tmp_path):
    stream = io.StringIO()
    path = tmp_path / 'index.html'
    copy = rdl.StreamedPageCopy(stream, str(path))
    copy.write('<p>row</p>' + '<!-- ' + ' ' * 1500 + ' -->\n')
    copy.flush()
    assert not path.exists() and stream.getvalue().count(' ') > 1500
    assert copy.finish() and path.read_text() == '<p>row</p>'
    copy.abandon()
    assert path.exists()
    unfinished = rdl.StreamedPageCopy(io.StringIO(), str(tmp_path / 'other.html'))
    unfinished.write('partial')
    unfinished.abandon()
    assert not list(tmp_path.glob('other.html*'))


def test_download_links_point_to_the_cgi(run):
    html_text = rdl.download_links_html('/cgi-bin/unmw', NAME)
    hrefs = re.findall(r"href='([^']+)'", html_text)
    assert [urllib.parse.parse_qs(urllib.parse.urlsplit(h.replace('&amp;', '&')).query)['what'][0]
            for h in hrefs] == ['zip', 'zip_cutouts', 'script']
    assert all(h.startswith('/cgi-bin/unmw/forced_phot_download.py?request=' + NAME)
               for h in hrefs)
    assert html_text.count("rel='nofollow'") == 3

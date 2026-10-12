"""Configure and probe only the scientific self-test's disposable CGI server."""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

if os.environ.get('GATEWAY_INTERFACE') or os.environ.get('REQUEST_METHOD'):
    sys.exit('ERROR: not a CGI')


# Exercise the same env-python3 shebang as upload.py, not an absolute interpreter
# that would hide a broken CGI PATH. rarfile is deliberately NOT a requirement.
PROBE = '''#!/usr/bin/env python3
import importlib
import json
import shutil
import subprocess
import sys
import warnings

warnings.filterwarnings('ignore', category=DeprecationWarning)
problems = []
for module in ('cgi', 'cgitb', 'bs4', 'lxml', 'jsonschema'):
    try:
        importlib.import_module(module)
    except ImportError:
        problems.append('Cannot import ' + module)
rar = shutil.which('rar') or shutil.which('unrar')
if rar is None:
    problems.append('No rar/unrar executable on CGI PATH')
else:
    try:
        subprocess.run([rar, '-?'], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append('Cannot run rar/unrar: ' + str(exc))
print('Content-Type: application/json\\n')
print(json.dumps({'executable': sys.executable, 'prefix': sys.prefix,
                  'version': list(sys.version_info[:3]), 'rar': rar,
                  'problems': problems}))
'''


def configure(header):
    """sthttpd replaces PATH rather than inheriting the server's environment."""
    cgi_path = str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']
    # The pinned upstream header has Latin-1 copyright bytes, not UTF-8.
    # Preserve all unrelated bytes and emit an ASCII-escaped C string.
    original = header.read_bytes()
    # Use a function replacement: re.sub must not reinterpret C backslashes.
    definition = b'#define CGI_PATH ' + json.dumps(cgi_path).encode('ascii')
    updated, count = re.subn(rb'^#define CGI_PATH [^\r\n]*',
                             lambda match: definition, original, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError('Expected exactly one CGI_PATH definition in ' + str(header))
    header.write_bytes(updated)


def validate_probe(report):
    problems = list(report['problems'])
    expected_python = (sys.prefix, list(sys.version_info[:3]), Path(sys.executable).parent)
    actual_python = (report['prefix'], report['version'], Path(report['executable']).parent)
    if actual_python != expected_python:
        problems.append('CGI is not using the selected test Python: ' + report['executable'])
    expected_rar = shutil.which('rar') or shutil.which('unrar')
    if not expected_rar or report['rar'] != expected_rar:
        problems.append('CGI is not using the selected test rar/unrar: ' + str(report['rar']))
    if problems:
        raise RuntimeError('CGI dependency preflight failed: ' + '; '.join(problems))


def check(root, port, timeout=20):
    if os.geteuid() == 0 or not (root / '.unmw-science-test').is_file():
        raise RuntimeError('CGI preflight requires an unprivileged, isolated science test')
    # Nothing exposing environment information is left behind after the check.
    with tempfile.TemporaryDirectory(prefix='cgi-preflight-', dir=root) as temporary:
        probe = Path(temporary) / 'probe.py'
        probe.write_text(PROBE)
        # sthttpd recognizes CGIs only with the other-execute bit set. The
        # containing temporary directory remains private (0700).
        probe.chmod(0o755)
        deadline = time.monotonic() + timeout
        while True:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
            try:
                connection.request('GET', '/' + probe.relative_to(root).as_posix())
                response = connection.getresponse()
                body = response.read(65537)
                if response.status != 200 or len(body) > 65536:
                    raise RuntimeError('Invalid CGI preflight response: HTTP ' + str(response.status))
                try:
                    report = json.loads(body)
                except ValueError as exc:
                    raise RuntimeError('CGI preflight did not return JSON: ' + repr(body[:500])) from exc
                validate_probe(report)
                print('CGI preflight passed: Python {} ({}); RAR {}'.format(
                    '.'.join(map(str, report['version'])), report['executable'], report['rar']))
                return report
            except ConnectionRefusedError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('CGI test server did not start before the preflight deadline')
                time.sleep(.1)
            finally:
                connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    configure_parser = commands.add_parser('configure')
    configure_parser.add_argument('header', type=Path)
    check_parser = commands.add_parser('check')
    check_parser.add_argument('root', type=Path)
    check_parser.add_argument('port', type=int)
    args = parser.parse_args()
    if args.command == 'configure':
        configure(args.header)
    else:
        check(args.root, args.port)


if __name__ == '__main__':
    main()

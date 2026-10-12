#!/usr/bin/env python3
"""HTTP-test processing stand-in: native extraction only, no science/submission.

The real upload CGI and wrapper invoke this only in their private test copy.
There is deliberately no import of production configuration or monitoring.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

if os.environ.get('GATEWAY_INTERFACE') or os.environ.get('REQUEST_METHOD'):
    sys.exit('ERROR: not a CGI')


def main():
    archive = Path(sys.argv[1]).resolve()
    root = Path(os.environ['UNMW_HTTP_TEST_ROOT']).resolve()
    if not (root / '.unmw-http-test').is_file() or root not in archive.parents:
        raise SystemExit('Refusing to run outside an explicitly marked HTTP test sandbox')
    destination = archive.parent / 'checked'
    destination.mkdir()
    if archive.suffix == '.rar':
        command = ['rar', 'e', '-cfg-', '-c-', '-p-', '-idq', '--', str(archive), str(destination) + '/']
    else:
        command = ['unzip', '-q', '-n', '-j', str(archive), '-d', str(destination)]
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL, timeout=30)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in destination.iterdir() if p.is_file()}
    (destination / 'result.json').write_text(json.dumps(hashes, sort_keys=True))
    (destination / 'index.html').write_text('<html><body>Isolated extraction finished.</body></html>')
    url = os.environ['UNMW_TEST_BASE_URL'] + '/uploads/' + archive.parent.name + '/checked/result.json'
    (archive.parent / 'results_url.txt').write_text(url + '\n')


if __name__ == '__main__':
    main()

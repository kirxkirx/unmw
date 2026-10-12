"""Fail CI if subprocess/branch instrumentation silently stopped working."""
import os
from pathlib import Path
import sys

if os.environ.get('GATEWAY_INTERFACE') or os.environ.get('REQUEST_METHOD'):
    sys.exit('ERROR: not a CGI')

import coverage

cov = coverage.Coverage()
cov.load()
data = cov.get_data()
assert data.has_arcs(), 'Branch coverage was not collected'
repo = Path(__file__).resolve().parents[1]
upload = repo / 'upload.py3'
lines = set(data.lines(str(upload)) or [])
# This statement executes only in main(), not when the unit tests import it.
main_lines = upload.read_text().splitlines()
entry = max(i for i, line in enumerate(main_lines, 1) if line.strip() == 'main()')
assert entry in lines, 'The real CGI subprocess entry point was not measured'
assert data.lines(str(repo / 'archive_phot_worker.py')), 'Worker coverage was not collected'

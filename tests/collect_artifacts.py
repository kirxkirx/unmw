"""Copy test reports/logs, not archives, image trees, or test credentials."""
import os
from pathlib import Path
import shutil
import sys

if os.environ.get('GATEWAY_INTERFACE') or os.environ.get('REQUEST_METHOD'):
    sys.exit('ERROR: not a CGI')


def collect(source, destination):
    patterns = ('unit/*.xml', 'unit/*.txt', 'unit/*.log', 'unit/coverage-html/*',
                'unit/pytest/**/*.log', 'science/selftest.log',
                'science/work/uploads/*.txt', 'science/work/uploads/*.log',
                'science/work/uploads/*.sha256', 'science/work/uploads/*.html',
                'science/work/uploads/*/*.log')
    for pattern in patterns:
        for path in source.glob(pattern):
            if path.is_symlink() or not path.is_file():
                continue
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


if __name__ == '__main__':
    collect(Path(sys.argv[1]), Path(sys.argv[2]))

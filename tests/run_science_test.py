"""Run the slow, network-dependent VaST self-test in a fresh disposable copy.

Only the test configuration is copied. No monitoring registry, observer
credentials, notification hooks, production data, or submission robot is used.
Unlike the offline pytest job, this suite needs remote astronomical services.
"""
import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

if os.environ.get('GATEWAY_INTERFACE') or os.environ.get('REQUEST_METHOD'):
    sys.exit('ERROR: not a CGI')


def clean_environment():
    return {'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
            'AAVSO_OBSCODE': 'XXX', 'PYTHONDONTWRITEBYTECODE': '1'}


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifacts', type=Path, help='new directory outside the source checkout')
    parser.add_argument('--timeout', type=int, default=3600)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('run as an unprivileged test user')
    source = Path(__file__).resolve().parents[1]
    root = args.artifacts.resolve()
    if root == source or source in root.parents:
        parser.error('artifacts must be outside the source checkout')
    root.mkdir(mode=0o700, parents=False)
    work = root / 'work'
    work.mkdir()
    # Copy versioned application files, not arbitrary untracked deployment
    # files. Include the new test helpers before they have been git-staged.
    tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=source, check=True,
                             capture_output=True).stdout.split(b'\0')
    paths = {Path(os.fsdecode(name)) for name in tracked if name}
    paths.update(path.relative_to(source) for path in (source / 'tests').rglob('*') if path.is_file())
    paths.add(Path('report_helpers.sh'))
    excluded = {'local_config.sh', 'upload.py', 'uploads', 'monitoring', '.git', '__pycache__'}
    for relative in sorted(paths):
        if any(part in excluded or part.startswith('.env') for part in relative.parts):
            continue
        origin = source / relative
        if origin.is_symlink() or not origin.is_file():
            continue
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)
    (work / '.unmw-science-test').write_text('isolated self-test\n')
    env = clean_environment()
    env['UNMW_SELFTEST_ROOT'] = str(work)
    signal.signal(signal.SIGTERM, interrupted)
    with (root / 'selftest.log').open('w') as log:
        process = subprocess.Popen(['bash', 'unmw_selftest.sh'], cwd=work, env=env,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            result = process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print('Scientific self-test exceeded its {}s deadline'.format(args.timeout), file=sys.stderr)
            result = 124
        except KeyboardInterrupt:
            print('Scientific self-test interrupted; stopping its own children', file=sys.stderr)
            result = 130
        finally:
            # This process group was created by this invocation, not discovered
            # by matching global process names. Stop any remaining test children.
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    print('Scientific test exit status: {}; logs/results: {}'.format(result, root))
    return result


if __name__ == '__main__':
    sys.exit(main())

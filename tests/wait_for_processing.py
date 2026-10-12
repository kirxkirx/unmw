"""Wait only for autoprocess instances whose cwd is in THIS test tree."""
import os
from pathlib import Path
import sys
import time

if os.environ.get('GATEWAY_INTERFACE') or os.environ.get('REQUEST_METHOD'):
    sys.exit('ERROR: not a CGI')


def processing_pids(root, proc_root=Path('/proc')):
    root = Path(root).resolve()
    found = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = (entry / 'cwd').resolve(strict=True)
            if cwd != root and root not in cwd.parents:
                continue
            args = (entry / 'cmdline').read_bytes().split(b'\0')
            if any(Path(os.fsdecode(arg)).name == 'autoprocess.sh' for arg in args if arg):
                found.append(int(entry.name))
        except (OSError, RuntimeError):
            continue  # process exited, or belongs to another user
    return found


def wait(root, timeout=1200):
    deadline = time.monotonic() + timeout
    while True:
        pids = processing_pids(root)
        if not pids:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError('Test processing exceeded {}s; owned PIDs: {}'.format(timeout, pids))
        time.sleep(.5)


if __name__ == '__main__':
    wait(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 1200)

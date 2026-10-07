#!/usr/bin/env python3
"""
btrfs-aware free space check for the unmw upload and processing paths.

Why this exists: df and statvfs report the free space INSIDE the data block
groups of a btrfs filesystem. When every byte of the device has already been
handed out to block groups and the metadata block groups are nearly full,
btrfs refuses to create, rename or write files (ENOSPC) while df still shows
hundreds of GB free. The classic threshold checks in upload.py3 and
autoprocess.sh cannot see that coming; this module can, from the counters the
kernel publishes under /sys/fs/btrfs, which are world-readable - so it works
as the web server user without root and without the btrfs command.

Rules:
  * the directory is not on btrfs, or anything needed cannot be read: the
    check PASSES ('OK') - it only ever adds information, it never blocks a
    deployment on another filesystem or an older kernel;
  * unallocated device space >= BTRFS_MIN_UNALLOCATED_BYTES: 'OK' (btrfs can
    still allocate new metadata block groups whenever it needs them);
  * otherwise, metadata headroom (space left in the metadata block groups
    minus the global reserve) < BTRFS_MIN_METADATA_HEADROOM_BYTES: 'ERROR'
    (writes are about to fail), else 'WARNING' (metadata cannot grow - run a
    filtered balance before it fills up).

The messages deliberately contain the phrases 'low on disk space' and
'out of disk space' used by the classic checks, so combine_reports.sh
surfaces them on the processing summary page in the same way.

Command line use (prints the message, exit status 0 = OK, 1 = WARNING,
2 = ERROR):
    python3 nmw_fs_check.py uploads/

Test hook: the sysfs root can be overridden with the UNMW_BTRFS_SYSFS_ROOT
environment variable (autoprocess.sh honours the same variable).
"""

import os
import socket
import sys

# A DUP metadata block group needs up to 2 x 1 GiB of unallocated space.
BTRFS_MIN_UNALLOCATED_BYTES = 2 * 1024 * 1024 * 1024
# Below this much reservable metadata space the next busy night fails.
BTRFS_MIN_METADATA_HEADROOM_BYTES = 1024 * 1024 * 1024

DEFAULT_SYSFS_ROOT = '/sys/fs/btrfs'
DEFAULT_MOUNTINFO = '/proc/self/mountinfo'
SECTOR_BYTES = 512  # /sys/class/block/<dev>/size is in 512-byte sectors

_RANK = {'OK': 0, 'WARNING': 1, 'ERROR': 2}


def _unescape_mountinfo(field):
    """mountinfo escapes space, tab, newline and backslash as \\040 etc."""
    out = []
    i = 0
    while i < len(field):
        if field[i] == '\\' and len(field) >= i + 4 and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return ''.join(out)


def find_mount(path, mountinfo_path=DEFAULT_MOUNTINFO):
    """Return (mount_point, fstype, source) of the mount holding path, or
    None. The longest mount point that is a prefix of the resolved path
    wins; among equals the later line wins (later mounts shadow earlier
    ones). Pure text processing over /proc/self/mountinfo, no root needed."""
    real = os.path.realpath(path)
    best = None
    try:
        with open(mountinfo_path) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for line in lines:
        if ' - ' not in line:
            continue
        left, _, right = line.partition(' - ')
        left_fields = left.split(' ')
        right_fields = right.split(' ')
        if len(left_fields) < 5 or len(right_fields) < 2:
            continue
        mount_point = _unescape_mountinfo(left_fields[4])
        fstype = right_fields[0]
        source = _unescape_mountinfo(right_fields[1])
        if mount_point == '/':
            covers = True
        else:
            covers = real == mount_point or real.startswith(mount_point + '/')
        if not covers:
            continue
        if best is None or len(mount_point) >= len(best[0]):
            best = (mount_point, fstype, source)
    return best


def btrfs_sysfs_dir(source, sysfs_root=DEFAULT_SYSFS_ROOT):
    """The /sys/fs/btrfs/<uuid> directory of the filesystem whose member
    device is `source` (a /dev path as listed in mountinfo), or None."""
    try:
        dev_name = os.path.basename(os.path.realpath(source))
        candidates = os.listdir(sysfs_root)
    except OSError:
        return None
    if not dev_name:
        return None
    for uuid in candidates:
        fs_dir = os.path.join(sysfs_root, uuid)
        if os.path.isdir(os.path.join(fs_dir, 'devices', dev_name)):
            return fs_dir
    return None


def _read_int(path):
    with open(path) as fh:
        return int(fh.read().strip())


def btrfs_space_figures(fs_dir):
    """Space accounting of one btrfs filesystem from its sysfs directory,
    in bytes: device_size, allocated, unallocated, metadata_total,
    metadata_used, global_rsv, metadata_headroom. Raises OSError/ValueError
    when a counter is missing (older kernels) - callers treat that as
    'cannot tell'."""
    device_size = 0
    devices_dir = os.path.join(fs_dir, 'devices')
    for dev in os.listdir(devices_dir):
        device_size += _read_int(os.path.join(devices_dir, dev, 'size')) \
            * SECTOR_BYTES
    if device_size <= 0:
        raise ValueError('no member devices listed in {}'.format(devices_dir))
    allocated = 0
    for kind in ('data', 'metadata', 'system'):
        allocated += _read_int(
            os.path.join(fs_dir, 'allocation', kind, 'disk_total'))
    metadata_total = _read_int(
        os.path.join(fs_dir, 'allocation', 'metadata', 'total_bytes'))
    metadata_used = _read_int(
        os.path.join(fs_dir, 'allocation', 'metadata', 'bytes_used'))
    global_rsv = _read_int(
        os.path.join(fs_dir, 'allocation', 'global_rsv_size'))
    return {
        'device_size': device_size,
        'allocated': allocated,
        'unallocated': max(0, device_size - allocated),
        'metadata_total': metadata_total,
        'metadata_used': metadata_used,
        'global_rsv': global_rsv,
        'metadata_headroom': max(0, metadata_total - metadata_used - global_rsv),
    }


def _mb(nbytes):
    return nbytes // (1024 * 1024)


def btrfs_space_status(directory, sysfs_root=None, mountinfo_path=DEFAULT_MOUNTINFO,
                       hostname=None):
    """(status, message) for directory: status is 'OK', 'WARNING' or
    'ERROR'. Non-btrfs directories and anything that cannot be determined
    come back as 'OK' with a message saying why the check was skipped."""
    if sysfs_root is None:
        sysfs_root = os.environ.get('UNMW_BTRFS_SYSFS_ROOT') or DEFAULT_SYSFS_ROOT
    if hostname is None:
        hostname = socket.gethostname()
    try:
        mount = find_mount(directory, mountinfo_path)
        if mount is None:
            return 'OK', 'btrfs check skipped: no mount found for {}'.format(directory)
        mount_point, fstype, source = mount
        if fstype != 'btrfs':
            return 'OK', 'btrfs check skipped: {} is on {}'.format(directory, fstype)
        fs_dir = btrfs_sysfs_dir(source, sysfs_root)
        if fs_dir is None:
            return 'OK', ('btrfs check skipped: no sysfs entry for {} under {}'
                          .format(source, sysfs_root))
        fig = btrfs_space_figures(fs_dir)
    except (OSError, ValueError) as exc:
        return 'OK', 'btrfs check skipped: {}'.format(exc)
    advice = "run 'btrfs balance start -dusage=50 {}' as root".format(mount_point)
    if fig['unallocated'] >= BTRFS_MIN_UNALLOCATED_BYTES:
        return 'OK', ('server {} btrfs at {}: {} MB unallocated, {} MB metadata'
                      ' headroom'.format(hostname, directory, _mb(fig['unallocated']),
                                         _mb(fig['metadata_headroom'])))
    if fig['metadata_headroom'] < BTRFS_MIN_METADATA_HEADROOM_BYTES:
        return 'ERROR', (
            'ERROR: server {} is out of disk space at {}: btrfs metadata is'
            ' exhausted ({} MB unallocated, {} MB metadata headroom) although df'
            ' reports free space - {}'.format(
                hostname, directory, _mb(fig['unallocated']),
                _mb(fig['metadata_headroom']), advice))
    return 'WARNING', (
        'WARNING: server {} is low on disk space at {}: btrfs has only {} MB'
        ' unallocated, metadata cannot grow beyond its {} MB headroom - {}'.format(
            hostname, directory, _mb(fig['unallocated']),
            _mb(fig['metadata_headroom']), advice))


def worst_status(first, second):
    """Combine two (status, message) pairs: the more severe status wins; a
    second WARNING/ERROR that does not win is appended to the message so no
    information is lost."""
    if _RANK[second[0]] > _RANK[first[0]]:
        return second
    if second[0] != 'OK' and second[1]:
        return first[0], first[1] + '; ' + second[1]
    return first


def main(argv):
    if os.environ.get('GATEWAY_INTERFACE') or os.environ.get('REQUEST_METHOD'):
        print('Content-Type: text/plain\n\nERROR: this script must not be run as CGI')
        return 1
    if len(argv) != 2:
        print('Usage: {} DIRECTORY'.format(argv[0]), file=sys.stderr)
        return 2
    status, message = btrfs_space_status(argv[1])
    print(message)
    return _RANK[status]


if __name__ == '__main__':
    sys.exit(main(sys.argv))

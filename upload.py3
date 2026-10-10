#!/usr/bin/env python3

# Handle cgi module removal in Python 3.13+
# The 'cgi' and 'cgitb' modules were removed from stdlib in Python 3.13.
# The 'legacy-cgi' package provides these modules for Python 3.13+.
# Install with: pip install legacy-cgi
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
try:
    import cgi
except ImportError:
    import sys
    sys.exit("Error: 'cgi' module not found. "
             "For Python 3.13+, install: pip install legacy-cgi")
import fcntl
import html
import locale
import os
import secrets
import stat
import string
import struct
import subprocess
import threading
import time
import sys
import socket
# Try to import archive handling libraries
try:
    import zipfile
    HAVE_ZIPFILE = True
except ImportError:
    HAVE_ZIPFILE = False

try:
    import rarfile
    HAVE_RARFILE = True
except ImportError:
    HAVE_RARFILE = False
import re
from collections import Counter
from typing import List, NamedTuple, Tuple

# btrfs can refuse writes (ENOSPC) while statvfs still reports hundreds of GB
# free: once the device is fully allocated to block groups and the metadata
# block groups fill up, nothing can be created or renamed. nmw_fs_check.py
# reads the kernel's btrfs counters (world-readable sysfs, no root needed)
# and passes on any other filesystem. It lives next to this script; the
# classic statvfs check alone is used if it is not deployed.
try:
    from nmw_fs_check import btrfs_space_status, worst_status
except ImportError:
    btrfs_space_status = None


# Constants for file validation
MIN_FILE_SIZE = 2 * 1024 * 1024  # 2MB
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
# The archive may fill MAX_FILE_SIZE; allow space for multipart headers and
# form fields while bounding the *whole* request before cgi buffers it.
MAX_REQUEST_SIZE = MAX_FILE_SIZE + 1024 * 1024
MAX_MULTIPART_HEADER_BYTES = 64 * 1024
ALLOWED_EXTENSIONS = {'.zip', '.rar'}
ALLOWED_IMAGE_EXTENSIONS = {'.fit', '.fits', '.fts'}
MIN_IMAGE_FILES = 2

# Decompression-bomb limits. MAX_FILE_SIZE bounds only the COMPRESSED upload,
# so without these a small archive can expand until the data volume is full.
# A night's upload is a few hundred 20-second frames; 4000 members and 32 GB
# leave a wide margin over that while still bounding the damage. Real FITS
# frames compress by roughly 2-4x, so a 200:1 ratio is far outside normal.
MAX_ARCHIVE_MEMBERS = 4000
MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024 * 1024  # 32GB
MAX_COMPRESSION_RATIO = 200
# zipfile buffers the central directory before constructing its members.
# Bound that allocation as well as the number of records (including when
# the end record lies about the count). Ordinary FITS archives need far less.
MAX_ZIP_DIRECTORY_BYTES = 16 * 1024 * 1024
MAX_RAR_LISTING_BYTES = 16 * 1024 * 1024
MAX_RAR_LISTING_LINE_BYTES = 64 * 1024
RAR_LISTING_TIMEOUT = 120

# Suspicious filename log. Every suspicious client-supplied file name - the
# name of each file part of the upload request and of each member of the
# uploaded archive - is appended to it, one line per name, whether the
# upload is accepted or rejected. A rejected upload is deleted, and the web
# server's access log never sees the names inside a request body, so without
# this log an attack probe would leave no trace. The log holds client
# addresses and attacker-chosen text, so it must NOT be readable over the
# web: a location inside a directory the web server is known to publish is
# refused (see suspicious_filenames_log_path()). SUSPICIOUS_FILENAMES_LOG
# (environment or local_config.sh) names the file; by default it is written
# next to the cgitb crash reports.
SUSPICIOUS_FILENAMES_LOG_NAME = 'unmw_suspicious_filenames.txt'
# A log that would grow past this size is first renamed to <log>.1 (replacing
# the previous one), so the two never take much more than twice this space -
# /tmp may be a RAM-backed tmpfs. That needs a directory the web server user
# can write; where the log cannot be renamed, logging stops at this size.
MAX_SUSPICIOUS_FILENAMES_LOG_BYTES = 64 * 1024 * 1024
# At most this many names of one upload get a line each; one more line counts
# the rest by problem. With the cut of every logged value to
# MAX_LOGGED_VALUE_CHARS characters (after escaping) this bounds what one
# request can add to the log to about 150 KB: a flood of crafted uploads
# needs hundreds of requests to push earlier records out of it.
MAX_LOGGED_NAMES_PER_UPLOAD = 100
MAX_LOGGED_VALUE_CHARS = 256

# Disk space thresholds in KB
# These defaults can be overridden by WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB
# and WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB environment variables or
# by exporting them in local_config.sh
DEFAULT_DISK_SPACE_SOFTLIMIT_KB = 100 * 1024 * 1024  # 100 GB
DEFAULT_DISK_SPACE_HARDLIMIT_KB = 5 * 1024 * 1024    # 5 GB


def _parse_config_value(config_path: str, var_name: str):
    """Read a variable value from a bash-style config file."""
    try:
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or '=' not in line:
                    continue
                if line.startswith('export '):
                    line = line[7:]
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # Strip inline comments: "12345 # comment" → "12345"
                comment_pos = value.find(' #')
                if comment_pos >= 0:
                    value = value[:comment_pos].strip()
                if key == var_name:
                    return value
    except (FileNotFoundError, PermissionError):
        pass
    return None


def get_disk_space_limits() -> Tuple[int, int]:
    """Get disk space soft and hard limits in KB.
    Checks environment variables first, then local_config.sh, then defaults."""
    softlimit = DEFAULT_DISK_SPACE_SOFTLIMIT_KB
    hardlimit = DEFAULT_DISK_SPACE_HARDLIMIT_KB

    # Path to local_config.sh in the same directory as this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, 'local_config.sh')

    for var_name, default_val, setter in [
        ('WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB', softlimit, 'soft'),
        ('WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB', hardlimit, 'hard'),
    ]:
        val = os.environ.get(var_name)
        if not val:
            val = _parse_config_value(config_path, var_name)
        if val and val.isdigit() and int(val) > 0:
            if setter == 'soft':
                softlimit = int(val)
            else:
                hardlimit = int(val)

    # Ensure softlimit >= hardlimit
    if softlimit < hardlimit:
        softlimit = hardlimit

    return softlimit, hardlimit


def check_disk_space_status(directory: str) -> Tuple[str, str]:
    """Check disk space and return (status, message).
    status is one of 'OK', 'WARNING', 'ERROR'.
    Message format matches the shell script check_free_space() output."""
    softlimit_kb, hardlimit_kb = get_disk_space_limits()
    hostname = socket.gethostname()

    st = os.statvfs(os.path.realpath(directory))
    free_kb = (st.f_bavail * st.f_frsize) // 1024

    free_mb = free_kb // 1024

    if free_kb >= softlimit_kb:
        result = ("OK", f"server {hostname} has sufficient free disk space available: {free_mb} MB at {directory}")
    elif free_kb >= hardlimit_kb:
        result = ("WARNING", f"WARNING: server {hostname} is low on disk space, only {free_mb} MB free at {directory}")
    else:
        result = ("ERROR", f"ERROR: server {hostname} is out of disk space, only {free_mb} MB free at {directory}")

    # The btrfs metadata check can only raise the severity (or add a note);
    # it never masks the classic result and never breaks the endpoint.
    if btrfs_space_status is not None:
        try:
            result = worst_status(result, btrfs_space_status(directory, hostname=hostname))
        except Exception:
            pass
    return result


# Suspicious patterns in a file name, each with the problem it reveals (the
# suspicious filename log records the problem)
DANGEROUS_FILENAME_PATTERNS = [
    (r'\.\.', 'path traversal'),
    (r'^\..*$', 'hidden file'),
    (r'[<>:"|?*]', 'Windows special characters'),
    (r'[;&|`$]', 'shell special characters'),
    (r'[^\w\-\.]', 'characters other than letters, digits, underscore, dash and dot'),
]

# The same for the directory names in the path of an archive member, searched
# in the whole directory part at once. The archive is extracted with its
# directories dropped (unzip -j, rar e), so these are only logged, and
# ordinary characters such as a space are not reported.
DANGEROUS_DIRECTORY_PATTERNS = [
    (r'(?:^|[/\\])\.(?!\.?(?:[/\\]|\Z))', 'hidden directory'),
    (r'[<>:"|?*]', 'Windows special characters in a directory name'),
    (r'[;&|`$]', 'shell special characters in a directory name'),
]


def unsafe_filename_problems(filename: str) -> List[str]:
    """
    The problems DANGEROUS_FILENAME_PATTERNS find in the last path component
    of filename - empty for a safe name
    """
    # Remove any directory components, keep just filename
    filename = os.path.basename(filename)
    return [problem for pattern, problem in DANGEROUS_FILENAME_PATTERNS
            if re.search(pattern, filename)]


def is_safe_filename(filename: str) -> bool:
    """
    Check if filename is safe - no path traversal, no special chars
    """
    return is_safe_member_path(filename) and not unsafe_filename_problems(filename)


def is_safe_member_path(name: str) -> bool:
    """Reject absolute/drive-qualified paths and parent components on either OS.

    Check directory entries too, before discarding directories for extraction.
    A single unsafe member invalidates the whole archive, not just that member.
    """
    absolute = name.startswith(('/', '\\')) or re.match(r'[A-Za-z]:', name)
    return not (absolute or '..' in re.split(r'[/\\]', name))


def path_problems(name: str) -> List[str]:
    """
    What a client-supplied path shows beyond its last component: no
    legitimate client sends an absolute path, a '..' component or invisible
    characters. Absolute paths and parent components reject the whole archive,
    even though its directory components would be dropped at extraction.
    """
    problems = []
    if name.startswith(('/', '\\')) or re.match(r'[A-Za-z]:', name):
        problems.append('absolute path')
    if '..' in re.split(r'[/\\]', name):
        problems.append('path traversal')
    if not name.isprintable():
        problems.append('control or invisible characters')
    return problems


def upload_name_problems(name: str) -> List[str]:
    """
    Why the client-supplied name of an uploaded file is suspicious - empty
    if it is not. Browsers and astrocam-go send a bare name ending in .zip or
    .rar; whatever this finds, the file is saved under a sanitized name.
    """
    base = os.path.basename(name)
    problems = path_problems(name)
    if '/' in name or '\\' in name:
        problems.append('directory part')
    if len(base) > 255:
        # longer than a file name may be on Linux: the upload cannot be saved
        problems.append('longer than 255 characters')
    problems += unsafe_filename_problems(base)
    if os.path.splitext(base)[1].lower() not in ALLOWED_EXTENSIONS:
        problems.append('not a .zip or .rar name')
    return list(dict.fromkeys(problems))


def member_problems(name: str, is_dir: bool = False, is_symlink: bool = False) -> List[str]:
    """
    Why an archive member name is suspicious - empty if it is not: the
    problems check_archive_contents() rejects, plus suspicious directory
    characters (other than absolute/parent paths), which are only logged
    """
    problems = path_problems(name)
    if is_symlink:
        problems.append('symlink')
    if is_dir:
        directories = name
    else:
        directories = re.match(r'(.*)[/\\]|', name, re.DOTALL).group(1) or ''
    problems += [problem for pattern, problem in DANGEROUS_DIRECTORY_PATTERNS
                 if re.search(pattern, directories)]
    if not is_dir:
        problems += unsafe_filename_problems(name)
        if os.path.splitext(name)[1].lower() not in ALLOWED_IMAGE_EXTENSIONS:
            problems.append('not a .fit, .fits or .fts image')
    return list(dict.fromkeys(problems))


class SuspiciousFilenames:
    """The suspicious file names met while handling one upload"""

    def __init__(self):
        self.entries = []                # (what, name, problems), a log line each
        self.more_names = 0              # names past MAX_LOGGED_NAMES_PER_UPLOAD...
        self.more_problems = Counter()   # ...and how often each problem occurs among them
        self.unexamined = 0              # archive members past MAX_ARCHIVE_MEMBERS
        self.members_examined = False
        self.remarks = []                # anything else worth a line in the log
        self.archive = ''                # the sanitized name the upload is saved under
        self.upload_dir = ''

    def note(self, what: str, name: str, problems: List[str]):
        if not problems:
            return
        if len(self.entries) < MAX_LOGGED_NAMES_PER_UPLOAD:
            self.entries.append((what, name, problems))
        else:
            self.more_names += 1
            self.more_problems.update(problems)


def private_log_dir() -> str:
    """
    Directory for logs that must not be web-served: the cgitb crash reports
    and, unless SUSPICIOUS_FILENAMES_LOG says otherwise, the suspicious
    filename log
    """
    return os.environ.get('UNMW_CGITB_LOGDIR', '/tmp')


def _config_value(var_name: str):
    """A setting from the environment, else from local_config.sh next to this script"""
    value = os.environ.get(var_name)
    if not value:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        value = _parse_config_value(os.path.join(script_dir, 'local_config.sh'), var_name)
    return value or None


def _config_path(var_name: str, script_dir: str) -> Tuple[str, str]:
    """
    A path setting as (path, complaint), with $PWD (this script's directory,
    as in fastplot.py) and the other variables expanded - from the
    environment, else from local_config.sh - and a relative path taken from
    this script's directory: ('', '') when it is unset, ('', complaint) when
    it holds a variable that cannot be resolved here. Shell quotes left in
    a value ("$WEB_ROOT"/unmw) are dropped, as no path here contains one.
    """
    def lookup(m):
        name = m.group(1) or m.group(2)
        return script_dir if name == 'PWD' else (_config_value(name) or m.group(0))

    value = _config_value(var_name)
    if not value:
        return '', ''
    for _ in range(5):  # a setting may refer to one that refers to another
        value = re.sub(r'\$\{(\w+)\}|\$(\w+)', lookup, value.replace('"', '').replace("'", ''))
    if '$' in value:
        return '', f"cannot resolve {var_name}={value}; give it as an absolute path"
    return os.path.join(script_dir, value), ''


def _directory_ids(paths) -> set:
    """(st_dev, st_ino) of each of the paths that is an existing directory"""
    ids = set()
    for path in paths:
        try:
            st = os.stat(path)
        except (OSError, ValueError):
            continue
        if stat.S_ISDIR(st.st_mode):
            ids.add((st.st_dev, st.st_ino))
    return ids


def _inside(path: str, directory_ids: set) -> bool:
    """
    Whether path lies inside one of the directories, compared by identity
    rather than by name, so a symlink or a bind mount cannot disguise one
    """
    directory = os.path.dirname(path)
    while True:
        try:
            st = os.stat(directory)
            if (st.st_dev, st.st_ino) in directory_ids:
                return True
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(directory)
        if parent == directory:
            return False
        directory = parent


def suspicious_filenames_log_path() -> Tuple[str, str]:
    """
    Choose the suspicious filename log: SUSPICIOUS_FILENAMES_LOG, else
    SUSPICIOUS_FILENAMES_LOG_NAME in private_log_dir(), else in /tmp - the
    first that is not inside a directory this script knows the web server
    publishes: this checkout (the document root of the test servers, which
    serve its files as they are), the uploads tree and the data directories
    behind it (DATA_PROCESSING_ROOT, IMAGE_DATA_ROOT), HTDOCS_DIR and the
    DOCUMENT_ROOT the web server passes. Returns (path, complaint): the
    complaint explains a refused location or an unusable setting; path is
    empty only if every candidate was refused.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    complaints = []
    published = [script_dir, os.path.join(script_dir, 'uploads'), 'uploads',
                 os.environ.get('DOCUMENT_ROOT', '')]
    for var_name in ('HTDOCS_DIR', 'DATA_PROCESSING_ROOT', 'IMAGE_DATA_ROOT'):
        directory, complaint = _config_path(var_name, script_dir)
        published.append(directory)
        if complaint:
            complaints.append(complaint)
    published = _directory_ids(directory for directory in published if directory)

    configured, complaint = _config_path('SUSPICIOUS_FILENAMES_LOG', script_dir)
    if complaint:
        complaints.append(complaint)
    candidates = [configured] if configured else []
    candidates += [os.path.join(private_log_dir(), SUSPICIOUS_FILENAMES_LOG_NAME),
                   os.path.join('/tmp', SUSPICIOUS_FILENAMES_LOG_NAME)]
    for candidate in candidates:
        # Resolve symlinked directories; the file itself is opened O_NOFOLLOW
        path = os.path.join(os.path.realpath(os.path.dirname(candidate)),
                            os.path.basename(candidate))
        if not _inside(path, published):
            return path, '; '.join(complaints)
        complaints.append(f"refusing to write the suspicious filename log to {path}: "
                          f"it is inside a web-served directory")
    return '', '; '.join(complaints)


def _log_value(value: str) -> str:
    """
    value as one single-quoted token of printable ASCII in the syntax of a
    Python string literal: quotes, backslashes, newlines, terminal escape
    sequences and every other non-printable or non-ASCII character are
    escaped, so a crafted value can neither break the one-line-per-name
    format nor pass for another field. The escaped text is cut at
    MAX_LOGGED_VALUE_CHARS characters, marked by '...' after the quote.
    """
    escaped = []
    size = 0
    for char in str(value):
        if char in "'\\":
            char = '\\' + char
        elif not ' ' <= char <= '~':
            char = ascii(char)[1:-1]
        size += len(char)
        if size > MAX_LOGGED_VALUE_CHARS:
            return "'" + ''.join(escaped) + "'..."
        escaped.append(char)
    return "'" + ''.join(escaped) + "'"


def _to_error_log(message: str):
    """
    One line for the web server error log, where Apache keeps a CGI's
    stderr. stdout is flushed first: thttpd passes stderr down the same pipe
    as stdout, and a note must not get ahead of the buffered headers.
    """
    try:
        sys.stdout.flush()
        sys.stderr.write(f"upload.py3: {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _open_private_log(path: str) -> int:
    """
    Open the log at path for appending, creating it readable by its owner
    only. The default location is the world-writable /tmp, so never follow
    a symlink, block on a FIFO, or write into a hard link or a file another
    local user planted there. A file owned by root is accepted too, so an
    administrator may create the log beforehand (rotation replaces it with
    a new file of the web server user, mode 0600).
    """
    flags = os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, os.O_RDWR | flags, 0o600)
    except PermissionError:
        # writable but not readable: append without repairing a cut-off line
        fd = os.open(path, os.O_WRONLY | flags, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("it is not a regular file")
        if st.st_uid not in (os.geteuid(), 0):
            raise OSError(f"it is owned by another user (uid {st.st_uid})")
        if st.st_nlink > 1:
            raise OSError("it has more than one hard link")
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _append_private_log(path: str, data: bytes):
    """
    Append data - whole lines - to the log at path. Concurrent uploads take
    turns (flock), so the lines of one upload stay together, and a log that
    would grow past MAX_SUSPICIOUS_FILENAMES_LOG_BYTES is first renamed to
    <path>.1. The OSError of a failed write tells in lines_written how many
    of the lines did reach the log.
    """
    for _ in range(3):
        fd = _open_private_log(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            st = os.fstat(fd)
            if st.st_size and st.st_size + len(data) > MAX_SUSPICIOUS_FILENAMES_LOG_BYTES:
                try:
                    # unless another upload rotated it while this one waited
                    current = os.stat(path, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (st.st_dev, st.st_ino):
                        os.rename(path, path + '.1')
                except FileNotFoundError:
                    pass
                continue
            try:
                cut_off = st.st_size and os.pread(fd, 1, st.st_size - 1) != b'\n'
            except OSError:
                cut_off = False
            if cut_off:
                # an earlier write was cut short (disk full): end its line
                os.write(fd, b'\n')
            done = 0
            try:
                while done < len(data):
                    done += os.write(fd, data[done:])
            except OSError as e:
                e.lines_written = data[:done].count(b'\n')
                raise
            return
        finally:
            os.close(fd)
    raise OSError("other uploads keep rotating it")


def log_suspicious_filenames(found: SuspiciousFilenames, result: Tuple[bool, str, str, str]):
    """
    Append a line per suspicious name in found to the suspicious filename
    log - when, from where, which name, what is wrong with it and what
    became of the upload - and one line counting the names left out
    (MAX_LOGGED_NAMES_PER_UPLOAD, MAX_ARCHIVE_MEMBERS) and giving the
    remarks, if there are any. Every value goes
    through _log_value(). Never raises: a logging failure must not break the
    upload; it is reported to the web server error log instead.
    """
    if not found.entries and not found.unexamined and not found.remarks:
        return
    lines = []
    path = ''
    try:
        outcome = 'accepted' if result[0] else f'rejected: {result[1]}'
        upload = os.path.basename(found.upload_dir.rstrip('/')) or '-'
        fields = [time.strftime('%Y-%m-%d %H:%M:%S %z')]
        for key, var in (('addr', 'REMOTE_ADDR'), ('user', 'REMOTE_USER'),
                         ('agent', 'HTTP_USER_AGENT')):
            fields.append(f'{key}={_log_value(os.environ.get(var) or "-")}')
        fields.append(f'upload={_log_value(upload)}')
        fields.append(f'archive={_log_value(found.archive or "-")}')
        prefix = ' '.join(fields)
        suffix = f'result={_log_value(outcome)}\n'
        for what, name, problems in found.entries:
            lines.append(f'{prefix} {what}={_log_value(name)} '
                         f'problems={_log_value("; ".join(problems))} {suffix}')
        more = []
        if found.more_names:
            counts = ', '.join(f'{problem} x{count}'
                               for problem, count in found.more_problems.most_common())
            more.append(f'{found.more_names} more suspicious names ({counts})')
        if found.unexamined:
            more.append(f'{found.unexamined} more archive members not examined')
        more += found.remarks
        if more:
            lines.append(f'{prefix} more={_log_value("; ".join(more))} {suffix}')
        path, complaint = suspicious_filenames_log_path()
        if complaint:
            _to_error_log(complaint)
        if not path:
            raise OSError("every candidate location is web-served")
        _append_private_log(path, ''.join(lines).encode('ascii'))
    except Exception as e:
        lost = len(lines) - getattr(e, 'lines_written', 0)
        _to_error_log(f"cannot write the suspicious filename log {path or '(no location)'}: "
                      f"{e}; {lost} line(s) not logged")


def validate_archive_size(filesize: int) -> bool:
    """
    Validate archive file size is within acceptable range
    """
    return MIN_FILE_SIZE <= filesize <= MAX_FILE_SIZE


def get_mime_type(filepath: str) -> str:
    """
    Identify archives by their content, without an optional MIME dependency.
    Only ordinary ZIP/RAR uploads are supported, as in wrapper.sh; executable
    self-extractors are still inspected separately for suspicious names.
    """
    with open(filepath, 'rb') as archive:
        signature = archive.read(8)
    if signature.startswith((b'Rar!\x1a\x07\x00', b'Rar!\x1a\x07\x01\x00')):
        return 'application/vnd.rar'
    if signature.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')):
        return 'application/zip'
    return 'application/octet-stream'


def validate_archive_type(filepath: str) -> Tuple[bool, str]:
    """
    Validate that file is a legitimate archive of allowed type.
    """
    mime_type = get_mime_type(filepath)
    ext = os.path.splitext(filepath)[1].lower()

    if ext not in ALLOWED_EXTENSIONS:
        return False, f"Invalid file extension: {ext}"

    valid_mime_types = {
        '.zip': 'application/zip',
        '.rar': ['application/x-rar', 'application/vnd.rar']
    }

    if isinstance(valid_mime_types.get(ext), list):
        if mime_type not in valid_mime_types[ext]:
            return False, f"MIME type mismatch: {mime_type}"
    else:
        if mime_type != valid_mime_types.get(ext):
            return False, f"MIME type mismatch: {mime_type}"

    return True, ""


class RarValidationError(ValueError):
    """The native RAR listing could not be validated safely."""

    def __init__(self, message, member_name=None):
        super().__init__(message)
        self.member_name = member_name


def _rar_utf8_locale():
    """Select an installed UTF-8 locale, or fail closed.

    In the plain C locale native RAR can silently drop Unicode characters
    even with -scf. Checking both listings would not detect that shared loss.
    Restore the CGI's locale before launching the tool with its own setting.
    """
    previous = locale.setlocale(locale.LC_CTYPE)
    try:
        for candidate in ('C.UTF-8', 'C.utf8', previous, 'en_US.UTF-8', 'en_US.utf8'):
            try:
                locale.setlocale(locale.LC_CTYPE, candidate)
            except locale.Error:
                continue
            if locale.nl_langinfo(locale.CODESET).replace('-', '').lower() == 'utf8':
                return candidate
    finally:
        locale.setlocale(locale.LC_CTYPE, previous)
    raise RarValidationError('A UTF-8 locale is required to inspect RAR names safely')


def _rar_listing_lines(filepath, mode):
    """Read native listings with byte, line, member and wall-clock bounds.

    Disable user configuration, comments, prompts and stdin; force UTF-8
    output. Never fall back to another binary after a tool rejects a file.
    A filename cannot be interpreted as a switch or a list of input files.
    """
    env = os.environ.copy()
    env['LC_ALL'] = _rar_utf8_locale()
    env['LANGUAGE'] = 'C'
    env.pop('RAR', None)
    env.pop('RARINISWITCHES', None)
    for binary in ('rar', 'unrar', '/opt/bin/rar', '/opt/bin/unrar'):
        try:
            proc = subprocess.Popen([binary, mode, '-cfg-', '-c-', '-p-', '-scf',
                                     '-@', '--', os.path.abspath(filepath)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=env)
        except OSError:
            continue
        watchdog = threading.Timer(RAR_LISTING_TIMEOUT, proc.kill)
        watchdog.start()
        try:
            lines = []
            total = 0
            max_lines = MAX_ARCHIVE_MEMBERS if mode == 'lb' else 20 * MAX_ARCHIVE_MEMBERS + 32
            while True:
                line = proc.stdout.readline(MAX_RAR_LISTING_LINE_BYTES + 1)
                if not line:
                    break
                total += len(line)
                if len(line) > MAX_RAR_LISTING_LINE_BYTES or total > MAX_RAR_LISTING_BYTES:
                    raise RarValidationError('RAR listing exceeds the metadata limit')
                if len(lines) >= max_lines:
                    raise RarValidationError('RAR listing exceeds the member or line limit')
                lines.append(line)
            if proc.wait() != 0:
                raise RarValidationError('RAR listing failed or timed out')
            return lines
        finally:
            watchdog.cancel()
            proc.kill()
            proc.wait()
            proc.stdout.close()
    raise RarValidationError('No working rar/unrar binary is available')


def rar_member_names_via_binary(filepath: str):
    """Best-effort names for logging only; never use this lossy list as a verdict."""
    try:
        text = b''.join(_rar_listing_lines(filepath, 'lb')).decode('utf-8', 'replace')
        return [line.strip() for line in text.splitlines() if line.strip()]
    except RarValidationError:
        return None


# A 'Key: value' line of the technical listing of the rar binary
RAR_LISTING_FIELD = re.compile(r' *[A-Za-z][A-Za-z0-9 ]*: ')


def _parse_rar_technical_listing(lines, header: str):
    """
    ([name, is_dir, is_symlink] of the members, whether the listing was
    regular) from the lines of a 'rar lt' listing, read from the 'Archive:'
    line naming the file on. A line that is not 'Key: value' continues the
    name above it (a name with a line break). A crafted name or symlink
    target can imitate the listing's lines, so it counts as regular only if
    every member starts after a blank line and has exactly one 'Type:' line,
    right after its name - which a forged line upsets - and if it has at
    most MAX_ARCHIVE_MEMBERS members in at most 20 lines each. None if the
    'Archive:' line never comes: a listing not understood here.
    """
    members = []
    started = in_name = blank = False
    types = 0
    for count, raw in enumerate(lines):
        if count > 20 * MAX_ARCHIVE_MEMBERS:
            return (members, False) if started else None
        line = raw[:-1] if raw.endswith(b'\n') else raw
        line = line.decode('utf-8', 'replace')
        if not started:
            started = line == header
        elif line.startswith('        Name: '):
            if len(members) == MAX_ARCHIVE_MEMBERS or not blank or (members and types != 1):
                return members, False
            members.append([line[len('        Name: '):], False, False])
            in_name = True
            types = 0
        elif in_name and not RAR_LISTING_FIELD.match(line):
            members[-1][0] += '\n' + line
        elif members:
            is_type = line.startswith('        Type: ')
            if in_name and not is_type:
                return members, False
            if is_type:
                types += 1
                kind = line[len('        Type: '):].strip().lower()
                members[-1][1] = kind == 'directory'
                members[-1][2] = 'symbolic link' in kind or 'junction' in kind
            in_name = False
        blank = line == ''
    return (members, not members or types == 1) if started else None


def rar_members_via_binary(filepath: str):
    """
    (name, is_dir, is_symlink) of the members and whether the listing was
    regular (see _parse_rar_technical_listing()), from the technical listing
    ('lt') of the rar/unrar binary - for the suspicious filename log only.
    Unlike a bare listing ('lb'), it tells the
    member type and keeps leading and trailing spaces and line breaks in a
    name. It is read as it is produced and cut off where it stops being
    regular, and the archive comment is left out (-c-). None if no binary
    gives a listing.
    """
    try:
        lines = _rar_listing_lines(filepath, 'lt')
        listing = _parse_rar_technical_listing(lines, 'Archive: ' + os.path.abspath(filepath))
        if listing is not None:
            return [tuple(member) for member in listing[0]], listing[1]
    except RarValidationError:
        pass
    return None


class RarMember(NamedTuple):
    filename: str
    file_size: int
    compress_size: int
    directory: bool

    def is_dir(self):
        return self.directory


def _parse_rar_validation_listing(lines, filepath):
    """Strict metadata parser, separate from the best-effort forensic parser.

    Every file must have one Name, Type, Size and Packed size. A name cannot
    continue over lines or manufacture duplicate metadata. Only regular files
    and directories are supported; links/redirections and encrypted/split
    inputs are not ordinary camera archives. Cross-check names with lb too.
    """
    members = []
    record = {}
    started = details = False

    def finish_record():
        if not record:
            return
        if len(members) >= MAX_ARCHIVE_MEMBERS:
            raise RarValidationError('Too many files in archive')
        name = record['Name']
        kind = record.get('Type')
        if kind not in ('File', 'Directory'):
            # The tolerant forensic parser already identifies symlinks.
            # Attach other unsupported types' names for the private log;
            # the caller cross-checks that they occur in the bare listing.
            noted_name = name if kind and 'symbolic link' not in kind.lower() and 'junction' not in kind.lower() else None
            raise RarValidationError('Unsupported RAR member type (links are not allowed)', noted_name)
        if not name or not name.isprintable():
            raise RarValidationError('Empty or ambiguous RAR member name')
        if kind == 'Directory':
            # Native tools omit size fields for ordinary empty directories.
            # If a tool does report them, nonzero payloads are inconsistent.
            for key in ('Size', 'Packed size'):
                if record.setdefault(key, '0') != '0':
                    raise RarValidationError('Invalid RAR directory sizes')
        if not all(re.fullmatch(r'[0-9]{1,20}', record.get(key, ''))
                   for key in ('Size', 'Packed size')):
            raise RarValidationError('Missing or invalid RAR member sizes')
        flags = record.get('Flags', '').lower()
        if any(flag in flags for flag in ('encrypted', 'split', 'volume')):
            raise RarValidationError('Encrypted or split RAR members are not supported')
        members.append(RarMember(name, int(record['Size']), int(record['Packed size']),
                                 kind == 'Directory'))

    for raw in lines:
        if not raw.endswith(b'\n'):
            raise RarValidationError('Incomplete RAR listing line')
        line = raw[:-1].decode('utf-8')
        if not started:
            started = line == 'Archive: ' + os.path.abspath(filepath)
        elif not details:
            if not line.startswith('Details: RAR '):
                raise RarValidationError('Unrecognized RAR archive details')
            if 'volume' in line.lower() or 'encrypted' in line.lower():
                raise RarValidationError('Encrypted or multivolume RAR archives are not supported')
            details = True
        elif not line:
            finish_record()
            record = {}
        else:
            field = re.fullmatch(r' *([A-Za-z][A-Za-z0-9 ]*): (.*)', line)
            if field is None or not line.isprintable():
                raise RarValidationError('Ambiguous RAR technical listing')
            key, value = field.groups()
            expected = 'Name' if not record else 'Type' if len(record) == 1 else None
            if key in record or (expected is not None and key != expected) or len(record) >= 20:
                raise RarValidationError('Ambiguous RAR technical listing')
            record[key] = value
    finish_record()
    if not details:
        raise RarValidationError('No recognizable RAR technical listing')
    return members


def rar_validation_members(filepath, suspicious=None):
    """Metadata-aware validation without a rarfile dependency or extraction."""
    bare_lines = []
    try:
        bare_lines = _rar_listing_lines(filepath, 'lb')
        technical_lines = _rar_listing_lines(filepath, 'lt')
        if suspicious is not None:
            # Preserve the richer forensic log, including rejected multiline
            # names. Its tolerant parsing is never used for acceptance.
            bare_text = b''.join(bare_lines).decode('utf-8', 'replace')
            log_names = [line.strip() for line in bare_text.splitlines() if line.strip()]
            listing = _parse_rar_technical_listing(technical_lines, 'Archive: ' + os.path.abspath(filepath))
            note_archive_members(suspicious, filepath, filelist=log_names,
                                 binary_listing=listing or ([], False))
        members = _parse_rar_validation_listing(technical_lines, filepath)
        if not all(line.endswith(b'\n') for line in bare_lines):
            raise RarValidationError('Incomplete RAR bare listing')
        # Do not strip spaces, skip blank entries, or split Unicode newlines.
        names = [line[:-1].decode('utf-8') for line in bare_lines]
        if names != [member.filename for member in members]:
            raise RarValidationError('RAR member listings disagree')
        return members
    except (RarValidationError, UnicodeError) as error:
        name = getattr(error, 'member_name', None)
        if suspicious is not None and name and (name + '\n').encode('utf-8') in bare_lines:
            suspicious.note('member', name, ['unsupported RAR member type or file redirection'])
        if suspicious is not None and not suspicious.members_examined:
            suspicious.remarks.append('RAR validation could not examine all members: ' + str(error))
        raise


def _member_record(info) -> Tuple[str, bool, bool]:
    """
    (name, is_dir, is_symlink) of a zipfile or rarfile member, by the tests
    check_archive_contents() makes, but never raising on an odd member
    (before Python 3.11 ZipInfo.is_dir() fails on an empty name)
    """
    name = info.filename
    try:
        is_dir = name.endswith('/') or bool(getattr(info, 'is_dir', lambda: False)())
    except Exception:
        is_dir = False
    try:
        mode = ((getattr(info, 'external_attr', 0) or 0) >> 16) & 0o170000
        is_link = getattr(info, 'is_symlink', None)
        is_symlink = mode == 0o120000 or (callable(is_link) and bool(is_link()))
    except Exception:
        is_symlink = False
    return name, is_dir, is_symlink


def note_archive_members(found: SuspiciousFilenames, filepath: str, infolist=None, filelist=None,
                         binary_listing=None):
    """
    Note the suspicious members of an archive, given its zipfile/rarfile
    infolist or else the bare listing of the rar binary (filelist). For the
    log only: it examines at most MAX_ARCHIVE_MEMBERS members and never
    raises, so it cannot change a verdict.

    With the rar binary the technical listing (rar_members_via_binary())
    shows what the bare one hides - symlinks, spaces around a name, line
    breaks in it - but a crafted name or symlink target can imitate its
    lines, so it is used only if it is regular and its names give exactly
    the bare listing the verdict saw, and not at all for an archive the
    verdict rejects for its size, which would make it costly.
    """
    try:
        if infolist is not None:
            members = [_member_record(info) for info in infolist[:MAX_ARCHIVE_MEMBERS]]
            found.unexamined += max(len(infolist) - MAX_ARCHIVE_MEMBERS, 0)
        else:
            listing = binary_listing
            if listing is None and len(filelist) <= MAX_ARCHIVE_MEMBERS:
                listing = rar_members_via_binary(filepath)
            elif len(filelist) > MAX_ARCHIVE_MEMBERS:
                found.remarks.append('too many members to read their types: symlink '
                                     'members cannot be told')
            if listing is not None:
                names = '\n'.join(name for name, _, _ in listing[0])
                if not listing[1] or [part.strip() for part in names.splitlines() if part.strip()] != filelist:
                    found.remarks.append('the two listings of this RAR archive by the rar '
                                         'binary disagree (crafted member names or symlink '
                                         'targets?); symlink members cannot be told')
                    listing = None
            if listing is not None:
                members = listing[0]
            else:
                members = [(name, name.endswith('/'), False) for name in filelist[:MAX_ARCHIVE_MEMBERS]]
                found.unexamined += max(len(filelist) - MAX_ARCHIVE_MEMBERS, 0)
        found.members_examined = found.members_examined or bool(members)
        for name, is_dir, is_symlink in members:
            found.note('member', name, member_problems(name, is_dir, is_symlink))
    except Exception as e:
        _to_error_log(f"cannot examine the archive members for the suspicious filename log: {e}")


def note_rejected_archive_members(found: SuspiciousFilenames, filepath: str, tried: str = ''):
    """
    Note the suspicious members of a rejected upload, unless that is done:
    one rejected before its contents were checked (too small, or not the
    archive its name says), or whose listing as the archive its name says
    ('tried', its extension, if that was attempted) found no members. It
    goes by what the file contains - a RAR archive, also a self-extracting
    one (rar looks for its signature in the first 4 MiB), and a ZIP archive;
    both, if the file is both. For the log only; never raises.
    """
    if found.members_examined:
        return
    try:
        with open(filepath, 'rb') as f:
            head = f.read(4 * 1024 * 1024)
        if b'Rar!\x1a\x07' in head and tried != '.rar':
            try:
                if HAVE_RARFILE:
                    with rarfile.RarFile(filepath) as rf:
                        note_archive_members(found, filepath, infolist=rf.infolist())
                else:
                    filelist = rar_member_names_via_binary(filepath)
                    if filelist:
                        note_archive_members(found, filepath, filelist=filelist)
            except Exception:
                pass  # not a RAR archive after all
        if HAVE_ZIPFILE and tried != '.zip' and zipfile.is_zipfile(filepath):
            if zip_directory_error(filepath, found):
                return
            with zipfile.ZipFile(filepath) as zf:
                note_archive_members(found, filepath, infolist=zf.infolist())
    except Exception:
        pass  # not an archive that can be listed: there are no member names to note


def zip_directory_error(filepath, suspicious=None):
    """Check a ZIP's directory in bounded memory before opening ZipFile.

    Use the stdlib's end-record reader (which handles ZIP64 and prepended
    data), then stream fixed-size headers and skip variable-length metadata.
    Never trust the end record's member count alone. The first 4000 names
    remain available for the suspicious-name log when the limit is exceeded.
    """
    with open(filepath, 'rb') as archive:
        end = zipfile._EndRecData(archive)
        if not end:
            raise zipfile.BadZipFile('Missing end of central directory')
        size = end[zipfile._ECD_SIZE]
        location = end[zipfile._ECD_LOCATION]
        # Older Python versions leave LOCATION at the ordinary end record;
        # newer versions point it at the ZIP64 record itself. Match each
        # version's ZipFile reader without hard-coding interpreter versions.
        if end[zipfile._ECD_SIGNATURE] == zipfile.stringEndArchive64:
            archive.seek(location)
            if archive.read(4) == zipfile.stringEndArchive:
                location -= zipfile.sizeEndCentDir64 + zipfile.sizeEndCentDir64Locator
        start = location - size
        if start < 0:
            raise zipfile.BadZipFile('Bad central directory offset')
        if size > MAX_ZIP_DIRECTORY_BYTES:
            if suspicious is not None:
                suspicious.members_examined = True
                suspicious.remarks.append('ZIP directory exceeds the metadata limit; members not examined')
            return 'ZIP directory exceeds the metadata limit'
        archive.seek(start)
        names = []
        count = 0
        while archive.tell() < location:
            header = archive.read(zipfile.sizeCentralDir)
            if len(header) != zipfile.sizeCentralDir:
                raise zipfile.BadZipFile('Truncated central directory')
            fields = struct.unpack(zipfile.structCentralDir, header)
            if fields[zipfile._CD_SIGNATURE] != zipfile.stringCentralDir:
                raise zipfile.BadZipFile('Bad central directory signature')
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                declared = end[zipfile._ECD_ENTRIES_TOTAL]
                total = max(count, declared)
                if suspicious is not None:
                    suspicious.members_examined = True
                    suspicious.unexamined += total - MAX_ARCHIVE_MEMBERS
                    for name, is_dir, is_link in names:
                        suspicious.note('member', name, member_problems(name, is_dir, is_link))
                return f'Too many files in archive: {total} (maximum {MAX_ARCHIVE_MEMBERS})'
            name_size = fields[zipfile._CD_FILENAME_LENGTH]
            skip = fields[zipfile._CD_EXTRA_FIELD_LENGTH] + fields[zipfile._CD_COMMENT_LENGTH]
            if archive.tell() + name_size + skip > location:
                raise zipfile.BadZipFile('Truncated central directory member')
            raw_name = archive.read(name_size)
            encoding = 'utf-8' if fields[zipfile._CD_FLAG_BITS] & 0x800 else 'cp437'
            name = raw_name.decode(encoding)
            mode = (fields[zipfile._CD_EXTERNAL_FILE_ATTRIBUTES] >> 16) & 0o170000
            names.append((name, name.endswith('/'), mode == 0o120000))
            archive.seek(skip, os.SEEK_CUR)
    return ''


def check_archive_contents(filepath: str, suspicious: SuspiciousFilenames = None) -> Tuple[bool, str]:
    """
    Validate archive contents without extracting.
    Directories are allowed; only file extensions are checked.

    Beyond names this enforces the decompression-bomb limits and rejects
    symlink members, which a name-only check cannot see: Info-ZIP recreates a
    symlink member even under 'unzip -j', and a later 'chmod' or 'find' that
    follows it would act outside the upload directory.

    Given 'suspicious', every suspicious member is noted there for the log
    first, by a separate pass that cannot change the verdict below.
    """
    ext = os.path.splitext(filepath)[1].lower()
    image_files = []
    total_uncompressed = 0
    total_compressed = 0
    infolist = None
    filelist = None
    binary_log_done = False

    if ext == '.zip' and not HAVE_ZIPFILE:
        return False, "Cannot validate ZIP archive: the zipfile module is unavailable"

    try:
        if ext == '.zip' and HAVE_ZIPFILE:
            error = zip_directory_error(filepath, suspicious)
            if error:
                return False, error
            with zipfile.ZipFile(filepath) as zf:
                infolist = zf.infolist()
        elif ext == '.rar' and HAVE_RARFILE:
            with rarfile.RarFile(filepath) as rf:
                infolist = rf.infolist()
        elif ext == '.rar':
            infolist = rar_validation_members(filepath, suspicious)
            binary_log_done = True
        else:
            return False, f"Unsupported archive type: {ext}"

        if suspicious is not None and not binary_log_done:
            note_archive_members(suspicious, filepath, infolist, filelist)

        if infolist is not None:
            if len(infolist) > MAX_ARCHIVE_MEMBERS:
                return False, (f"Too many files in archive: {len(infolist)} "
                               f"(maximum {MAX_ARCHIVE_MEMBERS})")
            filelist = []
            for info in infolist:
                name = info.filename
                if not name:
                    return False, "Empty filename in archive"
                # ZipInfo.filename is truncated at a NUL; inspect the original
                # decoded header name as well, before skipping any directory.
                raw_name = getattr(info, 'orig_filename', name)
                if not isinstance(raw_name, str):
                    raw_name = name
                if '\x00' in raw_name:
                    return False, "NUL in archive member name is not allowed"
                if not is_safe_member_path(raw_name):
                    return False, f"Unsafe path in archive: {raw_name}"
                # Symlink members carry file type 0o120000 in the high 16 bits
                # of external_attr (Unix mode). RAR members expose is_symlink().
                mode = (getattr(info, 'external_attr', 0) >> 16) & 0o170000
                is_link = getattr(info, 'is_symlink', None)
                if mode == 0o120000 or (callable(is_link) and is_link()):
                    return False, f"Symlink in archive is not allowed: {name}"
                if getattr(info, 'file_redir', None) is not None:
                    if suspicious is not None:
                        suspicious.note('member', name, ['link or file redirection'])
                    return False, f"Link or file-redirection member is not allowed: {name}"
                needs_password = getattr(info, 'needs_password', None)
                if callable(needs_password) and needs_password():
                    return False, "Encrypted archive members are not supported"
                if name.endswith('/') or getattr(info, 'is_dir', lambda: False)():
                    continue
                total_uncompressed += getattr(info, 'file_size', 0) or 0
                total_compressed += getattr(info, 'compress_size', 0) or 0
                filelist.append(name)

            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                return False, (f"Archive expands to {total_uncompressed} bytes, "
                               f"over the {MAX_UNCOMPRESSED_BYTES} byte limit")
            zero_packed_data = total_compressed == 0 and total_uncompressed > 0
            if zero_packed_data or total_uncompressed / max(total_compressed, 1) > MAX_COMPRESSION_RATIO:
                return False, (f"Suspicious compression ratio "
                               f"{total_uncompressed // max(total_compressed, 1)}:1 "
                               f"(maximum {MAX_COMPRESSION_RATIO}:1)")
        elif len(filelist) > MAX_ARCHIVE_MEMBERS:
            return False, (f"Too many files in archive: {len(filelist)} "
                           f"(maximum {MAX_ARCHIVE_MEMBERS})")

        # Check each entry in the archive
        for fname in filelist:
            if fname.endswith('/'):  # Skip directories
                continue

            if not is_safe_filename(fname):
                return False, f"Unsafe filename in archive: {fname}"

            file_ext = os.path.splitext(fname)[1].lower()
            if file_ext in ALLOWED_IMAGE_EXTENSIONS:
                image_files.append(fname)
            else:
                return False, f"Unrecognized file extension in archive: {fname} {file_ext}"

        if len(image_files) < MIN_IMAGE_FILES:
            return False, f"Not enough image files found. Minimum required: {MIN_IMAGE_FILES}"

        return True, ""

    except Exception as e:
        if isinstance(e, RarValidationError):
            return False, f"Cannot validate RAR archive: {e}"
        elif ext == '.zip' and HAVE_ZIPFILE and isinstance(e, zipfile.BadZipFile):
            return False, f"Invalid ZIP format: {str(e)}"
        elif ext == '.rar' and HAVE_RARFILE and isinstance(e, rarfile.BadRarFile):
            return False, f"Invalid RAR format: {str(e)}"
        return False, f"Error checking archive: {str(e)}"


def secure_upload_handler(form: cgi.FieldStorage, upload_dir: str) -> Tuple[bool, str, str, str]:
    """
    Handle file upload with security checks
    Returns: (success, message, dirname, saved_filepath). saved_filepath is the
    absolute-or-relative path of the file actually written to disk under the
    SANITIZED name; the wrapper must be launched with this path, never with a
    name reconstructed from the raw multipart filename (which is attacker
    controlled and would allow shell command injection).

    Every suspicious file name met on the way - the name of each file part of
    the request and of each archive member - is appended to the suspicious
    filename log together with the outcome, even if the handler fails with
    an exception.
    """
    found = SuspiciousFilenames()
    result = (False, "Upload error: unexpected exception", "", "")
    try:
        result = _handle_upload(form, upload_dir, found)
    finally:
        log_suspicious_filenames(found, result)
    return result


def _file_parts(form):
    """The parts of a parsed request that carry a file name, at any depth"""
    for part in getattr(form, 'list', None) or []:
        if getattr(part, 'filename', None):
            yield part
        yield from _file_parts(part)


def _handle_upload(form: cgi.FieldStorage, upload_dir: str,
                   found: SuspiciousFilenames) -> Tuple[bool, str, str, str]:
    """secure_upload_handler() without the logging"""
    dirname = filepath = ''
    directory_created = False
    try:
        # Note the name of every file part, not only of the 'file' field read
        # below: a second 'file' part, another field name or a nested
        # multipart/mixed part must not keep a name out of the log
        for part in _file_parts(form):
            found.note('upload_name', part.filename, upload_name_problems(part.filename))

        # Get the uploaded file
        try:
            fileitem = form['file']
        except KeyError:
            return False, "No file uploaded", "", ""
        if isinstance(fileitem, list):
            return False, "Please upload exactly one archive", "", ""
        if not fileitem.filename:
            return False, "No file uploaded", "", ""

        # Reject names that cannot designate a regular file before creating
        # anything. Sanitization produces ASCII, so 255 characters also fit
        # the filesystem's 255-byte NAME_MAX. Do not truncate the extension.
        filename = re.sub(r'[^a-zA-Z0-9._-]', '_', os.path.basename(fileitem.filename))
        if filename in ('', '.', '..'):
            return False, "Invalid upload filename", "", ""
        if len(filename) > 255:
            return False, "Upload filename exceeds 255 characters", "", ""
        found.archive = filename

        # Generate secure directory name
        pid = os.getpid()
        random_str = ''.join(secrets.choice(string.ascii_letters)
                             for _ in range(8))
        dirname = os.path.join(upload_dir, f'web_upload_{pid}{random_str}/')
        found.upload_dir = dirname

        # Create upload directory
        os.mkdir(dirname, mode=0o750)
        directory_created = True

        # Save file with sanitized name
        filepath = os.path.join(dirname, filename)

        # Write file in chunks with size validation
        total_size = 0
        with open(filepath, 'wb') as f:
            while True:
                chunk = fileitem.file.read(8192)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > MAX_FILE_SIZE:
                    os.unlink(filepath)
                    os.rmdir(dirname)
                    return False, f"File too large. Maximum size: {MAX_FILE_SIZE / (1024 * 1024)}MB", "", ""
                f.write(chunk)

        if not validate_archive_size(total_size):
            note_rejected_archive_members(found, filepath)
            os.unlink(filepath)
            os.rmdir(dirname)
            return False, f"File size ({total_size / (1024 * 1024):.1f}MB) outside allowed range", "", ""

        # Validate archive type
        valid, error_msg = validate_archive_type(filepath)
        if not valid:
            note_rejected_archive_members(found, filepath)
            os.unlink(filepath)
            os.rmdir(dirname)
            return False, error_msg, "", ""

        # Check archive contents
        valid, error_msg = check_archive_contents(filepath, found)
        if not valid:
            note_rejected_archive_members(found, filepath, tried=os.path.splitext(filepath)[1].lower())
            os.unlink(filepath)
            os.rmdir(dirname)
            return False, error_msg, "", ""

        return True, "File uploaded and validated successfully", dirname, filepath

    except Exception as e:
        _to_error_log(f"upload failed: {e!r}")
        # Only clean a directory we created. A collision or mkdir failure
        # must never delete an existing upload, and cleanup must not replace
        # the original failure with an uncaught IsADirectoryError.
        if directory_created:
            try:
                if filepath and os.path.lexists(filepath):
                    os.unlink(filepath)
                os.rmdir(dirname)
            except OSError as cleanup_error:
                _to_error_log(f"upload cleanup failed: {cleanup_error!r}")
        return False, "Unable to save or validate the upload", "", ""


class LimitedRequestBody:
    """A binary CGI input stream that cannot read past Content-Length.

    cgi's own limit is not applied to every multipart read, notably while
    seeking a boundary. Unsized readline calls collect headers/preambles;
    cap their combined size too. File payloads use sized, 64 KiB reads.
    """
    def __init__(self, stream, length):
        self.stream = stream
        self.remaining = length
        self.header_remaining = MAX_MULTIPART_HEADER_BYTES

    def _read(self, method, size):
        if self.remaining <= 0:
            return b''
        if size < 0 or size > self.remaining:
            size = self.remaining
        data = method(size)
        self.remaining -= len(data)
        return data

    def read(self, size=-1):
        # A nested URL-encoded part can ask FieldStorage to read the entire
        # remaining body into memory. Ordinary file parts use small reads.
        requested = self.remaining if size < 0 else min(size, self.remaining)
        if requested > MAX_MULTIPART_HEADER_BYTES:
            raise ValueError('Multipart parser requested an oversized read')
        return self._read(self.stream.read, size)

    def readline(self, size=-1):
        if size < 0:
            data = self._read(self.stream.readline, self.header_remaining + 1)
            self.header_remaining -= len(data)
            if self.header_remaining < 0:
                raise ValueError('Multipart headers or preamble are too large')
            return data
        return self._read(self.stream.readline, size)


_response_started = False


def _response_headers(status=None, content_type='text/html; charset=utf-8'):
    global _response_started
    if not _response_started:
        if status:
            print(f'Status: {status}')
        print(f'Content-Type: {content_type}')
        print('X-Content-Type-Options: nosniff\n')
        _response_started = True


def upload_error(message, status=None):
    """Keep the camera client's failure marker, escape every displayed value."""
    _response_headers(status)
    print(f'<html><body>UNMW_STATUS:ERROR {html.escape(str(message))}</body></html>')


def main():
    global _response_started
    _response_started = False
    try:
        _main()
    except Exception as error:
        # cgitb can disclose locals, environment secrets, and even with
        # display=0 the path of its report. Keep failures in the server log.
        _to_error_log(f'unexpected upload failure: {error!r}')
        upload_error('Unable to handle the upload', '500 Internal Server Error')
        sys.exit(1)


def _main():
    upload_dir = 'uploads'
    request_method = os.environ.get('REQUEST_METHOD', 'POST')

    # ---- GET: preflight disk/load status check for astrocam-go ----
    if request_method == 'GET':
        # Check system load
        try:
            with open('/proc/loadavg', 'r') as f:
                load = float(f.readline().split()[1])
                if load > 50.0:
                    print("Status: 503 Service Unavailable")
                    print("Content-Type: text/plain\n")
                    print(f"UNMW_STATUS:ERROR system load too high: {load}")
                    sys.exit(0)
        except Exception:
            pass  # If we can't check load, continue to disk check

        # Check disk space
        try:
            if not os.path.exists(upload_dir):
                print("Status: 500 Internal Server Error")
                print("Content-Type: text/plain\n")
                print("UNMW_STATUS:ERROR upload directory missing")
                sys.exit(0)

            status, message = check_disk_space_status(upload_dir)
            if status == "ERROR":
                print("Status: 507 Insufficient Storage")
                print("Content-Type: text/plain\n")
                print(f"UNMW_STATUS:ERROR {message}")
            elif status == "WARNING":
                print("Content-Type: text/plain\n")
                print(f"UNMW_STATUS:WARNING {message}")
            else:
                print("Content-Type: text/plain\n")
                print("UNMW_STATUS:OK")
        except Exception as e:
            _to_error_log(f'preflight disk check failed: {e!r}')
            print("Status: 500 Internal Server Error")
            print("Content-Type: text/plain\n")
            print("UNMW_STATUS:ERROR failed to check disk space")
        sys.exit(0)

    # ---- POST: file upload handling ----
    if request_method != 'POST':
        upload_error('Use POST to upload an archive', '405 Method Not Allowed')
        sys.exit(1)

    raw_length = os.environ.get('CONTENT_LENGTH', '')
    if not raw_length:
        upload_error('Content-Length is required', '411 Length Required')
        sys.exit(1)
    if not re.fullmatch(r'[0-9]{1,20}', raw_length):
        upload_error('Invalid Content-Length', '400 Bad Request')
        sys.exit(1)
    content_length = int(raw_length)
    if content_length > MAX_REQUEST_SIZE:
        upload_error('Upload request is too large', '413 Content Too Large')
        sys.exit(1)
    content_type = os.environ.get('CONTENT_TYPE', '').partition(';')[0].strip().lower()
    if content_type != 'multipart/form-data':
        upload_error('Use multipart/form-data to upload an archive', '415 Unsupported Media Type')
        sys.exit(1)
    if len(os.environ.get('QUERY_STRING', '')) > MAX_MULTIPART_HEADER_BYTES:
        upload_error('Query string is too large', '400 Bad Request')
        sys.exit(1)

    # Check system load
    try:
        with open('/proc/loadavg', 'r') as f:
            load = float(f.readline().split()[1])
            if load > 50.0:
                upload_error(f'system load too high: {load}', '503 Service Unavailable')
                sys.exit(1)
    except Exception as e:
        _to_error_log(f'load check failed: {e!r}')
        upload_error('failed to check system load', '500 Internal Server Error')
        sys.exit(1)

    # Check upload directory and disk space
    try:
        if not os.path.exists(upload_dir):
            upload_error('upload directory missing', '500 Internal Server Error')
            sys.exit(1)

        status, message = check_disk_space_status(upload_dir)
        if status == "ERROR":
            _to_error_log(message)
            upload_error('Insufficient disk space', '507 Insufficient Storage')
            sys.exit(1)
    except Exception as e:
        _to_error_log(f'upload directory check failed: {e!r}')
        upload_error('failed to check upload directory', '500 Internal Server Error')
        sys.exit(1)

    _response_headers()

    # Handle upload
    cgi.maxlen = MAX_REQUEST_SIZE
    request_body = LimitedRequestBody(sys.stdin.buffer, content_length)
    try:
        form = cgi.FieldStorage(fp=request_body, limit=content_length,
                                max_num_fields=100)
    except (ValueError, TypeError, EOFError) as error:
        _to_error_log(f'invalid multipart request: {error!r}')
        upload_error('Invalid upload request')
        sys.exit(1)
    success, message, dirname, saved_filepath = secure_upload_handler(form, upload_dir)

    if not success:
        # Headers (HTTP 200) were already sent above, so the failure is signaled
        # in-body with UNMW_STATUS:ERROR; the client treats any such body as a
        # failed upload and keeps the local archive for retry.
        upload_error(message)
        sys.exit(1)

    # Run processing
    if dirname:
        # Log upload details
        with open(os.path.join(dirname, 'upload.log'), 'a') as listing_log:
            subprocess.run(['ls', '-lh', '--', saved_filepath],
                           stdout=listing_log, stderr=listing_log, check=False)
        
        # Get the current working directory - for debugging
        cwd = os.getcwd()
        
        # Debug: log environment and paths
        debug_log = os.path.join(dirname, 'upload.log')
        with open(debug_log, 'a') as f:
            f.write("=== upload.py3 ===\n")
            f.write(f"CWD: {cwd}\n")
            f.write(f"wrapper.sh exists: {os.path.isfile('./wrapper.sh')}\n")
            f.write(f"wrapper.sh executable: {os.access('./wrapper.sh', os.X_OK)}\n")
            f.write(f"Full wrapper path: {os.path.abspath('./wrapper.sh')}\n")
            f.write(f"dirname: {dirname}\n")
            f.write(f"Command: ./wrapper.sh {saved_filepath}\n")

        # Check if ./wrapper.sh exists in the current directory
        if os.path.isfile('./wrapper.sh'):
            # Run the processing wrapper with the SANITIZED saved path passed as
            # a single argv element (no shell). Never rebuild the path from the
            # raw multipart filename (form['file'].filename): it is attacker
            # controlled, and interpolating it into a shell command allowed
            # command injection. subprocess.call returns the wrapper's real exit
            # code (0 success, 1 failure) -- unlike os.system, which returned a
            # wait-status where 'exit 1' shows up as 256.
            # Strip the CGI request markers from the child's environment, the
            # same way kick_worker() does for archive_phot_worker.py. This is a
            # legitimate, already-validated invocation from inside a request, but
            # wrapper.sh refuses to run when it sees GATEWAY_INTERFACE - and a
            # subprocess inherits our environment, so without this the guard
            # would fire on every upload.
            child_env = os.environ.copy()
            # The wrapper reuses this validator, including in a virtualenv.
            child_env['UNMW_UPLOAD_PYTHON'] = sys.executable
            for cgi_var in ('GATEWAY_INTERFACE', 'REQUEST_METHOD', 'QUERY_STRING',
                            'CONTENT_LENGTH', 'CONTENT_TYPE'):
                child_env.pop(cgi_var, None)
            try:
                exit_status = subprocess.call(['./wrapper.sh', saved_filepath],
                                              env=child_env)
            except Exception as e:
                _to_error_log(f'error launching processing wrapper: {e!r}')
                exit_status = 1
        else:
            _to_error_log('processing wrapper is missing')
            exit_status = 1

        # Check exit status of wrapper.sh (real exit code from subprocess.call)
        if exit_status != 0:
            # Cleanup on failure
            _to_error_log(f'processing wrapper failed with status {exit_status}')
            upload_error('Error during processing')
            try:
                for root, dirs, files in os.walk(dirname, topdown=False):
                    for file in files:
                        os.unlink(os.path.join(root, file))
                    for directory in dirs:
                        os.rmdir(os.path.join(root, directory))
                os.rmdir(dirname)
            except Exception as e:
                _to_error_log(f'processing cleanup failed: {e!r}')
                sys.exit(1)
            sys.exit(1)
        # otherwise autoprocess.sh should delete the input after it completes
        
        # Wait for autoprocess.sh to create results_url.txt
        # autoprocess.sh will keep running while wrapper.sh exits
        #
        # sthttpd (and others?) have a 30 sec timeout for the cgi script to start printing stuff
        #
        # Let's start printing something - maybe that'll make the web server wait
        print(" ")
        # NOTE that results_url.txt should not be deleted with the folder containing it by autoprocess.sh
        # before upload.py gets a chance to read it! autoprocess.sh may exit very fast on error.
        time.sleep(1)
        results_url = None
        for _ in range(24):
            if os.path.isfile(dirname + "results_url.txt"):
                with open(dirname + "results_url.txt") as f:
                    results_url = f.readline().strip()
                break
            time.sleep(1)

        # If results_url.txt was never created 
        # - point uset to the upload directory where it should appear,
        # where it may appear... eventually.
        if not results_url:
            results_url = f'http://{socket.getfqdn()}/unmw/{dirname}'

        print(f"""
        <html>
        <head>
        <meta http-equiv="Refresh" content="0; url={html.escape(results_url, quote=True)}">
        </head>
        <body>
        <!-- UNMW_STATUS:OK -->
        <p>Upload successful. Redirecting to results...</p>
        </body>
        </html>
        """)


if __name__ == "__main__":
    is_cgi = os.environ.get('GATEWAY_INTERFACE') or os.environ.get('REQUEST_METHOD')
    if len(sys.argv) == 3 and sys.argv[1] == '--validate-archive' and not is_cgi:
        # Local wrapper re-check: no CGI parsing, logging, or extraction.
        valid, message = validate_archive_type(sys.argv[2])
        if valid:
            valid, message = check_archive_contents(sys.argv[2])
        if not valid:
            print('Archive validation failed: ' + ascii(message))
        sys.exit(0 if valid else 1)
    else:
        main()

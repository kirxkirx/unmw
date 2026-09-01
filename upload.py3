#!/usr/bin/env python3

# Handle cgi module removal in Python 3.13+
# The 'cgi' and 'cgitb' modules were removed from stdlib in Python 3.13.
# The 'legacy-cgi' package provides these modules for Python 3.13+.
# Install with: pip install legacy-cgi
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
try:
    import cgi
    import cgitb
except ImportError:
    import sys
    sys.exit("Error: 'cgi' module not found. "
             "For Python 3.13+, install: pip install legacy-cgi")
import os
import random
import string
import subprocess
import time
import sys
import socket
import pwd
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
from typing import Tuple


# Constants for file validation
MIN_FILE_SIZE = 2 * 1024 * 1024  # 2MB
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
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
        return "OK", f"server {hostname} has sufficient free disk space available: {free_mb} MB at {directory}"
    elif free_kb >= hardlimit_kb:
        return "WARNING", f"WARNING: server {hostname} is low on disk space, only {free_mb} MB free at {directory}"
    else:
        return "ERROR", f"ERROR: server {hostname} is out of disk space, only {free_mb} MB free at {directory}"


def is_safe_filename(filename: str) -> bool:
    """
    Check if filename is safe - no path traversal, no special chars
    """
    # Remove any directory components, keep just filename
    filename = os.path.basename(filename)

    # Check for suspicious patterns
    dangerous_patterns = [
        r'\.\.',           # Path traversal
        r'^\..*$',         # Hidden files
        r'[<>:"|?*]',     # Windows special chars
        r'[;&|`$]',       # Shell special chars
        r'[^\w\-\.]'      # Only allow alphanumeric, dash, dot
    ]

    return all(not re.search(pattern, filename) for pattern in dangerous_patterns)


def validate_archive_size(filesize: int) -> bool:
    """
    Validate archive file size is within acceptable range
    """
    return MIN_FILE_SIZE <= filesize <= MAX_FILE_SIZE


def get_mime_type(filepath: str) -> str:
    """
    Get MIME type of file using python-magic, handling different implementations
    """
    try:
        import magic
        # Try python-magic implementation
        try:
            # Try using mime=True parameter
            mime = magic.Magic(mime=True)
            return mime.from_file(filepath)
        except:
            # Fall back to older python-magic API
            mime = magic.open(magic.MAGIC_MIME_TYPE)
            mime.load()
            return mime.file(filepath)
    except:
        try:
            # Try direct use of the magic module
            return magic.from_file(filepath, mime=True)
        except:
            # Last resort: try to get MIME type without python-magic
            import mimetypes
            mtype, _ = mimetypes.guess_type(filepath)
            if mtype:
                return mtype
            return "application/octet-stream"  # Default MIME type


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


def rar_member_names_via_binary(filepath: str):
    """Bare member listing from the rar/unrar binary, for the case where the
    Python rarfile module is not installed (it is not, in the production venv).
    Without this the .rar branch would have to either skip validation entirely
    or reject every RAR upload, and the uploading client (astrocam-go) emits
    both .zip and .rar. Returns a list of names, or None if no usable binary.
    'lb' is the bare-list mode: one member name per line, nothing else."""
    for binary in ('rar', 'unrar', '/opt/bin/rar', '/opt/bin/unrar'):
        try:
            proc = subprocess.run([binary, 'lb', filepath],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,
                                  timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            # A non-zero exit is a real verdict from a working binary (corrupt
            # archive, unsafe link member): treat it as "no usable listing"
            # so the caller fails closed rather than trusting a partial list.
            return None
        text = proc.stdout.decode('utf-8', 'replace')
        return [line.strip() for line in text.splitlines() if line.strip()]
    return None


def check_archive_contents(filepath: str) -> Tuple[bool, str]:
    """
    Validate archive contents without extracting.
    Directories are allowed; only file extensions are checked.

    Beyond names this enforces the decompression-bomb limits and rejects
    symlink members, which a name-only check cannot see: Info-ZIP recreates a
    symlink member even under 'unzip -j', and a later 'chmod' or 'find' that
    follows it would act outside the upload directory.
    """
    ext = os.path.splitext(filepath)[1].lower()
    image_files = []
    total_uncompressed = 0
    total_compressed = 0
    infolist = None
    filelist = None

    if ext == '.zip' and not HAVE_ZIPFILE:
        return False, "Cannot validate ZIP archive: the zipfile module is unavailable"

    try:
        if ext == '.zip' and HAVE_ZIPFILE:
            with zipfile.ZipFile(filepath) as zf:
                infolist = zf.infolist()
        elif ext == '.rar' and HAVE_RARFILE:
            with rarfile.RarFile(filepath) as rf:
                infolist = rf.infolist()
        elif ext == '.rar':
            filelist = rar_member_names_via_binary(filepath)
            if filelist is None:
                return False, ("Cannot validate RAR archive: neither the rarfile "
                               "module nor a working rar/unrar binary is available")
        else:
            return False, f"Unsupported archive type: {ext}"

        if infolist is not None:
            if len(infolist) > MAX_ARCHIVE_MEMBERS:
                return False, (f"Too many files in archive: {len(infolist)} "
                               f"(maximum {MAX_ARCHIVE_MEMBERS})")
            filelist = []
            for info in infolist:
                name = info.filename
                if name.endswith('/') or getattr(info, 'is_dir', lambda: False)():
                    continue
                # Symlink members carry file type 0o120000 in the high 16 bits
                # of external_attr (Unix mode). RAR members expose is_symlink().
                mode = (getattr(info, 'external_attr', 0) >> 16) & 0o170000
                is_link = getattr(info, 'is_symlink', None)
                if mode == 0o120000 or (callable(is_link) and is_link()):
                    return False, f"Symlink in archive is not allowed: {name}"
                total_uncompressed += getattr(info, 'file_size', 0) or 0
                total_compressed += getattr(info, 'compress_size', 0) or 0
                filelist.append(name)

            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                return False, (f"Archive expands to {total_uncompressed} bytes, "
                               f"over the {MAX_UNCOMPRESSED_BYTES} byte limit")
            if total_compressed > 0 and total_uncompressed / total_compressed > MAX_COMPRESSION_RATIO:
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
        if ext == '.zip' and HAVE_ZIPFILE and isinstance(e, zipfile.BadZipFile):
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
    """
    try:
        # Get the uploaded file
        fileitem = form['file']
        if not fileitem.filename:
            return False, "No file uploaded", "", ""

        # Generate secure directory name
        pid = os.getpid()
        random_str = ''.join(random.choice(string.ascii_letters)
                             for _ in range(8))
        dirname = os.path.join(upload_dir, f'web_upload_{pid}{random_str}/')

        # Create upload directory
        try:
            os.makedirs(dirname, mode=0o750)  # Restrictive permissions
        except PermissionError as e:
            user_info = pwd.getpwuid(os.getuid())
            return False, f"Permission error creating directory. Running as {user_info.pw_name}. Exception: {e}", "", ""

        # Save file with sanitized name
        filename = os.path.basename(fileitem.filename)[:256]
        filename = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
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
            os.unlink(filepath)
            os.rmdir(dirname)
            return False, f"File size ({total_size / (1024 * 1024):.1f}MB) outside allowed range", "", ""

        # Validate archive type
        valid, error_msg = validate_archive_type(filepath)
        if not valid:
            os.unlink(filepath)
            os.rmdir(dirname)
            return False, error_msg, "", ""

        # Check archive contents
        valid, error_msg = check_archive_contents(filepath)
        if not valid:
            os.unlink(filepath)
            os.rmdir(dirname)
            return False, error_msg, "", ""

        return True, "File uploaded and validated successfully", dirname, filepath

    except Exception as e:
        if 'dirname' in locals() and os.path.exists(dirname):
            if 'filepath' in locals() and os.path.exists(filepath):
                os.unlink(filepath)
            os.rmdir(dirname)
        return False, f"Upload error: {str(e)}", "", ""


def main():

    # CGI error reporting. display=0 keeps the traceback - which carries source
    # lines, frame locals and the whole CGI environment, including any secret
    # sourced from local_config.sh - out of the HTTP response; logdir keeps it
    # on disk for debugging. The log directory is deliberately NOT under the
    # web-served uploads tree.
    cgitb_logdir = os.environ.get('UNMW_CGITB_LOGDIR', '/tmp')
    try:
        cgitb.enable(display=0, logdir=cgitb_logdir)
    except Exception:
        # Never let error reporting itself break the upload handler.
        cgitb.enable(display=0)

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
            print("Status: 500 Internal Server Error")
            print("Content-Type: text/plain\n")
            print(f"UNMW_STATUS:ERROR failed to check disk space: {e}")
        sys.exit(0)

    # ---- POST: file upload handling ----

    # Check system load
    try:
        with open('/proc/loadavg', 'r') as f:
            load = float(f.readline().split()[1])
            if load > 50.0:
                print("Status: 503 Service Unavailable")
                print("Content-Type: text/html\n")
                print(f"<html><body>UNMW_STATUS:ERROR system load too high: {load}</body></html>")
                sys.exit(1)
    except Exception as e:
        print("Status: 500 Internal Server Error")
        print("Content-Type: text/html\n")
        print(f"<html><body>UNMW_STATUS:ERROR failed to check system load: {e}</body></html>")
        sys.exit(1)

    # Check upload directory and disk space
    try:
        if not os.path.exists(upload_dir):
            print("Status: 500 Internal Server Error")
            print("Content-Type: text/html\n")
            print("<html><body>UNMW_STATUS:ERROR upload directory missing</body></html>")
            sys.exit(1)

        status, message = check_disk_space_status(upload_dir)
        if status == "ERROR":
            print("Status: 507 Insufficient Storage")
            print("Content-Type: text/html\n")
            print(f"<html><body>UNMW_STATUS:ERROR Insufficient disk space: {message}</body></html>")
            sys.exit(1)
    except Exception as e:
        print("Status: 500 Internal Server Error")
        print("Content-Type: text/html\n")
        print(
            f"<html><body>UNMW_STATUS:ERROR failed to check upload directory: {e}</body></html>")
        sys.exit(1)

    print("Content-Type: text/html\n")

    # Handle upload
    form = cgi.FieldStorage()
    success, message, dirname, saved_filepath = secure_upload_handler(form, upload_dir)

    if not success:
        # Headers (HTTP 200) were already sent above, so the failure is signaled
        # in-body with UNMW_STATUS:ERROR; the client treats any such body as a
        # failed upload and keeps the local archive for retry.
        print(f"<html><body>UNMW_STATUS:ERROR {message}</body></html>")
        sys.exit(1)

    # Run processing
    if dirname:
        # Log upload details
        os.system(f'ls -lh {dirname}* >> {dirname}upload.log')
        
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
            try:
                exit_status = subprocess.call(['./wrapper.sh', saved_filepath])
            except Exception as e:
                print(f"<html><body>UNMW_STATUS:ERROR Error running wrapper.sh command: {e}<br>Current working directory: {cwd}</body></html>")
                exit_status = 1
        else:
            print(f"<html><body>UNMW_STATUS:ERROR ./wrapper.sh does not exist!<br>Current working directory: {cwd}</body></html>")
            exit_status = 1

        # Check exit status of wrapper.sh (real exit code from subprocess.call)
        if exit_status != 0:
            # Cleanup on failure
            print(f"<html><body>UNMW_STATUS:ERROR Error during processing.<br>./wrapper.sh {saved_filepath}<br>Exit status {exit_status}<br>Current working directory: {cwd}<br>Cleaning up...</body></html>")
            try:
                for root, dirs, files in os.walk(dirname, topdown=False):
                    for file in files:
                        os.unlink(os.path.join(root, file))
                    for directory in dirs:
                        os.rmdir(os.path.join(root, directory))
                os.rmdir(dirname)
            except Exception as e:
                print(f"<html><body>UNMW_STATUS:ERROR Error during cleanup: {e}</body></html>")
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
        <meta http-equiv="Refresh" content="0; url={results_url}">
        </head>
        <body>
        <!-- UNMW_STATUS:OK -->
        <p>Upload successful. Redirecting to results...</p>
        </body>
        </html>
        """)


if __name__ == "__main__":
    main()

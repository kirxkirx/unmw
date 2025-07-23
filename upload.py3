#!/usr/bin/env python3

# The DeprecationWarning about 'import cgi' will corrput mime
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
import cgi
import os
import cgitb
import random
import string
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
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB
ALLOWED_EXTENSIONS = {'.zip', '.rar'}
ALLOWED_IMAGE_EXTENSIONS = {'.fit', '.fits', '.fts'}
MIN_IMAGE_FILES = 2


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


def check_archive_contents(filepath: str) -> Tuple[bool, str]:
    """
    Validate archive contents without extracting.
    Directories are allowed; only file extensions are checked.
    """
    ext = os.path.splitext(filepath)[1].lower()
    image_files = []

    # If neither library is available, perform basic size and MIME checks only
    if ext == '.zip' and not HAVE_ZIPFILE:
        return True, "Warning: zipfile module not available, skipping detailed archive validation"
    elif ext == '.rar' and not HAVE_RARFILE:
        return True, "Warning: rarfile module not available, skipping detailed archive validation"

    try:
        if ext == '.zip' and HAVE_ZIPFILE:
            with zipfile.ZipFile(filepath) as zf:
                filelist = zf.namelist()
        elif ext == '.rar' and HAVE_RARFILE:
            with rarfile.RarFile(filepath) as rf:
                filelist = rf.namelist()
        else:
            return False, f"Unsupported archive type: {ext}"

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
        if ext == '.zip' and isinstance(e, zipfile.BadZipFile):
            return False, f"Invalid ZIP format: {str(e)}"
        elif ext == '.rar' and isinstance(e, rarfile.BadRarFile):
            return False, f"Invalid RAR format: {str(e)}"
        return False, f"Error checking archive: {str(e)}"


def secure_upload_handler(form: cgi.FieldStorage, upload_dir: str) -> Tuple[bool, str, str]:
    """
    Handle file upload with security checks
    Returns: (success, message, dirname)
    """
    try:
        # Get the uploaded file
        fileitem = form['file']
        if not fileitem.filename:
            return False, "No file uploaded", ""

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
            return False, f"Permission error creating directory. Running as {user_info.pw_name}. Exception: {e}", ""

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
                    return False, f"File too large. Maximum size: {MAX_FILE_SIZE / (1024 * 1024)}MB", ""
                f.write(chunk)

        if not validate_archive_size(total_size):
            os.unlink(filepath)
            os.rmdir(dirname)
            return False, f"File size ({total_size / (1024 * 1024):.1f}MB) outside allowed range", ""

        # Validate archive type
        valid, error_msg = validate_archive_type(filepath)
        if not valid:
            os.unlink(filepath)
            os.rmdir(dirname)
            return False, error_msg, ""

        # Check archive contents
        valid, error_msg = check_archive_contents(filepath)
        if not valid:
            os.unlink(filepath)
            os.rmdir(dirname)
            return False, error_msg, ""

        return True, "File uploaded and validated successfully", dirname

    except Exception as e:
        if 'dirname' in locals() and os.path.exists(dirname):
            if 'filepath' in locals() and os.path.exists(filepath):
                os.unlink(filepath)
            os.rmdir(dirname)
        return False, f"Upload error: {str(e)}", ""


def main():

    # Enable CGI error reporting
    cgitb.enable()

    print("Content-Type: text/html\n")

    # Check system load
    try:
        with open('/proc/loadavg', 'r') as f:
            load = float(f.readline().split()[1])
            if load > 50.0:
                print("<html><body>System load too high</body></html>")
                sys.exit(1)
    except Exception as e:
        print(f"<html><body>Error checking system load: {e}</body></html>")
        sys.exit(1)

    # Check upload directory
    upload_dir = 'uploads'
    try:
        if not os.path.exists(upload_dir):
            print("<html><body>Upload directory missing</body></html>")
            sys.exit(1)

        st = os.statvfs(os.path.realpath(upload_dir))
        free_space = st.f_bavail * st.f_frsize
        if free_space < 1024 * 1024 * 1024:  # 1GB
            print("<html><body>Insufficient disk space</body></html>")
            sys.exit(1)
    except Exception as e:
        print(
            f"<html><body>Error checking upload directory: {e}</body></html>")
        sys.exit(1)

    # Handle upload
    form = cgi.FieldStorage()
    success, message, dirname = secure_upload_handler(form, upload_dir)

    if not success:
        print(f"<html><body>{message}</body></html>")
        sys.exit(1)

    # Run processing
    if dirname:
        # Handle email notifications
        if form.getvalue('workstartemail'):
            os.system(f'touch {dirname}workstartemail')
        if form.getvalue('workendemail'):
            os.system(f'touch {dirname}workendemail')

        # Log upload details
        os.system(f'ls -lh {dirname}* > {dirname}upload.log')

        # Get the current working directory - for debugging
        cwd = os.getcwd()
        
        # Check if ./wrapper.sh exists in the current directory
        if os.path.isfile('./wrapper.sh'):
            # Run processing wrapper
            wrapper_command = f'./wrapper.sh {dirname}{os.path.basename(form["file"].filename)}'
            try:
                exit_status = os.system(wrapper_command)
            except Exception as e:
                print(f"<html><body>Error running wrapper.sh command: {e}<br>Current working directory: {cwd}</body></html>")
        else:
            print(f"<html><body>./wrapper.sh does not exist!<br>Current working directory: {cwd}</body></html>")
            exit_status = 1

        # Check exit status of wrapper.sh
        # 256 wrap around to 0
        # An exit code of 256 is equivalent to 256 % 256 = 0.
        # This means the actual exit code might have been 0, but a mistake in interpreting the value or truncation occurred.
        if exit_status != 0 and exit_status != 256:
            # Cleanup on failure
            print(f"<html><body>Error during processing.<br>./wrapper.sh {dirname}{os.path.basename(form['file'].filename)}<br>Exit status {exit_status}<br>Current working directory: {cwd}<br>Cleaning up...</body></html>")
            try:
                for root, dirs, files in os.walk(dirname, topdown=False):
                    for file in files:
                        os.unlink(os.path.join(root, file))
                    for directory in dirs:
                        os.rmdir(os.path.join(root, directory))
                os.rmdir(dirname)
            except Exception as e:
                print(f"<html><body>Error during cleanup: {e}</body></html>")
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
        <p>Upload successful. Redirecting to results...</p>
        </body>
        </html>
        """)


if __name__ == "__main__":
    main()

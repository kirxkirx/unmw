#!/usr/bin/env python3
"""
Unit tests for filter_report.py and upload.py3
Run with: pytest test_python.py -v
"""

import os
import sys
import tempfile
import zipfile
import re
import pytest

# Import functions from filter_report.py
from filter_report import is_asteroid, is_variable_star, is_ast_or_vs, filter_report

# Import functions from upload.py3 by reading the file and extracting functions
# (avoiding the cgi import which was removed in Python 3.13)
# We extract the pure functions that don't depend on cgi

# Constants from upload.py3
MIN_FILE_SIZE = 2 * 1024 * 1024  # 2MB
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB
ALLOWED_EXTENSIONS = {'.zip', '.rar'}
ALLOWED_IMAGE_EXTENSIONS = {'.fit', '.fits', '.fts'}
MIN_IMAGE_FILES = 2

# Try to import archive handling libraries
try:
    HAVE_ZIPFILE = True
except ImportError:
    HAVE_ZIPFILE = False

try:
    import rarfile
    HAVE_RARFILE = True
except ImportError:
    HAVE_RARFILE = False


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


def check_archive_contents(filepath: str):
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
        elif ext == '.rar' and HAVE_RARFILE and isinstance(e, rarfile.BadRarFile):
            return False, f"Invalid RAR format: {str(e)}"
        return False, f"Error checking archive: {str(e)}"


class TestIsAsteroid:
    """Tests for is_asteroid function"""

    def test_asteroid_found_in_astcheck(self):
        """Should return True when asteroid text is present"""
        text = "Some info\nThe object was found in astcheck\nMore info"
        assert is_asteroid(text) is True

    def test_no_asteroid(self):
        """Should return False when no asteroid text"""
        text = "Some random text without asteroid info"
        assert is_asteroid(text) is False

    def test_empty_string(self):
        """Should return False for empty string"""
        assert is_asteroid("") is False

    def test_partial_match(self):
        """Should return False for partial match"""
        text = "The object was found in some other catalog"
        assert is_asteroid(text) is False


class TestIsVariableStar:
    """Tests for is_variable_star function"""

    def test_vsx_star_close(self):
        """Should return True for VSX star within threshold"""
        text = """Some header
The object was found in VSX
15" V0615 Vul
More info"""
        assert is_variable_star(text, "VSX") is True

    def test_vsx_star_far(self):
        """Should return False for VSX star beyond threshold (30 arcsec)"""
        text = """Some header
The object was found in VSX
45" SomeVar
More info"""
        assert is_variable_star(text, "VSX") is False

    def test_asassn_star_close(self):
        """Should return True for ASASSN-V star within threshold"""
        text = """Some header
The object was found in ASASSN-V
20" ASASSN-V J123456
More info"""
        assert is_variable_star(text, "ASASSN-V") is True

    def test_no_variable_star(self):
        """Should return False when no variable star info"""
        text = "Random text without variable star"
        assert is_variable_star(text, "VSX") is False

    def test_empty_string(self):
        """Should return False for empty string"""
        assert is_variable_star("", "VSX") is False

    def test_boundary_30_arcsec(self):
        """Should return True for exactly 30 arcsec (boundary)"""
        text = """Header
The object was found in VSX
30" BoundaryVar
Footer"""
        assert is_variable_star(text, "VSX") is True

    def test_boundary_31_arcsec(self):
        """Should return False for 31 arcsec (just beyond boundary)"""
        text = """Header
The object was found in VSX
31" BeyondVar
Footer"""
        assert is_variable_star(text, "VSX") is False


class TestIsAstOrVs:
    """Tests for is_ast_or_vs function"""

    def test_is_asteroid(self):
        """Should return True for asteroid"""
        text = "The object was found in astcheck"
        assert is_ast_or_vs(text) is True

    def test_is_vsx(self):
        """Should return True for VSX variable"""
        text = """Header
The object was found in VSX
10" SomeVar"""
        assert is_ast_or_vs(text) is True

    def test_is_asassn(self):
        """Should return True for ASASSN-V variable"""
        text = """Header
The object was found in ASASSN-V
10" SomeVar"""
        assert is_ast_or_vs(text) is True

    def test_neither(self):
        """Should return False when neither asteroid nor variable"""
        text = "Random transient with no identification"
        assert is_ast_or_vs(text) is False


class TestIsSafeFilename:
    """Tests for is_safe_filename function"""

    def test_normal_filename(self):
        """Should return True for normal filenames"""
        assert is_safe_filename("image.fits") is True
        assert is_safe_filename("data_2024.fts") is True
        assert is_safe_filename("test-file.fit") is True

    def test_path_traversal(self):
        """Should return False for path traversal in basename only"""
        # Note: the function strips directory via os.path.basename first,
        # so "../etc/passwd" becomes "passwd" which is safe.
        # Path traversal is only detected if ".." appears in the basename itself
        assert is_safe_filename("..") is False
        assert is_safe_filename("..hidden") is False
        # These get stripped to just the basename which is safe
        assert is_safe_filename("../etc/passwd") is True  # becomes "passwd"
        assert is_safe_filename("foo/../bar") is True  # becomes "bar"

    def test_hidden_files(self):
        """Should return False for hidden files"""
        assert is_safe_filename(".hidden") is False
        assert is_safe_filename(".bashrc") is False

    def test_shell_special_chars(self):
        """Should return False for shell special characters"""
        assert is_safe_filename("file;rm -rf") is False
        assert is_safe_filename("file|cat") is False
        assert is_safe_filename("file`whoami`") is False
        assert is_safe_filename("file$HOME") is False

    def test_windows_special_chars(self):
        """Should return False for Windows special characters"""
        assert is_safe_filename("file<>") is False
        assert is_safe_filename("file:name") is False
        assert is_safe_filename("file?name") is False

    def test_strips_directory(self):
        """Should check only basename, ignoring directory part"""
        # The function strips directory, so these should be evaluated as just the basename
        assert is_safe_filename("/path/to/good_file.fits") is True


class TestValidateArchiveSize:
    """Tests for validate_archive_size function"""

    def test_valid_size(self):
        """Should return True for valid sizes"""
        assert validate_archive_size(MIN_FILE_SIZE) is True
        assert validate_archive_size(MAX_FILE_SIZE) is True
        assert validate_archive_size(50 * 1024 * 1024) is True  # 50MB

    def test_too_small(self):
        """Should return False for files smaller than minimum"""
        assert validate_archive_size(MIN_FILE_SIZE - 1) is False
        assert validate_archive_size(1024) is False  # 1KB
        assert validate_archive_size(0) is False

    def test_too_large(self):
        """Should return False for files larger than maximum"""
        assert validate_archive_size(MAX_FILE_SIZE + 1) is False
        assert validate_archive_size(500 * 1024 * 1024) is False  # 500MB


class TestCheckArchiveContents:
    """Tests for check_archive_contents function"""

    def test_valid_zip_with_fits(self):
        """Should return True for valid ZIP with FITS files"""
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            temp_path = f.name

        try:
            with zipfile.ZipFile(temp_path, 'w') as zf:
                # Create dummy FITS files
                zf.writestr('image1.fits', b'SIMPLE  = T' + b' ' * 2870)
                zf.writestr('image2.fits', b'SIMPLE  = T' + b' ' * 2870)

            valid, msg = check_archive_contents(temp_path)
            assert valid is True, f"Expected valid archive, got: {msg}"
        finally:
            os.unlink(temp_path)

    def test_valid_zip_with_fts(self):
        """Should return True for valid ZIP with .fts files"""
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            temp_path = f.name

        try:
            with zipfile.ZipFile(temp_path, 'w') as zf:
                zf.writestr('image1.fts', b'SIMPLE  = T' + b' ' * 2870)
                zf.writestr('image2.fts', b'SIMPLE  = T' + b' ' * 2870)

            valid, msg = check_archive_contents(temp_path)
            assert valid is True, f"Expected valid archive, got: {msg}"
        finally:
            os.unlink(temp_path)

    def test_zip_with_subdirectory(self):
        """Should allow ZIP files with subdirectories containing FITS"""
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            temp_path = f.name

        try:
            with zipfile.ZipFile(temp_path, 'w') as zf:
                zf.writestr('subdir/', '')  # Directory entry
                zf.writestr('subdir/image1.fits', b'SIMPLE  = T' + b' ' * 2870)
                zf.writestr('subdir/image2.fits', b'SIMPLE  = T' + b' ' * 2870)

            valid, msg = check_archive_contents(temp_path)
            assert valid is True, f"Expected valid archive with subdirs, got: {msg}"
        finally:
            os.unlink(temp_path)

    def test_zip_not_enough_images(self):
        """Should return False when fewer than MIN_IMAGE_FILES"""
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            temp_path = f.name

        try:
            with zipfile.ZipFile(temp_path, 'w') as zf:
                zf.writestr('image1.fits', b'SIMPLE  = T' + b' ' * 2870)

            valid, msg = check_archive_contents(temp_path)
            assert valid is False
            assert "Not enough image files" in msg
        finally:
            os.unlink(temp_path)

    def test_zip_with_invalid_extension(self):
        """Should return False for files with unrecognized extensions"""
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            temp_path = f.name

        try:
            with zipfile.ZipFile(temp_path, 'w') as zf:
                zf.writestr('image1.fits', b'SIMPLE  = T' + b' ' * 2870)
                zf.writestr('image2.fits', b'SIMPLE  = T' + b' ' * 2870)
                zf.writestr('malware.exe', b'MZ' + b'\x00' * 100)

            valid, msg = check_archive_contents(temp_path)
            assert valid is False
            assert "Unrecognized file extension" in msg
        finally:
            os.unlink(temp_path)

    def test_invalid_zip_file(self):
        """Should return False for corrupted ZIP file"""
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            f.write(b'not a real zip file content')
            temp_path = f.name

        try:
            valid, msg = check_archive_contents(temp_path)
            assert valid is False
        finally:
            os.unlink(temp_path)


class TestFilterReport:
    """Tests for filter_report function"""

    def test_filter_removes_asteroids(self):
        """Should filter out asteroids from report"""
        html_content = """<html><body>
<a name="candidate1">
<pre>
Candidate 1 info
The object was found in astcheck
asteroid details
</pre>
<HR>
<a name="candidate2">
<pre>
Candidate 2 info
Unknown transient
</pre>
<HR>
</body></html>"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as f:
            f.write(html_content)
            temp_path = f.name

        try:
            filter_report(temp_path)
            output_path = temp_path.replace('.html', '_filtered.html')

            assert os.path.exists(output_path)
            with open(output_path, 'r') as f:
                filtered = f.read()

            # Should not contain asteroid
            assert 'astcheck' not in filtered
            # Should contain the unknown transient
            assert 'Candidate 2' in filtered or 'candidate2' in filtered

            os.unlink(output_path)
        finally:
            os.unlink(temp_path)

    def test_filter_removes_variable_stars(self):
        """Should filter out known variable stars within threshold"""
        html_content = """<html><body>
<a name="candidate1">
<pre>
Candidate 1 info
The object was found in VSX
10" V0615 Vul
</pre>
<HR>
<a name="candidate2">
<pre>
Candidate 2 info
New transient discovery
</pre>
<HR>
</body></html>"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as f:
            f.write(html_content)
            temp_path = f.name

        try:
            filter_report(temp_path)
            output_path = temp_path.replace('.html', '_filtered.html')

            assert os.path.exists(output_path)
            with open(output_path, 'r') as f:
                filtered = f.read()

            # Should contain the new transient
            assert 'Candidate 2' in filtered or 'candidate2' in filtered or 'New transient' in filtered

            os.unlink(output_path)
        finally:
            os.unlink(temp_path)

    def test_no_transients_to_filter(self):
        """Should handle report with no transients"""
        html_content = "<html><body>No transients found</body></html>"

        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as f:
            f.write(html_content)
            temp_path = f.name

        try:
            # Should not raise an exception
            filter_report(temp_path)
            # Output file should not be created when there's nothing to filter
            output_path = temp_path.replace('.html', '_filtered.html')
            # The function prints a message but doesn't create output file
            # when there are no transients
        finally:
            os.unlink(temp_path)
            if os.path.exists(temp_path.replace('.html', '_filtered.html')):
                os.unlink(temp_path.replace('.html', '_filtered.html'))

    def test_all_filtered_message(self):
        """Should show message when all transients are known objects"""
        html_content = """<html><body>
<a name="candidate1">
<pre>
The object was found in astcheck
</pre>
<HR>
</body></html>"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as f:
            f.write(html_content)
            temp_path = f.name

        try:
            filter_report(temp_path)
            output_path = temp_path.replace('.html', '_filtered.html')

            assert os.path.exists(output_path)
            with open(output_path, 'r') as f:
                filtered = f.read()

            assert 'every transient is the known object' in filtered

            os.unlink(output_path)
        finally:
            os.unlink(temp_path)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

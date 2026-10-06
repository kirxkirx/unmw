#!/usr/bin/env python3
"""
Unit tests for filter_report.py and upload.py3
Run with: pytest test_python.py -v
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os
import sys
import tempfile
import zipfile
import re
import pytest
import shutil

# Import functions from filter_report.py
from filter_report import is_asteroid, is_variable_star, is_ast_or_vs, filter_report

# Import functions from upload.py3 by reading the file and extracting functions
# (avoiding the cgi import which was removed in Python 3.13)
# We extract the pure functions that don't depend on cgi

# Constants from upload.py3
MIN_FILE_SIZE = 2 * 1024 * 1024  # 2MB
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
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
        assert validate_archive_size(3 * 1024 * 1024 * 1024) is False  # 3GB


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

    def test_filter_classifies_asteroids(self):
        """Asteroids should be in output wrapped in transient-asteroid class"""
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

            # Asteroid content is present but in hidden-by-default class
            assert 'astcheck' in filtered
            assert 'transient-asteroid' in filtered
            # Unknown transient is in its own class
            assert 'transient-unknown' in filtered
            assert 'Candidate 2' in filtered
            # All candidates from original must be present
            for anchor in re.findall(r'<a name="([^"]+)"', html_content):
                assert anchor in filtered, f"Candidate {anchor} missing from filtered output"

            os.unlink(output_path)
        finally:
            os.unlink(temp_path)

    def test_filter_classifies_variable_stars(self):
        """Variable stars should be in output wrapped in transient-varstar class"""
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

            # Variable star content is present in varstar class
            assert 'transient-varstar' in filtered
            assert 'V0615 Vul' in filtered
            # Unknown transient is in its own class
            assert 'transient-unknown' in filtered
            assert 'New transient' in filtered
            # All candidates from original must be present
            for anchor in re.findall(r'<a name="([^"]+)"', html_content):
                assert anchor in filtered, f"Candidate {anchor} missing from filtered output"

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

    def test_all_known_objects_message(self):
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

            # Message about all being known objects
            assert 'All' in filtered and 'known objects' in filtered
            # Asteroid is still in the output (hidden by default)
            assert 'transient-asteroid' in filtered
            assert 'astcheck' in filtered
            # All candidates from original must be present
            for anchor in re.findall(r'<a name="([^"]+)"', html_content):
                assert anchor in filtered, f"Candidate {anchor} missing from filtered output"

            os.unlink(output_path)
        finally:
            os.unlink(temp_path)

    def test_button_counts(self):
        """Toggle buttons should show correct counts"""
        html_content = """<html><body>
<a name="c1">
<pre>
The object was found in astcheck
</pre>
<HR>
<a name="c2">
<pre>
The object was found in astcheck
</pre>
<HR>
<a name="c3">
<pre>
The object was found in VSX
10" V0615 Vul
</pre>
<HR>
<a name="c4">
<pre>
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

            # 2 asteroids, 1 variable star
            assert 'Asteroids (2)' in filtered
            assert 'Variable Stars (1)' in filtered
            # All candidates from original must be present
            for anchor in re.findall(r'<a name="([^"]+)"', html_content):
                assert anchor in filtered, f"Candidate {anchor} missing from filtered output"

            os.unlink(output_path)
        finally:
            os.unlink(temp_path)


class TestFWHMExtraction:
    """Tests for FWHM extraction logic in combine_reports.sh"""

    def test_fwhm_extraction_with_fd_prefix(self):
        """Should extract FWHM value from calibrated image lines (fd_ prefix)"""
        # Simulate index.html content with both FWHM and star elongation lines
        html_content = """
SECOND_EPOCH__FIRST_IMAGE= /path/to/161_2026-2-19_18-43-44_002.fts
SECOND_EPOCH__SECOND_IMAGE= /path/to/161_2026-2-19_18-44-38_003.fts
The star elongation is within the allowed range: median(A-B)=0.12 pix  161_2026-2-19_18-43-44_002.fts
The star elongation is within the allowed range: median(A-B)=0.13 pix  161_2026-2-19_18-44-38_003.fts
 1.7 pix  fd_161_2026-2-19_18-43-44_002.fts
 1.6 pix  fd_161_2026-2-19_18-44-38_003.fts
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html',
                                         delete=False) as f:
            f.write(html_content)
            temp_path = f.name

        try:
            # Extract FWHM using the same logic as combine_reports.sh
            # This tests that we get numeric FWHM values, not "The"
            import subprocess
            bash_cmd = f'''
SECOND_EPOCH_FIRST=$(grep 'SECOND_EPOCH__FIRST_IMAGE=' "{temp_path}" | head -n1 | sed 's|.*/||' | sed 's/<.*//')
SECOND_EPOCH_SECOND=$(grep 'SECOND_EPOCH__SECOND_IMAGE=' "{temp_path}" | head -n1 | sed 's|.*/||' | sed 's/<.*//')
FWHM_PIX=$( {{
  [ -n "$SECOND_EPOCH_FIRST" ] && grep "pix.*$SECOND_EPOCH_FIRST" "{temp_path}" | awk '$1 ~ /^[0-9.]+$/ {{print $1}}'
  [ -n "$SECOND_EPOCH_SECOND" ] && grep "pix.*$SECOND_EPOCH_SECOND" "{temp_path}" | awk '$1 ~ /^[0-9.]+$/ {{print $1}}'
}} | sort -rn | head -n1 )
echo "$FWHM_PIX"
'''
            result = subprocess.run(['bash', '-c', bash_cmd],
                                    capture_output=True, text=True)
            fwhm_value = result.stdout.strip()

            # FWHM should be a number (1.7), not "The"
            assert fwhm_value != "The", \
                f"FWHM extraction returned 'The' instead of numeric value"
            assert fwhm_value == "1.7", \
                f"Expected FWHM 1.7, got {fwhm_value}"
        finally:
            os.unlink(temp_path)

    def test_fwhm_extraction_without_fd_prefix(self):
        """Should extract FWHM value from non-calibrated image lines"""
        html_content = """
SECOND_EPOCH__FIRST_IMAGE= /path/to/image_001.fts
SECOND_EPOCH__SECOND_IMAGE= /path/to/image_002.fts
 2.3 pix  image_001.fts
 2.1 pix  image_002.fts
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html',
                                         delete=False) as f:
            f.write(html_content)
            temp_path = f.name

        try:
            import subprocess
            bash_cmd = f'''
SECOND_EPOCH_FIRST=$(grep 'SECOND_EPOCH__FIRST_IMAGE=' "{temp_path}" | head -n1 | sed 's|.*/||' | sed 's/<.*//')
SECOND_EPOCH_SECOND=$(grep 'SECOND_EPOCH__SECOND_IMAGE=' "{temp_path}" | head -n1 | sed 's|.*/||' | sed 's/<.*//')
FWHM_PIX=$( {{
  [ -n "$SECOND_EPOCH_FIRST" ] && grep "pix.*$SECOND_EPOCH_FIRST" "{temp_path}" | awk '$1 ~ /^[0-9.]+$/ {{print $1}}'
  [ -n "$SECOND_EPOCH_SECOND" ] && grep "pix.*$SECOND_EPOCH_SECOND" "{temp_path}" | awk '$1 ~ /^[0-9.]+$/ {{print $1}}'
}} | sort -rn | head -n1 )
echo "$FWHM_PIX"
'''
            result = subprocess.run(['bash', '-c', bash_cmd],
                                    capture_output=True, text=True)
            fwhm_value = result.stdout.strip()

            assert fwhm_value == "2.3", \
                f"Expected FWHM 2.3, got {fwhm_value}"
        finally:
            os.unlink(temp_path)

    def test_fwhm_not_extracted_from_elongation_lines(self):
        """Should NOT extract from star elongation lines (regression test)"""
        # This is the exact bug scenario - only elongation lines, no FWHM lines
        html_content = """
SECOND_EPOCH__FIRST_IMAGE= /path/to/161_2026-2-19_18-43-44_002.fts
The star elongation is within the allowed range: median(A-B)=0.12 pix  161_2026-2-19_18-43-44_002.fts
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html',
                                         delete=False) as f:
            f.write(html_content)
            temp_path = f.name

        try:
            import subprocess
            bash_cmd = f'''
SECOND_EPOCH_FIRST=$(grep 'SECOND_EPOCH__FIRST_IMAGE=' "{temp_path}" | head -n1 | sed 's|.*/||' | sed 's/<.*//')
FWHM_PIX=$( {{
  [ -n "$SECOND_EPOCH_FIRST" ] && grep "pix.*$SECOND_EPOCH_FIRST" "{temp_path}" | awk '$1 ~ /^[0-9.]+$/ {{print $1}}'
}} | sort -rn | head -n1 )
echo "$FWHM_PIX"
'''
            result = subprocess.run(['bash', '-c', bash_cmd],
                                    capture_output=True, text=True)
            fwhm_value = result.stdout.strip()

            # Should be empty, NOT "The"
            assert fwhm_value != "The", \
                "FWHM extraction incorrectly returned 'The' from elongation line"
            assert fwhm_value == "", \
                f"Expected empty FWHM (no valid lines), got '{fwhm_value}'"
        finally:
            os.unlink(temp_path)


# ---------------------------------------------------------------------------
# Monitoring within-visit consistency check (nmw_monitoring_lib)
# ---------------------------------------------------------------------------

import nmw_monitoring_lib as nml


def _det(basename, jd, mag, err=0.01, camera='CAM1'):
    return {'basename': basename, 'jd': str(jd), 'mag': str(mag),
            'err': str(err), 'status': 'detection', 'camera': camera,
            'jd_float': jd, 'mag_float': mag, 'err_float': err}


def test_visit_consistency_clean_pair_kept():
    rows = [_det('a.fits', 2461000.500, 9.70),
            _det('b.fits', 2461000.501, 9.72)]
    kept, _kept_lim, flagged = nml.split_inconsistent_visits(rows, [])
    assert len(kept) == 2 and not flagged


def test_visit_consistency_discrepant_pair_flagged():
    rows = [_det('a.fits', 2461000.500, 9.66),
            _det('b.fits', 2461000.501, 10.31)]
    kept, _kept_lim, flagged = nml.split_inconsistent_visits(rows, [])
    assert not kept and len(flagged) == 2


def test_visit_consistency_separate_visits_not_compared():
    # same camera, 0.65 mag apart but 1 hour apart: separate visits
    rows = [_det('a.fits', 2461000.500, 9.66),
            _det('b.fits', 2461000.542, 10.31)]
    kept, _kept_lim, flagged = nml.split_inconsistent_visits(rows, [])
    assert len(kept) == 2 and not flagged


def test_visit_consistency_cameras_independent():
    # two cameras at the same time never form one visit
    rows = [_det('a.fits', 2461000.500, 9.66, camera='CAM1'),
            _det('b.fits', 2461000.501, 10.31, camera='CAM2')]
    kept, _kept_lim, flagged = nml.split_inconsistent_visits(rows, [])
    assert len(kept) == 2 and not flagged


def test_visit_consistency_large_errors_tolerated():
    # near the detection limit the spread must beat ERR_SCALE * err
    rows = [_det('a.fits', 2461000.500, 14.1, err=0.2),
            _det('b.fits', 2461000.501, 14.6, err=0.2)]
    kept, _kept_lim, flagged = nml.split_inconsistent_visits(rows, [])
    assert len(kept) == 2 and not flagged


def test_visit_consistency_three_frame_visit_chained():
    # gap chaining: 3 frames each 5 min apart form one visit; one outlier
    # condemns all three
    rows = [_det('a.fits', 2461000.500, 9.70),
            _det('b.fits', 2461000.5035, 9.71),
            _det('c.fits', 2461000.507, 10.40)]
    kept, _kept_lim, flagged = nml.split_inconsistent_visits(rows, [])
    assert not kept and len(flagged) == 3


def test_visit_consistency_singleton_never_flagged():
    rows = [_det('a.fits', 2461000.500, 12.0)]
    kept, _kept_lim, flagged = nml.split_inconsistent_visits(rows, [])
    assert len(kept) == 1 and not flagged


def _lim(basename, jd, mag, camera='CAM1'):
    return {'basename': basename, 'jd': str(jd), 'mag': str(mag),
            'err': '99.0', 'status': 'upperlimit', 'camera': camera,
            'jd_float': jd, 'mag_float': mag, 'err_float': 99.0}


def test_visit_mixed_detection_and_limit_discarded_whole():
    # a visit with one detection and one upper limit is discarded whole,
    # regardless of how physical the pair looks
    dets = [_det('a.fits', 2461000.500, 14.0)]
    lims = [_lim('b.fits', 2461000.501, 13.0)]
    kept_det, kept_lim, flagged = nml.split_inconsistent_visits(dets, lims)
    assert not kept_det and not kept_lim and len(flagged) == 2


def test_visit_mixed_deep_limit_discarded_whole():
    # the unphysical case (limit deeper than the detection) as well
    dets = [_det('a.fits', 2461000.500, 14.0)]
    lims = [_lim('b.fits', 2461000.501, 15.5)]
    kept_det, kept_lim, flagged = nml.split_inconsistent_visits(dets, lims)
    assert not kept_det and not kept_lim and len(flagged) == 2


def test_visit_limits_only_always_kept():
    lims = [_lim('a.fits', 2461000.500, 13.0),
            _lim('b.fits', 2461000.501, 15.5)]
    kept_det, kept_lim, flagged = nml.split_inconsistent_visits([], lims)
    assert not kept_det and len(kept_lim) == 2 and not flagged


def test_visit_mixed_other_camera_not_grouped():
    # a limit from ANOTHER camera at the same time does not poison the visit
    dets = [_det('a.fits', 2461000.500, 14.0, camera='CAM1')]
    lims = [_lim('b.fits', 2461000.501, 15.5, camera='CAM2')]
    kept_det, kept_lim, flagged = nml.split_inconsistent_visits(dets, lims)
    assert len(kept_det) == 1 and len(kept_lim) == 1 and not flagged


def test_visit_mixed_three_frame_discarded_whole():
    # two consistent detections plus one limit in the same visit: all out
    dets = [_det('a.fits', 2461000.500, 14.0),
            _det('b.fits', 2461000.5035, 14.02)]
    lims = [_lim('c.fits', 2461000.507, 14.5)]
    kept_det, kept_lim, flagged = nml.split_inconsistent_visits(dets, lims)
    assert not kept_det and not kept_lim and len(flagged) == 3


# ---------------------------------------------------------------------------
# Monitoring frame quality (cloud) check (nmw_frame_quality_lib)
# ---------------------------------------------------------------------------

import nmw_frame_quality_lib as nfq


def _grid_stars(n_side=30, mag=-10.0):
    # a uniform grid of fake stars over a 4000x3000 px frame with a fake
    # linear WCS of 2 arcsec/px around RA=180 Dec=0
    stars = []
    for i in range(n_side):
        for j in range(n_side):
            x = 50.0 + i * 3900.0 / (n_side - 1)
            y = 50.0 + j * 2900.0 / (n_side - 1)
            ra = 180.0 + (x - 2000.0) * 2.0 / 3600.0
            dec = (y - 1500.0) * 2.0 / 3600.0
            stars.append((ra, dec, x, y, mag))
    return stars


def test_frame_quality_identical_frame_is_ok():
    ref = _grid_stars()
    metrics = nfq.frame_quality_metrics(list(ref), ref)
    cloudy, tripped = nfq.frame_verdict(metrics)
    assert not cloudy and metrics['missing'] == 0.0


def test_frame_quality_uniform_haze_is_ok():
    # 0.8 mag uniform dimming: big zero-point shift but NOT cloudy (the
    # per-image calibration absorbs uniform transparency loss)
    ref = _grid_stars()
    frame = [(ra, dec, x, y, mag + 0.8) for ra, dec, x, y, mag in ref]
    metrics = nfq.frame_quality_metrics(frame, ref)
    cloudy, tripped = nfq.frame_verdict(metrics)
    assert not cloudy and abs(metrics['dzp'] - 0.8) < 0.01


def test_frame_quality_patchy_cloud_is_cloudy():
    # left half of the frame dimmed by 0.6 mag: patchiness + faint tail
    ref = _grid_stars()
    frame = [(ra, dec, x, y, mag + (0.6 if x < 2000.0 else 0.0))
             for ra, dec, x, y, mag in ref]
    metrics = nfq.frame_quality_metrics(frame, ref)
    cloudy, tripped = nfq.frame_verdict(metrics)
    assert cloudy and 'patch' in tripped


def test_frame_quality_missing_stars_is_cloudy():
    # a third of the reference stars are gone (thick clouds)
    ref = _grid_stars()
    frame = [s for k, s in enumerate(ref) if k % 3 != 0]
    metrics = nfq.frame_quality_metrics(frame, ref)
    cloudy, tripped = nfq.frame_verdict(metrics)
    assert cloudy and 'missing' in tripped


def test_frame_quality_small_reference_skips_check():
    ref = _grid_stars(n_side=10)  # 100 stars < MIN_REF_STARS_FOR_CHECK
    metrics = nfq.frame_quality_metrics(list(ref), ref)
    assert metrics is None
    cloudy, tripped = nfq.frame_verdict(metrics)
    assert not cloudy


def test_classify_routes_cloudy_rows():
    rows = [
        {'basename': 'a.fits', 'jd': '2461000.5', 'mag': '9.7',
         'err': '0.01', 'status': 'detection', 'camera': 'C1'},
        {'basename': 'b.fits', 'jd': '2461000.6', 'mag': '10.4',
         'err': '0.02', 'status': 'cloudy', 'camera': 'C1'},
        {'basename': 'c.fits', 'jd': '2461000.7', 'mag': '<13.0',
         'err': 'na', 'status': 'upperlimit', 'camera': 'C1'},
    ]
    det, ul, excl = nml.classify_ledger_rows(rows)
    assert len(det) == 1 and len(ul) == 1 and len(excl) == 1
    assert excl[0]['reason'] == nml.REASON_CLOUDY


def test_classify_routes_manual_rows():
    rows = [
        {'basename': 'a.fits', 'jd': '2461000.5', 'mag': '9.7',
         'err': '0.01', 'status': 'manual', 'camera': 'C1'},
        {'basename': 'b.fits', 'jd': '2461000.6', 'mag': '<13.0',
         'err': 'na', 'status': 'manual', 'camera': 'C1'},
    ]
    det, ul, excl = nml.classify_ledger_rows(rows)
    assert not det and not ul and len(excl) == 2
    assert all(r['reason'] == nml.REASON_MANUAL for r in excl)


def test_rewrite_measurement_status_roundtrip():
    import tempfile, shutil
    uploads = tempfile.mkdtemp()
    try:
        sdir = nml.source_dir_path(uploads, 'SRC')
        os.makedirs(sdir)
        ledger = os.path.join(sdir, nml.LEDGER_BASENAME)
        with open(ledger, 'w') as fh:
            fh.write('# image_basename JD mag err status camera\n'
                     'good.fits 2461000.5 9.70 0.01 detection C1\n'
                     'bad.fits 2461000.6 10.40 0.02 detection C1\n'
                     'faint.fits 2461000.7 <13.0 na upperlimit C1\n')
        # exclude with a .fz-suffixed name: must match via ledger_key
        assert nml.rewrite_measurement_status(uploads, 'SRC',
                                              'bad.fits.fz') == 1
        lines = open(ledger).read().splitlines()
        assert lines[0].startswith('#')
        assert 'good.fits 2461000.5 9.70 0.01 detection C1' in lines[1]
        assert 'bad.fits 2461000.6 10.40 0.02 manual C1' in lines[2]
        # excluding again is a no-op
        assert nml.rewrite_measurement_status(uploads, 'SRC',
                                              'bad.fits') == 0
        # unknown image is a no-op
        assert nml.rewrite_measurement_status(uploads, 'SRC',
                                              'nosuch.fits') == 0
        # restore flips back to detection
        assert nml.rewrite_measurement_status(uploads, 'SRC', 'bad.fits',
                                              restore=True) == 1
        assert 'bad.fits 2461000.6 10.40 0.02 detection C1' in \
            open(ledger).read()
        # upper limit round trip: manual and back to upperlimit
        assert nml.rewrite_measurement_status(uploads, 'SRC',
                                              'faint.fits') == 1
        assert nml.rewrite_measurement_status(uploads, 'SRC', 'faint.fits',
                                              restore=True) == 1
        assert 'faint.fits 2461000.7 <13.0 na upperlimit C1' in \
            open(ledger).read()
    finally:
        shutil.rmtree(uploads, ignore_errors=True)


def test_quarantine_enumeration():
    import tempfile, shutil
    import monitoring_update as mu
    fields = {'CrB-02-Q1b1x1'}
    # unset -> silent skip
    assert mu.enumerate_quarantine_images({}, fields) == []
    # configured but missing -> skip
    assert mu.enumerate_quarantine_images(
        {'IMAGE_QUARANTINE_DIR': '/nonexistent/quarantine'}, fields) == []
    # populated quarantine: only wcs_* images of covering fields are picked
    q = tempfile.mkdtemp()
    try:
        d = os.path.join(q, 'img_2026-07-01_CI_CrB-02-Q1b1x1_x')
        os.makedirs(d)
        good = 'wcs_fd_CrB-02-Q1b1x1_2026-07-01_01-00-00_20.00sec_' \
               '5.00C_LIGHT_0001.fits'
        for name in (good,
                     'fd_CrB-02-Q1b1x1_2026-07-01_01-00-00_20.00sec_'
                     '5.00C_LIGHT_0001.fits',
                     'wcs_fd_Vul-09-Q1b1x1_2026-07-01_01-05-00_20.00sec_'
                     '5.00C_LIGHT_0002.fits'):
            open(os.path.join(d, name), 'w').close()
        os.makedirs(os.path.join(q, 'results_not_an_img_dir'))
        found = mu.enumerate_quarantine_images(
            {'IMAGE_QUARANTINE_DIR': q}, fields)
        assert [os.path.basename(p) for p in found] == [good]
    finally:
        shutil.rmtree(q, ignore_errors=True)


def test_page_message_rendering_and_sanitization():
    import tempfile, shutil
    sandbox = tempfile.mkdtemp()
    try:
        entry = {'name': 'Fake', 'ra': '12:00:00.00', 'dec': '+30:00:00.0',
                 'source_id': 'Fake'}
        # message present: rendered after the caveat block, HTML-escaped
        # (the two None plot arguments are the full-range and the
        # last-30-days plot basenames)
        nml._write_source_page(
            sandbox, entry, [], [], [], [], None, None, [], '',
            page_message='Use freely. <script>alert(1)</script>')
        html = open(os.path.join(sandbox, 'index.html')).read()
        assert 'Use freely. &lt;script&gt;alert(1)&lt;/script&gt;' in html
        assert '<script>alert(1)</script>' not in html
        assert html.index('Use freely.') > html.index('not very precise')
        # no message: nothing extra rendered
        nml._write_source_page(sandbox, entry, [], [], [], [], None, None,
                               [], '')
        html = open(os.path.join(sandbox, 'index.html')).read()
        assert 'Use freely.' not in html
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_detection_threshold_file_roundtrip():
    import tempfile, shutil
    sandbox = tempfile.mkdtemp()
    try:
        # nothing set yet
        assert nml.read_detection_threshold(sandbox) is None
        assert nml.write_detection_threshold(sandbox, 13.0) \
            == 'threshold set to 13.00'
        assert nml.read_detection_threshold(sandbox) == 13.0
        assert nml.write_detection_threshold(sandbox, None) \
            == 'threshold cleared'
        assert nml.read_detection_threshold(sandbox) is None
        assert nml.write_detection_threshold(sandbox, None) \
            == 'no threshold was set'
        # unparseable content is ignored (treated as no threshold)
        with open(os.path.join(
                sandbox, nml.DETECTION_THRESHOLD_BASENAME), 'w') as fh:
            fh.write('not a number\n')
        assert nml.read_detection_threshold(sandbox) is None
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_resolve_source_selector_short_names():
    import monitoring_update as mu
    entries = [
        {'source_id': 'AT_2026xyz_-_Nova_in_Cyg_2026',
         'name': 'AT 2026xyz - Nova in Cyg 2026'},
        {'source_id': 'AU_CVn_=_1308+326_-_blazar',
         'name': 'AU CVn = 1308+326 - blazar'},
        {'source_id': 'T_CrB', 'name': 'T CrB'},
    ]
    # short AAVSO name
    entry, problem = mu.resolve_source_selector(entries, 'AT 2026xyz')
    assert problem is None and entry['source_id'].startswith('AT_2026xyz')
    entry, problem = mu.resolve_source_selector(entries, ' AU CVn ')
    assert problem is None and entry['source_id'].startswith('AU_CVn')
    # full name and source_id still work
    entry, problem = mu.resolve_source_selector(
        entries, 'AT 2026xyz - Nova in Cyg 2026')
    assert problem is None
    entry, problem = mu.resolve_source_selector(entries, 'T_CrB')
    assert problem is None and entry['name'] == 'T CrB'
    # no match
    entry, problem = mu.resolve_source_selector(entries, 'No Such Star')
    assert entry is None and 'no monitoring list entry' in problem


def test_apply_detection_threshold():
    def det(jd, mag):
        return {'jd_float': jd, 'mag_float': mag, 'status': 'detection'}
    def ul(jd, mag):
        return {'jd_float': jd, 'mag_float': mag, 'status': 'upperlimit'}
    detections = [det(100.0, 12.5), det(101.0, 14.2), det(102.0, 13.0)]
    upperlimits = [ul(103.0, 15.5)]
    # None threshold: everything unchanged
    d2, u2 = nml.apply_detection_threshold(detections, upperlimits, None)
    assert d2 is detections and u2 is upperlimits
    # threshold 13.0: the 14.2 detection is demoted (13.0 itself stays)
    d2, u2 = nml.apply_detection_threshold(list(detections),
                                           list(upperlimits), 13.0)
    assert [r['mag_float'] for r in d2] == [12.5, 13.0]
    assert [r['mag_float'] for r in u2] == [14.2, 15.5]  # sorted by JD
    demoted = [r for r in u2 if r['mag_float'] == 14.2][0]
    assert demoted['status'] == nml.STATUS_BELOW_THRESHOLD


def test_aavso_star_name_stripping():
    assert nml.aavso_star_name('AT 2026rdg - Nova in Aql 2026') \
        == 'AT 2026rdg'
    assert nml.aavso_star_name('TCP J02191736+2857158 - dwarf nova') \
        == 'TCP J02191736+2857158'
    assert nml.aavso_star_name('  GK Per  ') == 'GK Per'
    # the '= alias' convention is cut too - VSX does not resolve it
    assert nml.aavso_star_name('AU CVn = 1308+326') == 'AU CVn'
    # hyphenated identifiers survive: the cut is only at a '-' that
    # follows whitespace
    assert nml.aavso_star_name('ASAS-SN 26abc - CV') == 'ASAS-SN 26abc'
    assert nml.aavso_star_name('QSO B1420+326') == 'QSO B1420+326'


# ---------------------------------------------------------------------------
# run_forced_photometry_c: off-frame targets in the monitoring backfill
# ---------------------------------------------------------------------------
import nmw_forced_phot_lib as nfp


class _FakeCompletedRun:
    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRunForcedPhotometryOffImage:
    """forced_photometry.sh exits 1 when sky2xy puts the target outside the
    frame. The monitoring backfill must record that as a terminal 'edge'
    row (like the factory does) instead of retrying the image on every
    rescan; the web/archive callers keep getting None because they need
    pixel positions for thumbnails."""

    IMAGE = ('/data/img_x/wcs_fd_Ser-07-Q2b1x1_2026-02-12_05-22-31_20.00sec'
             '_-14.90C_LIGHT_0019.fits')
    OFF_IMAGE_STDERR = (
        'Step 4: Converting RA/Dec to pixel coordinates...\n'
        'ERROR: target coordinates are off the image\n'
        '  sky2xy output: 18:31:15.620000 -05:34:31.900000 J2000 -> '
        '9739.977 1478.242 (off image)\n')

    def _call(self, monkeypatch, tmp_path, stderr, off_image_as_edge):
        monkeypatch.setattr(
            nfp, '_run_capture_session',
            lambda cmd, cwd=None, env=None, timeout=None:
            _FakeCompletedRun(1, '', stderr))
        skip_log = str(tmp_path / 'measurement_skipped.log')
        fp = nfp.run_forced_photometry_c(
            str(tmp_path), None, self.IMAGE, self.IMAGE,
            '18:31:15.62', '-05:34:31.9', 'V', debug_log=skip_log,
            off_image_as_edge=off_image_as_edge)
        return fp, skip_log

    def test_off_image_becomes_edge_for_the_backfill(self, monkeypatch,
                                                     tmp_path):
        fp, skip_log = self._call(monkeypatch, tmp_path,
                                  self.OFF_IMAGE_STDERR, True)
        assert fp is not None
        assert fp['status'] == 'edge'
        assert fp['mag'] == '99.0000'
        assert fp['err'] == '99.0000'
        assert fp['jd'] is None
        assert fp['basename'] == os.path.basename(self.IMAGE)
        assert fp['x'] is None
        assert fp['y'] is None
        assert fp['aperture'] is None
        with open(skip_log) as fh:
            assert 'recorded as edge' in fh.read()

    def test_off_image_stays_none_without_opt_in(self, monkeypatch, tmp_path):
        fp, _ = self._call(monkeypatch, tmp_path, self.OFF_IMAGE_STDERR, False)
        assert fp is None

    def test_other_failures_stay_none_with_opt_in(self, monkeypatch, tmp_path):
        fp, skip_log = self._call(monkeypatch, tmp_path,
                                  'ERROR: sky2xy failed\n  Output: junk\n',
                                  True)
        assert fp is None
        with open(skip_log) as fh:
            assert 'forced_photometry.sh exited 1' in fh.read()

    def test_edge_margin_reaches_the_tool_through_the_environment(
            self, monkeypatch, tmp_path):
        seen = {}

        def fake_run(cmd, cwd=None, env=None, timeout=None):
            seen['env'] = dict(env or {})
            return _FakeCompletedRun(1, '', 'ERROR: sky2xy failed\n')
        monkeypatch.setattr(nfp, '_run_capture_session', fake_run)
        monkeypatch.delenv('FORCED_PHOTOMETRY_EDGE_MARGIN_PIX', raising=False)
        nfp.run_forced_photometry_c(
            str(tmp_path), None, self.IMAGE, self.IMAGE,
            '18:31:15.62', '-05:34:31.9', 'V', edge_margin_pix=100)
        assert seen['env'].get('FORCED_PHOTOMETRY_EDGE_MARGIN_PIX') == '100'
        nfp.run_forced_photometry_c(
            str(tmp_path), None, self.IMAGE, self.IMAGE,
            '18:31:15.62', '-05:34:31.9', 'V')
        assert 'FORCED_PHOTOMETRY_EDGE_MARGIN_PIX' not in seen['env']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


# ---------------------------------------------------------------------------
# Monitoring list page: coordinate rounding for display (nmw_monitoring_lib)
# ---------------------------------------------------------------------------

def test_round_sexagesimal_ra_two_decimals():
    assert nml.display_ra('12:34:56.789') == '12:34:56.79'
    assert nml.display_ra('12:34:56') == '12:34:56.00'
    assert nml.display_ra('5:03:07.1') == '05:03:07.10'
    # half-up, not banker's rounding
    assert nml.display_ra('01:02:03.125') == '01:02:03.13'


def test_round_sexagesimal_ra_carry_chain():
    assert nml.display_ra('12:34:59.995') == '12:35:00.00'
    assert nml.display_ra('12:59:59.996') == '13:00:00.00'
    # 24h wrap, as in VaST lib/deg2hms
    assert nml.display_ra('23:59:59.999') == '00:00:00.00'
    # just below the threshold does not carry
    assert nml.display_ra('12:34:59.994') == '12:34:59.99'


def test_round_sexagesimal_dec_one_decimal():
    assert nml.display_dec('-12:34:56.78') == '-12:34:56.8'
    assert nml.display_dec('12:34:56.75') == '+12:34:56.8'
    assert nml.display_dec('+0:00:00') == '+00:00:00.0'
    assert nml.display_dec('+12:34:59.96') == '+12:35:00.0'
    assert nml.display_dec('-45:59:59.95') == '-46:00:00.0'
    assert nml.display_dec('89:59:59.97') == '+90:00:00.0'


def test_round_sexagesimal_unparsable_passthrough():
    assert nml.display_ra('garbage') == 'garbage'
    assert nml.display_dec('12:34') == '12:34'


def test_central_index_shows_rounded_coordinates_with_full_tooltip():
    import shutil
    sandbox = tempfile.mkdtemp()
    try:
        entry = {'ra': '12:34:56.789', 'dec': '-12:34:56.78',
                 'name': 'Test Star', 'source_id': 'Test_Star', 'line_no': 1}
        root = nml.monitoring_root(sandbox)
        os.makedirs(nml.source_dir_path(sandbox, 'Test_Star'))
        nml.rebuild_central_index(sandbox, [entry], None)
        with open(os.path.join(root, 'index.html')) as fh:
            page = fh.read()
        assert '<td class="code" title="12:34:56.789">12:34:56.79</td>' in page
        assert '<td class="code" title="-12:34:56.78">-12:34:56.8</td>' in page
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


# ---------------------------------------------------------------------------
# Transient-search run verdicts, the rejected-run ingest and the refusal
# statuses of the forced photometry frame-level checks (nmw_monitoring_lib,
# monitoring_update, nmw_forced_phot_lib)
# ---------------------------------------------------------------------------

_FRAME = 'Per-02-Q1b1x1_2026-09-17_23-03-28_20.00sec_0.00C_LIGHTs_0111.fits'
_UPLOAD = 'img_2026-09-17_CI_Per-02-Q1b1x1_230448_TTUQ1b1x1_1712436_eIesSJ9U'
_COMPLETE_REPORT = '<H2>Processing complete!</H2>\n'


def _write_file(path, text=''):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        fh.write(text)


def _ledger_by_basename(uploads, source_id):
    rows, _ = nml.read_ledger(nml.source_dir_path(uploads, source_id))
    return {r['basename']: (r['status'], r['mag'], r['err']) for r in rows}


def test_image_core_name_strips_pipeline_prefixes_and_fz():
    for name in (_FRAME, 'fd_' + _FRAME, 'wcs_fd_' + _FRAME,
                 'wcs_fd_' + _FRAME + '.fz', '/q/img_x/wcs_' + _FRAME,
                 'd_' + _FRAME):
        assert nml.image_core_name(name) == _FRAME


def test_read_run_verdicts_newest_line_wins(tmp_path):
    _write_file(str(tmp_path / nml.RUN_VERDICTS_BASENAME),
                '1000 pending results_A X.fits\n'
                'garbage\n'
                '1100 error results_A X.fits ERROR: passing clouds\n'
                '1050 ok results_B X.fits\n'
                '1000 ok results_A Y.fits\n')
    verdicts = nml.read_run_verdicts(str(tmp_path))
    assert verdicts['X.fits'] == (1100.0, 'error', 'results_A',
                                  'ERROR: passing clouds')
    assert verdicts['Y.fits'][1] == 'ok'
    assert nml.read_run_verdicts(str(tmp_path / 'missing')) == {}


def test_verdict_from_report_rules(tmp_path):
    reports = {
        'ok': _COMPLETE_REPORT,
        'error': '<b>ERROR: too few stars</b>\n' + _COMPLETE_REPORT,
        'killed': "manually reload the page untill the 'Processing complete'"
                  " message appears\n",
        'empty': '',
    }
    for name, text in reports.items():
        _write_file(str(tmp_path / (name + '.html')), text)
    assert nml.verdict_from_report(str(tmp_path / 'ok.html')) == ('ok', '')
    assert nml.verdict_from_report(str(tmp_path / 'error.html')) == (
        'error', 'ERROR: too few stars')
    assert nml.verdict_from_report(str(tmp_path / 'killed.html'))[0] == \
        'failed'
    assert nml.verdict_from_report(str(tmp_path / 'empty.html')) is None
    assert nml.verdict_from_report(str(tmp_path / 'missing.html')) is None


def test_upload_dir_of_results_dir_names():
    assert nml.upload_dir_of_results_dir(
        'results_20260917_230531_' + _UPLOAD[4:]) == (
            _UPLOAD, '20260917_230531')
    assert nml.upload_dir_of_results_dir(
        'results_20260920_101010_reprocess_' + _UPLOAD + '_349237_BVS8Ieoj'
    ) == (_UPLOAD, '20260920_101010')
    assert nml.upload_dir_of_results_dir(
        'results_20260728_034122_reprocess_img_X_1283857_349237') == (
            'img_X_1283857', '20260728_034122')
    assert nml.upload_dir_of_results_dir('results_comets.txt') == (None, None)


def test_run_verdict_resolver_sources(tmp_path):
    root = str(tmp_path / 'workdir')
    copy_in_quarantine = os.path.join(str(tmp_path), 'quarantine', _UPLOAD,
                                      'wcs_fd_' + _FRAME)
    copy_in_archive = str(tmp_path / 'archive' / ('wcs_fd_' + _FRAME + '.fz'))
    _write_file(copy_in_quarantine)
    _write_file(copy_in_archive)
    original_run = os.path.join(root, 'results_20260917_230531_' + _UPLOAD[4:])
    _write_file(os.path.join(original_run, 'index.html'),
                '<b>ERROR: passing clouds</b>\n' + _COMPLETE_REPORT)
    # The upload directory names its run
    resolver = nml.RunVerdictResolver(root)
    assert resolver.verdict_for_image(copy_in_quarantine)[0] == 'error'
    # The archive copy is found through the frame preview of the run
    assert resolver.verdict_for_image(copy_in_archive)[0] is None
    _write_file(os.path.join(original_run, _FRAME + '_preview.png'))
    resolver = nml.RunVerdictResolver(root)
    assert resolver.verdict_for_image(copy_in_archive)[0] == 'error'
    # A newer successful reprocessing wins; one without a report says nothing
    reprocessing = os.path.join(
        root, 'results_20260920_101010_reprocess_{}_4242_abcdEFGH'.format(
            _UPLOAD))
    _write_file(os.path.join(reprocessing, 'index.html'), _COMPLETE_REPORT)
    _write_file(os.path.join(reprocessing, 'fd_' + _FRAME + '_preview.png'))
    os.makedirs(os.path.join(
        root, 'results_20260921_101010_reprocess_{}_4343_abcdEFGH'.format(
            _UPLOAD)))
    resolver = nml.RunVerdictResolver(root)
    assert resolver.verdict_for_image(copy_in_quarantine)[0] == 'ok'
    assert resolver.verdict_for_image(copy_in_archive)[0] == 'ok'
    # A newer reprocessing that was killed (no completion marker) did not
    # judge the frame: the earlier conclusive verdict stands
    killed = os.path.join(
        root, 'results_20260922_101010_reprocess_{}_4444_abcdEFGH'.format(
            _UPLOAD))
    _write_file(os.path.join(killed, 'index.html'), 'header only\n')
    resolver = nml.RunVerdictResolver(root)
    assert resolver.verdict_for_image(copy_in_quarantine)[0] == 'ok'
    # The verdict list written by autoprocess.sh overrides the reports
    now = 1790000000.0
    _write_file(os.path.join(root, nml.RUN_VERDICTS_BASENAME),
                '%d failed results_X %s the transient search exited with '
                'code 1\n' % (now - 100, _FRAME))
    resolver = nml.RunVerdictResolver(root, now=now)
    for copy in (copy_in_quarantine, copy_in_archive):
        assert resolver.verdict_for_image(copy)[:2] == (
            'failed', 'the transient search exited with code 1')
    # A run in progress is pending; one that never finished counts as failed
    with open(os.path.join(root, nml.RUN_VERDICTS_BASENAME), 'a') as fh:
        fh.write('%d pending results_Y %s\n' % (now - 60, _FRAME))
    assert nml.RunVerdictResolver(root, now=now).verdict_for_image(
        copy_in_archive)[0] == 'pending'
    stale = now + nml.PENDING_RUN_VERDICT_STALE_SECONDS + 1
    assert nml.RunVerdictResolver(root, now=stale).verdict_for_image(
        copy_in_archive)[0] == 'failed'


def test_verdict_for_copies_prefers_ok_then_rejected_then_pending():
    class FixedVerdicts(nml.RunVerdictResolver):
        def __init__(self, table):
            self.table = table

        def verdict_for_image(self, image_path):
            return self.table[image_path]
    resolver = FixedVerdicts({'a': ('error', 'x', 'o1'),
                              'b': ('ok', '', 'o2'),
                              'c': (None, '', 'no verdict'),
                              'd': ('pending', '', 'o3')})
    assert resolver.verdict_for_copies(['a', 'b'])[0] == 'ok'
    assert resolver.verdict_for_copies(['c', 'a'])[0] == 'error'
    assert resolver.verdict_for_copies(['c', 'a'])[3] == 'a'
    assert resolver.verdict_for_copies(['d', 'c'])[0] == 'pending'
    assert resolver.verdict_for_copies(['c'])[0] is None


def test_append_ledger_rows_supersedes_run_error_rows_only(tmp_path):
    uploads = str(tmp_path)

    def row(basename, status, mag='99.0000', err='99.0000'):
        return {'basename': basename, 'jd': '2461000.5', 'mag': mag,
                'err': err, 'status': status, 'camera': 'C1'}
    assert nml.append_ledger_rows(uploads, 'SRC', [
        row('a.fits', 'run_error', '12.5000', '0.0500'),
        row('b.fits', 'detection', '13.0000', '0.0200')]) == 2
    # a rejected run never replaces a row
    assert nml.append_ledger_rows(uploads, 'SRC',
                                  [row('a.fits.fz', 'run_error')]) == 0
    # a successful run replaces run_error rows, and nothing else
    assert nml.append_ledger_rows(uploads, 'SRC', [
        row('a.fits', 'detection', '12.4000', '0.0400'),
        row('b.fits', 'upperlimit', '15.0000'),
        row('c.fits', 'detection', '14.0000', '0.1000')],
        supersede_statuses=nml.SUPERSEDABLE_STATUSES) == 2
    ledger = _ledger_by_basename(uploads, 'SRC')
    assert ledger == {'a.fits': ('detection', '12.4000', '0.0400'),
                      'b.fits': ('detection', '13.0000', '0.0200'),
                      'c.fits': ('detection', '14.0000', '0.1000')}


def _patch_ingest_context(monkeypatch, uploads, entry):
    import monitoring_update as mu
    monkeypatch.setattr(mu, 'load_context',
                        lambda: (uploads, {}, uploads, None))
    monkeypatch.setattr(mu, 'list_entries_or_exit',
                        lambda: ('monitoring_list.txt', [entry]))
    monkeypatch.setattr(mu, 'read_factory_text', lambda cfg: '')
    monkeypatch.setattr(nml, 'rebuild_source_products',
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(nml, 'rebuild_central_index',
                        lambda *args, **kwargs: None)
    return mu


def test_ingest_rejected_records_run_error_and_ingest_replaces_it(
        tmp_path, monkeypatch):
    uploads = str(tmp_path)
    os.makedirs(nml.source_dir_path(uploads, 'SRC'))
    entry = {'source_id': 'SRC', 'name': 'SRC', 'ra': '01:00:00.00',
             'dec': '+10:00:00.0'}
    mu = _patch_ingest_context(monkeypatch, uploads, entry)
    raw = tmp_path / 'raw_rejected.txt'
    raw.write_text(
        'SRC wcs_fd_a.fits 2461000.5 12.5000 0.0500 detection C1\n'
        'SRC wcs_fd_b.fits 2461000.5 15.1000 99.0000 upperlimit C1\n'
        'SRC wcs_fd_c.fits 2461000.5 99.0000 99.0000 edge C1\n'
        'SRC wcs_fd_d.fits 2461000.5 99.0000 99.0000 run_error C1\n')
    assert mu.mode_ingest(str(raw), rejected=True) == 0
    ledger = _ledger_by_basename(uploads, 'SRC')
    assert ledger['wcs_fd_a.fits'] == ('run_error', '12.5000', '0.0500')
    assert ledger['wcs_fd_b.fits'] == ('run_error', '15.1000', '99.0000')
    assert ledger['wcs_fd_c.fits'][0] == 'edge'
    assert ledger['wcs_fd_d.fits'][0] == 'run_error'
    # A later successful reprocessing replaces the run_error rows only
    raw_ok = tmp_path / 'raw_ok.txt'
    raw_ok.write_text(
        'SRC wcs_fd_a.fits 2461000.5 12.4000 0.0400 detection C1\n'
        'SRC wcs_fd_c.fits 2461000.5 13.0000 0.0300 detection C1\n')
    assert mu.mode_ingest(str(raw_ok)) == 0
    ledger = _ledger_by_basename(uploads, 'SRC')
    assert ledger['wcs_fd_a.fits'] == ('detection', '12.4000', '0.0400')
    assert ledger['wcs_fd_c.fits'][0] == 'edge'


def test_manual_measurement_honours_run_verdicts(tmp_path, monkeypatch):
    import monitoring_update as mu
    root = str(tmp_path / 'workdir')
    os.makedirs(nml.source_dir_path(root, 'SRC'))
    entry = {'source_id': 'SRC', 'name': 'SRC', 'ra': '01:00:00.00',
             'dec': '+10:00:00.0'}
    now = 1790000000.0
    _write_file(os.path.join(root, nml.RUN_VERDICTS_BASENAME),
                '%d error results_A bad.fits ERROR: passing clouds\n'
                '%d pending results_B busy.fits\n'
                '%d pending results_C stale.fits\n'
                '%d ok results_D good.fits\n'
                '%d error results_E offimage.fits ERROR: wrong field?\n'
                '%d error results_F packed.fits ERROR: passing clouds\n' % (
                    now - 100, now - 100, now - 3 * 86400, now - 100,
                    now - 100, now - 100))
    images = [os.path.join(root, 'img_A', 'wcs_fd_' + name) for name in (
        'bad.fits', 'busy.fits', 'stale.fits', 'good.fits', 'unknown.fits',
        'offimage.fits')]
    # an image seen only through an fpack-compressed archive copy
    images.append(os.path.join(root, 'archive', 'wcs_fd_packed.fits.fz'))
    for image in images:
        _write_file(image)
    monkeypatch.setattr(mu, 'read_factory_text', lambda cfg: '')
    sky2xy_calls = []

    def fake_sky2xy(vast_dir, path, ra, dec):
        sky2xy_calls.append(os.path.basename(path))
        return None if 'offimage' in path else (100.0, 100.0)
    monkeypatch.setattr(mu, 'sky2xy_on_image', fake_sky2xy)
    monkeypatch.setattr(nfp, 'get_jd_and_atel_date',
                        lambda vast_dir, path: ('2461000.5000', None))
    monkeypatch.setattr(nfp, 'camera_settings_for_path',
                        lambda text, path: 'C1')
    monkeypatch.setattr(nfp, 'derive_band', lambda text, path, default: 'V')
    monkeypatch.setattr(nfp, 'derive_sextractor_config',
                        lambda text, path: None)
    work_dir = str(tmp_path / 'work')
    seen = {}

    def fake_setup(vast_dir, parent_dir, prefix=None):
        os.makedirs(work_dir, exist_ok=True)
        return work_dir

    def fake_solve(work, local_config_path, todo, workers, skip_log):
        seen['todo'] = sorted(os.path.basename(p) for p in todo)
        return None, None, None, {}, None
    monkeypatch.setattr(nfp, 'setup_vast_working_copy', fake_setup)
    monkeypatch.setattr(nfp, '_phase1_parallel_solve_plate', fake_solve)
    n_rows = mu.measure_images_for_source(
        {}, None, entry, images, root,
        verdicts=nml.RunVerdictResolver(root, now=now))
    # the images of rejected runs are recorded without being measured; a
    # position off the image gets no row, an fpack-compressed copy is
    # recorded without the on-image test
    assert n_rows == 3
    assert _ledger_by_basename(root, 'SRC') == {
        'wcs_fd_bad.fits': ('run_error', '99.0000', '99.0000'),
        'wcs_fd_stale.fits': ('run_error', '99.0000', '99.0000'),
        'wcs_fd_packed.fits.fz': ('run_error', '99.0000', '99.0000')}
    assert 'wcs_fd_packed.fits.fz' not in sky2xy_calls
    assert 'wcs_fd_offimage.fits' in sky2xy_calls
    # the run in progress is left alone; ok and unknown runs are measured
    assert seen['todo'] == ['wcs_fd_good.fits', 'wcs_fd_unknown.fits']


def test_refusal_statuses_are_listed_as_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv('MPLCONFIGDIR', str(tmp_path / 'mpl'))
    uploads = str(tmp_path / 'uploads')
    source_dir = nml.source_dir_path(uploads, 'SRC')
    _write_file(os.path.join(source_dir, nml.LEDGER_BASENAME),
                '# image_basename JD mag err status camera\n'
                'ok.fits 2461000.4 12.1000 0.0300 detection C1\n'
                'a.fits 2461000.5 12.3456 0.0500 bad_wcs C1\n'
                'b.fits 2461000.6 15.6501 99.0000 no_nearby_stars C1\n'
                'c.fits 2461000.7 99.0000 99.0000 run_error C1\n'
                'd.fits na 99.0000 99.0000 run_error C1\n'
                'e.fits 2461000.8 14.4269 99.0000 cloudy C1\n')
    rows, _ = nml.read_ledger(source_dir)
    det, lim, excluded = nml.classify_ledger_rows(rows)
    assert [r['basename'] for r in det] == ['ok.fits'] and not lim
    by_name = {r['basename']: r for r in excluded}
    assert set(by_name) == {'a.fits', 'b.fits', 'c.fits', 'e.fits'}
    assert by_name['a.fits']['reason'] == 'unreliable_plate_solution'
    assert by_name['a.fits']['err_float'] == 0.05
    assert by_name['b.fits']['reason'] == 'no_stars_around_position'
    assert by_name['c.fits']['reason'] == 'processing_error_in_run'
    assert by_name['c.fits']['mag_float'] is None
    entry = {'source_id': 'SRC', 'name': 'SRC', 'ra': '01:00:00.00',
             'dec': '+10:00:00.0'}
    nml.rebuild_source_products(uploads, entry, {}, '')
    with open(os.path.join(source_dir,
                           nml.EXCLUDED_MEASUREMENTS_BASENAME)) as fh:
        text = fh.read()
    assert '12.3456 0.0500 C1 unreliable_plate_solution a.fits' in text
    assert '15.6501 99.0000 C1 no_stars_around_position b.fits' in text
    assert '99.0000 99.0000 C1 processing_error_in_run c.fits' in text
    assert '14.4269 99.0000 C1 cloudy_frame e.fits' in text
    with open(os.path.join(source_dir, nml.LIGHTCURVE_BASENAME)) as fh:
        published = [line for line in fh if not line.startswith('#')]
    assert len(published) == 1 and ' 12.1000 ' in published[0]


def test_restore_measurement_keeps_real_format_upper_limits(tmp_path):
    uploads = str(tmp_path)
    source_dir = nml.source_dir_path(uploads, 'SRC')
    _write_file(os.path.join(source_dir, nml.LEDGER_BASENAME),
                '# image_basename JD mag err status camera\n'
                'lim.fits 2461000.7 13.6758 99.0000 upperlimit C1\n')
    assert nml.rewrite_measurement_status(uploads, 'SRC', 'lim.fits') == 1
    assert nml.rewrite_measurement_status(uploads, 'SRC', 'lim.fits',
                                          restore=True) == 1
    assert _ledger_by_basename(uploads, 'SRC')['lim.fits'] == (
        'upperlimit', '13.6758', '99.0000')


def test_coordinate_lightcurve_files_skip_refused_rows(tmp_path):
    results = [
        {'jd': '2461000.5', 'mag': '12.34', 'err': '0.05',
         'status': 'detection'},
        {'jd': '2461000.6', 'mag': '>15.10', 'err': '99.00',
         'status': 'upperlimit'},
        {'jd': '2461000.7', 'mag': '12.40', 'err': '0.05',
         'status': 'bad_wcs'},
        {'jd': '2461000.8', 'mag': '15.60', 'err': '99.00',
         'status': 'no_nearby_stars'},
        {'jd': '2461000.9', 'mag': '99.00', 'err': '99.00', 'status': 'edge'},
    ]
    lc_path, ul_path = nfp._write_lightcurve_data_files(str(tmp_path),
                                                        results)
    with open(lc_path) as fh:
        lc_rows = [line for line in fh if not line.startswith('#')]
    with open(ul_path) as fh:
        ul_rows = [line for line in fh if not line.startswith('#')]
    assert len(lc_rows) == 1 and lc_rows[0].startswith('2461000.50000 12.340')
    assert len(ul_rows) == 1 and ul_rows[0].startswith('2461000.60000 15.100')


class TestRunForcedPhotometryFrameChecks:
    """The unmw forced photometry asks util/forced_photometry for its
    frame-level checks and hands the refusal statuses back unchanged."""

    IMAGE = TestRunForcedPhotometryOffImage.IMAGE

    def test_checks_requested_and_inherited_overrides_dropped(
            self, monkeypatch, tmp_path):
        seen = {}

        def fake_run(cmd, cwd=None, env=None, timeout=None):
            seen['env'] = dict(env or {})
            return _FakeCompletedRun(1, '', 'ERROR: sky2xy failed\n')
        monkeypatch.setattr(nfp, '_run_capture_session', fake_run)
        monkeypatch.setenv('FORCED_PHOTOMETRY_STAR_CATALOG', '/x/other.wcscat')
        monkeypatch.setenv('FORCED_PHOTOMETRY_WCS_IMAGE', '/x/other.fits')
        nfp.run_forced_photometry_c(
            str(tmp_path), None, self.IMAGE, self.IMAGE,
            '18:31:15.62', '-05:34:31.9', 'V')
        assert seen['env'].get('FORCED_PHOTOMETRY_FRAME_CHECKS') == 'yes'
        assert 'FORCED_PHOTOMETRY_STAR_CATALOG' not in seen['env']
        assert 'FORCED_PHOTOMETRY_WCS_IMAGE' not in seen['env']

    def test_refusal_status_passes_through_with_its_values(
            self, monkeypatch, tmp_path):
        stdout = ('# aperture_diameter_pix: 3.0\n# target_pixel: 100 200\n'
                  '# C implementation:\n'
                  '2461000.5  12.3456  0.0500  bad_wcs  wcs_x.fits\n')
        monkeypatch.setattr(
            nfp, '_run_capture_session',
            lambda cmd, cwd=None, env=None, timeout=None:
            _FakeCompletedRun(0, stdout, ''))
        fp = nfp.run_forced_photometry_c(
            str(tmp_path), None, self.IMAGE, self.IMAGE,
            '18:31:15.62', '-05:34:31.9', 'V')
        assert (fp['status'], fp['mag'], fp['err']) == (
            'bad_wcs', '12.3456', '0.0500')


def test_listed_verdicts_earlier_conclusive_verdict_stands(tmp_path):
    root = str(tmp_path)
    now = 1790000000.0
    day = 86400

    def verdict_after(*lines):
        _write_file(os.path.join(root, nml.RUN_VERDICTS_BASENAME),
                    ''.join('%d %s %s X.fits\n' % (now - age, verdict, run)
                            for age, verdict, run in lines))
        return nml.RunVerdictResolver(root, now=now).verdict_for_image(
            '/q/img_a/wcs_fd_X.fits')[0]
    # a later run that failed or never finished does not reject the frame
    assert verdict_after((5 * day, 'ok', 'r1'), (4 * day, 'failed', 'r2')) == 'ok'
    assert verdict_after((5 * day, 'ok', 'r1'), (3 * day, 'pending', 'r2')) == 'ok'
    # a later run in progress makes the frame wait
    assert verdict_after((5 * day, 'ok', 'r1'), (60, 'pending', 'r2')) == 'pending'
    # a conclusive later verdict wins, either way
    assert verdict_after((5 * day, 'ok', 'r1'), (4 * day, 'error', 'r2')) == 'error'
    assert verdict_after((5 * day, 'error', 'r1'), (4 * day, 'ok', 'r2')) == 'ok'
    # with nothing conclusive, an unfinished run rejects the frame
    assert verdict_after((3 * day, 'pending', 'r1')) == 'failed'
    assert verdict_after((3 * day, 'failed', 'r1')) == 'failed'


def test_verdict_list_followed_while_it_grows(tmp_path):
    root = str(tmp_path)
    path = os.path.join(root, nml.RUN_VERDICTS_BASENAME)
    _write_file(path, '1000 ok results_A A.fits\n')
    resolver = nml.RunVerdictResolver(root, now=2000.0)
    assert resolver.verdict_for_image('/x/img_a/wcs_fd_A.fits')[0] == 'ok'
    assert resolver.verdict_for_image('/x/img_b/wcs_fd_B.fits')[0] is None
    # an upload processed while the manual run goes on
    with open(path, 'a') as fh:
        fh.write('1500 pending results_B B.fits\n1600 error resul')
    assert resolver.verdict_for_image('/x/img_b/wcs_fd_B.fits')[0] == 'pending'
    # the partial line is read once it is complete
    with open(path, 'a') as fh:
        fh.write('ts_B B.fits ERROR: passing clouds\n')
    assert resolver.verdict_for_image('/x/img_b/wcs_fd_B.fits')[:2] == (
        'error', 'ERROR: passing clouds')
    # a list that got shorter (replaced) is read anew
    _write_file(path, '1700 ok results_C B.fits\n')
    assert resolver.verdict_for_image('/x/img_b/wcs_fd_B.fits')[0] == 'ok'


def test_archive_copy_not_matched_to_a_run_that_used_it_as_reference(tmp_path):
    root = str(tmp_path / 'workdir')
    copy_in_archive = str(tmp_path / 'archive' / ('wcs_fd_' + _FRAME))
    _write_file(copy_in_archive)
    own_run = os.path.join(root, 'results_20260917_230531_' + _UPLOAD[4:])
    _write_file(os.path.join(own_run, 'index.html'), _COMPLETE_REPORT)
    _write_file(os.path.join(own_run, _FRAME + '_preview.png'))
    _write_file(os.path.join(own_run, 'fits_images_for_download.txt'),
                'Per-02-Q1b1x1 second-epoch /w/{}/{}\n'.format(_UPLOAD,
                                                               _FRAME))
    # A later run of the field the next day used the frame as its reference
    later_run = os.path.join(
        root, 'results_20260918_230000_2026-09-18_CI_Per-02-Q1b1x1_225959_'
        'TTUQ1b1x1_99_abcdEFGH')
    _write_file(os.path.join(later_run, 'index.html'),
                'ERROR: passing clouds\n' + _COMPLETE_REPORT)
    _write_file(os.path.join(later_run, _FRAME + '_preview.png'))
    _write_file(os.path.join(later_run, 'fits_images_for_download.txt'),
                'Per-02-Q1b1x1 reference /refs/' + _FRAME + '\n'
                'Per-02-Q1b1x1 second-epoch /w/img_y/Per-02-Q1b1x1_other.fits\n')
    assert nml.RunVerdictResolver(root).verdict_for_image(
        copy_in_archive)[0] == 'ok'


# ---------------------------------------------------------------------------
# The ingest's cloud verdicts reused by the manual modes, and the camera's
# bad-region list in the manual measurement path
# ---------------------------------------------------------------------------

def test_cloudy_status_matches_the_frame_quality_lib():
    assert nml.CLOUDY_STATUS == nfq.CLOUDY_STATUS


def test_frames_judged_cloudy_reads_every_ledger(tmp_path):
    uploads = str(tmp_path)
    _write_file(os.path.join(nml.source_dir_path(uploads, 'A'),
                             nml.LEDGER_BASENAME),
                '# image_basename JD mag err status camera\n'
                'wcs_fd_X.fits 2461000.5 12.0000 0.0200 cloudy C1\n'
                'wcs_fd_Y.fits 2461000.6 12.0000 0.0200 detection C1\n'
                'wcs_fd_Z.fits.fz 2461000.7 15.0000 99.0000 cloudy C1\n')
    # a row re-qualified by hand for B does not undo A's verdict on X, and
    # a 'manual' exclusion is not a cloud verdict
    _write_file(os.path.join(nml.source_dir_path(uploads, 'B'),
                             nml.LEDGER_BASENAME),
                'wcs_fd_X.fits 2461000.5 13.0000 0.0300 detection C1\n'
                'wcs_fd_W.fits 2461000.8 13.0000 0.0300 manual C1\n')
    assert nml.frames_judged_cloudy(uploads) == {'X.fits', 'Z.fits'}
    assert nml.frames_judged_cloudy(str(tmp_path / 'nowhere')) == set()


def test_unmeasured_cloudy_rows_are_listed_as_excluded():
    rows = [{'basename': 'a.fits', 'jd': '2461000.5', 'mag': '99.0000',
             'err': '99.0000', 'status': 'cloudy', 'camera': 'C1'},
            {'basename': 'b.fits', 'jd': '2461000.6', 'mag': '12.3000',
             'err': '0.0400', 'status': 'cloudy', 'camera': 'C1'}]
    det, lim, excluded = nml.classify_ledger_rows(rows)
    assert not det and not lim
    by_name = {r['basename']: r for r in excluded}
    assert by_name['a.fits']['reason'] == nml.REASON_CLOUDY
    assert by_name['a.fits']['mag_float'] is None
    assert by_name['b.fits']['mag_float'] == 12.3
    assert by_name['b.fits']['err_float'] == 0.04


def test_manual_measurement_reuses_the_ingest_cloud_verdicts(tmp_path,
                                                             monkeypatch):
    import monitoring_update as mu
    root = str(tmp_path / 'workdir')
    os.makedirs(nml.source_dir_path(root, 'SRC'))
    # the ingest condemned these frames while measuring another source
    _write_file(os.path.join(nml.source_dir_path(root, 'OTHER'),
                             nml.LEDGER_BASENAME),
                ''.join('wcs_fd_{}.fits 2461000.5 12.0 0.02 cloudy C1\n'.format(n)
                        for n in ('cloudydet', 'cloudyedge',
                                  'cloudyrejected', 'cloudybusy')))
    now = 1790000000.0
    _write_file(os.path.join(root, nml.RUN_VERDICTS_BASENAME),
                '%d error results_A cloudyrejected.fits ERROR: x\n'
                '%d pending results_B cloudybusy.fits\n' % (now, now - 60))
    entry = {'source_id': 'SRC', 'name': 'SRC', 'ra': '01:00:00.00',
             'dec': '+10:00:00.0'}
    names = ('cloudydet', 'cloudyedge', 'cloudyrejected', 'cloudybusy',
             'clear')
    images = [os.path.join(root, 'img_A', 'wcs_fd_{}.fits'.format(n))
              for n in names]
    for image in images:
        _write_file(image)
    monkeypatch.setattr(mu, 'read_factory_text', lambda cfg: '')
    monkeypatch.setattr(mu, 'sky2xy_on_image',
                        lambda vast_dir, path, ra, dec: (100.0, 100.0))
    monkeypatch.setattr(nfp, 'get_jd_and_atel_date',
                        lambda vast_dir, path: ('2461000.5000', None))
    monkeypatch.setattr(nfp, 'camera_settings_for_path',
                        lambda text, path: 'C1')
    monkeypatch.setattr(nfp, 'derive_band', lambda text, path, default: 'V')
    monkeypatch.setattr(nfp, 'derive_sextractor_config',
                        lambda text, path: None)
    work_dir = str(tmp_path / 'work')

    def fake_setup(vast_dir, parent_dir, prefix=None):
        os.makedirs(work_dir, exist_ok=True)
        return work_dir

    def fake_solve(work, local_config_path, todo, workers, skip_log):
        return None, None, None, {p: p for p in todo}, None

    def fake_measure(work, local_config_path, img, compute_path, ra, dec,
                     band, debug_log=None, off_image_as_edge=False,
                     edge_margin_pix=None):
        if 'edge' in img:
            return {'jd': '2461000.5000', 'mag': '99.0000',
                    'err': '99.0000', 'status': 'edge'}
        return {'jd': '2461000.5000', 'mag': '12.3456', 'err': '0.0210',
                'status': 'detection'}
    monkeypatch.setattr(nfp, 'setup_vast_working_copy', fake_setup)
    monkeypatch.setattr(nfp, '_phase1_parallel_solve_plate', fake_solve)
    monkeypatch.setattr(nfp, 'run_forced_photometry_c', fake_measure)
    mu.measure_images_for_source(
        {}, None, entry, images, root,
        verdicts=nml.RunVerdictResolver(root, now=now))
    # the condemned frame is measured and its detection recorded as cloudy
    # with the values; an edge result keeps its status; the rejected run wins
    # over the cloud verdict; the run in progress is left alone
    assert _ledger_by_basename(root, 'SRC') == {
        'wcs_fd_cloudydet.fits': ('cloudy', '12.3456', '0.0210'),
        'wcs_fd_cloudyedge.fits': ('edge', '99.0000', '99.0000'),
        'wcs_fd_cloudyrejected.fits': ('run_error', '99.0000', '99.0000'),
        'wcs_fd_clear.fits': ('detection', '12.3456', '0.0210')}


def test_frames_judged_cloudy_reports_an_unreadable_ledger(tmp_path):
    uploads = str(tmp_path)
    path = os.path.join(nml.source_dir_path(uploads, 'A'),
                        nml.LEDGER_BASENAME)
    _write_file(path, 'wcs_fd_X.fits 2461000.5 12.0 0.02 cloudy C1\n')
    os.chmod(path, 0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip('running as a user who can read anything')
        messages = []
        assert nml.frames_judged_cloudy(uploads, messages.append) == set()
        assert len(messages) == 1 and 'cannot read' in messages[0]
    finally:
        os.chmod(path, 0o644)


_FACTORY_CAMERA_BLOCKS = '''
if [ -n "$CAMERA_SETTINGS" ];then
 if [ "$CAMERA_SETTINGS" = "CAM_ABS" ];then
  #BAD_REGION_FILE="../old_bad_region.lst"
  BAD_REGION_FILE="$NMW_CALIBRATION/$CAMERA_SETTINGS/CAM_bad_region.lst"
 fi
 if [ "$CAMERA_SETTINGS" = "CAM_BRACES" ];then
  BAD_REGION_FILE="${NMW_CALIBRATION}/${CAMERA_SETTINGS}_bad_region.lst"
 fi
 if [ "$CAMERA_SETTINGS" = "CAM_REL" ];then
  BAD_REGION_FILE="../REL_bad_region.lst"
 fi
 if [ "$CAMERA_SETTINGS" = "CAM_UNKNOWN_VAR" ];then
  BAD_REGION_FILE="$SOMEWHERE/x.lst"
 fi
 if [ "$CAMERA_SETTINGS" = "CAM_NONE" ];then
  SEXTRACTOR_CONFIG_FILES="default.sex"
 fi
fi
'''


def test_bad_region_file_for_camera():
    work = '/u/uploads/vast_monitoring_x'
    assert nfp.bad_region_file_for_camera(
        _FACTORY_CAMERA_BLOCKS, 'CAM_ABS', '/cal', work) == \
        '/cal/CAM_ABS/CAM_bad_region.lst'
    assert nfp.bad_region_file_for_camera(
        _FACTORY_CAMERA_BLOCKS, 'CAM_BRACES', '/cal', work) == \
        '/cal/CAM_BRACES_bad_region.lst'
    assert nfp.bad_region_file_for_camera(
        _FACTORY_CAMERA_BLOCKS, 'CAM_REL', '/cal', work) == \
        '/u/uploads/REL_bad_region.lst'
    for camera in ('CAM_UNKNOWN_VAR', 'CAM_NONE', 'CAM_MISSING', ''):
        assert nfp.bad_region_file_for_camera(
            _FACTORY_CAMERA_BLOCKS, camera, '/cal', work) is None


def test_install_bad_region_list_per_camera(tmp_path, monkeypatch):
    import monitoring_update as mu
    calibration = tmp_path / 'cal'
    _write_file(str(calibration / 'CAM_ABS' / 'CAM_bad_region.lst'),
                '7500 0 9576 6388\n')
    monkeypatch.setattr(nml, 'resolve_nmw_calibration_dir',
                        lambda: str(calibration))
    work = tmp_path / 'work'
    work.mkdir()
    assert mu.install_bad_region_list(str(work), _FACTORY_CAMERA_BLOCKS,
                                      'CAM_ABS', '0 0 0 0\n')
    assert (work / 'bad_region.lst').read_text() == '7500 0 9576 6388\n'
    # a camera without a list of its own gets the VaST default back
    assert mu.install_bad_region_list(str(work), _FACTORY_CAMERA_BLOCKS,
                                      'CAM_NONE', '0 0 0 0\n')
    assert (work / 'bad_region.lst').read_text() == '0 0 0 0\n'
    # a failed copy reports failure and leaves no partial list behind
    monkeypatch.setattr(shutil, 'copyfile', _raise_oserror)
    assert not mu.install_bad_region_list(str(work), _FACTORY_CAMERA_BLOCKS,
                                          'CAM_ABS', '0 0 0 0\n')
    assert (work / 'bad_region.lst').read_text() == '0 0 0 0\n'
    assert sorted(os.listdir(str(work))) == ['bad_region.lst']


def _raise_oserror(*args, **kwargs):
    raise OSError('simulated failure')


def test_manual_measurement_installs_each_camera_list_before_solving(
        tmp_path, monkeypatch):
    import monitoring_update as mu
    calibration = tmp_path / 'cal'
    _write_file(str(calibration / 'CAM_ABS' / 'CAM_bad_region.lst'),
                'CAM_ABS list\n')
    monkeypatch.setattr(nml, 'resolve_nmw_calibration_dir',
                        lambda: str(calibration))
    root = str(tmp_path / 'workdir')
    os.makedirs(nml.source_dir_path(root, 'SRC'))
    entry = {'source_id': 'SRC', 'name': 'SRC', 'ra': '01:00:00.00',
             'dec': '+10:00:00.0'}
    images = [os.path.join(root, 'img_A', name) for name in (
        'wcs_fd_a1.fits', 'wcs_fd_n1.fits', 'wcs_fd_a2.fits')]
    for image in images:
        _write_file(image)
    monkeypatch.setattr(mu, 'read_factory_text',
                        lambda cfg: _FACTORY_CAMERA_BLOCKS)
    monkeypatch.setattr(nfp, 'camera_settings_for_path',
                        lambda text, path: 'CAM_ABS' if '_a' in path
                        else 'CAM_NONE')
    monkeypatch.setattr(nfp, 'derive_band', lambda text, path, default: 'V')
    monkeypatch.setattr(nfp, 'derive_sextractor_config',
                        lambda text, path: None)
    work_dir = str(tmp_path / 'work')
    seen = []

    def fake_setup(vast_dir, parent_dir, prefix=None):
        _write_file(os.path.join(work_dir, 'bad_region.lst'), 'DEFAULT\n')
        return work_dir

    def fake_solve(work, local_config_path, todo, workers, skip_log):
        with open(os.path.join(work, 'bad_region.lst')) as fh:
            seen.append((sorted(os.path.basename(p) for p in todo),
                         fh.read()))
        return None, None, None, {}, None
    monkeypatch.setattr(nfp, 'setup_vast_working_copy', fake_setup)
    monkeypatch.setattr(nfp, '_phase1_parallel_solve_plate', fake_solve)
    mu.measure_images_for_source({}, None, entry, images, root,
                                 verdicts=nml.RunVerdictResolver(root))
    assert seen == [(['wcs_fd_a1.fits', 'wcs_fd_a2.fits'], 'CAM_ABS list\n'),
                    (['wcs_fd_n1.fits'], 'DEFAULT\n')]


# ---------------------------------------------------------------------------
# Frames excluded by hand: excluded for every monitored source on them, and
# for the sources activated later (monitoring_excluded_frames.txt)
# ---------------------------------------------------------------------------

def _ledger_rows(*lines):
    return '# image_basename JD mag err status camera\n' + ''.join(
        line + '\n' for line in lines)


def _write_ledger(uploads, source_id, *lines):
    _write_file(os.path.join(nml.source_dir_path(uploads, source_id),
                             nml.LEDGER_BASENAME), _ledger_rows(*lines))


def _entries(source_ids):
    return [{'source_id': sid, 'name': sid, 'ra': '01:00:00.00',
             'dec': '+10:00:00.0'} for sid in source_ids]


def _patch_exclusion_context(monkeypatch, uploads, source_ids,
                             real_rebuild=False):
    """Patch the updater's context. The products are rebuilt for real with
    real_rebuild; otherwise the stub records, per call, the source and the
    set of frames excluded by hand it was given."""
    import monitoring_update as mu
    entries = _entries(source_ids)
    rebuilt = []
    rebuilt_frames = {}
    messages = []
    monkeypatch.setattr(mu, 'load_context',
                        lambda: (uploads, {}, uploads, None))
    monkeypatch.setattr(mu, 'list_entries_or_exit',
                        lambda: ('monitoring_list.txt', entries))
    monkeypatch.setattr(mu, 'read_factory_text', lambda cfg: '')
    monkeypatch.setattr(mu, 'log', messages.append)
    if not real_rebuild:
        def fake_rebuild(uploads_dir, entry, cfg, factory_text,
                         excluded_frames=None):
            rebuilt.append(entry['source_id'])
            rebuilt_frames[entry['source_id']] = excluded_frames
        monkeypatch.setattr(nml, 'rebuild_source_products', fake_rebuild)
        monkeypatch.setattr(nml, 'rebuild_central_index',
                            lambda *args, **kwargs: None)
    return mu, rebuilt, rebuilt_frames, messages


def _aavso_jds(uploads, source_id):
    path = os.path.join(nml.source_dir_path(uploads, source_id),
                        nml.AAVSO_BASENAME)
    with open(path) as fh:
        return sorted(line.split(',')[1] for line in fh
                      if line.strip() and not line.startswith('#'))


def test_excluded_frames_list_roundtrip(tmp_path):
    uploads = str(tmp_path)
    assert nml.read_excluded_frames(uploads) is None
    old_umask = os.umask(0o077)
    try:
        nml.write_excluded_frames(uploads, {'B.fits': '2026-10-05T00:00:00Z x',
                                            'A.fits': ''})
    finally:
        os.umask(old_umask)
    path = nml.excluded_frames_path(uploads)
    assert os.path.basename(path) == nml.EXCLUDED_FRAMES_BASENAME
    # world-readable whatever the umask of the writer
    assert os.stat(path).st_mode & 0o777 == 0o644
    assert nml.read_excluded_frames(uploads) == {
        'A.fits': '', 'B.fits': '2026-10-05T00:00:00Z x'}
    # every copy name of a frame is read as its core name; comments and
    # blank lines are skipped; the first line of a frame wins
    with open(path, 'a') as fh:
        fh.write('\n# a comment\nwcs_fd_C.fits.fz 2026 note\nC.fits later\n')
    frames = nml.read_excluded_frames(uploads)
    assert set(frames) == {'A.fits', 'B.fits', 'C.fits'}
    assert frames['C.fits'] == '2026 note'
    # a list without frames is still a list (no fallback to the ledgers)
    nml.write_excluded_frames(uploads, {})
    assert nml.read_excluded_frames(uploads) == {}


def test_empty_or_headerless_list_is_refused(tmp_path):
    uploads = str(tmp_path)
    path = nml.excluded_frames_path(uploads)
    for content in ('', 'X.fits 2026 note\n'):
        with open(path, 'w') as fh:
            fh.write(content)
        with pytest.raises(nml.ExcludedFramesUnreadable):
            nml.read_excluded_frames(uploads)
        with pytest.raises(nml.ExcludedFramesUnreadable):
            nml.excluded_frame_set(uploads)


def test_excluded_frame_set_is_the_list_or_else_the_manual_rows(tmp_path):
    uploads = str(tmp_path)
    _write_ledger(uploads, 'A', 'wcs_fd_M.fits 2461000.5 12.0 0.02 manual C1',
                  'wcs_fd_N.fits 2461000.6 12.0 0.02 detection C1')
    _write_ledger(uploads, 'FROZEN',
                  'wcs_fd_F.fits 2461000.5 12.0 0.02 manual C1')
    # no list yet: the manual rows of every registry ledger stand in for it
    assert nml.excluded_frame_set(uploads) == {'M.fits', 'F.fits'}
    assert nml.frames_with_manual_rows(uploads, ['A', 'FROZEN']) == {
        'M.fits': ['A'], 'F.fits': ['FROZEN']}
    assert nml.registry_source_ids(uploads) == ['A', 'FROZEN']
    # the first locked run writes them into the list, which then rules alone
    listed, created = nml.ensure_excluded_frames_list(uploads)
    assert created and set(listed) == {'M.fits', 'F.fits'}
    assert 'migrated from the manual rows of A' in listed['M.fits']
    nml.write_excluded_frames(uploads, {'L.fits': ''})
    assert nml.excluded_frame_set(uploads) == {'L.fits'}
    assert nml.ensure_excluded_frames_list(uploads) == ({'L.fits': ''}, False)


def test_migration_is_not_written_when_a_ledger_cannot_be_read(tmp_path):
    uploads = str(tmp_path)
    _write_ledger(uploads, 'A', 'wcs_fd_M.fits 2461000.5 12.0 0.02 manual C1')
    path = os.path.join(nml.source_dir_path(uploads, 'A'), nml.LEDGER_BASENAME)
    os.chmod(path, 0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip('running as a user who can read anything')
        with pytest.raises(nml.ExcludedFramesUnreadable):
            nml.ensure_excluded_frames_list(uploads)
    finally:
        os.chmod(path, 0o644)
    assert nml.read_excluded_frames(uploads) is None
    # once the ledger is readable again the migration does happen
    listed, created = nml.ensure_excluded_frames_list(uploads)
    assert created and set(listed) == {'M.fits'}


def test_unreadable_list_stops_every_mode_without_changes(tmp_path,
                                                         monkeypatch):
    uploads = str(tmp_path)
    _write_ledger(uploads, 'A', 'wcs_fd_X.fits 2461000.5 12.0 0.02 detection C1',
                  'wcs_fd_Y.fits 2461000.6 12.1 0.02 manual C1')
    nml.write_excluded_frames(uploads, {'Y.fits': 't', 'Q.fits': 't'})
    path = nml.excluded_frames_path(uploads)
    with open(path) as fh:
        content = fh.read()
    mu, rebuilt, _, messages = _patch_exclusion_context(monkeypatch, uploads,
                                                        ['A'])
    os.chmod(path, 0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip('running as a user who can read anything')
        with pytest.raises(nml.ExcludedFramesUnreadable):
            nml.read_excluded_frames(uploads)
        assert mu.mode_flag_measurement('X.fits', restore=False) == 1
        assert mu.mode_flag_measurement('Y.fits', restore=True) == 1
        assert mu.mode_sync_exclusions() == 1
        assert mu.mode_rebuild_pages() == 1
        raw = tmp_path / 'raw.txt'
        raw.write_text('A wcs_fd_Q.fits 2461000.7 12.5 0.05 detection C1\n')
        assert mu.mode_ingest(str(raw)) == 1
    finally:
        os.chmod(path, 0o644)
    with open(path) as fh:
        assert fh.read() == content
    assert _ledger_by_basename(uploads, 'A') == {
        'wcs_fd_X.fits': ('detection', '12.0', '0.02'),
        'wcs_fd_Y.fits': ('manual', '12.1', '0.02')}
    assert rebuilt == []
    assert any('cannot read' in m for m in messages)


def test_products_never_publish_a_listed_frame(tmp_path, monkeypatch):
    monkeypatch.setenv('MPLCONFIGDIR', str(tmp_path / 'mpl'))
    uploads = str(tmp_path / 'uploads')
    # e.g. a source re-added with the registry directory it had before the
    # frame was excluded: its ledger still says detection
    _write_ledger(uploads, 'SRC',
                  'wcs_fd_ok.fits 2461000.4 12.1000 0.0300 detection C1',
                  'wcs_fd_X.fits 2461000.5 12.3000 0.0300 detection C1',
                  'wcs_fd_L.fits 2461000.6 14.0000 99.0000 upperlimit C1')
    nml.write_excluded_frames(uploads, {'X.fits': 't', 'L.fits': 't'})
    rows, _ = nml.read_ledger(nml.source_dir_path(uploads, 'SRC'))
    det, lim, excl = nml.classify_ledger_rows(rows, {'X.fits', 'L.fits'})
    assert [r['basename'] for r in det] == ['wcs_fd_ok.fits'] and not lim
    assert all(r['reason'] == nml.REASON_MANUAL for r in excl)
    nml.rebuild_source_products(uploads, _entries(['SRC'])[0], {}, '')
    source_dir = nml.source_dir_path(uploads, 'SRC')
    with open(os.path.join(source_dir, nml.LIGHTCURVE_BASENAME)) as fh:
        published = [line for line in fh if not line.startswith('#')]
    assert len(published) == 1 and ' 12.1000 ' in published[0]
    assert _aavso_jds(uploads, 'SRC') == ['2461000.40000']
    with open(os.path.join(source_dir,
                           nml.EXCLUDED_MEASUREMENTS_BASENAME)) as fh:
        text = fh.read()
    assert 'manual_exclusion wcs_fd_X.fits' in text
    assert 'manual_exclusion wcs_fd_L.fits' in text


def test_central_index_counts_what_the_products_publish(tmp_path,
                                                       monkeypatch):
    monkeypatch.setenv('MPLCONFIGDIR', str(tmp_path / 'mpl'))
    uploads = str(tmp_path / 'uploads')
    _write_ledger(uploads, 'SRC',
                  'wcs_fd_a.fits 2461000.4 12.0000 0.0300 detection C1',
                  'wcs_fd_b.fits 2461001.4 13.0000 0.0300 detection C1',
                  'wcs_fd_X.fits 2461002.4 12.3000 0.0300 detection C1')
    nml.write_detection_threshold(nml.source_dir_path(uploads, 'SRC'), 12.5)
    nml.write_excluded_frames(uploads, {'X.fits': 't'})
    nml.rebuild_central_index(uploads, _entries(['SRC']), '')
    with open(os.path.join(nml.monitoring_root(uploads), 'index.html')) as fh:
        page = fh.read()
    # one detection (12.0), one upper limit (13.0 above the threshold), the
    # excluded frame not counted
    assert re.search(r'<td>1</td><td>1</td>', page)


def test_rewrite_measurement_status_matches_every_copy_of_a_frame(tmp_path):
    uploads = str(tmp_path)
    _write_ledger(uploads, 'SRC',
                  'fd_X.fits.fz 2461000.5 12.0000 0.0200 detection C1',
                  'wcs_fd_Y.fits 2461000.6 12.5000 0.0300 detection C1')
    assert nml.rewrite_measurement_status(uploads, 'SRC',
                                          '/archive/wcs_fd_X.fits') == 1
    assert _ledger_by_basename(uploads, 'SRC')['fd_X.fits.fz'][0] == 'manual'
    assert nml.rewrite_measurement_status(uploads, 'SRC', 'X.fits',
                                          restore=True) == 1
    ledger = _ledger_by_basename(uploads, 'SRC')
    assert ledger['fd_X.fits.fz'] == ('detection', '12.0000', '0.0200')
    assert ledger['wcs_fd_Y.fits'][0] == 'detection'


def test_exclude_measurement_excludes_the_frame_for_every_source(
        tmp_path, monkeypatch):
    uploads = str(tmp_path)
    _write_ledger(uploads, 'A',
                  'wcs_fd_X.fits 2461000.5 12.0000 0.0200 detection C1')
    _write_ledger(uploads, 'B',
                  'fd_X.fits.fz 2461000.5 15.1000 99.0000 upperlimit C1')
    _write_ledger(uploads, 'C',
                  'wcs_fd_X.fits 2461000.5 99.0000 99.0000 edge C1')
    _write_ledger(uploads, 'D',
                  'wcs_fd_Y.fits 2461000.6 12.3000 0.0200 detection C1')
    mu, rebuilt, frames, _ = _patch_exclusion_context(
        monkeypatch, uploads, ['A', 'B', 'C', 'D'])
    assert mu.mode_flag_measurement('/data/wcs_fd_X.fits.fz',
                                    restore=False) == 0
    assert _ledger_by_basename(uploads, 'A')['wcs_fd_X.fits'][0] == 'manual'
    assert _ledger_by_basename(uploads, 'B')['fd_X.fits.fz'] == (
        'manual', '15.1000', '99.0000')
    assert _ledger_by_basename(uploads, 'C')['wcs_fd_X.fits'][0] == 'edge'
    assert _ledger_by_basename(uploads, 'D')['wcs_fd_Y.fits'][0] == 'detection'
    # every source with a row on the frame is rebuilt with the frame excluded
    assert sorted(rebuilt) == ['A', 'B', 'C']
    assert all('X.fits' in frames[sid] for sid in ('A', 'B', 'C'))
    assert set(nml.read_excluded_frames(uploads)) == {'X.fits'}
    # excluding it again succeeds and rebuilds the same sources
    del rebuilt[:]
    assert mu.mode_flag_measurement('X.fits', restore=False) == 0
    assert sorted(rebuilt) == ['A', 'B', 'C']
    assert set(nml.read_excluded_frames(uploads)) == {'X.fits'}
    # a frame no activated source has measured is refused (typo guard)
    assert mu.mode_flag_measurement('wcs_fd_nosuch.fits', restore=False) == 1
    assert set(nml.read_excluded_frames(uploads)) == {'X.fits'}


def test_restore_measurement_restores_the_frame_for_every_source(
        tmp_path, monkeypatch):
    uploads = str(tmp_path)
    _write_ledger(uploads, 'A',
                  'wcs_fd_X.fits 2461000.5 12.0000 0.0200 manual C1',
                  'wcs_fd_Z.fits 2461000.7 12.2000 0.0200 manual C1')
    _write_ledger(uploads, 'B',
                  'wcs_fd_X.fits 2461000.5 15.1000 99.0000 manual C1',
                  'wcs_fd_Z.fits 2461000.7 12.9000 0.0300 cloudy C1')
    nml.write_excluded_frames(uploads, {'X.fits': 't', 'Z.fits': 't'})
    mu, rebuilt, frames, _ = _patch_exclusion_context(monkeypatch, uploads,
                                                      ['A', 'B'])
    assert mu.mode_flag_measurement('X.fits', restore=True) == 0
    assert _ledger_by_basename(uploads, 'A')['wcs_fd_X.fits'][0] == 'detection'
    assert _ledger_by_basename(uploads, 'B')['wcs_fd_X.fits'][0] == 'upperlimit'
    assert set(nml.read_excluded_frames(uploads)) == {'Z.fits'}
    assert all('X.fits' not in frames[sid] for sid in ('A', 'B'))
    # restoring a frame that is not excluded changes nothing and says so
    assert mu.mode_flag_measurement('X.fits', restore=True) == 1
    # a restore undoes the exclusion: back to the measured status, even on a
    # frame another source has as cloudy (that row stays cloudy)
    assert mu.mode_flag_measurement('wcs_fd_Z.fits', restore=True) == 0
    assert _ledger_by_basename(uploads, 'A')['wcs_fd_Z.fits'] == (
        'detection', '12.2000', '0.0200')
    assert _ledger_by_basename(uploads, 'B')['wcs_fd_Z.fits'][0] == 'cloudy'
    assert nml.read_excluded_frames(uploads) == {}


def test_restore_publishes_again_even_where_only_the_list_held_it(
        tmp_path, monkeypatch):
    monkeypatch.setenv('MPLCONFIGDIR', str(tmp_path / 'mpl'))
    uploads = str(tmp_path / 'uploads')
    _write_ledger(uploads, 'A', 'wcs_fd_X.fits 2461000.5 12.0000 0.0200 manual C1')
    # C's row was never flipped (re-added source, raced ingest); only the
    # list keeps it out of the products
    _write_ledger(uploads, 'C', 'wcs_fd_X.fits 2461000.5 13.0000 0.0300 detection C1',
                  'wcs_fd_c.fits 2461001.5 13.1000 0.0300 detection C1')
    nml.write_excluded_frames(uploads, {'X.fits': 't'})
    for sid in ('A', 'C'):
        nml.rebuild_source_products(uploads, _entries([sid])[0], {}, '')
    assert _aavso_jds(uploads, 'C') == ['2461001.50000']
    # an interrupted restore: the list was rewritten, nothing else happened
    nml.write_excluded_frames(uploads, {})
    mu, _, _, messages = _patch_exclusion_context(
        monkeypatch, uploads, ['A', 'C'], real_rebuild=True)
    assert mu.mode_flag_measurement('X.fits', restore=True) == 0
    assert _aavso_jds(uploads, 'A') == ['2461000.50000']
    assert _aavso_jds(uploads, 'C') == ['2461000.50000', '2461001.50000']


def test_frozen_sources_are_left_alone(tmp_path, monkeypatch):
    uploads = str(tmp_path)
    _write_ledger(uploads, 'A',
                  'wcs_fd_X.fits 2461000.5 12.0000 0.0200 detection C1')
    frozen_rows = ('wcs_fd_X.fits 2461000.5 13.0000 0.0300 detection C1',
                   'wcs_fd_W.fits 2461000.9 13.1000 0.0300 manual C1')
    _write_ledger(uploads, 'FROZEN', *frozen_rows)
    nml.write_excluded_frames(uploads, {'W.fits': 't'})
    mu, rebuilt, _, _ = _patch_exclusion_context(monkeypatch, uploads, ['A'])
    path = os.path.join(nml.source_dir_path(uploads, 'FROZEN'),
                        nml.LEDGER_BASENAME)
    for call in (lambda: mu.mode_flag_measurement('X.fits', restore=False),
                 lambda: mu.mode_flag_measurement('W.fits', restore=True),
                 mu.mode_sync_exclusions):
        call()
        with open(path) as fh:
            assert fh.read() == _ledger_rows(*frozen_rows)
    assert 'FROZEN' not in rebuilt


def test_exclusion_command_line_takes_one_image_and_no_source(monkeypatch):
    import monitoring_update as mu

    def refuse(*args, **kwargs):
        raise AssertionError('must not be called')
    monkeypatch.setattr(mu, 'mode_flag_measurement', refuse)
    monkeypatch.delenv('GATEWAY_INTERFACE', raising=False)
    assert mu.main(['monitoring_update.py', '--exclude-measurement',
                    'X.fits', '--source', 'A']) == 1
    assert mu.main(['monitoring_update.py', '--restore-measurement',
                    'X.fits', 'extra']) == 1


def test_exclusion_reports_what_the_rebuild_publishes_elsewhere(tmp_path,
                                                              monkeypatch):
    monkeypatch.setenv('MPLCONFIGDIR', str(tmp_path / 'mpl'))
    uploads = str(tmp_path / 'uploads')
    _write_ledger(uploads, 'A',
                  'wcs_fd_V2.fits 2461000.50100 12.4000 0.0200 detection C1')
    # B's visit mixes a detection and an upper limit: held back whole
    _write_ledger(uploads, 'B',
                  'wcs_fd_V1.fits 2461000.50000 12.0000 0.0200 detection C1',
                  'wcs_fd_V2.fits 2461000.50100 13.5000 99.0000 upperlimit C1')
    for sid in ('A', 'B'):
        nml.rebuild_source_products(uploads, _entries([sid])[0], {}, '')
    assert _aavso_jds(uploads, 'B') == []
    mu, _, _, messages = _patch_exclusion_context(
        monkeypatch, uploads, ['A', 'B'], real_rebuild=True)
    assert mu.mode_flag_measurement('V2.fits', restore=False) == 0
    assert _aavso_jds(uploads, 'B') == ['2461000.50000']
    published = [m for m in messages if 'NOW PUBLISHED' in m]
    assert len(published) == 1 and 'B:' in published[0] \
        and '2461000.50000' in published[0]
    # A's own point on the excluded frame is not reported as a side effect
    assert not any('withdrawn' in m and 'A:' in m for m in messages)
    del messages[:]
    assert mu.mode_flag_measurement('V2.fits', restore=True) == 0
    assert any('withdrawn' in m and '2461000.50000' in m for m in messages)


def test_rerunning_an_interrupted_exclusion_rebuilds_stale_sources(
        tmp_path, monkeypatch):
    uploads = str(tmp_path)
    _write_ledger(uploads, 'A', 'wcs_fd_X.fits 2461000.5 12.0 0.02 manual C1')
    _write_ledger(uploads, 'B', 'wcs_fd_X.fits 2461000.5 13.0 0.03 manual C1')
    nml.write_excluded_frames(uploads, {'X.fits': 't'})
    # B's rows were flipped but its products were never rebuilt
    stale_aavso = os.path.join(nml.source_dir_path(uploads, 'B'),
                               nml.AAVSO_BASENAME)
    _write_file(stale_aavso, 'B,2461000.5,13.0\n')
    os.utime(stale_aavso, (1000000000, 1000000000))
    mu, rebuilt, frames, _ = _patch_exclusion_context(monkeypatch, uploads,
                                                      ['A', 'B'])
    assert mu.mode_flag_measurement('X.fits', restore=False) == 0
    assert 'B' in rebuilt and 'X.fits' in frames['B']


def test_locked_runs_apply_a_newly_created_list_at_once(tmp_path,
                                                       monkeypatch):
    uploads = str(tmp_path)
    # excluded for GB6 only before the list existed; RX And measured later
    _write_ledger(uploads, 'GB6', 'wcs_fd_G.fits 2461000.5 12.0 0.02 manual C1')
    _write_ledger(uploads, 'RX', 'wcs_fd_G.fits 2461000.5 13.0 0.03 detection C1')
    _write_ledger(uploads, 'OTHER', 'wcs_fd_O.fits 2461000.9 13.0 0.03 detection C1')
    mu, rebuilt, frames, _ = _patch_exclusion_context(
        monkeypatch, uploads, ['GB6', 'RX', 'OTHER'])
    # any locked run will do - here a threshold for an unrelated source
    assert mu.mode_set_threshold('OTHER', 15.0) == 0
    assert set(nml.read_excluded_frames(uploads)) == {'G.fits'}
    assert _ledger_by_basename(uploads, 'RX')['wcs_fd_G.fits'][0] == 'manual'
    assert 'RX' in rebuilt and 'G.fits' in frames['RX']


def test_ingest_records_rows_on_excluded_frames_as_manual(tmp_path,
                                                          monkeypatch):
    uploads = str(tmp_path)
    os.makedirs(nml.source_dir_path(uploads, 'NEW'))
    nml.write_excluded_frames(uploads, {'X.fits': 't', 'C.fits': 't'})
    # once the list exists, a manual row elsewhere no longer excludes a frame
    _write_ledger(uploads, 'OLD', 'wcs_fd_Z.fits 2461000.7 12.0 0.02 manual C1')
    mu, _, frames, _ = _patch_exclusion_context(monkeypatch, uploads,
                                                ['NEW', 'OLD'])
    raw = tmp_path / 'raw.txt'
    raw.write_text(
        'NEW wcs_fd_X.fits 2461000.5 12.5000 0.0500 detection C1\n'
        'NEW wcs_fd_C.fits 2461000.55 12.6000 0.0500 cloudy C1\n'
        'NEW wcs_fd_Z.fits 2461000.7 15.1000 99.0000 upperlimit C1\n'
        'NEW wcs_fd_E.fits 2461000.8 99.0000 99.0000 edge C1\n'
        'NEW wcs_fd_Y.fits 2461000.6 12.4000 0.0400 detection C1\n')
    assert mu.mode_ingest(str(raw)) == 0
    assert _ledger_by_basename(uploads, 'NEW') == {
        'wcs_fd_X.fits': ('manual', '12.5000', '0.0500'),
        'wcs_fd_C.fits': ('cloudy', '12.6000', '0.0500'),
        'wcs_fd_Z.fits': ('upperlimit', '15.1000', '99.0000'),
        'wcs_fd_E.fits': ('edge', '99.0000', '99.0000'),
        'wcs_fd_Y.fits': ('detection', '12.4000', '0.0400')}
    assert frames['NEW'] == {'X.fits', 'C.fits'}


def test_manual_measurement_applies_frames_excluded_by_hand(tmp_path,
                                                            monkeypatch):
    import monitoring_update as mu
    root = str(tmp_path / 'workdir')
    os.makedirs(nml.source_dir_path(root, 'SRC'))
    nml.write_excluded_frames(root, {'excl.fits': 't', 'excledge.fits': 't',
                                     'both.fits': 't'})
    _write_file(os.path.join(nml.source_dir_path(root, 'OTHER'),
                             nml.LEDGER_BASENAME),
                'wcs_fd_both.fits 2461000.5 12.0 0.02 cloudy C1\n')
    entry = _entries(['SRC'])[0]
    names = ('excl', 'excledge', 'both', 'clear')
    images = [os.path.join(root, 'img_A', 'wcs_fd_{}.fits'.format(n))
              for n in names]
    for image in images:
        _write_file(image)
    monkeypatch.setattr(mu, 'read_factory_text', lambda cfg: '')
    monkeypatch.setattr(mu, 'sky2xy_on_image',
                        lambda vast_dir, path, ra, dec: (100.0, 100.0))
    monkeypatch.setattr(nfp, 'get_jd_and_atel_date',
                        lambda vast_dir, path: ('2461000.5000', None))
    monkeypatch.setattr(nfp, 'camera_settings_for_path',
                        lambda text, path: 'C1')
    monkeypatch.setattr(nfp, 'derive_band', lambda text, path, default: 'V')
    monkeypatch.setattr(nfp, 'derive_sextractor_config',
                        lambda text, path: None)
    work_dir = str(tmp_path / 'work')

    def fake_setup(vast_dir, parent_dir, prefix=None):
        os.makedirs(work_dir, exist_ok=True)
        return work_dir

    def fake_solve(work, local_config_path, todo, workers, skip_log):
        return None, None, None, {p: p for p in todo}, None

    def fake_measure(work, local_config_path, img, compute_path, ra, dec,
                     band, debug_log=None, off_image_as_edge=False,
                     edge_margin_pix=None):
        if 'edge' in img:
            return {'jd': '2461000.5000', 'mag': '99.0000',
                    'err': '99.0000', 'status': 'edge'}
        return {'jd': '2461000.5000', 'mag': '12.3456', 'err': '0.0210',
                'status': 'detection'}
    monkeypatch.setattr(nfp, 'setup_vast_working_copy', fake_setup)
    monkeypatch.setattr(nfp, '_phase1_parallel_solve_plate', fake_solve)
    monkeypatch.setattr(nfp, 'run_forced_photometry_c', fake_measure)
    mu.measure_images_for_source(
        {}, None, entry, images, root,
        verdicts=nml.RunVerdictResolver(root, now=1790000000.0))
    # the excluded frame is measured and recorded as manual with the values;
    # an edge result keeps its status; the cloud verdict comes first
    assert _ledger_by_basename(root, 'SRC') == {
        'wcs_fd_excl.fits': ('manual', '12.3456', '0.0210'),
        'wcs_fd_excledge.fits': ('edge', '99.0000', '99.0000'),
        'wcs_fd_both.fits': ('cloudy', '12.3456', '0.0210'),
        'wcs_fd_clear.fits': ('detection', '12.3456', '0.0210')}


def test_manual_modes_pass_the_listed_frames_on_and_rebuild_stale_sources(
        tmp_path, monkeypatch):
    uploads = str(tmp_path)
    _write_ledger(uploads, 'A', 'wcs_fd_M.fits 2461000.5 12.0 0.02 manual C1')
    open(os.path.join(nml.source_dir_path(uploads, 'A'),
                      nml.BACKFILL_MARKER_BASENAME), 'w').close()
    # A's products predate its ledger (an interrupted update)
    stale_aavso = os.path.join(nml.source_dir_path(uploads, 'A'),
                               nml.AAVSO_BASENAME)
    _write_file(stale_aavso, 'A,2461000.5,12.0\n')
    os.utime(stale_aavso, (1000000000, 1000000000))
    mu, rebuilt, frames, _ = _patch_exclusion_context(monkeypatch, uploads,
                                                      ['A'])
    seen = []
    monkeypatch.setattr(mu, 'covering_fields_for_entry',
                        lambda cfg, entry: {'F1'})
    monkeypatch.setattr(mu, 'enumerate_archive_images',
                        lambda cfg, fields: ['/archive/wcs_fd_new.fits'])
    monkeypatch.setattr(nml, 'RunVerdictResolver',
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(mu, 'measure_images_for_source',
                        lambda cfg, lcp, entry, images, uploads_dir,
                        verdicts=None, excluded_frames=None:
                        seen.append(excluded_frames) or 0)
    assert mu.mode_rescan('rescan-archive', '--all') == 0
    # no list yet: the run created it from the manual rows and passed it on
    assert seen == [{'M.fits'}]
    assert set(nml.read_excluded_frames(uploads)) == {'M.fits'}
    assert 'A' in rebuilt and frames['A'] == {'M.fits'}


def test_sync_exclusions_migrates_then_applies_the_list(tmp_path, monkeypatch):
    uploads = str(tmp_path)
    # excluded for A only, before the list existed
    _write_ledger(uploads, 'A', 'wcs_fd_X.fits 2461000.5 12.0000 0.0200 manual C1')
    _write_ledger(uploads, 'B',
                  'wcs_fd_X.fits 2461000.5 13.0000 0.0300 detection C1',
                  'wcs_fd_W.fits 2461000.9 13.1000 0.0300 detection C1')
    _write_ledger(uploads, 'C',
                  'wcs_fd_Y.fits 2461000.6 15.2000 99.0000 upperlimit C1')
    mu, rebuilt, frames, messages = _patch_exclusion_context(
        monkeypatch, uploads, ['A', 'B', 'C'])
    assert mu.mode_sync_exclusions() == 0
    listed = nml.read_excluded_frames(uploads)
    assert set(listed) == {'X.fits'}
    assert 'migrated from the manual rows of A' in listed['X.fits']
    assert _ledger_by_basename(uploads, 'B')['wcs_fd_X.fits'][0] == 'manual'
    assert _ledger_by_basename(uploads, 'B')['wcs_fd_W.fits'][0] == 'detection'
    assert _ledger_by_basename(uploads, 'C')['wcs_fd_Y.fits'][0] == 'upperlimit'
    assert rebuilt == ['B'] and frames['B'] == {'X.fits'}
    # once the list exists a manual row on an unlisted frame is reported, not
    # turned into a frame-wide exclusion
    _write_ledger(uploads, 'C',
                  'wcs_fd_Y.fits 2461000.6 15.2000 99.0000 manual C1')
    del rebuilt[:]
    del messages[:]
    assert mu.mode_sync_exclusions() == 0
    assert set(nml.read_excluded_frames(uploads)) == {'X.fits'}
    assert any('Y.fits is excluded for this source only' in m
               for m in messages)
    assert rebuilt == []

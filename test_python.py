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
    kept, flagged = nml.split_inconsistent_visits(rows)
    assert len(kept) == 2 and not flagged


def test_visit_consistency_discrepant_pair_flagged():
    rows = [_det('a.fits', 2461000.500, 9.66),
            _det('b.fits', 2461000.501, 10.31)]
    kept, flagged = nml.split_inconsistent_visits(rows)
    assert not kept and len(flagged) == 2


def test_visit_consistency_separate_visits_not_compared():
    # same camera, 0.65 mag apart but 1 hour apart: separate visits
    rows = [_det('a.fits', 2461000.500, 9.66),
            _det('b.fits', 2461000.542, 10.31)]
    kept, flagged = nml.split_inconsistent_visits(rows)
    assert len(kept) == 2 and not flagged


def test_visit_consistency_cameras_independent():
    # two cameras at the same time never form one visit
    rows = [_det('a.fits', 2461000.500, 9.66, camera='CAM1'),
            _det('b.fits', 2461000.501, 10.31, camera='CAM2')]
    kept, flagged = nml.split_inconsistent_visits(rows)
    assert len(kept) == 2 and not flagged


def test_visit_consistency_large_errors_tolerated():
    # near the detection limit the spread must beat ERR_SCALE * err
    rows = [_det('a.fits', 2461000.500, 14.1, err=0.2),
            _det('b.fits', 2461000.501, 14.6, err=0.2)]
    kept, flagged = nml.split_inconsistent_visits(rows)
    assert len(kept) == 2 and not flagged


def test_visit_consistency_three_frame_visit_chained():
    # gap chaining: 3 frames each 5 min apart form one visit; one outlier
    # condemns all three
    rows = [_det('a.fits', 2461000.500, 9.70),
            _det('b.fits', 2461000.5035, 9.71),
            _det('c.fits', 2461000.507, 10.40)]
    kept, flagged = nml.split_inconsistent_visits(rows)
    assert not kept and len(flagged) == 3


def test_visit_consistency_singleton_never_flagged():
    rows = [_det('a.fits', 2461000.500, 12.0)]
    kept, flagged = nml.split_inconsistent_visits(rows)
    assert len(kept) == 1 and not flagged


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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

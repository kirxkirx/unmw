#!/usr/bin/env python3
"""
Unit tests for filter_report.py and upload.py3
Run with: pytest test_python.py -v
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os
import sys

if os.environ.get('GATEWAY_INTERFACE') or os.environ.get('REQUEST_METHOD'):
    sys.stdout.write('Content-Type: text/plain\n\nERROR: this script must not be run as CGI\n')
    sys.exit(1)

import tempfile
import zipfile
import re
import pytest
import shutil
import errno
import importlib.machinery
import importlib.util
import io
import stat
import types

# Import functions from filter_report.py
from filter_report import is_asteroid, is_variable_star, is_ast_or_vs, filter_report

def _load_upload_py3():
    """Test the deployed source, including on Python without legacy-cgi."""
    stand_ins = {}
    for name in ('cgi', 'cgitb'):
        try:
            __import__(name)
        except ImportError:
            stand_ins[name] = types.ModuleType(name)
    if 'cgi' in stand_ins:
        stand_ins['cgi'].FieldStorage = object
    sys.modules.update(stand_ins)
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'upload.py3')
        loader = importlib.machinery.SourceFileLoader('unmw_upload_py3', path)
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        return module
    finally:
        for name in stand_ins:
            sys.modules.pop(name, None)


up = _load_upload_py3()
is_safe_filename = up.is_safe_filename
validate_archive_size = up.validate_archive_size
check_archive_contents = up.check_archive_contents
MIN_FILE_SIZE = up.MIN_FILE_SIZE
MAX_FILE_SIZE = up.MAX_FILE_SIZE


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
        """Reject parent components even outside the basename."""
        assert is_safe_filename("..") is False
        assert is_safe_filename("..hidden") is False
        assert is_safe_filename("../etc/passwd") is False
        assert is_safe_filename("foo/../bar") is False

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

    def test_relative_directories_only(self):
        assert is_safe_filename("/path/to/good_file.fits") is False
        assert is_safe_filename("path/to/good_file.fits") is True


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

    @pytest.fixture(autouse=True)
    def report_directory(self, tmp_path, monkeypatch):
        # filter_report writes both HTML and a JSON sibling. Keep all of
        # them in pytest's managed directory, including when a test fails.
        original = tempfile.NamedTemporaryFile
        monkeypatch.setattr(tempfile, 'NamedTemporaryFile',
                            lambda *args, **kwargs: original(*args, dir=str(tmp_path), **kwargs))

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
        assert '<td class="mono" title="12:34:56.789">12:34:56.79</td>' in page
        assert '<td class="mono" title="-12:34:56.78">-12:34:56.8</td>' in page
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
    assert re.search(r'<td class="num">1</td><td class="num">1</td>', page)


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


# ---------------------------------------------------------------------------
# btrfs-aware free space check (nmw_fs_check.py and its bash twin in
# autoprocess.sh): btrfs refuses writes once the device is fully allocated
# and the metadata block groups fill up, while df still shows free space.
# ---------------------------------------------------------------------------

import subprocess
import nmw_fs_check as nfc

GIB = 1024 ** 3
MIB = 1024 ** 2


def _write_int(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        fh.write('{}\n'.format(value))


def _fake_btrfs(sandbox, mount_dir, unallocated, metadata_used,
                device='fakedev', device_size=2000 * GIB, metadata_total=10 * GIB,
                global_rsv=512 * MIB, fstype='btrfs', drop=None):
    """A fake /sys/fs/btrfs tree plus a mountinfo file that puts mount_dir
    on /dev/<device>. Returns (sysfs_root, mountinfo_path)."""
    sysfs_root = os.path.join(sandbox, 'sysfs')
    fs_dir = os.path.join(sysfs_root, 'deadbeef-0000-4000-8000-000000000001')
    _write_int(os.path.join(fs_dir, 'devices', device, 'size'), device_size // 512)
    system_total = 8 * MIB
    data_total = device_size - unallocated - 2 * metadata_total - 2 * system_total
    _write_int(os.path.join(fs_dir, 'allocation', 'data', 'disk_total'), data_total)
    _write_int(os.path.join(fs_dir, 'allocation', 'metadata', 'disk_total'), 2 * metadata_total)
    _write_int(os.path.join(fs_dir, 'allocation', 'system', 'disk_total'), 2 * system_total)
    _write_int(os.path.join(fs_dir, 'allocation', 'metadata', 'total_bytes'), metadata_total)
    _write_int(os.path.join(fs_dir, 'allocation', 'metadata', 'bytes_used'), metadata_used)
    _write_int(os.path.join(fs_dir, 'allocation', 'global_rsv_size'), global_rsv)
    if drop:
        os.remove(os.path.join(fs_dir, drop))
    real_mount = os.path.realpath(mount_dir).replace(' ', '\\040')
    mountinfo = os.path.join(sandbox, 'mountinfo')
    with open(mountinfo, 'w') as fh:
        fh.write('25 1 259:3 / / rw,relatime - ext4 /dev/root rw\n')
        fh.write('36 25 0:38 / {} rw,noatime - {} /dev/{} rw,compress=zstd:3\n'
                 .format(real_mount, fstype, device))
    return sysfs_root, mountinfo


def test_fs_check_passes_on_non_btrfs():
    sandbox = tempfile.mkdtemp()
    try:
        mount = os.path.join(sandbox, 'mnt'); os.makedirs(mount)
        sysfs_root, mountinfo = _fake_btrfs(sandbox, mount, unallocated=0,
                                            metadata_used=10 * GIB, fstype='ext4')
        status, message = nfc.btrfs_space_status(
            os.path.join(mount, 'uploads'), sysfs_root, mountinfo, hostname='h')
        assert status == 'OK' and 'skipped' in message and 'ext4' in message
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_fs_check_healthy_btrfs_is_ok():
    sandbox = tempfile.mkdtemp()
    try:
        mount = os.path.join(sandbox, 'mnt'); os.makedirs(mount)
        sysfs_root, mountinfo = _fake_btrfs(sandbox, mount, unallocated=600 * GIB,
                                            metadata_used=int(9.9 * GIB))
        status, message = nfc.btrfs_space_status(mount, sysfs_root, mountinfo, hostname='h')
        # metadata block groups nearly full is NORMAL while new ones can be allocated
        assert status == 'OK' and 'unallocated' in message
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_fs_check_no_unallocated_but_headroom_is_warning():
    sandbox = tempfile.mkdtemp()
    try:
        mount = os.path.join(sandbox, 'mnt'); os.makedirs(mount)
        sysfs_root, mountinfo = _fake_btrfs(sandbox, mount, unallocated=1 * MIB,
                                            metadata_used=5 * GIB)
        status, message = nfc.btrfs_space_status(mount, sysfs_root, mountinfo, hostname='h')
        assert status == 'WARNING'
        assert 'low on disk space' in message       # combine_reports.sh surfaces this phrase
        assert 'btrfs balance start -dusage=50' in message
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_fs_check_tau_2026_10_06_numbers_are_error():
    # The real counters from tau at the moment renames started failing
    sandbox = tempfile.mkdtemp()
    try:
        mount = os.path.join(sandbox, 'mnt'); os.makedirs(mount)
        sysfs_root, mountinfo = _fake_btrfs(
            sandbox, mount, device_size=2000397795328, unallocated=1048576,
            metadata_total=10737418240, metadata_used=10195222528, global_rsv=536870912)
        status, message = nfc.btrfs_space_status(mount, sysfs_root, mountinfo, hostname='tau')
        assert status == 'ERROR'
        assert message.startswith('ERROR: server tau is out of disk space at')
        assert '1 MB unallocated' in message and '5 MB metadata headroom' in message
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_fs_check_fails_open_when_counters_are_missing():
    sandbox = tempfile.mkdtemp()
    try:
        mount = os.path.join(sandbox, 'mnt'); os.makedirs(mount)
        # older kernel without the allocation/<kind>/disk_total files
        sysfs_root, mountinfo = _fake_btrfs(sandbox, mount, unallocated=1 * MIB,
                                            metadata_used=10 * GIB,
                                            drop='allocation/data/disk_total')
        status, message = nfc.btrfs_space_status(mount, sysfs_root, mountinfo, hostname='h')
        assert status == 'OK' and 'skipped' in message
        # no sysfs at all (non-Linux, or btrfs module not loaded)
        status, message = nfc.btrfs_space_status(
            mount, os.path.join(sandbox, 'nowhere'), mountinfo, hostname='h')
        assert status == 'OK' and 'skipped' in message
        # unreadable mountinfo
        status, message = nfc.btrfs_space_status(
            mount, sysfs_root, os.path.join(sandbox, 'nomountinfo'), hostname='h')
        assert status == 'OK' and 'skipped' in message
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_fs_check_mountinfo_longest_prefix_and_escapes():
    sandbox = tempfile.mkdtemp()
    try:
        mount = os.path.join(sandbox, 'data disk'); os.makedirs(mount)
        sysfs_root, mountinfo = _fake_btrfs(sandbox, mount, unallocated=1 * MIB,
                                            metadata_used=10 * GIB)
        found = nfc.find_mount(os.path.join(mount, 'sub', 'dir'), mountinfo)
        assert found == (os.path.realpath(mount), 'btrfs', '/dev/fakedev')
        # a path outside the fake mount falls through to the root line
        assert nfc.find_mount(sandbox, mountinfo)[1] == 'ext4'
        # the escaped mount point still resolves the sysfs figures
        status, _ = nfc.btrfs_space_status(mount, sysfs_root, mountinfo, hostname='h')
        assert status == 'ERROR'
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_fs_check_worst_status_combination():
    ok = ('OK', 'fine')
    warn = ('WARNING', 'WARNING: low')
    err = ('ERROR', 'ERROR: out')
    assert nfc.worst_status(ok, ok) == ok
    assert nfc.worst_status(ok, warn) == warn
    assert nfc.worst_status(warn, ok) == warn
    assert nfc.worst_status(warn, err) == err
    assert nfc.worst_status(err, warn) == ('ERROR', 'ERROR: out; WARNING: low')


def _run_bash_twin(sandbox, sysfs_root, directory, fstype):
    """Run check_btrfs_metadata_space() from autoprocess.sh with stat and df
    replaced by shims (the fake filesystem is not really mounted)."""
    shims = os.path.join(sandbox, 'bin'); os.makedirs(shims, exist_ok=True)
    with open(os.path.join(shims, 'stat'), 'w') as fh:
        fh.write('#!/bin/sh\necho {}\n'.format(fstype))
    with open(os.path.join(shims, 'df'), 'w') as fh:
        fh.write('#!/bin/sh\necho "Filesystem 1024-blocks Used Available Capacity Mounted on"\n'
                 'echo "/dev/fakedev 1 1 1 1% /mnt/fake"\n')
    for name in ('stat', 'df'):
        os.chmod(os.path.join(shims, name), 0o755)
    script = ('source <(sed -n "/^function check_btrfs_metadata_space/,/^}/p" autoprocess.sh); '
              'check_btrfs_metadata_space "$1"')
    env = dict(os.environ, PATH=shims + os.pathsep + os.environ.get('PATH', ''),
               UNMW_BTRFS_SYSFS_ROOT=sysfs_root, HOSTNAME='h')
    proc = subprocess.run(['bash', '-c', script, 'bash', directory],
                          capture_output=True, text=True, env=env,
                          cwd=os.path.dirname(os.path.abspath(__file__)))
    return proc.returncode, proc.stdout.strip()


def test_fs_check_bash_twin_matches_python():
    sandbox = tempfile.mkdtemp()
    try:
        mount = os.path.join(sandbox, 'mnt'); os.makedirs(mount)
        # ERROR: the tau numbers
        sysfs_root, _ = _fake_btrfs(
            sandbox, mount, device_size=2000397795328, unallocated=1048576,
            metadata_total=10737418240, metadata_used=10195222528, global_rsv=536870912)
        code, out = _run_bash_twin(sandbox, sysfs_root, mount, 'btrfs')
        assert code == 2, out
        assert out.startswith('ERROR: server h is out of disk space at')
        assert '1 MB unallocated, 5 MB metadata headroom' in out
        assert "btrfs balance start -dusage=50 /mnt/fake" in out
        # WARNING: no unallocated space, metadata still has headroom
        shutil.rmtree(sysfs_root)
        sysfs_root, _ = _fake_btrfs(sandbox, mount, unallocated=1 * MIB, metadata_used=5 * GIB)
        code, out = _run_bash_twin(sandbox, sysfs_root, mount, 'btrfs')
        assert code == 1 and 'low on disk space' in out
        # OK: plenty unallocated -> silent
        shutil.rmtree(sysfs_root)
        sysfs_root, _ = _fake_btrfs(sandbox, mount, unallocated=600 * GIB, metadata_used=9 * GIB)
        code, out = _run_bash_twin(sandbox, sysfs_root, mount, 'btrfs')
        assert code == 0 and out == ''
        # not btrfs -> silent pass even with the broken fake tree in place
        shutil.rmtree(sysfs_root)
        sysfs_root, _ = _fake_btrfs(sandbox, mount, unallocated=1 * MIB, metadata_used=10 * GIB)
        code, out = _run_bash_twin(sandbox, sysfs_root, mount, 'ext4')
        assert code == 0 and out == ''
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


# ---------------------------------------------------------------------------
# Suspicious filename log (upload.py3): every suspicious upload or archive
# member name is appended, escaped, to a log that is never inside a
# web-served directory. All upload tests use the real upload.py3.
# ---------------------------------------------------------------------------

FITS_STUB = b'SIMPLE  = T' + b' ' * 2870


def _write_zip(path, names, symlinks=(), payload=FITS_STUB,
               compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, 'w', compression=compression) as zf:
        for name in names:
            zf.writestr(name, payload)
        for name in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, 'target')


def _logged_names(found):
    return [name for _, name, _ in found.entries]


@pytest.fixture
def suspicious_log(tmp_path, monkeypatch):
    """Point the suspicious filename log at a private temporary file, keep
    the developer's environment and local_config.sh out of the way and give
    the request a client address."""
    log = tmp_path / 'private' / 'suspicious_filenames.txt'
    log.parent.mkdir()
    monkeypatch.setattr(up, '_parse_config_value', lambda config_path, var_name: None)
    for var in ('HTDOCS_DIR', 'DATA_PROCESSING_ROOT', 'IMAGE_DATA_ROOT', 'DOCUMENT_ROOT',
                'REMOTE_USER', 'HTTP_USER_AGENT'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv('UNMW_CGITB_LOGDIR', str(log.parent))
    monkeypatch.setenv('SUSPICIOUS_FILENAMES_LOG', str(log))
    monkeypatch.setenv('REMOTE_ADDR', '203.0.113.7')
    # a refused location would send the test lines to a shared log instead
    assert up.suspicious_filenames_log_path() == (os.path.realpath(str(log)), '')
    return log


def _log_lines(log):
    return log.read_text().splitlines() if log.exists() else []


def test_upload_name_problems():
    assert up.upload_name_problems('2025-01-07_Vul8_183150_Stas.rar') == []
    assert up.upload_name_problems('NMW__NovaVul24_Stas__WebCheck__NotReal.zip') == []
    assert up.upload_name_problems('../../x.zip') == ['path traversal', 'directory part']
    assert up.upload_name_problems('/etc/x.zip') == ['absolute path', 'directory part']
    assert up.upload_name_problems('shell.php') == ['not a .zip or .rar name']
    assert 'hidden file' in up.upload_name_problems('.htaccess.zip')
    assert 'shell special characters' in up.upload_name_problems('x;reboot.zip')
    assert 'control or invisible characters' in up.upload_name_problems('x\n.zip')
    # a name longer than NAME_MAX (255) cannot even be saved
    assert up.upload_name_problems('a' * 251 + '.zip') == []
    assert 'longer than 255 characters' in up.upload_name_problems('a' * 252 + '.zip')


def test_member_problems():
    assert up.member_problems('image1.fits') == []
    assert up.member_problems('night/image1.fts') == []
    assert up.member_problems('My Images/image1.fts') == []   # a space in a directory is fine
    assert up.member_problems('night/', is_dir=True) == []
    # Rejected even though extraction currently flattens paths.
    assert up.member_problems('../image1.fits') == ['path traversal']
    assert up.member_problems('/tmp/image1.fits') == ['absolute path']
    assert up.member_problems('../../', is_dir=True) == ['path traversal']
    assert 'path traversal' in up.member_problems('..\\..\\image1.fits')
    assert up.member_problems('$(reboot)/image1.fits') == ['shell special characters in a directory name']
    assert up.member_problems('.ssh/image1.fits') == ['hidden directory']
    assert up.member_problems('a<b>/', is_dir=True) == ['Windows special characters in a directory name']
    assert up.member_problems('link_dir/', is_dir=True, is_symlink=True) == ['symlink']
    assert up.member_problems('evil.php') == ['not a .fit, .fits or .fts image']
    assert up.member_problems('image1.fits', is_symlink=True) == ['symlink']
    assert up.member_problems('\x1b[2Jx.fits') == [
        'control or invisible characters',
        'characters other than letters, digits, underscore, dash and dot']
    assert up.member_problems('a/./b/../c/.../x.fits') == ['path traversal', 'hidden directory']
    assert up.is_safe_filename('data_2024.fts') and not up.is_safe_filename('../etc/passwd')
    assert not up.is_safe_filename('..hidden') and not up.is_safe_filename('file;rm')


def test_check_archive_contents_logs_every_suspicious_member(tmp_path):
    archive = str(tmp_path / 'x.zip')
    _write_zip(archive, ['ok1.fits', 'bad;name.fits', 'ok2.fits', 'evil.php',
                         '../escape.fits', '.hidden.fits', 'night/'])
    found = up.SuspiciousFilenames()
    valid, message = up.check_archive_contents(archive, found)
    # Whole-path checks, like symlink checks, precede basename checks...
    assert (valid, message) == (False, 'Unsafe path in archive: ../escape.fits')
    # ...but every suspicious member is recorded, not only that one
    assert _logged_names(found) == ['bad;name.fits', 'evil.php', '../escape.fits', '.hidden.fits']


def test_check_archive_contents_rejects_and_logs_parent_paths(tmp_path):
    archive = str(tmp_path / 'x.zip')
    _write_zip(archive, ['ok1.fits', '../../ok2.fits', '$(id)/ok3.fits'])
    found = up.SuspiciousFilenames()
    assert up.check_archive_contents(archive, found) == (False, 'Unsafe path in archive: ../../ok2.fits')
    assert found.entries == [('member', '../../ok2.fits', ['path traversal']),
                             ('member', '$(id)/ok3.fits', ['shell special characters in a directory name'])]
    # the optional collector does not have to be passed
    assert up.check_archive_contents(archive) == (False, 'Unsafe path in archive: ../../ok2.fits')


def test_check_archive_contents_symlink_verdict_kept_and_all_logged(tmp_path):
    archive = str(tmp_path / 'x.zip')
    _write_zip(archive, ['ok1.fits', 'evil.php', 'ok2.fits'], symlinks=['link.fits', 'link2.fits'])
    found = up.SuspiciousFilenames()
    valid, message = up.check_archive_contents(archive, found)
    assert (valid, message) == (False, 'Symlink in archive is not allowed: link.fits')
    assert _logged_names(found) == ['evil.php', 'link.fits', 'link2.fits']
    assert ('member', 'link2.fits', ['symlink']) in found.entries


def test_check_archive_contents_empty_member_name_after_a_symlink(tmp_path):
    # Before Python 3.11 ZipInfo.is_dir() raises on an empty name; the
    # verdict must stay the symlink one and the names must still be logged
    archive = str(tmp_path / 'x.zip')
    with zipfile.ZipFile(archive, 'w') as zf:
        for name in ('image1.fits', 'image2.fits', 'evil.php'):
            zf.writestr(name, FITS_STUB)
        info = zipfile.ZipInfo('link.fits')
        info.external_attr = 0o120777 << 16
        zf.writestr(info, 'target')
        zf.writestr(zipfile.ZipInfo(''), b'x')
    found = up.SuspiciousFilenames()
    valid, message = up.check_archive_contents(archive, found)
    assert (valid, message) == (False, 'Symlink in archive is not allowed: link.fits')
    assert _logged_names(found) == ['evil.php', 'link.fits', '']


def test_check_archive_contents_bomb_verdict_kept_and_names_logged(tmp_path):
    archive = str(tmp_path / 'x.zip')
    _write_zip(archive, ['ok1.fits', 'ok2.fits', 'x|y.fits'], payload=b'\0' * (1024 * 1024),
               compression=zipfile.ZIP_DEFLATED)
    found = up.SuspiciousFilenames()
    valid, message = up.check_archive_contents(archive, found)
    assert valid is False and message.startswith('Suspicious compression ratio')
    assert _logged_names(found) == ['x|y.fits']


def test_check_archive_contents_too_many_members_logs_the_first_ones(tmp_path, suspicious_log):
    archive = str(tmp_path / 'x.zip')
    names = ['image%04d.fits' % i for i in range(up.MAX_ARCHIVE_MEMBERS + 5)]
    names[10] = 'evil.php'
    names[up.MAX_ARCHIVE_MEMBERS + 2] = 'late.php'   # past the limit: never examined
    _write_zip(archive, names, payload=b'x')
    found = up.SuspiciousFilenames()
    assert up.check_archive_contents(archive, found) == (
        False, 'Too many files in archive: 4005 (maximum 4000)')
    assert _logged_names(found) == ['evil.php'] and found.unexamined == 5
    up.log_suspicious_filenames(found, (False, 'Too many files in archive: 4005 (maximum 4000)', '', ''))
    lines = _log_lines(suspicious_log)
    assert len(lines) == 2
    assert "more='5 more archive members not examined'" in lines[1]


@pytest.mark.skipif(shutil.which('rar') is None, reason='needs the rar binary')
@pytest.mark.parametrize('backend', ['rarfile', 'rar binary'])
def test_check_archive_contents_logs_suspicious_rar_members(tmp_path, monkeypatch, backend):
    if backend == 'rarfile' and not up.HAVE_RARFILE:
        pytest.skip('the rarfile module is not installed')
    monkeypatch.setattr(up, 'HAVE_RARFILE', backend == 'rarfile')
    src = tmp_path / 'src'
    src.mkdir()
    names = ['a.fits', 'b.fits', 'x;y.fits', ' lead.fits', 'c.fits\nd.fits']
    for name in names:
        (src / name).write_bytes(FITS_STUB)
    os.symlink('/etc/passwd', str(src / 'link.fits'))
    archive = str(tmp_path / 'x.rar')
    subprocess.run(['rar', 'a', '-ep', '-ol', archive] + names + ['link.fits'],
                   cwd=str(src), check=True, stdout=subprocess.DEVNULL)
    found = up.SuspiciousFilenames()
    valid, message = up.check_archive_contents(archive, found)
    assert valid is False   # the message depends on the backend, as before
    # the rar binary's bare listing hides the symlink, the leading space and
    # the line break; the log must show the real names all the same
    problems = {name: found_problems for _, name, found_problems in found.entries}
    assert set(problems) == {'x;y.fits', ' lead.fits', 'c.fits\nd.fits', 'link.fits'}
    assert problems['link.fits'] == ['symlink']
    assert 'control or invisible characters' in problems['c.fits\nd.fits']


@pytest.mark.skipif(shutil.which('rar') is None, reason='needs the rar binary')
def test_rar_technical_listing_ignores_the_archive_comment(tmp_path):
    (tmp_path / 'a.fits').write_bytes(FITS_STUB)
    (tmp_path / 'comment.txt').write_text('hello\n        Name: forged.php\n        Type: File\n')
    archive = str(tmp_path / 'x.rar')
    subprocess.run(['rar', 'a', '-ep', '-zcomment.txt', archive, 'a.fits'],
                   cwd=str(tmp_path), check=True, stdout=subprocess.DEVNULL)
    assert up.rar_members_via_binary(archive) == ([('a.fits', False, False)], True)


def test_suspicious_filename_log_lines_are_escaped_and_private(suspicious_log, monkeypatch):
    monkeypatch.setenv('HTTP_USER_AGENT', 'probe\n\x1b[2J')
    found = up.SuspiciousFilenames()
    found.archive = 'evil.zip'
    found.upload_dir = 'uploads/web_upload_1abcdefgh/'
    found.note('upload_name', '../evil\n.zip', up.upload_name_problems('../evil\n.zip'))
    member = '\x1b]0;owned\x07x.fits'
    found.note('member', member, up.member_problems(member))
    up.log_suspicious_filenames(found, (False, 'Unsafe filename in archive: ' + member, '', ''))
    data = suspicious_log.read_bytes()
    assert stat.S_IMODE(os.stat(str(suspicious_log)).st_mode) == 0o600
    # one line per name, nothing but printable ASCII in them
    assert all(32 <= byte < 127 for byte in data.replace(b'\n', b''))
    lines = data.decode('ascii').splitlines()
    assert len(lines) == 2
    assert re.match(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4} ', lines[0])
    assert "addr='203.0.113.7' user='-' agent='probe\\n\\x1b[2J'" in lines[0]
    assert "upload='web_upload_1abcdefgh' archive='evil.zip'" in lines[0]
    assert "upload_name='../evil\\n.zip' problems='path traversal; control or invisible" in lines[0]
    assert "member='\\x1b]0;owned\\x07x.fits'" in lines[1]
    assert lines[1].endswith("result='rejected: Unsafe filename in archive: \\x1b]0;owned\\x07x.fits'")
    # the next upload appends
    up.log_suspicious_filenames(found, (True, 'File uploaded and validated successfully', '', ''))
    lines = _log_lines(suspicious_log)
    assert len(lines) == 4 and lines[3].endswith("result='accepted'")


def test_suspicious_filename_log_values_cannot_pass_for_other_fields(suspicious_log, monkeypatch):
    monkeypatch.setenv('HTTP_USER_AGENT', "Mozilla/5.0' addr='192.0.2.66' result='accepted")
    found = up.SuspiciousFilenames()
    found.note('member', "x' result='accepted.php", ['not a .fit, .fits or .fts image'])
    up.log_suspicious_filenames(found, (False, 'x', '', ''))
    line = suspicious_log.read_text()
    assert "addr='192.0.2.66'" not in line and "result='accepted" not in line
    assert "agent='Mozilla/5.0\\' addr=\\'192.0.2.66\\' result=\\'accepted'" in line
    # every value is a Python string literal
    import ast
    assert ast.literal_eval(line.split(' member=')[1].split(' problems=')[0]) == "x' result='accepted.php"


def test_suspicious_filename_log_cuts_overlong_values_after_escaping(suspicious_log):
    found = up.SuspiciousFilenames()
    found.note('member', 'a' * 5000 + '.php', ['not a .fit, .fits or .fts image'])
    found.note('member', '\U0001f600' * 5000 + '.php', ['not a .fit, .fits or .fts image'])
    up.log_suspicious_filenames(found, (False, 'x', '', ''))
    lines = _log_lines(suspicious_log)
    assert "member='" + 'a' * up.MAX_LOGGED_VALUE_CHARS + "'..." in lines[0]
    # an escape sequence is never split, and the line does not grow with it
    assert "member='" + '\\U0001f600' * (up.MAX_LOGGED_VALUE_CHARS // 10) + "'..." in lines[1]
    assert all(len(line) < 900 for line in lines)


def test_suspicious_filename_log_counts_the_names_past_the_per_upload_limit(tmp_path, suspicious_log):
    archive = str(tmp_path / 'x.zip')
    _write_zip(archive, ['image1.fits', 'image2.fits'] + ['bad;%03d.fits' % i for i in range(150)])
    found = up.SuspiciousFilenames()
    result = up.check_archive_contents(archive, found)
    up.log_suspicious_filenames(found, result + ('', ''))
    lines = _log_lines(suspicious_log)
    assert len(lines) == up.MAX_LOGGED_NAMES_PER_UPLOAD + 1
    assert "member='bad;099.fits'" in lines[-2]
    assert ("more='50 more suspicious names (shell special characters x50, "
            "characters other than letters, digits, underscore, dash and dot x50)'") in lines[-1]


def test_nothing_is_logged_without_suspicious_names(suspicious_log):
    up.log_suspicious_filenames(up.SuspiciousFilenames(), (True, '', '', ''))
    assert not suspicious_log.exists()


def test_suspicious_filename_log_never_inside_web_served_dirs(tmp_path, monkeypatch):
    here = os.path.dirname(os.path.abspath(up.__file__))
    private = tmp_path / 'private'
    private.mkdir()
    default = os.path.join(os.path.realpath(str(private)), 'unmw_suspicious_filenames.txt')
    monkeypatch.setenv('UNMW_CGITB_LOGDIR', str(private))
    for var in ('HTDOCS_DIR', 'DATA_PROCESSING_ROOT', 'IMAGE_DATA_ROOT', 'DOCUMENT_ROOT'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(up, '_parse_config_value', lambda config_path, var_name: None)
    # an uploads symlink in the current directory, as in the CGI directory
    data_root = tmp_path / 'NMW_web_upload'
    data_root.mkdir()
    monkeypatch.chdir(str(tmp_path))
    os.symlink(str(data_root), 'uploads')
    htdocs = tmp_path / 'htdocs'
    (htdocs / 'unmw').mkdir(parents=True)
    other_data = tmp_path / 'other_data'
    other_data.mkdir()
    monkeypatch.setenv('HTDOCS_DIR', str(htdocs / 'unmw'))
    monkeypatch.setenv('DOCUMENT_ROOT', str(htdocs))
    monkeypatch.setenv('DATA_PROCESSING_ROOT', str(other_data))
    for configured in ('suspicious.txt',                    # relative: inside the checkout
                       'uploads/suspicious.txt',
                       os.path.join(here, 'suspicious.txt'),
                       str(data_root / 'suspicious.txt'),   # the uploads symlink target
                       str(tmp_path / 'uploads' / 'x.txt'),
                       str(htdocs / 'unmw' / 'suspicious.txt'),
                       str(htdocs / 'suspicious.txt'),      # DOCUMENT_ROOT
                       str(other_data / 'logs' / 'x.txt')):
        monkeypatch.setenv('SUSPICIOUS_FILENAMES_LOG', configured)
        path, complaint = up.suspicious_filenames_log_path()
        assert path == default, configured
        assert 'inside a web-served directory' in complaint
    # a private location is used as given
    monkeypatch.setenv('SUSPICIOUS_FILENAMES_LOG', str(tmp_path / 'logs' / 'x.txt'))
    assert up.suspicious_filenames_log_path() == (os.path.join(os.path.realpath(str(tmp_path)), 'logs', 'x.txt'), '')
    # unset: the cgitb log directory; if that is web-served, /tmp
    monkeypatch.delenv('SUSPICIOUS_FILENAMES_LOG')
    assert up.suspicious_filenames_log_path() == (default, '')
    monkeypatch.setenv('UNMW_CGITB_LOGDIR', str(data_root))
    path, complaint = up.suspicious_filenames_log_path()
    assert path == os.path.join(os.path.realpath('/tmp'), 'unmw_suspicious_filenames.txt')
    assert complaint


def test_suspicious_filename_log_location_check_compares_directories_not_names(tmp_path, monkeypatch):
    # A bind mount gives the data directory a second name that realpath()
    # cannot see through; without realpath() a symlink does the same here
    data_root = tmp_path / 'data'
    data_root.mkdir()
    os.symlink(str(data_root), str(tmp_path / 'mounted'))
    published = up._directory_ids([str(data_root)])
    assert up._inside(str(tmp_path / 'mounted' / 'logs' / 'x.txt'), published)
    assert not up._inside(str(tmp_path / 'elsewhere' / 'x.txt'), published)


def test_suspicious_filename_log_settings_with_variables(tmp_path, monkeypatch):
    here = os.path.dirname(os.path.abspath(up.__file__))
    monkeypatch.setattr(up, '_parse_config_value', lambda config_path, var_name: None)
    monkeypatch.setenv('LOGS', str(tmp_path))
    monkeypatch.setenv('SUSPICIOUS_FILENAMES_LOG', '$LOGS/x.txt')
    assert up._config_path('SUSPICIOUS_FILENAMES_LOG', here) == (str(tmp_path / 'x.txt'), '')
    monkeypatch.setenv('DATA_PROCESSING_ROOT', '$PWD/uploads')
    assert up._config_path('DATA_PROCESSING_ROOT', here) == (os.path.join(here, 'uploads'), '')
    monkeypatch.setenv('HTDOCS_DIR', '$WEB_ROOT_NOT_SET/htdocs/unmw')
    path, complaint = up._config_path('HTDOCS_DIR', here)
    assert path == '' and 'give it as an absolute path' in complaint


def test_suspicious_filename_log_refuses_symlinks_fifos_links_and_planted_files(suspicious_log, monkeypatch, capsys):
    found = up.SuspiciousFilenames()
    found.note('member', 'x.php', ['not a .fit, .fits or .fts image'])
    target = suspicious_log.parent / 'target.txt'
    target.write_text('')
    os.symlink(str(target), str(suspicious_log))
    up.log_suspicious_filenames(found, (False, 'x', '', ''))
    assert target.read_text() == ''
    err = capsys.readouterr().err
    assert 'cannot write the suspicious filename log' in err
    assert '1 line(s) not logged' in err
    assert 'x.php' not in err   # no client-supplied text in the error log
    # a FIFO with no reader must not hang the upload
    os.unlink(str(suspicious_log))
    os.mkfifo(str(suspicious_log))
    up.log_suspicious_filenames(found, (False, 'x', '', ''))
    assert 'cannot write the suspicious filename log' in capsys.readouterr().err
    # a hard link to another file
    os.unlink(str(suspicious_log))
    os.link(str(target), str(suspicious_log))
    up.log_suspicious_filenames(found, (False, 'x', '', ''))
    assert 'more than one hard link' in capsys.readouterr().err
    assert target.read_text() == ''
    # a file planted by another user (simulated: we are not its owner)
    os.unlink(str(suspicious_log))
    suspicious_log.write_text('')
    if os.stat(str(suspicious_log)).st_uid != 0:
        monkeypatch.setattr(up.os, 'geteuid', lambda: os.stat(str(suspicious_log)).st_uid + 1)
        up.log_suspicious_filenames(found, (False, 'x', '', ''))
        assert 'owned by another user' in capsys.readouterr().err
        assert suspicious_log.read_text() == ''


def test_suspicious_filename_log_rotates_at_its_size_limit(suspicious_log, monkeypatch):
    monkeypatch.setattr(up, 'MAX_SUSPICIOUS_FILENAMES_LOG_BYTES', 1000)
    old = b'x' * 900 + b'\n'
    suspicious_log.write_bytes(old)
    os.chmod(str(suspicious_log), 0o600)
    found = up.SuspiciousFilenames()
    found.note('member', 'x.php', ['not a .fit, .fits or .fts image'])
    up.log_suspicious_filenames(found, (False, 'x', '', ''))
    assert (suspicious_log.parent / (suspicious_log.name + '.1')).read_bytes() == old
    lines = _log_lines(suspicious_log)
    assert len(lines) == 1 and "member='x.php'" in lines[0]
    # the next upload appends to the new log
    up.log_suspicious_filenames(found, (False, 'x', '', ''))
    assert len(_log_lines(suspicious_log)) == 2


def test_suspicious_filename_log_ends_a_cut_off_line_first(suspicious_log):
    cut = b"2026-10-08 22:26:15 -0500 addr='127.0.0.1' user='-' agent='a' upload='u' archive='a' member='b;"
    suspicious_log.write_bytes(cut)
    os.chmod(str(suspicious_log), 0o600)
    found = up.SuspiciousFilenames()
    found.note('member', 'x.php', ['not a .fit, .fits or .fts image'])
    up.log_suspicious_filenames(found, (False, 'x', '', ''))
    data = suspicious_log.read_bytes()
    assert data.startswith(cut + b'\n')
    assert data.count(b'\n') == 2 and b"member='x.php'" in data.splitlines()[1]


def test_suspicious_filename_log_counts_the_lines_a_failed_write_lost(suspicious_log, monkeypatch, capsys):
    found = up.SuspiciousFilenames()
    for name in ('a.php', 'b.php', 'c.php'):
        found.note('member', name, ['not a .fit, .fits or .fts image'])
    real_write = os.write

    def disk_fills_up(fd, data):
        if b"member='" not in data:
            return real_write(fd, data)
        if not os.fstat(fd).st_size:
            return real_write(fd, data[:data.index(b'\n') + 1] + data[data.index(b'\n') + 1:][:10])
        raise OSError(errno.ENOSPC, 'No space left on device')
    monkeypatch.setattr(up.os, 'write', disk_fills_up)
    up.log_suspicious_filenames(found, (False, 'x', '', ''))
    assert 'No space left on device; 2 line(s) not logged' in capsys.readouterr().err


class _FakeFileItem:
    def __init__(self, filename, data, name='file'):
        self.name = name
        self.filename = filename
        self.file = io.BytesIO(data)


class _FakeForm:
    """What the handler uses of cgi.FieldStorage: .list and form['name']"""
    def __init__(self, *parts):
        self.list = list(parts)

    def __getitem__(self, name):
        parts = [part for part in self.list if part.name == name]
        if not parts:
            raise KeyError(name)
        return parts[0] if len(parts) == 1 else parts


def _upload_archive(tmp_path, names, big=True):
    """A ZIP archive with the given members, over the 2MB minimum upload
    size unless big is False."""
    archive = tmp_path / 'upload_source.zip'
    with zipfile.ZipFile(str(archive), 'w') as zf:
        zf.writestr('image1.fits', os.urandom(up.MIN_FILE_SIZE) if big else FITS_STUB)
        for name in names:
            zf.writestr(name, FITS_STUB)
    return archive.read_bytes()


@pytest.fixture
def uploads(tmp_path):
    directory = tmp_path / 'uploads'
    directory.mkdir()
    return directory


def test_secure_upload_handler_logs_the_upload_name_and_every_member(tmp_path, uploads, suspicious_log):
    data = _upload_archive(tmp_path, ['image2.fits', '$(reboot).fits', '../../escape.fits'])
    form = _FakeForm(_FakeFileItem('../evil name.zip', data))
    ok, message, dirname, saved = up.secure_upload_handler(form, str(uploads))
    assert (ok, message) == (False, 'Unsafe path in archive: ../../escape.fits')
    assert os.listdir(str(uploads)) == []   # the rejected upload is gone...
    lines = _log_lines(suspicious_log)   # ...but not its trace
    assert len(lines) == 3
    assert "archive='evil_name.zip' upload_name='../evil name.zip'" in lines[0]
    assert "member='$(reboot).fits' problems='shell special characters" in lines[1]
    assert "member='../../escape.fits' problems='path traversal'" in lines[2]
    assert all("result='rejected: Unsafe path in archive: ../../escape.fits'" in line
               for line in lines)
    assert all(re.search(r"upload='web_upload_\d+[A-Za-z]{8}'", line) for line in lines)


def test_secure_upload_handler_logs_the_members_of_a_small_probe(tmp_path, uploads, suspicious_log):
    data = _upload_archive(tmp_path, ['../../etc/cron.d/x.fits', 'shell.php'], big=False)
    form = _FakeForm(_FakeFileItem('probe.zip', data))
    ok, message, _, _ = up.secure_upload_handler(form, str(uploads))
    assert (ok, message) == (False, 'File size (0.0MB) outside allowed range')
    assert os.listdir(str(uploads)) == []
    lines = _log_lines(suspicious_log)
    assert len(lines) == 2
    assert "member='../../etc/cron.d/x.fits' problems='path traversal'" in lines[0]
    assert "member='shell.php'" in lines[1]


def test_secure_upload_handler_logs_the_members_of_a_disguised_archive(tmp_path, uploads, suspicious_log):
    form = _FakeForm(_FakeFileItem('evil.php', _upload_archive(tmp_path, ['image2.fits', 'x;y.fits'])))
    ok, message, _, _ = up.secure_upload_handler(form, str(uploads))
    assert (ok, message) == (False, 'Invalid file extension: .php')
    lines = _log_lines(suspicious_log)
    assert len(lines) == 2
    assert "upload_name='evil.php' problems='not a .zip or .rar name'" in lines[0]
    assert "member='x;y.fits'" in lines[1]


def test_secure_upload_handler_logs_every_file_part(tmp_path, uploads, suspicious_log):
    # two 'file' parts make form['file'] a list, and the handler fails...
    form = _FakeForm(_FakeFileItem('../../etc/cron.d/x.zip', b'x'), _FakeFileItem('shell.php', b'x'))
    ok, message, _, _ = up.secure_upload_handler(form, str(uploads))
    assert not ok and message == 'Please upload exactly one archive'
    lines = _log_lines(suspicious_log)   # ...after noting both names
    assert len(lines) == 2
    assert "upload_name='../../etc/cron.d/x.zip'" in lines[0] and "upload_name='shell.php'" in lines[1]
    # a file part under another field name is noted too
    form = _FakeForm(_FakeFileItem('../x.zip', b'x', name='upload'))
    assert up.secure_upload_handler(form, str(uploads))[:2] == (False, "No file uploaded")
    assert "upload_name='../x.zip'" in _log_lines(suspicious_log)[2]


def test_secure_upload_handler_with_a_real_multipart_request(tmp_path, uploads, suspicious_log):
    if not hasattr(up.cgi.FieldStorage, 'read_multi'):
        pytest.skip('no cgi module (legacy-cgi) in this Python')
    body = (b'--B\r\nContent-Disposition: form-data; name="file"; filename="../a.zip"\r\n'
            b'Content-Type: application/zip\r\n\r\nPK\r\n'
            b'--B\r\nContent-Disposition: form-data; name="file"; filename="shell.php"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\nx\r\n--B--\r\n')
    environ = {'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': 'multipart/form-data; boundary=B',
               'CONTENT_LENGTH': str(len(body))}
    form = up.cgi.FieldStorage(fp=io.BytesIO(body), environ=environ)
    ok, message, _, _ = up.secure_upload_handler(form, str(uploads))
    assert not ok
    lines = _log_lines(suspicious_log)
    assert len(lines) == 2
    assert "upload_name='../a.zip'" in lines[0] and "upload_name='shell.php'" in lines[1]


def test_secure_upload_handler_logs_accepted_uploads_too(tmp_path, uploads, suspicious_log):
    form = _FakeForm(_FakeFileItem('my images.zip', _upload_archive(tmp_path, ['image2.fits'])))
    ok, message, dirname, saved = up.secure_upload_handler(form, str(uploads))
    assert ok, message
    assert os.path.basename(saved) == 'my_images.zip'
    lines = _log_lines(suspicious_log)
    assert len(lines) == 1
    assert "archive='my_images.zip' upload_name='my images.zip'" in lines[0]
    assert lines[0].endswith("result='accepted'")


def test_secure_upload_handler_logs_nothing_for_a_clean_upload(tmp_path, uploads, suspicious_log):
    form = _FakeForm(_FakeFileItem('2025-01-07_Vul8_183150_Stas.zip',
                                   _upload_archive(tmp_path, ['image2.fits'])))
    ok, message, dirname, saved = up.secure_upload_handler(form, str(uploads))
    assert ok, message
    assert not suspicious_log.exists()


def test_secure_upload_handler_logs_even_if_the_handler_raises(tmp_path, suspicious_log, monkeypatch):
    def exploding(form, upload_dir, found):
        found.note('upload_name', '../x.zip', up.upload_name_problems('../x.zip'))
        raise RuntimeError('boom')
    monkeypatch.setattr(up, '_handle_upload', exploding)
    with pytest.raises(RuntimeError):
        up.secure_upload_handler(_FakeForm(), str(tmp_path))
    assert "result='rejected: Upload error: unexpected exception'" in suspicious_log.read_text()


def test_member_problems_directory_checks_match_a_per_directory_check():
    # The directory patterns run over the whole directory part at once; the
    # result must be what checking each directory name separately gives
    import random
    rng = random.Random(1)
    alphabet = ['a', 'b', '.', '/', '\\', ';', '$', '|', '<', '?', ' ', '..', '\n']

    def per_directory(name, is_dir):
        parts = re.split(r'[/\\]', name)
        found = set()
        for part in parts if is_dir else parts[:-1]:
            if part in ('', '.', '..'):
                continue
            if part.startswith('.'):
                found.add('hidden directory')
            if re.search(r'[<>:"|?*]', part):
                found.add('Windows special characters in a directory name')
            if re.search(r'[;&|`$]', part):
                found.add('shell special characters in a directory name')
        return found
    for _ in range(20000):
        name = ''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        is_dir = rng.random() < 0.3
        got = set(problem for problem in up.member_problems(name, is_dir)
                  if 'directory' in problem)
        assert got == per_directory(name, is_dir), (name, is_dir)


def test_member_problems_cost_does_not_grow_with_the_directory_count():
    import time
    start = time.time()
    for _ in range(10):
        up.member_problems('a/' * 32000 + 'x.fits')
    assert time.time() - start < 1.0


def _rar(tmp_path, names, symlinks=(), sfx=None):
    """A RAR archive of FITS stubs and symlinks (name, target), made by the rar
    binary; a self-extracting one with sfx, the path of an SFX module"""
    src = tmp_path / 'rar_src'
    src.mkdir()
    for name in names:
        (src / name).write_bytes(FITS_STUB)
    for name, target in symlinks:
        os.symlink(target, str(src / name))
    archive = str(tmp_path / ('x.sfx' if sfx else 'x.rar'))
    command = ['rar', 'a', '-ep', '-ol'] + (['-sfx' + sfx] if sfx else []) + [archive]
    subprocess.run(command + list(names) + [name for name, _ in symlinks],
                   cwd=str(src), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return archive


needs_rar = pytest.mark.skipif(shutil.which('rar') is None, reason='needs the rar binary')


@needs_rar
@pytest.mark.parametrize('symlinks', [
    # a target imitating a 'Type:' line (a second Type line)
    [('hide.fits', '/etc/passwd\n        Type: File')],
    # a target imitating a whole member
    [('poison.fits', '/x\n\n        Name: injected.php\n        Type: File')],
    # a name imitating a 'Type:' line, balanced by a 'Name:' line in the target
    [('img1.fits\n        Type: File', '/etc/passwd\n        Name: Type: File')],
    [('shell.php\n        Type: Directory', 'x\n\n        Name: Type: Directory\n        Type: File')],
])
def test_rar_technical_listing_cannot_be_forged(tmp_path, monkeypatch, symlinks):
    # The production backend: no rarfile, the rar binary lists the archive
    monkeypatch.setattr(up, 'HAVE_RARFILE', False)
    archive = _rar(tmp_path, ['a.fits', 'b.fits'], symlinks=symlinks)
    found = up.SuspiciousFilenames()
    up.check_archive_contents(archive, found)
    # the listing is not trusted, and the log says so; what is logged are the
    # names of the bare listing the verdict saw, never a forged member
    assert len(found.remarks) == 1 and 'disagree' in found.remarks[0]
    bare = up.rar_member_names_via_binary(archive)
    assert all(name in bare for name in _logged_names(found))
    assert 'injected.php' not in _logged_names(found)


@needs_rar
def test_rar_technical_listing_trusted_for_an_honest_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(up, 'HAVE_RARFILE', False)
    archive = _rar(tmp_path, ['a.fits', 'b.fits', ' lead.fits', 'c.fits\nd.fits'],
                   symlinks=[('link.fits', '/etc/passwd'), ('nl.fits', 'x\ny')])
    found = up.SuspiciousFilenames()
    up.check_archive_contents(archive, found)
    assert found.remarks == []
    problems = dict((name, found_problems) for _, name, found_problems in found.entries)
    assert problems['link.fits'] == ['symlink'] and problems['nl.fits'] == ['symlink']
    assert ' lead.fits' in problems and 'c.fits\nd.fits' in problems


def test_rar_technical_listing_parser_bounds_and_header():
    head = [b'Archive: x.rar\n', b'Details: RAR 5\n', b'\n']

    def member(name, kind=b'File'):
        return [b'        Name: ' + name + b'\n', b'        Type: ' + kind + b'\n', b'\n']
    lines = head + member(b'a.fits') + member(b'sub', b'Directory') + member(b'l.fits', b'Unix symbolic link')
    assert up._parse_rar_technical_listing(iter(lines), 'Archive: x.rar') == (
        [['a.fits', False, False], ['sub', True, False], ['l.fits', False, True]], True)
    # no 'Archive:' line naming the file: not understood, no verdict on it
    assert up._parse_rar_technical_listing(iter(lines), 'Archive: y.rar') is None
    # a name of countless line breaks stops being read
    flood = head + [b'        Name: a.fits\n'] + [b'\n'] * (20 * up.MAX_ARCHIVE_MEMBERS + 5)
    assert up._parse_rar_technical_listing(iter(flood), 'Archive: x.rar')[1] is False


def test_rar_technical_listing_stops_after_the_member_limit(monkeypatch):
    monkeypatch.setattr(up, 'MAX_ARCHIVE_MEMBERS', 3)
    lines = [b'Archive: x.rar\n', b'Details: RAR 5\n', b'\n']
    for i in range(10):
        lines += [b'        Name: m%d.fits\n' % i, b'        Type: File\n', b'\n']
    members, regular = up._parse_rar_technical_listing(iter(lines), 'Archive: x.rar')
    assert [name for name, _, _ in members] == ['m0.fits', 'm1.fits', 'm2.fits'] and not regular


def test_rar_technical_listing_skipped_for_an_oversized_archive(monkeypatch):
    def must_not_run(filepath):
        raise AssertionError('rar lt run for an archive the verdict rejects anyway')
    monkeypatch.setattr(up, 'rar_members_via_binary', must_not_run)
    filelist = ['image%04d.fits' % i for i in range(up.MAX_ARCHIVE_MEMBERS + 3)]
    filelist[5] = 'evil.php'
    found = up.SuspiciousFilenames()
    up.note_archive_members(found, 'x.rar', filelist=filelist)
    assert _logged_names(found) == ['evil.php'] and found.unexamined == 3
    assert found.remarks == ['too many members to read their types: symlink members cannot be told']


@needs_rar
def test_secure_upload_handler_logs_a_zip_uploaded_as_rar(tmp_path, uploads, suspicious_log, monkeypatch):
    # The rar binary lists nothing for ZIP data: list it as what it is
    monkeypatch.setattr(up, 'HAVE_RARFILE', False)
    monkeypatch.setattr(up, 'validate_archive_type', lambda filepath: (True, ''))   # no python-magic
    form = _FakeForm(_FakeFileItem('night.rar', _upload_archive(tmp_path, ['image2.fits', '../../x.fits'])))
    ok, message, _, _ = up.secure_upload_handler(form, str(uploads))
    assert not ok and message.startswith('Cannot validate RAR archive:')
    assert "member='../../x.fits'" in suspicious_log.read_text()


@needs_rar
def test_rejected_archive_listed_as_rar_and_as_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(up, 'HAVE_RARFILE', False)
    # a small RAR probe whose last member is a ZIP with innocent names
    decoy = tmp_path / 'zz.zip'
    _write_zip(str(decoy), ['a.fits', 'b.fits'])
    (tmp_path / 'rar_src').mkdir()
    shutil.copy(str(decoy), str(tmp_path / 'rar_src' / 'zz.zip'))
    (tmp_path / 'rar_src' / 'shell.php').write_bytes(b'x')
    archive = str(tmp_path / 'probe.rar')
    subprocess.run(['rar', 'a', '-ep', '-m0', archive, 'shell.php', 'zz.zip'],
                   cwd=str(tmp_path / 'rar_src'), check=True, stdout=subprocess.DEVNULL)
    assert zipfile.is_zipfile(archive)
    found = up.SuspiciousFilenames()
    up.note_rejected_archive_members(found, archive)
    assert 'shell.php' in _logged_names(found)


@needs_rar
def test_rejected_rar_after_a_long_prefix_is_listed(tmp_path, monkeypatch):
    # rar finds an archive up to 4 MiB into a file (a self-extractor stub)
    monkeypatch.setattr(up, 'HAVE_RARFILE', False)
    archive = _rar(tmp_path, ['a.fits', 'x;y.fits'])
    padded = tmp_path / 'padded.rar'
    padded.write_bytes(b'\0' * 1500000 + open(archive, 'rb').read())
    found = up.SuspiciousFilenames()
    up.note_rejected_archive_members(found, str(padded))
    assert _logged_names(found) == ['x;y.fits']


def test_rejected_archive_not_listed_again_as_what_was_tried(tmp_path, monkeypatch):
    # The content check already ran the rar binary on a .rar upload
    def must_not_run(filepath):
        raise AssertionError('listed again')
    monkeypatch.setattr(up, 'HAVE_RARFILE', False)
    monkeypatch.setattr(up, 'rar_member_names_via_binary', must_not_run)
    probe = tmp_path / 'x.rar'
    probe.write_bytes(b'Rar!\x1a\x07\x01\x00 corrupt')
    up.note_rejected_archive_members(up.SuspiciousFilenames(), str(probe), tried='.rar')


@needs_rar
def test_rejected_self_extracting_rar_is_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(up, 'HAVE_RARFILE', False)
    module = os.path.join(os.path.dirname(os.path.realpath(shutil.which('rar'))), 'default.sfx')
    if not os.path.isfile(module):
        pytest.skip('no self-extractor module next to the rar binary')
    archive = _rar(tmp_path, ['a.fits', 'x;y.fits'], sfx=module)
    assert not open(archive, 'rb').read(6).startswith(b'Rar!')
    found = up.SuspiciousFilenames()
    up.note_rejected_archive_members(found, archive)
    assert _logged_names(found) == ['x;y.fits']


def test_secure_upload_handler_with_a_nested_multipart_part(tmp_path, uploads, suspicious_log):
    if not hasattr(up.cgi.FieldStorage, 'read_multi'):
        pytest.skip('no cgi module (legacy-cgi) in this Python')
    body = (b'--B\r\nContent-Disposition: form-data; name="file"\r\n'
            b'Content-Type: multipart/mixed; boundary=C\r\n\r\n'
            b'--C\r\nContent-Disposition: file; filename="../../etc/cron.d/evil.zip"\r\n'
            b'Content-Type: application/zip\r\n\r\nPK\r\n'
            b'--C\r\nContent-Disposition: file; filename="shell.php"\r\n'
            b'Content-Type: text/plain\r\n\r\nx\r\n--C--\r\n--B--\r\n')
    environ = {'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': 'multipart/form-data; boundary=B',
               'CONTENT_LENGTH': str(len(body))}
    form = up.cgi.FieldStorage(fp=io.BytesIO(body), environ=environ)
    ok, message, _, _ = up.secure_upload_handler(form, str(uploads))
    assert not ok
    lines = _log_lines(suspicious_log)
    assert len(lines) == 2
    assert "upload_name='../../etc/cron.d/evil.zip'" in lines[0] and "upload_name='shell.php'" in lines[1]


def test_suspicious_filename_log_remarks_are_logged_without_names(suspicious_log):
    found = up.SuspiciousFilenames()
    found.remarks.append('the two listings disagree')
    up.log_suspicious_filenames(found, (True, '', '', ''))
    lines = _log_lines(suspicious_log)
    assert len(lines) == 1 and "more='the two listings disagree' result='accepted'" in lines[0]


def test_suspicious_filename_log_appends_to_a_write_only_log(suspicious_log):
    suspicious_log.write_bytes(b'')
    os.chmod(str(suspicious_log), 0o200)
    found = up.SuspiciousFilenames()
    found.note('member', 'x.php', ['not a .fit, .fits or .fts image'])
    up.log_suspicious_filenames(found, (False, 'x', '', ''))
    os.chmod(str(suspicious_log), 0o600)
    assert "member='x.php'" in suspicious_log.read_text()


def test_suspicious_filename_log_settings_refer_to_other_settings(tmp_path, monkeypatch):
    here = os.path.dirname(os.path.abspath(up.__file__))
    settings = {'IMAGE_DATA_ROOT': str(tmp_path / 'data'), 'DATA_PROCESSING_ROOT': '$IMAGE_DATA_ROOT',
                'WEB_ROOT': str(tmp_path / 'web'), 'HTDOCS_DIR': '${WEB_ROOT}/unmw'}
    monkeypatch.setattr(up, '_parse_config_value', lambda config_path, var_name: settings.get(var_name))
    for var in settings:
        monkeypatch.delenv(var, raising=False)
    assert up._config_path('DATA_PROCESSING_ROOT', here) == (str(tmp_path / 'data'), '')
    assert up._config_path('HTDOCS_DIR', here) == (str(tmp_path / 'web' / 'unmw'), '')
    # bash quoting, as in local_config.sh_example, and $PWD in a referenced setting
    settings.update({'HTDOCS_DIR': '"$WEB_ROOT"/unmw', 'WEB_ROOT': '$PWD/../htdocs',
                     # what _parse_config_value() leaves of '"/var/log/x.txt" # comment'
                     'SUSPICIOUS_FILENAMES_LOG': '/var/log/x.txt"'})
    assert up._config_path('HTDOCS_DIR', here) == (os.path.join(here, '..', 'htdocs', 'unmw'), '')
    assert up._config_path('SUSPICIOUS_FILENAMES_LOG', here) == ('/var/log/x.txt', '')


@pytest.mark.parametrize('filename', ['.', '..', 'x/', '/', 'a' * 252 + '.zip', 'a' * 300 + '.zip'])
def test_upload_invalid_basename_leaves_no_debris(filename, uploads, suspicious_log):
    result = up.secure_upload_handler(_FakeForm(_FakeFileItem(filename, b'x')), str(uploads))
    assert not result[0]
    assert result[2:] == ('', '')
    assert not list(uploads.iterdir())
    assert str(uploads) not in result[1]


def test_upload_255_character_name_is_supported(tmp_path, uploads, suspicious_log):
    filename = 'a' * 251 + '.zip'
    form = _FakeForm(_FakeFileItem(filename, _upload_archive(tmp_path, ['b.fits'])))
    result = up.secure_upload_handler(form, str(uploads))
    assert result[0], result[1]
    assert os.path.basename(result[3]) == filename


def test_upload_directory_collision_preserves_existing_data(uploads, monkeypatch, suspicious_log):
    monkeypatch.setattr(up.secrets, 'choice', lambda alphabet: 'a')
    collision = uploads / ('web_upload_%daaaaaaaa' % os.getpid())
    collision.mkdir()
    sentinel = collision / 'night.zip'
    sentinel.write_bytes(b'existing upload')
    result = up.secure_upload_handler(_FakeForm(_FakeFileItem('night.zip', b'x')), str(uploads))
    assert not result[0]
    assert sentinel.read_bytes() == b'existing upload'


@pytest.mark.parametrize('suffix,payload', [('.rar', b'PK\x03\x04data'),
                                          ('.zip', b'Rar!\x1a\x07\x01\x00'),
                                          ('.zip', b'not an archive')])
def test_archive_type_checks_content_without_magic(tmp_path, suffix, payload):
    path = tmp_path / ('night' + suffix)
    path.write_bytes(payload)
    assert up.validate_archive_type(str(path))[0] is False


def test_empty_zip_member_is_a_validation_error(tmp_path):
    path = tmp_path / 'empty-name.zip'
    with zipfile.ZipFile(str(path), 'w') as archive:
        archive.writestr(zipfile.ZipInfo(''), b'x')
    assert up.check_archive_contents(str(path)) == (False, 'Empty filename in archive')


def test_symlink_cannot_hide_behind_directory_name(tmp_path):
    path = tmp_path / 'directory-link.zip'
    _write_zip(str(path), ['a.fits', 'b.fits'], symlinks=['link.fits/'])
    assert up.check_archive_contents(str(path)) == (
        False, 'Symlink in archive is not allowed: link.fits/')


@pytest.fixture
def upload_request(uploads, monkeypatch, suspicious_log):
    monkeypatch.chdir(uploads.parent)
    monkeypatch.setenv('REQUEST_METHOD', 'POST')
    monkeypatch.setenv('CONTENT_TYPE', 'multipart/form-data; boundary=nmwtest')
    monkeypatch.setattr(up, 'check_disk_space_status', lambda directory: ('OK', ''))
    real_open = open
    monkeypatch.setattr(up, 'open',
                        lambda path, *args, **kwargs: io.StringIO('0 0 0 1/1 1')
                        if path == '/proc/loadavg' else real_open(path, *args, **kwargs),
                        raising=False)

    def request(body=b'', content_length=None):
        stream = io.BytesIO(body)
        monkeypatch.setattr(up.sys, 'stdin', types.SimpleNamespace(buffer=stream))
        monkeypatch.setenv('CONTENT_LENGTH', str(len(body)) if content_length is None else content_length)
        return stream
    return request


def test_upload_response_escapes_malicious_member(tmp_path, upload_request, monkeypatch, capsys):
    attack = '<img src=x onerror=alert(1)>.fits'
    form = _FakeForm(_FakeFileItem('night.zip', _upload_archive(tmp_path, [attack])))
    monkeypatch.setattr(up.cgi, 'FieldStorage', lambda **kwargs: form)
    upload_request()
    with pytest.raises(SystemExit):
        up.main()
    response = capsys.readouterr().out
    assert 'UNMW_STATUS:ERROR' in response
    assert attack not in response
    assert '&lt;img src=x onerror=alert(1)&gt;.fits' in response


@pytest.mark.parametrize('length,status', [('', 411), ('-1', 400), ('invalid', 400),
                                         ('9' * 21, 400), (str(up.MAX_REQUEST_SIZE + 1), 413)])
def test_upload_request_limit_precedes_parsing(upload_request, monkeypatch, capsys, length, status):
    stream = upload_request(b'body must not be read', content_length=length)
    monkeypatch.setattr(up.cgi, 'FieldStorage', lambda **kwargs: pytest.fail('parsed oversized request'))
    with pytest.raises(SystemExit):
        up.main()
    assert stream.tell() == 0
    response = capsys.readouterr().out
    assert 'Status: %d' % status in response
    assert 'UNMW_STATUS:ERROR' in response


@pytest.mark.parametrize('method', ['read', 'readline'])
def test_request_stream_never_reads_past_declared_length(method):
    raw = io.BytesIO(b'1234567890')
    stream = up.LimitedRequestBody(raw, 5)
    assert getattr(stream, method)() == b'12345'
    assert getattr(stream, method)(100) == b''
    assert raw.tell() == 5


@pytest.mark.parametrize('content_type', ['', 'text/plain', 'application/zip',
                                         'application/x-www-form-urlencoded'])
def test_upload_rejects_non_multipart_without_reading(upload_request, monkeypatch, capsys, content_type):
    stream = upload_request(b'must not be parsed')
    monkeypatch.setenv('CONTENT_TYPE', content_type)
    monkeypatch.setattr(up.cgi, 'FieldStorage', lambda **kwargs: pytest.fail('parsed non-multipart body'))
    with pytest.raises(SystemExit):
        up.main()
    assert stream.tell() == 0
    assert '415 Unsupported Media Type' in capsys.readouterr().out


@pytest.mark.parametrize('headers', [b'x' * (up.MAX_MULTIPART_HEADER_BYTES + 1),
                                   b'X: y\r\n' * (up.MAX_MULTIPART_HEADER_BYTES // 6 + 1)])
def test_multipart_header_memory_is_bounded(upload_request, capsys, headers):
    if not hasattr(up.cgi.FieldStorage, 'read_multi'):
        pytest.skip('needs legacy-cgi')
    stream = upload_request(b'--nmwtest\r\n' + headers + b'\r\n\r\nignored')
    with pytest.raises(SystemExit):
        up.main()
    assert stream.tell() <= up.MAX_MULTIPART_HEADER_BYTES + 1
    assert 'UNMW_STATUS:ERROR Invalid upload request' in capsys.readouterr().out


def test_multipart_parser_cannot_read_whole_large_body():
    raw = io.BytesIO(b'x' * (up.MAX_MULTIPART_HEADER_BYTES + 1))
    stream = up.LimitedRequestBody(raw, len(raw.getvalue()))
    with pytest.raises(ValueError, match='oversized read'):
        stream.read()
    assert raw.tell() == 0


def test_unexpected_upload_error_is_generic(upload_request, monkeypatch, capsys):
    def broken_parser(**kwargs):
        raise RuntimeError('secret /srv/private/<script>')
    monkeypatch.setattr(up.cgi, 'FieldStorage', broken_parser)
    upload_request()
    with pytest.raises(SystemExit):
        up.main()
    response = capsys.readouterr()
    assert 'UNMW_STATUS:ERROR Unable to handle the upload' in response.out
    assert 'secret' not in response.out and '/srv/private' not in response.out
    assert 'secret' in response.err


def test_real_multipart_request_is_bounded(upload_request, monkeypatch, capsys):
    if not hasattr(up.cgi.FieldStorage, 'read_multi'):
        pytest.skip('needs legacy-cgi')
    monkeypatch.setenv('CONTENT_TYPE', 'multipart/form-data; boundary=nmwtest')
    body = (b'--nmwtest\r\nContent-Disposition: form-data; name="file"; filename="x.zip"'
            b'\r\n\r\nx\r\n--nmwtest--\r\n')
    raw = upload_request(body + b'excess data', content_length=str(len(body)))
    with pytest.raises(SystemExit):
        up.main()
    assert raw.tell() <= len(body)
    assert 'UNMW_STATUS:ERROR File size' in capsys.readouterr().out


def test_zip_member_limit_cannot_be_bypassed_by_forged_count(tmp_path, monkeypatch):
    archive = tmp_path / 'many.zip'
    _write_zip(str(archive), ['a%d.fits' % i for i in range(up.MAX_ARCHIVE_MEMBERS + 1)], payload=b'x')
    data = bytearray(archive.read_bytes())
    end = data.rfind(b'PK\x05\x06')
    import struct
    struct.pack_into('<HH', data, end + 8, 1, 1)
    archive.write_bytes(data)
    # The costly ZipFile constructor must never be reached.
    monkeypatch.setattr(up.zipfile, 'ZipFile', lambda *args, **kwargs: pytest.fail('unbounded parser'))
    valid, message = up.check_archive_contents(str(archive))
    assert not valid and 'Too many files' in message


def test_zip_metadata_limit_also_applies_to_rejected_archive_logging(tmp_path, monkeypatch):
    archive = tmp_path / 'large-directory.zip'
    _write_zip(str(archive), ['a.fits', 'b.fits'])
    monkeypatch.setattr(up, 'MAX_ZIP_DIRECTORY_BYTES', 10)
    monkeypatch.setattr(up.zipfile, 'ZipFile', lambda *args, **kwargs: pytest.fail('unbounded parser'))
    found = up.SuspiciousFilenames()
    assert not up.check_archive_contents(str(archive), found)[0]
    up.note_rejected_archive_members(found, str(archive))
    assert found.members_examined
    assert 'metadata limit' in found.remarks[0]


def test_zip64_directory_preflight(tmp_path, monkeypatch):
    archive = tmp_path / 'zip64.zip'
    monkeypatch.setattr(zipfile, 'ZIP64_LIMIT', 20)
    _write_zip(str(archive), ['a.fits', 'b.fits'])
    assert up.check_archive_contents(str(archive)) == (True, '')


@pytest.mark.parametrize('name', [
    '../escape.fits', 'night/../escape.fits', '/escape.fits', '//host/share/a.fits',
    'C:/night/a.fits', 'C:a.fits', r'C:\night\a.fits', r'\night\a.fits',
    r'..\escape.fits', r'night\..\a.fits', r'\\host\share\a.fits',
    '../', 'night/../', '/empty/', 'C:/empty/', r'..\empty/',
])
def test_unsafe_zip_path_rejects_entire_upload(tmp_path, uploads, suspicious_log, name):
    # Two good images remain: deleting only the bad member would be a bug.
    data = _upload_archive(tmp_path, ['image2.fits', name])
    result = up.secure_upload_handler(_FakeForm(_FakeFileItem('night.zip', data)), str(uploads))
    assert result[0] is False and 'Unsafe path in archive:' in result[1]
    assert result[2:] == ('', '')
    assert not list(uploads.iterdir())
    assert 'path traversal' in suspicious_log.read_text() or 'absolute path' in suspicious_log.read_text()


@pytest.mark.parametrize('name', ['night/a.fits', './night/a.fits', 'night/./a.fits',
                                 'night..old/a.fits', 'night/', 'night..old/'])
def test_safe_relative_zip_paths_still_work(tmp_path, name):
    archive = tmp_path / 'night.zip'
    _write_zip(str(archive), ['a.fits', 'b.fits', name])
    assert up.check_archive_contents(str(archive)) == (True, '')


def _technical_listing(filepath, records):
    lines = ['Archive: ' + os.path.abspath(str(filepath)), 'Details: RAR 5', '']
    for record in records:
        lines.extend('        %s: %s' % (key, value) for key, value in record)
        lines.append('')
    return [(line + '\n').encode('utf-8') for line in lines]


def _rar_fields(name, kind='File', size=100, packed=100):
    return [('Name', name), ('Type', kind), ('Size', str(size)), ('Packed size', str(packed))]


@pytest.mark.parametrize('case', ['valid', 'directory', 'symlink', 'hardlink', 'copy',
                                 'encrypted', 'negative_size', 'missing_size', 'duplicate_size',
                                 'duplicate_type', 'mismatch', 'blank_name', 'line_injection',
                                 'expansion', 'ratio', 'zero_packed'])
def test_rar_binary_validates_types_sizes_and_unambiguous_names(monkeypatch, case):
    monkeypatch.setattr(up, 'HAVE_RARFILE', False)
    records = [_rar_fields('a.fits'), _rar_fields('b.fits')]
    if case == 'directory':
        records += [_rar_fields('night', 'Directory', 0, 0)]
    elif case in ('symlink', 'hardlink', 'copy'):
        records += [_rar_fields('link.fits', {'symlink': 'Unix symbolic link',
                                             'hardlink': 'Hard link', 'copy': 'File reference'}[case])]
    elif case == 'encrypted':
        records[0] += [('Flags', 'encrypted')]
    elif case == 'negative_size':
        records[0][2] = ('Size', '-1')
    elif case == 'missing_size':
        records[0].pop(2)
    elif case in ('duplicate_size', 'duplicate_type'):
        records[0].append(records[0][2 if case == 'duplicate_size' else 1])
    elif case == 'blank_name':
        records[0][0] = ('Name', '')
    elif case == 'line_injection':
        records[0][0] = ('Name', 'a.fits\n        Type: File\n        Size: 1')
    elif case == 'expansion':
        records[0][2] = ('Size', str(up.MAX_UNCOMPRESSED_BYTES + 1))
    elif case == 'ratio':
        records[0][2] = ('Size', '1000000')
    elif case == 'zero_packed':
        records = [_rar_fields('a.fits', size=1000, packed=0), _rar_fields('b.fits', size=1000, packed=0)]
    bare = [(record[0][1] + '\n').encode('utf-8') for record in records]
    if case == 'mismatch':
        bare[0] = b'different.fits\n'
    technical = _technical_listing('night.rar', records)
    # Real tools split injected newlines into separate output lines.
    technical = b''.join(technical).splitlines(keepends=True)
    monkeypatch.setattr(up, '_rar_listing_lines', lambda path, mode: bare if mode == 'lb' else technical)
    result = up.check_archive_contents('night.rar')
    assert result[0] is (case in ('valid', 'directory')), result


@pytest.mark.parametrize('scenario', ['bytes', 'line', 'members', 'timeout', 'exit_error'])
def test_native_rar_listing_is_bounded_and_reaped(monkeypatch, scenario):
    real_popen = subprocess.Popen
    processes = []
    monkeypatch.setattr(up, 'MAX_RAR_LISTING_BYTES', 100)
    monkeypatch.setattr(up, 'MAX_RAR_LISTING_LINE_BYTES', 40)
    monkeypatch.setattr(up, 'MAX_ARCHIVE_MEMBERS', 10)
    monkeypatch.setattr(up, 'RAR_LISTING_TIMEOUT', 0.15 if scenario == 'timeout' else 2)
    monkeypatch.setenv('RAR', '-x*')
    monkeypatch.setenv('RARINISWITCHES', '-x*')
    payload = {'bytes': b'a' * 30 + b'\n', 'line': b'a' * 41,
               'members': b'\n', 'timeout': b'', 'exit_error': b'a.fits\n'}[scenario]
    repeats = {'bytes': 4, 'line': 1, 'members': 11, 'timeout': 1, 'exit_error': 1}[scenario]
    code = ('import sys,time; sys.stdout.buffer.write(%r * %d); sys.stdout.flush(); '
            '%s' % (payload, repeats, 'sys.exit(7)' if scenario == 'exit_error' else 'time.sleep(10)'))

    def spawn(command, **kwargs):
        assert '-cfg-' in command and '-c-' in command and '-p-' in command and '-scf' in command
        assert command[-2] == '--' and os.path.isabs(command[-1])
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['env']['LC_ALL'] == up._rar_utf8_locale()
        assert kwargs['env']['LANGUAGE'] == 'C'
        assert 'RAR' not in kwargs['env'] and 'RARINISWITCHES' not in kwargs['env']
        process = real_popen([sys.executable, '-c', code], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(up.subprocess, 'Popen', spawn)
    expected = {'bytes': 'metadata limit', 'line': 'metadata limit', 'members': 'member or line limit',
                'timeout': 'failed or timed out', 'exit_error': 'failed or timed out'}[scenario]
    with pytest.raises(up.RarValidationError, match=expected):
        up._rar_listing_lines('night.rar', 'lb')
    assert len(processes) == 1 and processes[0].poll() is not None


@needs_rar
@pytest.mark.parametrize('backend', ['binary', 'rarfile'])
@pytest.mark.parametrize('prefix', ['../', '/absolute/', 'night/../', 'C:/night/'])
def test_real_rar_unsafe_paths_reject_archive(tmp_path, monkeypatch, backend, prefix):
    if backend == 'rarfile' and not up.HAVE_RARFILE:
        pytest.skip('rarfile is optional')
    monkeypatch.setattr(up, 'HAVE_RARFILE', backend == 'rarfile')
    for name in ('a.fits', 'b.fits'):
        (tmp_path / name).write_bytes(FITS_STUB)
    archive = tmp_path / 'paths.rar'
    subprocess.run(['rar', 'a', '-idq', '-m0', '-ap' + prefix, str(archive), 'a.fits', 'b.fits'],
                   cwd=str(tmp_path), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = up.check_archive_contents(str(archive))
    assert result[0] is False and 'Unsafe path' in result[1], result


@needs_rar
@pytest.mark.parametrize('backend', ['binary', 'rarfile'])
def test_real_rar_directories_and_sizes(tmp_path, monkeypatch, backend):
    if backend == 'rarfile' and not up.HAVE_RARFILE:
        pytest.skip('rarfile is optional')
    monkeypatch.setattr(up, 'HAVE_RARFILE', backend == 'rarfile')
    (tmp_path / 'night').mkdir()
    for name in ('a.fits', 'b.fits'):
        (tmp_path / 'night' / name).write_bytes(FITS_STUB)
    archive = tmp_path / 'directory.rar'
    subprocess.run(['rar', 'a', '-idq', '-m0', str(archive), 'night'], cwd=str(tmp_path), check=True)
    assert up.check_archive_contents(str(archive)) == (True, '')
    monkeypatch.setattr(up, 'MAX_UNCOMPRESSED_BYTES', len(FITS_STUB))
    assert 'Archive expands to' in up.check_archive_contents(str(archive))[1]
    monkeypatch.setattr(up, 'MAX_UNCOMPRESSED_BYTES', 32 * 1024 ** 3)
    monkeypatch.setattr(up, 'MAX_ARCHIVE_MEMBERS', 2)
    assert up.check_archive_contents(str(archive))[0] is False  # directory counts too


@needs_rar
def test_native_validation_works_with_only_unrar(tmp_path, monkeypatch):
    unrar = shutil.which('unrar')
    if unrar is None:
        pytest.skip('needs the unrar binary')
    archive = _rar(tmp_path, ['a.fits', 'b.fits'])
    monkeypatch.setattr(up, 'HAVE_RARFILE', False)
    real_popen = subprocess.Popen

    def without_rar(command, **kwargs):
        if command[0] == 'rar':
            raise FileNotFoundError('rar is unavailable')
        assert command[0] == 'unrar'
        return real_popen([unrar] + command[1:], **kwargs)

    monkeypatch.setattr(up.subprocess, 'Popen', without_rar)
    assert up.check_archive_contents(archive) == (True, '')


@needs_rar
@pytest.mark.parametrize('backend', ['binary', 'rarfile'])
@pytest.mark.parametrize('case', ['symlink', 'hardlink', 'encrypted', 'ratio', 'unsafe_empty_dir',
                                 'invisible_unicode'])
def test_real_rar_rejects_unsafe_metadata(tmp_path, monkeypatch, backend, case):
    if backend == 'rarfile' and not up.HAVE_RARFILE:
        pytest.skip('rarfile is optional')
    monkeypatch.setattr(up, 'HAVE_RARFILE', backend == 'rarfile')
    payload = b'\0' * 65536 if case == 'ratio' else FITS_STUB
    for name in ('a.fits', 'b.fits'):
        (tmp_path / name).write_bytes(payload)
    members = ['a.fits', 'b.fits']
    switches = ['-m5'] if case == 'ratio' else ['-m0']
    if case == 'invisible_unicode':
        (tmp_path / 'x\u200b.fits').write_bytes(payload)
        members.append('x\u200b.fits')
    elif case == 'symlink':
        os.symlink('a.fits', str(tmp_path / 'link.fits'))
        members.append('link.fits')
        switches.append('-ol')
    elif case == 'hardlink':
        os.link(str(tmp_path / 'a.fits'), str(tmp_path / 'link.fits'))
        members.append('link.fits')
        switches.append('-oh')
    elif case == 'encrypted':
        switches.append('-pfixture-password')
    archive = tmp_path / 'metadata.rar'
    subprocess.run(['rar', 'a', '-idq'] + switches + [str(archive)] + members,
                   cwd=str(tmp_path), check=True)
    if case == 'unsafe_empty_dir':
        (tmp_path / 'empty').mkdir()
        subprocess.run(['rar', 'a', '-idq', '-ap../', str(archive), 'empty'],
                       cwd=str(tmp_path), check=True)
    found = up.SuspiciousFilenames()
    result = up.check_archive_contents(str(archive), found)
    assert result[0] is False, result
    if case == 'unsafe_empty_dir':
        assert 'Unsafe path' in result[1], result
    elif case == 'ratio':
        assert 'compression ratio' in result[1], result
    elif case == 'hardlink':
        assert 'link.fits' in _logged_names(found)


@needs_rar
@pytest.mark.parametrize('backend', ['binary', 'rarfile'])
def test_stored_rar4_compatibility(tmp_path, monkeypatch, backend):
    if backend == 'rarfile' and not up.HAVE_RARFILE:
        pytest.skip('rarfile is optional')
    import struct
    import zlib
    monkeypatch.setattr(up, 'HAVE_RARFILE', backend == 'rarfile')

    def header(data):
        return struct.pack('<H', zlib.crc32(data) & 0xffff) + data

    # Minimal stored RAR4 fixture: marker, archive header, two ordinary file
    # headers/data and end marker. Modern rar cannot create RAR4 itself.
    data = b'Rar!\x1a\x07\0' + header(struct.pack('<BHH6s', 0x73, 0, 13, b'\0' * 6))
    for name in (b'a.fits', b'b.fits'):
        size = len(FITS_STUB)
        file_header = struct.pack('<BHHIIBIIBBHI', 0x74, 0x8000, 32 + len(name),
                                  size, size, 3, zlib.crc32(FITS_STUB), 0, 20, 0x30,
                                  len(name), 0o100644) + name
        data += header(file_header) + FITS_STUB
    data += header(struct.pack('<BHH', 0x7b, 0x4000, 7))
    archive = tmp_path / 'stored-rar4.rar'
    archive.write_bytes(data)
    assert up.check_archive_contents(str(archive)) == (True, '')


def test_native_rar_requires_a_lossless_locale(monkeypatch):
    def only_ascii(category, value=None):
        if value in (None, 'C'):
            return 'C'
        raise up.locale.Error('unavailable')
    monkeypatch.setattr(up.locale, 'setlocale', only_ascii)
    monkeypatch.setattr(up.locale, 'nl_langinfo', lambda item: 'ANSI_X3.4-1968')
    monkeypatch.setattr(up.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('lossy locale used'))
    with pytest.raises(up.RarValidationError, match='UTF-8 locale is required'):
        up._rar_listing_lines('night.rar', 'lb')


@needs_rar
@pytest.mark.parametrize('unsafe', [False, True])
def test_wrapper_uses_native_metadata_without_rarfile(tmp_path, unsafe):
    import time
    repo = os.path.dirname(__file__)
    (tmp_path / 'night').mkdir()
    for name in ('a.fits', 'b.fits'):
        (tmp_path / 'night' / name).write_bytes(FITS_STUB)
    archive = tmp_path / 'night.rar'
    args = ['rar', 'a', '-idq', '-m0'] + (['-ap../'] if unsafe else [])
    subprocess.run(args + [str(archive), 'night'], cwd=str(tmp_path), check=True)
    # Force ImportError in a fresh interpreter: this is not just a patched
    # boolean in an otherwise rarfile-enabled Python process.
    blocked = tmp_path / 'blocked_imports'
    blocked.mkdir()
    (blocked / 'rarfile.py').write_text('raise ImportError("rarfile intentionally unavailable")\n')
    stub = tmp_path / 'autoprocess.sh'
    stub.write_text('#!/bin/sh\nprintf started > invoked\n')
    stub.chmod(0o700)
    env = os.environ.copy()
    for key in ('GATEWAY_INTERFACE', 'REQUEST_METHOD'):
        env.pop(key, None)
    env['UNMW_UPLOAD_PYTHON'] = sys.executable
    env['PYTHONPATH'] = str(blocked)
    result = subprocess.run(['bash', os.path.join(repo, 'wrapper.sh'), str(archive)],
                            cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is (not unsafe), result.stdout + result.stderr
    if not unsafe:
        # The real wrapper intentionally backgrounds processing.
        deadline = time.monotonic() + 2
        while not (tmp_path / 'invoked').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    assert (tmp_path / 'invoked').exists() is (not unsafe)


def test_wrapper_keeps_complete_filenames(tmp_path):
    # "bad.fits " used to become "bad.fits" in awk and launch processing.
    # The stub makes even that regression harmless and observable.
    wrapper = os.path.join(os.path.dirname(__file__), 'wrapper.sh')
    archive = tmp_path / 'night.zip'
    _write_zip(str(archive), ['a.fits', 'bad.fits '])
    stub = tmp_path / 'autoprocess.sh'
    stub.write_text('#!/bin/sh\nprintf started > invoked\n')
    stub.chmod(0o700)
    env = os.environ.copy()
    for key in ('GATEWAY_INTERFACE', 'REQUEST_METHOD'):
        env.pop(key, None)
    result = subprocess.run(['bash', wrapper, str(archive)], cwd=str(tmp_path),
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert not (tmp_path / 'invoked').exists()


@pytest.mark.parametrize('marker', ['GATEWAY_INTERFACE', 'REQUEST_METHOD'])
def test_autoprocess_refuses_direct_cgi_before_sourcing_config(tmp_path, marker):
    script = tmp_path / 'autoprocess.sh'
    shutil.copyfile(os.path.join(os.path.dirname(__file__), 'autoprocess.sh'), str(script))
    (tmp_path / 'local_config.sh').write_text('touch config_was_sourced\n')
    env = os.environ.copy()
    env[marker] = 'CGI/1.1' if marker == 'GATEWAY_INTERFACE' else 'GET'
    result = subprocess.run(['bash', str(script), '/must/not/be/processed'],
                            cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert 'must not be run as CGI' in result.stdout
    assert not (tmp_path / 'config_was_sourced').exists()


def test_autoprocess_stages_without_overwriting_shared_archive(tmp_path):
    script = tmp_path / 'autoprocess.sh'
    shutil.copyfile(os.path.join(os.path.dirname(__file__), 'autoprocess.sh'), str(script))
    data = tmp_path / 'data'
    data.mkdir()
    incoming = tmp_path / 'incoming'
    incoming.mkdir()
    archive = incoming / 'same-night.zip'
    _write_zip(str(archive), ['a.fits', 'b.fits'])
    sentinel = data / archive.name
    sentinel.write_bytes(b'previous archive must survive')
    vast = tmp_path / 'vast'
    (vast / 'util' / 'transients').mkdir(parents=True)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for path, body in [(vast / 'vast', 'echo VaST'),
                       (vast / 'util' / 'transients' / 'transient_factory_test31.sh', 'exit 99'),
                       (bin_dir / 'unzip', 'echo stopped-before-extraction; exit 99')]:
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o700)
    env = os.environ.copy()
    for key in ('GATEWAY_INTERFACE', 'REQUEST_METHOD'):
        env.pop(key, None)
    env.update(IMAGE_DATA_ROOT=str(data), DATA_PROCESSING_ROOT=str(data),
               VAST_REFERENCE_COPY=str(vast), URL_OF_DATA_PROCESSING_ROOT='http://unused.test/uploads',
               AUTOPROCESS_NO_WAIT='yes', WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB='1',
               WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB='1',
               PATH=str(bin_dir) + os.pathsep + env['PATH'])
    result = subprocess.run(['bash', str(script), str(archive)], cwd=str(tmp_path),
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert 'stopped-before-extraction' in result.stdout, result.stdout + result.stderr
    assert sentinel.read_bytes() == b'previous archive must survive'
    staged = list(data.glob('img_same-night_*/*.zip'))
    assert len(staged) == 1
    assert staged[0].read_bytes() == archive.read_bytes()


def test_lock_files_never_truncate_or_follow_links(tmp_path):
    import nmw_coord_lib as ncl
    victim = tmp_path / 'victim'
    victim.write_bytes(b'precious')
    link = tmp_path / 'link.lock'
    link.symlink_to(victim)
    with pytest.raises(OSError):
        ncl.open_lock_file(str(link))
    hardlink = tmp_path / 'hardlink.lock'
    os.link(str(victim), str(hardlink))
    with pytest.raises(OSError):
        ncl.open_lock_file(str(hardlink))
    assert victim.read_bytes() == b'precious'
    regular = tmp_path / 'regular.lock'
    regular.write_text('old marker')
    with ncl.open_lock_file(str(regular)) as fh:
        assert fh.read() == 'old marker'


@pytest.mark.parametrize('length', ['', '-1', '65537', 'huge'])
def test_coordinate_forms_bound_body_before_reading(monkeypatch, capsys, length):
    import nmw_coord_lib as ncl
    monkeypatch.setenv('REQUEST_METHOD', 'POST')
    monkeypatch.setenv('CONTENT_LENGTH', length)
    raw = io.BytesIO(b'not read')
    monkeypatch.setattr(sys, 'stdin', types.SimpleNamespace(buffer=raw))
    with pytest.raises(SystemExit):
        ncl.parse_cgi_form(up.cgi)
    assert raw.tell() == 0
    assert '400 Bad Request' in capsys.readouterr().out


def test_coordinate_forms_reject_file_parts_and_accept_text(monkeypatch, capsys):
    if not hasattr(up.cgi.FieldStorage, 'read_multi'):
        pytest.skip('needs legacy-cgi')
    import nmw_coord_lib as ncl
    monkeypatch.setenv('REQUEST_METHOD', 'POST')
    monkeypatch.setenv('CONTENT_TYPE', 'multipart/form-data; boundary=nmw')
    monkeypatch.delenv('QUERY_STRING', raising=False)
    body = (b'--nmw\r\nContent-Disposition: form-data; name="coords"; filename="probe"'
            b'\r\n\r\n12 34\r\n--nmw--\r\n')
    monkeypatch.setenv('CONTENT_LENGTH', str(len(body)))
    monkeypatch.setattr(sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(body)))
    with pytest.raises(SystemExit):
        ncl.parse_cgi_form(up.cgi)
    assert '400 Bad Request' in capsys.readouterr().out
    body = body.replace(b'; filename="probe"', b'')
    monkeypatch.setenv('CONTENT_LENGTH', str(len(body)))
    monkeypatch.setattr(sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(body)))
    assert ncl.parse_cgi_form(up.cgi).getfirst('coords') == '12 34'


def test_cgi_exception_handler_never_displays_traceback(monkeypatch, capsys):
    import nmw_coord_lib as ncl
    monkeypatch.setattr(sys, 'excepthook', sys.excepthook)
    ncl.enable_cgi_error_logging()
    try:
        raise ValueError('private /srv/path <script>')
    except ValueError:
        sys.excepthook(*sys.exc_info())
    captured = capsys.readouterr()
    assert 'Unable to complete the request' in captured.out
    assert 'private' not in captured.out and '<script>' not in captured.out
    assert 'private' in captured.err


def test_existing_suspicious_log_permissions_are_tightened(suspicious_log):
    suspicious_log.write_text('previous\n')
    suspicious_log.chmod(0o644)
    up._append_private_log(str(suspicious_log), b'next\n')
    assert stat.S_IMODE(suspicious_log.stat().st_mode) == 0o600


@pytest.mark.parametrize('api_response,can_deploy', [
    ('{"total_count":1,"check_runs":[{"status":"completed","conclusion":"success"}]}', True),
    ('{"total_count":1,"check_runs":[]}', False),
    ('{"total_count":101,"check_runs":[{"status":"completed","conclusion":"success"}]}', False),
    ('{"total_count":1,"check_runs":[{"status":"completed","conclusion":null}]}', False),
    ('{"total_count":1,"check_runs":[{"status":"in_progress","conclusion":"success"}]}', False),
    ('{"total_count":1,"check_runs":[{"status":"completed","conclusion":"failure"}]}', False),
    ('not JSON', False),
])
def test_updater_uses_exact_tested_commit(tmp_path, api_response, can_deploy):
    source = os.path.join(os.path.dirname(__file__), 'git_unmw_automated_update.sh')
    with open(source) as fh:
        script = fh.read()
    updater = tmp_path / 'update.sh'
    updater.write_text(script)
    (tmp_path / '.git').mkdir()
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake_git = bin_dir / 'git'
    fake_git.write_text('''#!/bin/sh
case "$1" in
 symbolic-ref) echo refs/remotes/origin/main ;;
 rev-parse) if [ "$2" = HEAD ]; then echo old_commit; else echo tested_commit; fi ;;
 fetch|status) exit 0 ;;
 merge) printf '%s\\n' "$@" > deployed_command ;;
 pull) echo untested_commit > deployed_command ;;
 *) exit 99 ;;
esac
''')
    fake_curl = bin_dir / 'curl'
    import shlex
    fake_curl.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(api_response) + '\n')
    for executable in (fake_git, fake_curl):
        executable.chmod(0o700)
    env = os.environ.copy()
    for name in ('GATEWAY_INTERFACE', 'REQUEST_METHOD'):
        env.pop(name, None)
    env['PATH'] = str(bin_dir) + os.pathsep + env['PATH']
    result = subprocess.run(['bash', str(updater)], cwd=str(tmp_path), env=env,
                            capture_output=True, text=True, timeout=5)
    if can_deploy:
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / 'deployed_command').read_text().splitlines() == ['merge', '--ff-only', 'tested_commit']
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert not (tmp_path / 'deployed_command').exists()


def test_legacy_upload_handler_refuses_cgi_before_imports(tmp_path):
    # Python 2 is intentionally not installed. Its guard uses shared Python
    # 2/3 syntax, so execute the actual prefix up to the first CGI import.
    source = os.path.join(os.path.dirname(__file__), 'upload.py2')
    with open(source) as fh:
        prefix = fh.read().split('\nimport cgi\n', 1)[0]
    env = os.environ.copy()
    env['GATEWAY_INTERFACE'] = 'CGI/1.1'
    result = subprocess.run([sys.executable, '-c', prefix], cwd=str(tmp_path),
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert 'must not be run as CGI' in result.stdout
    assert not list(tmp_path.iterdir())


def test_updater_refuses_held_checkout_lock(tmp_path):
    import fcntl
    source = os.path.join(os.path.dirname(__file__), 'git_unmw_automated_update.sh')
    updater = tmp_path / 'update.sh'
    shutil.copyfile(source, str(updater))
    (tmp_path / '.git').mkdir()
    lock_path = tmp_path / '.git' / 'unmw_automated_update.lock'
    lock_path.write_text('existing lock file\n')
    env = os.environ.copy()
    for name in ('GATEWAY_INTERFACE', 'REQUEST_METHOD'):
        env.pop(name, None)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ('git', 'curl'):
        executable = bin_dir / name
        executable.write_text('#!/bin/sh\nexit 99\n')
        executable.chmod(0o700)
    env['PATH'] = str(bin_dir) + os.pathsep + env['PATH']
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(['bash', str(updater)], cwd=str(tmp_path), env=env,
                                capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Another updater is running' in result.stdout
    assert 'New version available' not in result.stdout
    assert lock_path.read_text() == 'existing lock file\n'


@pytest.mark.parametrize('case', ['accepted', 'bad_member', 'missing_wrapper'])
def test_upload_cgi_subprocess(tmp_path, case):
    if not hasattr(up.cgi.FieldStorage, 'read_multi'):
        pytest.skip('needs legacy-cgi')
    if os.getloadavg()[1] > 50:
        pytest.skip('host exceeds the real CGI emergency load threshold')
    script = tmp_path / 'upload.py'
    shutil.copyfile(up.__file__, str(script))
    (tmp_path / 'uploads').mkdir()
    private = tmp_path / 'private'
    private.mkdir()
    if case == 'accepted':
        wrapper = tmp_path / 'wrapper.sh'
        wrapper.write_text('''#!/bin/sh
[ -z "${GATEWAY_INTERFACE:-}${REQUEST_METHOD:-}" ] || exit 17
printf '%s\\n' 'http://example.invalid/results/' > "$(dirname -- "$1")/results_url.txt"
''')
        wrapper.chmod(0o700)
    attack = '<img src=x onerror=alert(1)>.fits'
    names = [attack] if case == 'bad_member' else ['b.fits']
    data = _upload_archive(tmp_path, names)
    body = (b'--nmw\r\nContent-Disposition: form-data; name="file"; filename="night;name.zip"'
            b'\r\nContent-Type: application/zip\r\n\r\n' + data + b'\r\n--nmw--\r\n')
    env = os.environ.copy()
    env.update(REQUEST_METHOD='POST', GATEWAY_INTERFACE='CGI/1.1',
               CONTENT_TYPE='multipart/form-data; boundary=nmw', CONTENT_LENGTH=str(len(body)),
               WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB='1', WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB='1',
               SUSPICIOUS_FILENAMES_LOG=str(private / 'names.log'))
    # It must be outside the copied CGI's published directory.
    env['SUSPICIOUS_FILENAMES_LOG'] = str(tmp_path.parent / (tmp_path.name + '-names.log'))
    try:
        result = subprocess.run([sys.executable, str(script)], cwd=str(tmp_path), env=env,
                                input=body, capture_output=True, timeout=10)
        output = result.stdout.decode('utf-8')
        assert output.startswith('Content-Type: text/html')
        if case == 'accepted':
            assert result.returncode == 0, output + result.stderr.decode()
            assert 'UNMW_STATUS:OK' in output
            assert 'http://example.invalid/results/' in output
            assert len(list((tmp_path / 'uploads').glob('*/night_name.zip'))) == 1
        else:
            assert result.returncode == 1
            assert 'UNMW_STATUS:ERROR' in output
            assert not list((tmp_path / 'uploads').iterdir())
            assert str(tmp_path) not in output
            assert attack not in output
            if case == 'bad_member':
                assert '&lt;img src=x onerror=alert(1)&gt;.fits' in output
    finally:
        if os.path.exists(env['SUSPICIOUS_FILENAMES_LOG']):
            os.unlink(env['SUSPICIOUS_FILENAMES_LOG'])

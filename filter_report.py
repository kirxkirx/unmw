#!/usr/bin/env python3

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from os.path import splitext

from bs4 import BeautifulSoup

# CONSTANTS
# MAX_MAG = 40
# AST_MAG_DIF_PREDICTED_OBSERVED = 2
# AST_1_RA_DIST_PREDICTED_OBSERVED_ARCSEC = 180
# AST_2_RA_DIST_PREDICTED_OBSERVED_ARCSEC = 180
VAR_MAX_DIST_ARCSEC = 30

# The JSON sibling of each _filtered.html. The schema lives next to this
# script as filter_report_json_schema.json; the user-facing description is
# in filter_report_json_format.md. Bump on breaking changes only.
JSON_SCHEMA_VERSION = 2

# VaST emits a single "This object is listed in <file>" marker next to each
# candidate when its position matches one of these exclusion lists. We map
# the source filenames to JSON keys exposed under `crossmatches`.
EXCLUSION_LIST_FILE_TO_JSON_KEY = {
    'tocp_transients_list.txt': 'tocp',
    'asassn_transients_list.txt': 'asassn_transients',
    'tns_transients_list.txt': 'tns_transients',
    'neverexclude_list.txt': 'neverexclude_list',
    'moons.txt': 'moons',
    'planets.txt': 'planets',
    'spacecraft.txt': 'spacecraft',
}

# A match against any of these promotes the candidate to "known_transient"
# (when it is not already classified as known_asteroid / known_variable).
KNOWN_TRANSIENT_LIST_FILES = (
    'tocp_transients_list.txt',
    'asassn_transients_list.txt',
    'tns_transients_list.txt',
)

# Default base URL when $URL_OF_DATA_PROCESSING_ROOT is not set in the
# environment. Mirrors combine_reports.sh's fallback so the JSON stays
# consistent with the HTML it accompanies.
DEFAULT_URL_OF_DATA_PROCESSING_ROOT = "http://vast.sai.msu.ru/unmw/uploads"


def is_asteroid(pre_el_text):
    try:
        if 'The object was found in astcheck' in pre_el_text:
            # Do not try to parse the asteroid string to get distance as it may
            # take very different shapes
            return True
        else:
            return False
    except (ValueError, IndexError) as e:
        print("Error in is_asteroid: {}".format(e))
        return False


def is_variable_star(pre_el_text, star_type):
    try:
        soup = BeautifulSoup(pre_el_text, 'html.parser')
        text_content = soup.get_text()

        if 'The object was found in {}'.format(star_type) in text_content:
            lines = text_content.split('\n')

            for idx, line in enumerate(lines):
                if star_type in line:
                    vs_idx = idx + 1
                    break

            vs_arcsec = int(lines[vs_idx].split()[0].replace('"', ''))
            return vs_arcsec <= VAR_MAX_DIST_ARCSEC
        else:
            return False
    except (ValueError, IndexError) as e:
        print("Error in is_variable_star: {}".format(e))
        return False


def is_in_neverexclude_list(pre_el_text):
    try:
        marker = 'This object is listed in neverexclude_list.txt'
        for line in pre_el_text.split('\n'):
            has_galactic = 'galactic' in line
            has_second_epoch = 'Second-epoch detections are separated by' in line
            has_marker = marker in line
            if has_galactic and has_second_epoch and has_marker:
                return True
        return False
    except (ValueError, IndexError) as e:
        print("Error in is_in_neverexclude_list: {}".format(e))
        return False


def is_ast_or_vs(pre_el_text):
    return (
        is_asteroid(pre_el_text) or is_variable_star(pre_el_text, "VSX") or is_variable_star(pre_el_text, "ASASSN-V")
    )


# CSS to inject before </HEAD>
FILTER_CSS_TEMPLATE = """
<style>
.transient-asteroid {{ display: none; }}
.transient-varstar {{ display: none; }}

#btn-asteroids {{
    left: calc(max(420px, 85vw));
    top: calc(5vh + 120px);
}}

#btn-varstars {{
    left: calc(max(420px, 85vw));
    top: calc(5vh + 180px);
}}
</style>

<script>
var asteroidsVisible = localStorage.getItem('filterAsteroids') === 'visible';
var varstarsVisible = localStorage.getItem('filterVarStars') === 'visible';

function applyFilterState() {{
    var astDivs = document.querySelectorAll('.transient-asteroid');
    var vsDivs = document.querySelectorAll('.transient-varstar');
    var astBtn = document.getElementById('btn-asteroids');
    var vsBtn = document.getElementById('btn-varstars');

    for (var i = 0; i < astDivs.length; i++) {{
        astDivs[i].style.display = asteroidsVisible ? 'block' : 'none';
    }}
    astBtn.textContent = (asteroidsVisible ? 'Hide' : 'Show') + ' Asteroids ({asteroid_count})';
    if (asteroidsVisible) astBtn.classList.add('active'); else astBtn.classList.remove('active');

    for (var i = 0; i < vsDivs.length; i++) {{
        vsDivs[i].style.display = varstarsVisible ? 'block' : 'none';
    }}
    vsBtn.textContent = (varstarsVisible ? 'Hide' : 'Show') + ' Variable Stars ({varstar_count})';
    if (varstarsVisible) vsBtn.classList.add('active'); else vsBtn.classList.remove('active');
}}

function toggleAsteroids() {{
    asteroidsVisible = !asteroidsVisible;
    localStorage.setItem('filterAsteroids', asteroidsVisible ? 'visible' : 'hidden');
    applyFilterState();
}}

function toggleVarStars() {{
    varstarsVisible = !varstarsVisible;
    localStorage.setItem('filterVarStars', varstarsVisible ? 'visible' : 'hidden');
    applyFilterState();
}}

document.addEventListener('DOMContentLoaded', applyFilterState);
</script>
"""

# Buttons and message to inject after <BODY> (before transient content)
FILTER_BODY_TEMPLATE = """
<button id="btn-asteroids" class="floating-btn" onclick="toggleAsteroids()">Show Asteroids ({asteroid_count})</button>
<button id="btn-varstars" class="floating-btn" onclick="toggleVarStars()">Show Variable Stars ({varstar_count})</button>

{message}
"""


# ---------------------------------------------------------------------------
# JSON-emitting helpers
# ---------------------------------------------------------------------------


def _now_utc_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _url_of_data_processing_root():
    return os.environ.get('URL_OF_DATA_PROCESSING_ROOT') or DEFAULT_URL_OF_DATA_PROCESSING_ROOT


def _resolve_get_image_date_binary():
    # Match the precedence autoprocess.sh uses for VAST_REFERENCE_COPY.
    candidates = []
    vrc = os.environ.get('VAST_REFERENCE_COPY')
    if vrc:
        candidates.append(os.path.join(vrc, 'util', 'get_image_date'))
    dpr = os.environ.get('DATA_PROCESSING_ROOT')
    if dpr:
        candidates.append(os.path.join(dpr, 'vast', 'util', 'get_image_date'))
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # Last resort: PATH lookup.
    for path_dir in os.environ.get('PATH', '').split(os.pathsep):
        path = os.path.join(path_dir, 'get_image_date')
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


_GET_IMAGE_DATE_BIN = None
_GET_IMAGE_DATE_BIN_RESOLVED = False
_GET_IMAGE_DATE_FALLBACK_USED = False
_JD_TO_ISO_CACHE = {}


def _jd_to_iso_utc(jd):
    """Convert a JD (UTC) to an ISO 8601 string. Uses get_image_date when
    available, falls back to Python datetime arithmetic. Memoized.
    Sets a module-level flag the caller can read to emit a single top-level
    warning rather than spamming per-candidate lists."""
    global _GET_IMAGE_DATE_BIN, _GET_IMAGE_DATE_BIN_RESOLVED
    global _GET_IMAGE_DATE_FALLBACK_USED
    if jd is None:
        return None
    if jd in _JD_TO_ISO_CACHE:
        return _JD_TO_ISO_CACHE[jd]
    iso = None
    if not _GET_IMAGE_DATE_BIN_RESOLVED:
        _GET_IMAGE_DATE_BIN = _resolve_get_image_date_binary()
        _GET_IMAGE_DATE_BIN_RESOLVED = True
    if _GET_IMAGE_DATE_BIN is not None:
        try:
            result = subprocess.run(
                [_GET_IMAGE_DATE_BIN, '{:.8f}'.format(jd)],
                capture_output=True, text=True, timeout=10, check=False)
            for line in result.stdout.splitlines():
                # Looking for: " (mid. exp) 2026-05-09T23:04:14.000"
                m = re.search(r'\(mid\. exp\)\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', line)
                if m:
                    iso = m.group(1) + 'Z'
                    break
        except (OSError, subprocess.SubprocessError):
            pass
    if iso is None:
        # Python fallback. JD 2440587.5 == 1970-01-01 00:00 UTC.
        _GET_IMAGE_DATE_FALLBACK_USED = True
        try:
            unix_seconds = (jd - 2440587.5) * 86400.0
            dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=unix_seconds)
            iso = dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        except (TypeError, ValueError):
            iso = None
    _JD_TO_ISO_CACHE[jd] = iso
    return iso


def _absolutize_url(rel_or_abs, base_url):
    if rel_or_abs is None:
        return None
    s = rel_or_abs.strip()
    if not s:
        return None
    if s.startswith('http://') or s.startswith('https://'):
        return s
    if s.startswith('./'):
        s = s[2:]
    if s.startswith('/'):
        s = s.lstrip('/')
    return base_url.rstrip('/') + '/' + s


def _hms_to_deg(hms):
    # "HH:MM:SS.SS" -> degrees, 0..360
    try:
        h, m, s = hms.split(':')
        return (int(h) + int(m) / 60.0 + float(s) / 3600.0) * 15.0
    except (ValueError, AttributeError):
        return None


def _dms_to_deg(dms):
    # "+DD:MM:SS.S" or "-DD:MM:SS.S" -> degrees, -90..90
    try:
        sign_char = dms[0] if dms[0] in '+-' else '+'
        body = dms[1:] if dms[0] in '+-' else dms
        d, m, s = body.split(':')
        magnitude = int(d) + int(m) / 60.0 + float(s) / 3600.0
        return -magnitude if sign_char == '-' else magnitude
    except (ValueError, AttributeError, IndexError):
        return None


def _parse_session_from_filename(path):
    base = os.path.basename(path)
    m = re.match(r'^(\d{8})_(morning|evening)_(\S+?)(?:_filtered)?\.html$', base, re.IGNORECASE)
    if not m:
        return {"date_utc": None, "session": None, "camera": None}
    yyyymmdd, session, camera = m.group(1), m.group(2).lower(), m.group(3)
    date_iso = '{}-{}-{}'.format(yyyymmdd[0:4], yyyymmdd[4:6], yyyymmdd[6:8])
    return {"date_utc": date_iso, "session": session, "camera": camera}


def _extract_field_name(transient_soup, candidate_id):
    # Preferred source: the "FIELD field processing log" link.
    link = transient_soup.find('a', class_='field-processing-log-link')
    if link is not None:
        text = link.get_text(strip=True)
        m = re.match(r'^(.+?)\s+field processing log$', text)
        if m:
            return m.group(1).strip()
    # Fallback: parse from the candidate id, e.g.
    # "20369_Cyg5_2026-..."         -> "Cyg5"
    # "03771_fd_Oph-05-Q1b1x1_2026-..." -> "Oph-05-Q1b1x1"
    m = re.match(r'^\d+_(?:fd_)?(.+?)_\d{4}-', candidate_id)
    if m:
        return m.group(1)
    return None


def _extract_mean_info(pre_text, warnings):
    # The "Mean magnitude and position" line:
    #     2026 05 12.1997  2461172.6997  10.86  17:12:51.43 -08:58:24.1
    # Followed by:
    #     12.93943  17.18504 galactic  Oph  Second-epoch detections are separated by ...
    out = {
        "date_utc_iso": None,
        "date_utc_dayfraction": None,
        "jd_utc": None,
        "mag": None,
        "ra_hms": None,
        "dec_dms": None,
        "ra_deg": None,
        "dec_deg": None,
        "galactic_l_deg": None,
        "galactic_b_deg": None,
        "constellation": None,
    }
    line_re = re.compile(
        r'^\s*(\d{4}\s+\d{1,2}\s+\d{1,2}\.\d+)\s+'   # dayfraction
        r'(\d+\.\d+)\s+'                              # JD
        r'(-?\d+\.\d+)\s+'                            # mag
        r'(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)\s+'         # RA hms
        r'([+\-]\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)',      # Dec dms
        re.MULTILINE)
    found = False
    for line_iter in pre_text.splitlines():
        m = line_re.match(line_iter)
        if m:
            out["date_utc_dayfraction"] = m.group(1)
            try:
                out["jd_utc"] = float(m.group(2))
            except ValueError:
                pass
            try:
                out["mag"] = float(m.group(3))
            except ValueError:
                pass
            out["ra_hms"] = m.group(4)
            out["dec_dms"] = m.group(5)
            out["ra_deg"] = _hms_to_deg(m.group(4))
            out["dec_deg"] = _dms_to_deg(m.group(5))
            found = True
            break
    if not found:
        warnings.append("mean: regex no match")
        return out
    out["date_utc_iso"] = _jd_to_iso_utc(out["jd_utc"])
    # Galactic + constellation line.
    gal_re = re.compile(
        r'^\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+galactic\s+([A-Za-z]{3})',
        re.MULTILINE)
    gm = gal_re.search(pre_text)
    if gm:
        try:
            out["galactic_l_deg"] = float(gm.group(1))
            out["galactic_b_deg"] = float(gm.group(2))
        except ValueError:
            pass
        out["constellation"] = gm.group(3)
    else:
        warnings.append("mean: galactic/constellation line not found")
    return out


def _extract_separation(pre_text, warnings):
    # Producer wraps the numbers in <font> tags, so match against the
    # tag-stripped text rather than the raw HTML.
    plain = BeautifulSoup(pre_text, 'lxml').get_text()
    m = re.search(
        r'Second-epoch detections are separated by\s+'
        r'(-?\d+\.?\d*)"\s+and\s+(-?\d+\.?\d*)\s*pix',
        plain)
    if not m:
        warnings.append("second_epoch_separation: regex no match")
        return None
    try:
        return {"arcsec": float(m.group(1)), "pix": float(m.group(2))}
    except ValueError:
        warnings.append("second_epoch_separation: number parse failed")
        return None


def _extract_cutouts(transient_soup, base_url, candidate_id):
    out = {}
    # Cutout filenames have a stable suffix: _reference.png, _discovery1.png, etc.
    for img in transient_soup.find_all('img'):
        src = img.get('src')
        if not src:
            continue
        # Anchor on candidate_id to avoid grabbing fullframepreview images.
        if candidate_id not in src:
            continue
        if src.endswith('_reference.png'):
            out['reference'] = _absolutize_url(src, base_url)
        else:
            disc_match = re.search(r'_discovery(\d+)\.png$', src)
            if disc_match:
                out['discovery{}'.format(disc_match.group(1))] = _absolutize_url(src, base_url)
    return out or None


def _extract_discovery_table(transient_soup, base_url, warnings):
    table = transient_soup.find('table')
    if table is None:
        warnings.append("discovery_images: table not found")
        return None
    rows = table.find_all('tr')
    out = []
    for row in rows[1:]:  # skip header
        cells = [c.get_text(strip=True) for c in row.find_all('td')]
        if len(cells) < 7:
            continue
        label = cells[0].rstrip()
        # Strip the trailing "&nbsp;&nbsp;" decoration: BeautifulSoup already
        # collapsed those to their text form, so just normalize whitespace.
        date_frac = cells[1].strip()
        try:
            jd_val = float(cells[2].strip())
        except (ValueError, AttributeError):
            jd_val = None
        try:
            mag_val = float(cells[3].strip())
        except (ValueError, AttributeError):
            mag_val = None
        radec = cells[4].strip()
        ra_match = re.match(r'^(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)\s+([+\-]\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)', radec)
        ra_hms = ra_match.group(1) if ra_match else None
        dec_dms = ra_match.group(2) if ra_match else None
        xy = cells[5].strip().split()
        x_pix = None
        y_pix = None
        if len(xy) >= 2:
            try:
                x_pix = float(xy[0])
            except ValueError:
                pass
            try:
                y_pix = float(xy[1])
            except ValueError:
                pass
        fits_path = cells[6].strip()
        out.append({
            "label": label,
            "date_utc_iso": _jd_to_iso_utc(jd_val),
            "date_utc_dayfraction": date_frac,
            "jd_utc": jd_val,
            "mag": mag_val,
            "ra_hms": ra_hms,
            "dec_dms": dec_dms,
            "x_pix": x_pix,
            "y_pix": y_pix,
            "fits_path": fits_path,
            "fits_url": None,
        })
    if not out:
        warnings.append("discovery_images: no rows parsed")
        return None
    # Look for matching fits_url links in the fullframepreview_* div.
    fullframe = transient_soup.find('div', id=re.compile(r'^fullframepreview_'))
    if fullframe is not None:
        for entry in out:
            if not entry["fits_path"]:
                continue
            basename = os.path.basename(entry["fits_path"])
            link = fullframe.find('a', string=basename)
            if link is None:
                # Some links wrap inside extra whitespace or have additional text.
                for a in fullframe.find_all('a'):
                    if a.get_text(strip=True) == basename:
                        link = a
                        break
            if link is not None and link.get('href'):
                entry["fits_url"] = _absolutize_url(link.get('href'), base_url)
    return out


def _extract_exclusion_list_match(plain_text):
    """Locate the at-most-one "This object is listed in <file>" marker.
    Returns (list_filename, raw_details_string) or (None, None)."""
    m = re.search(r'This object is listed in (\S+\.txt)(.*)', plain_text)
    if not m:
        return None, None
    raw_tail = m.group(2).strip() or None
    return m.group(1), raw_tail


def _extract_crossmatches(pre_text, warnings):
    out = {}
    lookup = [
        ('vsx', 'VSX'),
        ('asassn_v', 'ASASSN-V'),
        ('astcheck', 'astcheck'),
    ]
    # Strip HTML inside the pre block so the lines match the rendered text.
    plain = BeautifulSoup(pre_text, 'lxml').get_text()
    lines = plain.split('\n')
    # Indices of "The object was ... in X" lines, so we can carve out raw payloads.
    sentinel_re = re.compile(r'The object was\s+(found|not found)\s+in\s+([A-Za-z0-9_\-]+)')
    sentinels = []
    for idx, line in enumerate(lines):
        m = sentinel_re.search(line)
        if m:
            sentinels.append((idx, m.group(1).lower(), m.group(2)))
    name_to_idx = {raw_name.lower().replace('-', '_'): (i, found, raw_name)
                   for (i, found, raw_name) in
                   ((s[0], s[1], s[2]) for s in sentinels)}
    for json_key, html_name in lookup:
        key = html_name.lower().replace('-', '_')
        if key not in name_to_idx:
            # A missing line typically means the producer skipped that
            # crossmatch step (e.g., ASASSN-V is not run once VSX matched).
            # That is normal -- omit the key, do not warn.
            continue
        idx, status, _raw_name = name_to_idx[key]
        entry = {"found": status == 'found'}
        if entry["found"]:
            # Capture lines after the sentinel up to (but not including) the
            # next sentinel or an obvious end marker.
            end = len(lines)
            for nxt_idx, _, _ in sentinels:
                if nxt_idx > idx:
                    end = nxt_idx
                    break
            # Stop earlier at known terminators that come after crossmatches.
            for j in range(idx + 1, end):
                stripped = lines[j].strip()
                if (stripped.startswith('Forced photometry') or
                        stripped.startswith('online_id') or
                        stripped.startswith('Check this position in') or
                        stripped.startswith('Online MPChecker')):
                    end = j
                    break
            raw = '\n'.join(lines[idx + 1:end]).rstrip()
            entry["raw"] = raw if raw else None
        out[json_key] = entry
    # Exclusion-list match -- TOCP / ASAS-SN-list / TNS / moons / planets /
    # spacecraft / neverexclude_list. At most one is emitted by VaST.
    list_file, list_raw = _extract_exclusion_list_match(plain)
    if list_file is not None:
        json_key = EXCLUSION_LIST_FILE_TO_JSON_KEY.get(
            list_file, os.path.splitext(list_file)[0])
        out[json_key] = {"found": True, "raw": list_raw}
    return out or None


def _extract_forced_photometry(pre_text, warnings):
    plain = BeautifulSoup(pre_text, 'lxml').get_text()
    per_image_re = re.compile(
        r'^Forced photometry on\s+(\S+)\s+at\s+'
        r'(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s+[+\-]\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)\s*:\s*'
        r'(-?\d+\.\d+)\s*\+/-\s*(-?\d+\.\d+)\s+(\S+)\s*$',
        re.MULTILINE)
    avg_re = re.compile(
        r'^Forced photometry reference-image weighted average:\s*'
        r'(-?\d+\.\d+)\s*\+/-\s*(-?\d+\.\d+)\s*$',
        re.MULTILINE)
    per_image = []
    for m in per_image_re.finditer(plain):
        try:
            mag_val = float(m.group(3))
            err_val = float(m.group(4))
        except ValueError:
            mag_val = None
            err_val = None
        per_image.append({
            "wcs_fits": m.group(1),
            "position_hms": m.group(2),
            "mag": mag_val,
            "mag_err": err_val,
            "flag": m.group(5),
        })
    avg = None
    am = avg_re.search(plain)
    if am:
        try:
            avg = {"mag": float(am.group(1)), "mag_err": float(am.group(2))}
        except ValueError:
            avg = None
    if not per_image and not avg:
        # Some candidates legitimately have no forced photometry block;
        # leaving forced_photometry null already conveys that. Only warn
        # if the section is partially present (heuristic: the literal label
        # appears in the text but no row parsed).
        if 'Forced photometry on' in plain:
            warnings.append("forced_photometry: regex no match")
        return None
    return {
        "per_image": per_image or None,
        "reference_weighted_avg": avg,
    }


_EXTERNAL_LINK_RULES = [
    ('tns', 'wis-tns.org'),
    ('asassn_list', 'astronomy.ohio-state.edu/asassn'),
    ('simbad', 'simbad.u-strasbg.fr'),
    ('vizier', 'vizier.u-strasbg.fr'),
    ('wise', 'irsa.ipac.caltech.edu'),
    ('aladin_lite', 'aladin.u-strasbg.fr'),
    ('snad_ztf', 'ztf.snad.space'),
]


def _extract_external_links(transient_soup, base_url):
    out = {}
    for a in transient_soup.find_all('a', href=True):
        href = a['href']
        for key, needle in _EXTERNAL_LINK_RULES:
            if key in out:
                continue
            if needle in href:
                out[key] = _absolutize_url(href, base_url)
                break
    # ASAS-3 form (action attribute).
    asas_form = transient_soup.find('form', action=re.compile(r'astrouw\.edu\.pl'))
    if asas_form is not None and 'asas3' not in out:
        action = asas_form.get('action')
        if action:
            out['asas3'] = _absolutize_url(action, base_url)
    # NMW sky_archive form -- build a GET URL using the embedded hidden values.
    nmw_form = transient_soup.find('form', action=re.compile(r'sky_archive'))
    if nmw_form is not None:
        action = nmw_form.get('action')
        params = {}
        for inp in nmw_form.find_all('input'):
            n = inp.get('name')
            v = inp.get('value')
            if n and v is not None:
                params[n] = v
        if action and ('ra' in params and 'dec' in params):
            from urllib.parse import quote
            qs = '&'.join('{}={}'.format(k, quote(str(v), safe='')) for k, v in params.items() if k != 'submit')
            out['nmw_archive'] = '{}?{}'.format(action, qs)
    return out or None


def _extract_report_stubs(transient_soup, candidate_id, warnings):
    out = {
        "mpc_mean": None,
        "mpc_per_image": None,
        "tocp": None,
        "aavso": None,
        "vsnet": None,
    }
    # MPC: inside <div id="mpcstub_<id>"> there are at least two <pre> blocks
    # (mean position, then per-image positions).
    mpc_div = transient_soup.find('div', id='mpcstub_{}'.format(candidate_id))
    if mpc_div is not None:
        pres = mpc_div.find_all('pre')
        if pres:
            out["mpc_mean"] = pres[0].get_text().strip() or None
        if len(pres) >= 2:
            text = pres[1].get_text().strip()
            lines = [ln for ln in text.splitlines() if ln.strip()]
            out["mpc_per_image"] = lines or None
    else:
        warnings.append("report_stubs: mpc div not found")
    tocp_div = transient_soup.find('div', id='tocpstub_{}'.format(candidate_id))
    if tocp_div is not None:
        pre = tocp_div.find('pre')
        if pre is not None:
            out["tocp"] = pre.get_text().strip() or None
    else:
        warnings.append("report_stubs: tocp div not found")
    var_div = transient_soup.find('div', id='varstarstub_{}'.format(candidate_id))
    if var_div is not None:
        pre = var_div.find('pre')
        if pre is not None:
            blob = pre.get_text()
            # Split into AAVSO and VSNET sections. The producer marks them
            # with " **** AAVSO file format ****" and " **** VSNET file format ****".
            m = re.search(r'\*\*\*\*\s*VSNET file format\s*\*\*\*\*', blob)
            if m:
                out["aavso"] = blob[:m.start()].strip() or None
                out["vsnet"] = blob[m.end():].strip() or None
            else:
                out["aavso"] = blob.strip() or None
    else:
        warnings.append("report_stubs: varstarstub div not found")
    return out


def _build_candidate(transient_html, candidate_id, classification, base_url):
    warnings = []
    transient_soup = BeautifulSoup(transient_html, features="lxml")
    pre_el = transient_soup.find('pre')
    pre_text = str(pre_el) if pre_el is not None else ''
    field = _extract_field_name(transient_soup, candidate_id)
    if field is None:
        warnings.append("field: could not derive from link or id")
    mean = _extract_mean_info(pre_text, warnings) if pre_text else None
    separation = _extract_separation(pre_text, warnings) if pre_text else None
    cutouts = _extract_cutouts(transient_soup, base_url, candidate_id)
    discovery_images = _extract_discovery_table(transient_soup, base_url, warnings)
    crossmatches = _extract_crossmatches(pre_text, warnings) if pre_text else None
    forced_phot = _extract_forced_photometry(pre_text, warnings) if pre_text else None
    external_links = _extract_external_links(transient_soup, base_url)
    report_stubs = _extract_report_stubs(transient_soup, candidate_id, warnings)
    return {
        "id": candidate_id,
        "field": field,
        "classification": classification,
        "mean": mean,
        "second_epoch_separation": separation,
        "cutouts": cutouts,
        "discovery_images": discovery_images,
        "crossmatches": crossmatches,
        "forced_photometry": forced_phot,
        "external_links": external_links,
        "report_stubs": report_stubs,
        "parse_warnings": warnings,
    }


def _extract_candidate_id(transient_html):
    m = re.search(r"<a name=['\"]([^'\"]+)['\"]\s*>", transient_html)
    return m.group(1) if m else None


def _write_json(path, payload):
    try:
        with open(path, 'w') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=False)
            f.write('\n')
    except OSError as e:
        print("Error writing JSON {}: {}".format(path, e))


def filter_report(path_to_report):
    output_html_path = splitext(path_to_report)[0] + '_filtered.html'
    output_json_path = splitext(path_to_report)[0] + '_filtered.json'
    session_meta = _parse_session_from_filename(path_to_report)
    base_url = _url_of_data_processing_root()
    source_report_name = os.path.basename(path_to_report)
    try:
        with open(path_to_report, 'r') as f:
            content = f.read()

        a_name_first_occurance = content.find('<a name')

        if a_name_first_occurance == -1:
            print('No transients to filter in {}'.format(path_to_report))
            # Still emit a JSON skeleton so consumers can detect the zero-candidate case.
            _write_json(output_json_path, {
                "schema_version": JSON_SCHEMA_VERSION,
                "generated_at_utc": _now_utc_iso(),
                "source_report": source_report_name,
                "url_of_data_processing_root": base_url,
                "session": session_meta,
                "totals": {"total": 0, "new": 0, "known_asteroid": 0, "known_variable": 0, "known_transient": 0},
                "candidates": [],
                "parse_warnings": [],
            })
            return

        head = content[: a_name_first_occurance]
        transients = content[a_name_first_occurance:].split('<HR>')[:-1]

        asteroid_count = 0
        varstar_count = 0
        known_transient_count = 0
        unknown_count = 0
        wrapped = []
        candidates_json = []

        for transient in transients:
            soup = BeautifulSoup(transient, features="lxml")
            pre_text = soup.pre.text

            is_vsx = is_variable_star(pre_text, "VSX")
            is_asassn = is_variable_star(pre_text, "ASASSN-V")
            is_known_varstar = is_vsx or is_asassn
            keep_visible = is_in_neverexclude_list(pre_text)
            exclusion_list_file, _ = _extract_exclusion_list_match(pre_text)
            is_known_transient = exclusion_list_file in KNOWN_TRANSIENT_LIST_FILES

            if is_asteroid(pre_text):
                css_class = "transient-asteroid"
                classification = "known_asteroid"
                asteroid_count += 1
            elif is_known_varstar and not keep_visible:
                css_class = "transient-varstar"
                classification = "known_variable"
                varstar_count += 1
            elif is_known_transient:
                # Known-transient candidates stay visible in the filtered HTML;
                # the JSON consumer can distinguish them via classification.
                css_class = "transient-unknown"
                classification = "known_transient"
                known_transient_count += 1
            else:
                css_class = "transient-unknown"
                classification = "new"
                unknown_count += 1

            wrapped.append('<div class="{}">\n{}\n<HR></div>'.format(
                css_class, transient))

            candidate_id = _extract_candidate_id(transient)
            if candidate_id is not None:
                candidates_json.append(
                    _build_candidate(transient, candidate_id, classification, base_url))

        total = asteroid_count + varstar_count + known_transient_count + unknown_count
        if unknown_count == 0:
            message = ('<p>All {} candidates are known objects'
                       ' ({} asteroids, {} variable stars).'
                       ' Use the buttons to show them.</p>').format(
                           total, asteroid_count, varstar_count)
        else:
            message = ''

        filter_css = FILTER_CSS_TEMPLATE.format(
            asteroid_count=asteroid_count,
            varstar_count=varstar_count,
        )
        filter_body = FILTER_BODY_TEMPLATE.format(
            asteroid_count=asteroid_count,
            varstar_count=varstar_count,
            message=message,
        )

        # Inject CSS+JS before </HEAD> (case-insensitive) and buttons after
        # the head content (which ends right before the first <a name)
        head_with_css = re.sub(
            r'(</HEAD>)', filter_css + r'\1', head, count=1, flags=re.IGNORECASE)

        output = head_with_css + filter_body + '\n'.join(wrapped) + '\n</body></html>'

        with open(output_html_path, 'w') as f:
            f.write(output)

        top_warnings = []
        if _GET_IMAGE_DATE_FALLBACK_USED:
            top_warnings.append(
                'date: VAST get_image_date binary not found or unusable, '
                'used Python datetime fallback for ISO 8601 conversion')
        _write_json(output_json_path, {
            "schema_version": JSON_SCHEMA_VERSION,
            "generated_at_utc": _now_utc_iso(),
            "source_report": source_report_name,
            "url_of_data_processing_root": base_url,
            "session": session_meta,
            "totals": {
                "total": total,
                "new": unknown_count,
                "known_asteroid": asteroid_count,
                "known_variable": varstar_count,
                "known_transient": known_transient_count,
            },
            "candidates": candidates_json,
            "parse_warnings": top_warnings,
        })
    except Exception as e:
        print("Error in filter_report: {}".format(e))

        try:
            error_msg = ('<html><body>An error occurred while filtering the `{}` '
                         'file.</body></html>'.format(sys.argv[1]))
            with open(output_html_path, 'w') as f:
                f.write(error_msg)
        except Exception as e:
            print('An error occurred while writing the error message: {}'.format(e))
            exit(1)
        # Also emit a JSON sibling carrying the error so downstream tools
        # can detect the failure without having to parse the HTML.
        try:
            _write_json(output_json_path, {
                "schema_version": JSON_SCHEMA_VERSION,
                "generated_at_utc": _now_utc_iso(),
                "source_report": source_report_name,
                "url_of_data_processing_root": base_url,
                "session": session_meta,
                "error": str(e),
                "candidates": [],
                "parse_warnings": [],
            })
        except Exception as e2:
            print('An error occurred while writing the error JSON: {}'.format(e2))


if __name__ == '__main__':
    if len(sys.argv) == 1 or len(sys.argv) > 2 or sys.argv[1] in ['-h', '--help']:
        print('Usage: `python3 filter_report.py path/to/report.html`')
        exit(1)

    filter_report(sys.argv[1])

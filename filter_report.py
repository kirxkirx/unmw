#!/usr/bin/env python3

import re
import sys
from os.path import splitext

from bs4 import BeautifulSoup

# CONSTANTS
# MAX_MAG = 40
# AST_MAG_DIF_PREDICTED_OBSERVED = 2
# AST_1_RA_DIST_PREDICTED_OBSERVED_ARCSEC = 180
# AST_2_RA_DIST_PREDICTED_OBSERVED_ARCSEC = 180
VAR_MAX_DIST_ARCSEC = 30


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


def filter_report(path_to_report):
    try:
        with open(path_to_report, 'r') as f:
            content = f.read()

        a_name_first_occurance = content.find('<a name')

        if a_name_first_occurance == -1:
            print('No transients to filter in {}'.format(path_to_report))
            return

        head = content[: a_name_first_occurance]
        transients = content[a_name_first_occurance:].split('<HR>')[:-1]

        asteroid_count = 0
        varstar_count = 0
        unknown_count = 0
        wrapped = []

        for transient in transients:
            soup = BeautifulSoup(transient, features="lxml")
            pre_text = soup.pre.text

            if is_asteroid(pre_text):
                css_class = "transient-asteroid"
                asteroid_count += 1
            elif is_variable_star(pre_text, "VSX") or is_variable_star(pre_text, "ASASSN-V"):
                css_class = "transient-varstar"
                varstar_count += 1
            else:
                css_class = "transient-unknown"
                unknown_count += 1

            wrapped.append('<div class="{}">\n{}\n<HR></div>'.format(
                css_class, transient))

        total = asteroid_count + varstar_count + unknown_count
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

        output_path = splitext(path_to_report)[0] + '_filtered.html'
        with open(output_path, 'w') as f:
            f.write(output)
    except Exception as e:
        print("Error in filter_report: {}".format(e))

        try:
            error_msg = ('<html><body>An error occurred while filtering the `{}` '
                         'file.</body></html>'.format(sys.argv[1]))
            output_path = splitext(sys.argv[1])[0] + '_filtered.html'
            with open(output_path, 'w') as f:
                f.write(error_msg)
        except Exception as e:
            print('An error occurred while writing the error message: {}'.format(e))
            exit(1)


if __name__ == '__main__':
    if len(sys.argv) == 1 or len(sys.argv) > 2 or sys.argv[1] in ['-h', '--help']:
        print('Usage: `python3 filter_report.py path/to/report.html`')
        exit(1)

    filter_report(sys.argv[1])

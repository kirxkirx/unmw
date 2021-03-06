#!/usr/bin/env python3

import sys
from os.path import splitext

from bs4 import BeautifulSoup

# CONSTANTS
MAX_MAG = 40
AST_MAG_DIF_PREDICTED_OBSERVED = 1
AST_1_RA_DIST_PREDICTED_OBSERVED_ARCSEC = 10
AST_2_RA_DIST_PREDICTED_OBSERVED_ARCSEC = 10
VAR_MAX_DIST_ARCSEC = 10


def is_asteroid(pre_el_text):
    if 'The object was found in astcheck' in pre_el_text:
        pre_text_split = pre_el_text.split('\n')

        obj_mag = float(pre_text_split[2].strip().split()[4])
        assert obj_mag < MAX_MAG

        for idx, el in enumerate(pre_text_split):
            if 'astcheck' in el:
                ast_idx = idx - 1
                break

        ast_split = pre_text_split[ast_idx].strip().split()
        ast_1 = float(ast_split[3])
        ast_2 = float(ast_split[4])
        ast_mag = float(ast_split[5])
        assert ast_mag < MAX_MAG

        return (abs(obj_mag - ast_mag) <= AST_MAG_DIF_PREDICTED_OBSERVED and
                ast_1 <= AST_1_RA_DIST_PREDICTED_OBSERVED_ARCSEC and
                ast_2 <= AST_2_RA_DIST_PREDICTED_OBSERVED_ARCSEC )
    else:
        return False


def is_variable_star_vsx(pre_el_text):
    if 'The object was found in VSX' in pre_el_text:
        pre_text_split = pre_el_text.split('\n')

        for idx, el in enumerate(pre_text_split):
            if 'VSX' in el:
                vs_idx = idx + 1
                break

        vs_arcsec = int(pre_text_split[vs_idx].strip().split()[0][:-1])
        return vs_arcsec <= VAR_MAX_DIST_ARCSEC
    else:
        return False

def is_variable_star_asassn(pre_el_text):
    if 'The object was found in ASASSN-V' in pre_el_text:
        pre_text_split = pre_el_text.split('\n')

        for idx, el in enumerate(pre_text_split):
            if 'ASASSN-V' in el:
                vs_idx = idx + 1
                break

        vs_arcsec = int(pre_text_split[vs_idx].strip().split()[0][:-1])
        return vs_arcsec <= VAR_MAX_DIST_ARCSEC
    else:
        return False


def is_ast_or_vs(pre_el_text):
    return is_asteroid(pre_el_text) or is_variable_star_vsx(pre_el_text) or is_variable_star_asassn(pre_el_text)


def filter_report(path_to_report):
    with open(path_to_report, 'r') as f:
        content = f.read()

    a_name_first_occurance = content.find('<a name')

    if a_name_first_occurance == -1:
        print('No transients to filter in ' + path_to_report)
        return
    
    head = content[: a_name_first_occurance]
    #transients = content[a_name_first_occurance:].split('<hr>')[:-1]
    transients = content[a_name_first_occurance:].split('<HR>')[:-1]

    ast_or_vs_s = []
    for transient in transients:
        ast_or_vs_s.append(is_ast_or_vs(BeautifulSoup(transient, features="lxml").pre.text))
        #print(transient)

    not_ast_and_not_vs = []
    for transient, ast_or_vs_ in zip(transients, ast_or_vs_s):
        if not ast_or_vs_:
            not_ast_and_not_vs.append(transient)

    if len(not_ast_and_not_vs) == 0:
        output = head + '\nSeems like every transient is the known object.\n</body></html>'
    else:
        #output = head + '<hr>'.join(not_ast_and_not_vs) + '\n<hr></body></html>'
        output = head + '<HR>'.join(not_ast_and_not_vs) + '\n<HR></body></html>'

    with open(splitext(path_to_report)[0] + '_filtered.html', 'w') as f:
        f.write(output)


if __name__ == '__main__':
    if len(sys.argv) == 1 or len(sys.argv) > 2 or sys.argv[1] in ['-h', '--help']:
        print('Usage: `python3 filter_report.py path/to/report.html`')
        exit(1)
    try:
        filter_report(sys.argv[1])
    except:
        try:
            content = '<html><body>An error occured while filtering the `' + \
                sys.argv[1] + '` file.</body></html>'
            with open(splitext(sys.argv[1])[0] + '_filtered.html', 'w') as f:
                f.write(content)
        except:
            print('An error occured while filtering the `' + sys.argv[1] + '` file.')
            exit(1)

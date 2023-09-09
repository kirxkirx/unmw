#!/usr/bin/env python3

import sys
from os.path import splitext

from bs4 import BeautifulSoup

# CONSTANTS
MAX_MAG = 40
AST_MAG_DIF_PREDICTED_OBSERVED = 2
AST_1_RA_DIST_PREDICTED_OBSERVED_ARCSEC = 180
AST_2_RA_DIST_PREDICTED_OBSERVED_ARCSEC = 180
VAR_MAX_DIST_ARCSEC = 30


def is_asteroid(pre_el_text):
    try:
        if 'The object was found in astcheck' in pre_el_text:
            pre_text_split = pre_el_text.split('\n')
    
            obj_mag = float(pre_text_split[2].strip().split()[4])
            assert obj_mag < MAX_MAG, "Object magnitude exceeds the maximum limit."
    
            for idx, el in enumerate(pre_text_split):
                if 'astcheck' in el:
                    ast_idx = idx + 1
                    break
    
            ast_split = pre_text_split[ast_idx].strip().split()
            ast_1 = float(ast_split[3])
            ast_2 = float(ast_split[4])
            ast_mag = float(ast_split[5])
            assert ast_mag < MAX_MAG, "Asteroid magnitude exceeds the maximum limit."
    
            return (abs(obj_mag - ast_mag) <= AST_MAG_DIF_PREDICTED_OBSERVED and
                    ast_1 <= AST_1_RA_DIST_PREDICTED_OBSERVED_ARCSEC and
                    ast_2 <= AST_2_RA_DIST_PREDICTED_OBSERVED_ARCSEC )
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
    return is_asteroid(pre_el_text) or is_variable_star(pre_el_text, "VSX") or is_variable_star(pre_el_text, "ASASSN-V")


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
    
        ast_or_vs_s = []
        for transient in transients:
            ast_or_vs_s.append(is_ast_or_vs(BeautifulSoup(transient, features="lxml").pre.text))
    
        not_ast_and_not_vs = []
        for transient, ast_or_vs_ in zip(transients, ast_or_vs_s):
            if not ast_or_vs_:
                not_ast_and_not_vs.append(transient)
    
        if len(not_ast_and_not_vs) == 0:
            output = head + '\nSeems like every transient is the known object.\n</body></html>'
        else:
            output = head + '<HR>'.join(not_ast_and_not_vs) + '\n<HR></body></html>'
    
        with open(splitext(path_to_report)[0] + '_filtered.html', 'w') as f:
            f.write(output)
    except Exception as e:
        print("Error in filter_report: {}".format(e))

        try:
            content = '<html><body>An error occurred while filtering the `{}` file.</body></html>'.format(sys.argv[1])
            with open(splitext(sys.argv[1])[0] + '_filtered.html', 'w') as f:
                f.write(content)
        except Exception as e:
            print('An error occurred while writing the error message to the filtered report: {}'.format(e))
            exit(1)


if __name__ == '__main__':
    if len(sys.argv) == 1 or len(sys.argv) > 2 or sys.argv[1] in ['-h', '--help']:
        print('Usage: `python3 filter_report.py path/to/report.html`')
        exit(1)
    
    filter_report(sys.argv[1])

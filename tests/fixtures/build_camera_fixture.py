#!/usr/bin/env python3
"""Build small, reproducible FITS cutouts from the operator's real camera RAR.

No network, processing jobs, photometry submission, or production writes.
The pixels are copied, not synthesized. These are ingestion/header fixtures,
not calibrated scientific reference images. See README.md in this directory.
"""
import argparse
import os
import sys

if os.environ.get('GATEWAY_INTERFACE') or os.environ.get('REQUEST_METHOD'):
    sys.exit('ERROR: not a CGI')

import gzip
import hashlib
from pathlib import Path
import subprocess


MEMBERS = ('Vul8_2025-1-7_15-30-31_001.fts', 'Vul8_2025-1-7_15-31-1_002.fts')


def cutout(data, side=1024):
    cards = []
    for offset in range(0, len(data), 80):
        card = data[offset:offset + 80].decode('ascii')
        cards.append(card)
        if card[:8].strip() == 'END':
            break
    else:
        raise ValueError('Missing FITS END card')
    values = {c[:8].strip(): c[10:].split('/')[0].strip() for c in cards if c[8:10] == '= '}
    if values['NAXIS'] != '2' or int(values['BITPIX']) not in (16, 32, -32, -64):
        raise ValueError('Expected an ordinary two-dimensional FITS image')
    width, height = int(values['NAXIS1']), int(values['NAXIS2'])
    if min(width, height) < side:
        raise ValueError('Image is smaller than the requested cutout')
    start = ((len(cards) * 80 + 2879) // 2880) * 2880
    pixel_bytes = abs(int(values['BITPIX'])) // 8
    left, top = (width - side) // 2, (height - side) // 2
    output_cards = []
    for card in cards[:-1]:
        key = card[:8].strip()
        if key in ('CHECKSUM', 'DATASUM'):
            continue
        if key in ('NAXIS1', 'NAXIS2'):
            card = '{}= {:20d} / real-image test cutout'.format(key.ljust(8), side).ljust(80)
        elif key in ('CRPIX1', 'CRPIX2'):
            value = float(values[key]) - (left if key == 'CRPIX1' else top)
            card = '{}= {:20.8f}'.format(key.ljust(8), value).ljust(80)
        output_cards.append(card)
    output_cards.extend(['HISTORY Real camera pixels cropped for isolated UNMW CI; not for submission.'.ljust(80),
                         'END'.ljust(80)])
    header = ''.join(output_cards).encode('ascii')
    header += b' ' * (-len(header) % 2880)
    pixels = b''.join(data[start + ((top + row) * width + left) * pixel_bytes:
                           start + ((top + row) * width + left + side) * pixel_bytes]
                      for row in range(side))
    if len(pixels) != side * side * pixel_bytes:
        raise ValueError('Truncated FITS data')
    return header + pixels + b'\0' * (-len(pixels) % 2880)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    print('Source SHA256:', hashlib.sha256(args.archive.read_bytes()).hexdigest())
    for name in MEMBERS:
        result = subprocess.run(['rar', 'p', '-cfg-', '-c-', '-p-', '-idq', '--',
                                 str(args.archive.resolve()), name],
                                check=True, capture_output=True, timeout=60)
        output = args.output / (name + '.gz')
        if output.exists():
            raise SystemExit('Refusing to overwrite ' + str(output))
        # Explicit mtime and filename make the gzip fixture reproducible.
        with output.open('wb') as stream:
            with gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0) as zipped:
                zipped.write(cutout(result.stdout))
        print(hashlib.sha256(output.read_bytes()).hexdigest(), output.name)


if __name__ == '__main__':
    main()

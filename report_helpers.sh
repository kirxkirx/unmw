#!/usr/bin/env bash
# Pure report helpers, shared by the report generator and its regression tests.
if [ -n "${GATEWAY_INTERFACE:-}" ] || [ -n "${REQUEST_METHOD:-}" ]; then
 printf 'Content-Type: text/plain\n\nERROR: this script must not be run as CGI\n'
 exit 1
fi

nmw_second_epoch_fwhm() {
 local report="$1" first second
 first=$(grep 'SECOND_EPOCH__FIRST_IMAGE=' "$report" | head -n1 | sed 's|.*/||' | sed 's/<.*//')
 second=$(grep 'SECOND_EPOCH__SECOND_IMAGE=' "$report" | head -n1 | sed 's|.*/||' | sed 's/<.*//')
 # Only numeric first fields: do not mistake a star-elongation message for FWHM.
 {
  [ -n "$first" ] && grep "pix.*$first" "$report" | awk '$1 ~ /^[0-9.]+$/ {print $1}'
  [ -n "$second" ] && grep "pix.*$second" "$report" | awk '$1 ~ /^[0-9.]+$/ {print $1}'
 } | sort -rn | head -n1
}

nmw_second_epoch_min_altitude() {
 # The lowest altitude of the second-epoch images in degrees, to one decimal
 # place, from the 'Field altitude during imaging: NN.N deg' lines the VaST
 # transient factory writes for images whose FITS header records the altitude
 # (CENTALT or OBJCTALT). Empty when the report has no such line, so the
 # summary column stays blank for cameras whose headers carry no altitude.
 local report="$1"
 grep 'Field altitude during imaging:' "$report" | awk '{for (i = 2; i <= NF; i++) if ($i == "deg" && $(i-1) ~ /^[-+]?[0-9]+\.?[0-9]*$/) {print $(i-1); break}}' | sort -n | head -n1 | awk '{printf "%.1f", $1}'
}

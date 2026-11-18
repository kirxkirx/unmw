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

nmw_second_epoch_azimuth() {
 # The azimuth in degrees (from North through East), to one decimal place, of
 # the second-epoch image whose altitude the Altitude column shows - the one
 # with the lowest altitude - so that the two columns describe the same image.
 # Prefer VaST's 'Field azimuth during imaging:' line. For older reports,
 # fall back to the FITS header dump of the same image: CENTAZ (written by
 # N.I.N.A. and Sequence Generator Pro) or OBJCTAZ (MaxIm DL, SGP, ACP), as a
 # number or as a sexagesimal 'D M S' string - the same keyword families and
 # value forms as the altitude. The header reaches the report through the log
 # of the dark frame subtraction, util/ccd/ms: 'Exploring image header: <path>'
 # and then one 'Record N: "<header card>" status=0' line per card, once for
 # each of the two second-epoch images. Empty when the report has no usable
 # azimuth card - no dark frame subtraction in this run (no dark frames
 # configured, or the images were calibrated by an earlier run), or no
 # azimuth keyword in the header. Never pair the lowest altitude with another
 # image's azimuth. Without altitude lines use the first available azimuth.
 local report="$1"
 awk '
# The value of a FITS card: the text after the "=" value indicator in column
# 9, without the quotes of a string value and without the comment after "/".
function card_value(card,  value, quote_position) {
 if (substr(card, 9, 1) != "=") return ""
 value = substr(card, 10)
 sub(/^[ \t]+/, "", value)
 if (substr(value, 1, 1) == "\047") {
  value = substr(value, 2)
  quote_position = index(value, "\047")
  if (quote_position > 0) value = substr(value, 1, quote_position - 1)
 } else {
  sub(/\/.*$/, "", value)
 }
 sub(/^[ \t]+/, "", value)
 sub(/[ \t]+$/, "", value)
 return value
}
# Decimal degrees from a number (FITS allows a D exponent) or from a
# sexagesimal "D M S" or "D:M:S" string; "" when the value is neither.
function to_degrees(value,  n_parts, parts, i, sign, degrees) {
 if (value ~ /^[-+]?([0-9]+\.?[0-9]*|\.[0-9]+)([eEdD][-+]?[0-9]+)?$/) {
  sub(/[dD]/, "e", value)
  return value + 0
 }
 gsub(/:/, " ", value)
 n_parts = split(value, parts, /[ \t]+/)
 if (n_parts < 2 || n_parts > 3) return ""
 for (i = 1; i <= n_parts; i++) {
  if (parts[i] !~ /^[-+]?([0-9]+\.?[0-9]*|\.[0-9]+)$/) return ""
 }
 for (i = 2; i <= n_parts; i++) {
  if (parts[i] + 0 < 0 || parts[i] + 0 >= 60) return ""
 }
 sign = (parts[1] ~ /^-/) ? -1 : 1
 degrees = parts[1]
 sub(/^[-+]/, "", degrees)
 degrees = degrees + parts[2] / 60.0
 if (n_parts == 3) degrees = degrees + parts[3] / 3600.0
 return sign * degrees
}
function usable_azimuth(value) {
 return (value != "" && value >= 0.0 && value <= 360.0) ? value : ""
}
/Exploring image header: / {
 image = $0
 sub(/.*Exploring image header: /, "", image)
 sub(/<.*$/, "", image)
 sub(/[ \t]+$/, "", image)
 sub(/.*\//, "", image)
 next
}
/^Record [0-9]+: "/ {
 if (image == "") next
 card = $0
 sub(/^Record [0-9]+: "/, "", card)
 sub(/" status=.*$/, "", card)
 keyword = substr(card, 1, 8)
 if (keyword != "CENTAZ  " && keyword != "OBJCTAZ ") next
 if (!(image in listed)) { listed[image] = 1; images[++n_images] = image }
 if (keyword == "CENTAZ  " && !(image in centaz)) centaz[image] = usable_azimuth(to_degrees(card_value(card)))
 if (keyword == "OBJCTAZ " && !(image in objctaz)) objctaz[image] = usable_azimuth(to_degrees(card_value(card)))
 next
}
/Field altitude during imaging: / {
 line = $0
 gsub(/<[^>]*>/, "", line)
 sub(/[ \t]+$/, "", line)
 n_fields = split(line, fields, /[ \t]+/)
 name = fields[n_fields]
 sub(/.*\//, "", name)
 for (i = 2; i <= NF; i++) {
  if ($i == "deg" && $(i-1) ~ /^[-+]?[0-9]+\.?[0-9]*$/) {
   if (lowest_image == "" || $(i-1) + 0 < lowest_altitude) {
    lowest_image = name
    lowest_altitude = $(i-1) + 0
   }
   break
  }
 }
}
/Field azimuth during imaging: / {
 line = $0
 gsub(/<[^>]*>/, "", line)
 sub(/[ \t]+$/, "", line)
 n_fields = split(line, fields, /[ \t]+/)
 name = fields[n_fields]
 sub(/.*\//, "", name)
 for (i = 2; i <= n_fields; i++) {
  if (fields[i] == "deg") {
   value = usable_azimuth(to_degrees(fields[i-1]))
   if (value != "" && !(name in direct)) {
    direct[name] = value
    if (!(name in listed)) { listed[name] = 1; images[++n_images] = name }
   }
   break
  }
 }
}
END {
 if (lowest_image != "") {
  if (lowest_image in direct) chosen = direct[lowest_image]
  else if (centaz[lowest_image] != "") chosen = centaz[lowest_image]
  else chosen = objctaz[lowest_image]
  if (chosen != "") printf "%.1f", chosen
  exit
 }
 for (k = 1; k <= n_images; k++) {
  image = images[k]
  if (image in direct) chosen = direct[image]
  else chosen = (centaz[image] != "") ? centaz[image] : objctaz[image]
  if (chosen != "") {
   printf "%.1f", chosen
   exit
  }
 }
}' "$report"
}

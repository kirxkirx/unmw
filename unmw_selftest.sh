#!/usr/bin/env bash

# Exit if the script is run via a CGI request
if [[ -n "$REQUEST_METHOD" ]]; then
 echo "This script cannot be run via a web request."
 exit 1
fi

# change to the work directory
SCRIPTDIR=$(dirname "$(readlink -f "$0")")
cd "$SCRIPTDIR" || exit 1

if [ -f local_config.sh ];then
 echo "Move local_config.sh to a backup!
The test script will need to owerwrite this file."
 exit 1
fi

# Copy the config file
cp -v local_config.sh_for_test local_config.sh

# Create data directory
if [ ! -d uploads ];then
 mkdir "uploads" || exit 1
fi
cd "uploads" || exit 1
UPLOADS_DIR="$PWD"

# Install VaST if it was not installed before
if [ ! -d vast ];then
 git clone https://github.com/kirxkirx/vast.git || exit 1
 cd vast || exit 1
 make || exit 1
else
 cd vast || exit 1
fi
lib/update_offline_catalogs.sh all || exit 1
# VaST should be ready for work now

# Download test data
export REFERENCE_IMAGES="$UPLOADS_DIR/NMW__NovaVul24_Stas_test/reference_images" 
if [ ! -d "$REFERENCE_IMAGES" ];then
 cd "$UPLOADS_DIR" || exit 1
 {
  curl --silent --show-error -O "http://scan.sai.msu.ru/~kirx/pub/NMW__NovaVul24_Stas_test.tar.bz2" && \
  tar -xvjf NMW__NovaVul24_Stas_test.tar.bz2 && \
  rm -f NMW__NovaVul24_Stas_test.tar.bz2
 } || exit 1
fi
cd "$SCRIPTDIR" || exit 1
### Test ./autoprocess.sh without web upload scripts ###
./autoprocess.sh "$UPLOADS_DIR/NMW__NovaVul24_Stas_test/second_epoch_images" || exit 1
RESULTS_DIR_FROM_URL=$(grep 'The results should appear' uploads/autoprocess.txt | tail -n1 | awk -F'http://localhost:8080/' '{print $2}')
if [ -z "$RESULTS_DIR_FROM_URL" ];then
 echo "$0 test error: RESULTS_DIR_FROM_URL is empty"
 exit 1
fi
if [ ! -d "$RESULTS_DIR_FROM_URL" ];then
 echo "$0 test error: RESULTS_DIR_FROM_URL=$RESULTS_DIR_FROM_URL is not a directory"
 exit 1
fi
if [ ! -f "$RESULTS_DIR_FROM_URL/index.html" ];then
 echo "$0 test error: RESULTS_DIR_FROM_URL=$RESULTS_DIR_FROM_URL/index.html is not a file"
 exit 1
fi
if ! "$VAST_REFERENCE_COPY"/util/transients/validate_HTML_list_of_candidates.sh "$RESULTS_DIR_FROM_URL" ;then
 echo "$0 test error: RESULTS_DIR_FROM_URL=$RESULTS_DIR_FROM_URL/index.html validation failed"
 exit 1
fi
if ! grep --quiet 'V0615 Vul' "$RESULTS_DIR_FROM_URL/index.html" ;then
 echo "$0 test error: RESULTS_DIR_FROM_URL=$RESULTS_DIR_FROM_URL/index.html does not have 'V0615 Vul'"
 exit 1
fi
if ! grep --quiet 'PNV J19430751+2100204' "$RESULTS_DIR_FROM_URL/index.html" ;then
 echo "$0 test error: RESULTS_DIR_FROM_URL=$RESULTS_DIR_FROM_URL/index.html does not have 'PNV J19430751+2100204'"
 exit 1
fi

# Go back to the work directory
cd "$SCRIPTDIR" || exit 1

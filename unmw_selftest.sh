#!/usr/bin/env bash

# Exit if the script is run via a CGI request
if [[ -n "$REQUEST_METHOD" ]]; then
 echo "This script cannot be run via a web request."
 exit 1
fi

command -v zip &> /dev/null
if [ $? -ne 0 ];then
 echo "$0 test error: 'zip' command not found" 
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
VAST_INSTALL_DIR="$PWD"
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
if ! "$VAST_INSTALL_DIR"/util/transients/validate_HTML_list_of_candidates.sh "$RESULTS_DIR_FROM_URL" ;then
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

# Start the Python HTTP server in the background
cd "$SCRIPTDIR" || exit 1
if [ ! -f custom_http_server.py ];then
 echo "$0 test error: 'custom_http_server.py' not found in '$SCRIPTDIR'"
 exit 1
fi
if [ ! -s custom_http_server.py ];then
 echo "$0 test error: 'custom_http_server.py' is empty"
 exit 1
fi
python3 custom_http_server.py > "$UPLOADS_DIR/custom_http_server.log" 2>&1 &
SERVER_PID=$!

# Function to clean up (kill the server) on script exit
cleanup() {
 echo "Stopping the Python HTTP server..."
 kill $SERVER_PID 2>/dev/null
 echo "Logs of the Python HTTP server..."
 cat "$UPLOADS_DIR/custom_http_server.log"
 rm -fv "$UPLOADS_DIR/custom_http_server.log" 
}

# Trap script exit signals to ensure cleanup is executed
trap cleanup EXIT INT TERM

sleep 5  # Give the server some time to start
ps -ef | grep python3 | grep custom_http_server.py  # Check if the server is running
if [ $? -ne 0 ];then
 echo "$0 test error: looks like the HTTP server is not running"
 exit 1
fi

# Check if the server is working, serving the content of the current directory
if ! curl --silent --show-error 'http://localhost:8080/' | grep --quiet 'uploads/' ;then
 echo "$0 test error: something is wrong with the HTTP server"
 exit 1
fi
# Check the results of the previous manual run
if ! curl --silent --show-error 'http://localhost:8080/$RESULTS_DIR_FROM_URL' | grep --quiet 'V0615 Vul' ;then
 echo "$0 test error: failed to get manual run results page via the HTTP server"
 exit 1
fi

# Prepare zip archive with the images for the web upload test
cd "$UPLOADS_DIR/NMW__NovaVul24_Stas_test/" || exit 1
cp -r second_epoch_images NMW__NovaVul24_Stas__WebCheck__NotReal
zip -r NMW__NovaVul24_Stas__WebCheck__NotReal.zip NMW__NovaVul24_Stas__WebCheck__NotReal/
if [ ! -s NMW__NovaVul24_Stas__WebCheck__NotReal.zip ];then
 echo "$0 test error: failed to create a zip archive with the images"
 exit 1
fi
file NMW__NovaVul24_Stas__WebCheck__NotReal.zip | grep --quiet "Zip archive"
if [ $? -ne 0 ];then
 echo "$0 test error: NMW__NovaVul24_Stas__WebCheck__NotReal.zip does not look like a ZIP archive"
 exit 1
fi
results_url=$(curl -X POST -F 'file=@NMW__NovaVul24_Stas__WebCheck__NotReal.zip' -F 'workstartemail=' -F 'workendemail=' 'http://localhost:8080/upload.py' | grep 'url=' | head -n1 | awk -F'url=' '{print $2}')


# Go back to the work directory
cd "$SCRIPTDIR" || exit 1

# Stop the server
cleanup

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

### Define useful functions

# Function to find a free port for an HTTP server
get_free_port_for_http_server() {
    # Define the port range
    local START_PORT=8080
    local END_PORT=8090

    # Function to check if a command exists
    command_exists() {
        command -v "$1" >/dev/null 2>&1
    }

    # Function to check if a port is in use
    is_port_in_use() {
        local port=$1

        if command_exists ss; then
            # Use ss if available
            ss -tuln | grep -q ":$port "
        elif command_exists netstat; then
            # Use netstat if ss is not available
            netstat -tuln | grep -q ":$port "
        elif command_exists lsof; then
            # Use lsof if neither ss nor netstat is available
            lsof -i :$port >/dev/null 2>&1
        else
            echo "Error: None of ss, netstat, or lsof is available on this system." >&2
            return 1
        fi
    }

    # Find the first unused port
    for port in $(seq $START_PORT $END_PORT); do
        if ! is_port_in_use $port; then
            echo "$port"
            return 0
        fi
    done

    # If no free port is found
    echo "Error: No free port found in the range $START_PORT-$END_PORT." >&2
    return 1
}

UNMW_FREE_PORT=$(get_free_port_for_http_server)
if [[ $? -eq 0 ]]; then
    echo "Free port for HTTP server: $UNMW_FREE_PORT"
else
    echo "Failed to find a free port."
    exit 1
fi
# export UNMW_FREE_PORT as local_config.sh needs it
export UNMW_FREE_PORT

### Start the test

# Copy the config file
cp -v local_config.sh_for_test local_config.sh
# local_config.sh could be sourced here, but I'd rather let individual scripts source it on their own for testing

# Link the python3 version of the upload handler code
ln -s upload.py3 upload.py

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
#RESULTS_DIR_FROM_URL__MANUALRUN=$(grep 'The results should appear' uploads/autoprocess.txt | tail -n1 | awk -F'http://localhost:8080/' '{print $2}')
RESULTS_DIR_FROM_URL__MANUALRUN=$(grep 'The results should appear' uploads/autoprocess.txt | tail -n1 | awk -F"http://localhost:$UNMW_FREE_PORT/" '{print $2}')
if [ -z "$RESULTS_DIR_FROM_URL__MANUALRUN" ];then
 echo "$0 test error: RESULTS_DIR_FROM_URL__MANUALRUN is empty"
 exit 1
fi
if [ ! -d "$RESULTS_DIR_FROM_URL__MANUALRUN" ];then
 echo "$0 test error: RESULTS_DIR_FROM_URL__MANUALRUN=$RESULTS_DIR_FROM_URL__MANUALRUN is not a directory"
 exit 1
fi
if [ ! -f "${RESULTS_DIR_FROM_URL__MANUALRUN}index.html" ];then
 echo "$0 test error: RESULTS_DIR_FROM_URL__MANUALRUN=${RESULTS_DIR_FROM_URL__MANUALRUN}index.html is not a file"
 exit 1
fi
if ! "$VAST_INSTALL_DIR"/util/transients/validate_HTML_list_of_candidates.sh "$RESULTS_DIR_FROM_URL__MANUALRUN" ;then
 echo "$0 test error: RESULTS_DIR_FROM_URL__MANUALRUN=${RESULTS_DIR_FROM_URL__MANUALRUN}index.html validation failed"
 exit 1
fi
if ! grep --quiet 'V0615 Vul' "${RESULTS_DIR_FROM_URL__MANUALRUN}index.html" ;then
 echo "$0 test error: RESULTS_DIR_FROM_URL__MANUALRUN=${RESULTS_DIR_FROM_URL__MANUALRUN}index.html does not have 'V0615 Vul'"
 exit 1
fi
if ! grep --quiet 'PNV J19430751+2100204' "${RESULTS_DIR_FROM_URL__MANUALRUN}index.html" ;then
 echo "$0 test error: RESULTS_DIR_FROM_URL__MANUALRUN=${RESULTS_DIR_FROM_URL__MANUALRUN}index.html does not have 'PNV J19430751+2100204'"
 exit 1
fi

######## Prepare to run web servers

# Function to clean up (kill the server) on script exit
cleanup() {
 cd "$SCRIPTDIR" || exit 1
 #
 if [ -f "$UPLOADS_DIR/custom_http_server.log" ];then
  echo "____________ cleanup ____________"
  echo "Stopping the Python HTTP server..."
  kill $PYTHON_HTTP_SERVER_PID 2>/dev/null
  echo "Logs of the Python HTTP server..."
  cat "$UPLOADS_DIR/custom_http_server.log"
  rm -fv "$UPLOADS_DIR/custom_http_server.log" 
  echo "________________________________"
 fi
 #
 if [ -f "$UPLOADS_DIR/sthttpd_http_server.log" ];then
  echo "Stopping the sthttpd HTTP server..."
  kill $STHTTPD_SERVER_PID 2>/dev/null
  echo "Logs of the sthttpd HTTP server..."
  cat "$UPLOADS_DIR/sthttpd_http_server.log"
  rm -fv "$UPLOADS_DIR/sthttpd_http_server.log" 
  echo "________________________________"
 fi
}

# Trap script exit signals to ensure cleanup is executed
trap cleanup EXIT INT TERM


# Go back to the work directory
cd "$SCRIPTDIR" || exit 1

echo "Let's test with sthttpd HTTP server"

if [ ! -d sthttpd ];then
 echo "Get sthttpd"
 git clone https://github.com/blueness/sthttpd.git
 if [ $? -ne 0 ];then
  echo "$0 test error: cannot git clone sthttpd"
  exit 1
 fi
 cd sthttpd || exit 1
 ./autogen.sh || exit 1
 ./configure || exit 1
 make || exit 1
 if [ ! -x src/thttpd ];then
  echo "$0 test error: src/thttpd was not created"
  exit 1
 fi
fi

echo "Run sthttpd"
# Go back to the work directory
cd "$SCRIPTDIR" || exit 1


# Run the server - it will run in the background
if [ ! -x sthttpd/src/thttpd ];then
 echo "$0 test error: sthttpd/src/thttpd was not found"
 exit 1
fi
# by default, sthttpd will not pass UNMW_FREE_PORT to cgi scripts
# So actually our only hope is that UNMW_FREE_PORT=8080
# It will also not pass REFERENCE_IMAGES that we set above and that works well with Python HTTP server
# Will have to hardcode REFERENCE_IMAGES to local_config.sh_for_test
if [ "$UNMW_FREE_PORT" != "8080" ];then
 echo "$0 test error: the port 8080 needed for the sthttpd test is not free"
 exit 1
fi
sthttpd/src/thttpd -nos -p "$UNMW_FREE_PORT" -d "$PWD" -c "upload.py" -l "$UPLOADS_DIR/sthttpd_http_server.log"
STHTTPD_SERVER_PID=$!




# Prepare zip archive with the images for the web upload test
cd "$UPLOADS_DIR/NMW__NovaVul24_Stas_test/" || exit 1
# Clean what might be remaining from a previous test run
if [ -d NMW__NovaVul24_Stas__WebCheck__NotReal ];then
 rm -rfv NMW__NovaVul24_Stas__WebCheck__NotReal
fi
if [ -f NMW__NovaVul24_Stas__WebCheck__NotReal.zip ];then
 rm -fv NMW__NovaVul24_Stas__WebCheck__NotReal.zip
fi
#
cp -rv second_epoch_images NMW__NovaVul24_Stas__WebCheck__NotReal
zip -r NMW__NovaVul24_Stas__WebCheck__NotReal.zip NMW__NovaVul24_Stas__WebCheck__NotReal/
if [ ! -s NMW__NovaVul24_Stas__WebCheck__NotReal.zip ];then
 echo "$0 test error: failed to create a zip archive with the images"
 exit 1
fi
if ! file NMW__NovaVul24_Stas__WebCheck__NotReal.zip | grep --quiet 'Zip archive' ;then
 echo "$0 test error: NMW__NovaVul24_Stas__WebCheck__NotReal.zip does not look like a ZIP archive"
 exit 1
fi
echo "-- The content of the zip archive --"
unzip -l NMW__NovaVul24_Stas__WebCheck__NotReal.zip
echo "------------------------------------"

# Test if HTTP server is running
# (moved after zip file creation to give the server more time to start)
sleep 5  # Give the server some time to start
# Check if the server is running
if ! ps -ef | grep thttpd ;then
 echo "$0 test error: looks like the HTTP server is not running"
 exit 1
fi


# Check if the server is working, serving the content of the current directory
if ! curl --silent --show-error "http://localhost:$UNMW_FREE_PORT/" | grep --quiet 'uploads/' ;then
 echo "$0 test error: something is wrong with the HTTP server"
 exit 1
fi
# Check the results of the previous manual run
if ! curl --silent --show-error "http://localhost:$UNMW_FREE_PORT/$RESULTS_DIR_FROM_URL__MANUALRUN" | grep --quiet 'V0615 Vul' ;then
 echo "$0 test error: failed to get manual run results page via the HTTP server"
 exit 1
else
 echo "$0 successfully got the manual run results page via the HTTP server"
fi

# Upload the results file on server
if [ ! -f NMW__NovaVul24_Stas__WebCheck__NotReal.zip ];then
 echo "$0 test error: canot find NMW__NovaVul24_Stas__WebCheck__NotReal.zip"
 exit 1
else
 echo "$0 test: double-checking that NMW__NovaVul24_Stas__WebCheck__NotReal.zip is stil here"
fi
results_server_reply=$(curl --max-time 600 --silent --show-error -X POST -F 'file=@NMW__NovaVul24_Stas__WebCheck__NotReal.zip' -F 'workstartemail=' -F 'workendemail=' "http://localhost:$UNMW_FREE_PORT/upload.py")
if [ -z "$results_server_reply" ];then
 echo "$0 test error: empty HTTP server reply"
 exit 1
fi
echo "---- Server reply ---
$results_server_reply
---------------------"
results_url=$(echo "$results_server_reply" | grep 'url=' | head -n1 | awk -F'url=' '{print $2}' | awk -F'"' '{print $1}')
if [ -z "$results_url" ];then
 echo "$0 test error: empty results_url after parsing HTTP server reply"
 exit 1
fi
echo "---- results_url ---
$results_url
---------------------"
echo "Sleep to give the server some time to process the data"
# Wait until no copies of autoprocess.sh are running
# (this assumes no other copies of the script are running)
echo "Waiting for autoprocess.sh to finish..."
while pgrep -f "autoprocess.sh" > /dev/null; do
 #echo -n "."
 sleep 1  # Wait for 1 second before checking again
done
#
if ! curl --silent --show-error "$results_url" | grep --quiet 'V0615 Vul' ;then
#if ! curl --silent --show-error "http://localhost:$UNMW_FREE_PORT/$RESULTS_DIR_FROM_URL__MANUALRUN" | grep --quiet 'V0615 Vul' ;then
 echo "$0 test error: failed to get web run results page via the HTTP server"
 exit 1
else
 echo "V0615 Vul is fond in HTTP-uploaded results"
fi

# Go back to the work directory
cd "$SCRIPTDIR" || exit 1
cd "$UPLOADS_DIR" || exit 1

# RAR file with cloudy images test
if [ ! -f "2025-01-07_Vul8_183150_Stas.rar" ];then
 {
  curl --silent --show-error -O "http://scan.sai.msu.ru/~kirx/pub/2025-01-07_Vul8_183150_Stas.rar" 
 } || exit 1
fi
if [ ! -s 2025-01-07_Vul8_183150_Stas.rar ];then
 echo "$0 test error: failed to download a archive with the images"
 exit 1
else
 echo "Downloaded test file 2025-01-07_Vul8_183150_Stas.rar"
fi
if ! file 2025-01-07_Vul8_183150_Stas.rar | grep --quiet 'RAR archive' ;then
 echo "$0 test error: 2025-01-07_Vul8_183150_Stas.rar does not look like a RAR archive"
 exit 1
fi
echo "-- The content of the rar archive --"
if command -v rar &> /dev/null ;then
 echo "Using rar"
 rar l 2025-01-07_Vul8_183150_Stas.rar
elif command -v unrar &> /dev/null ;then
 echo "Using unrar"
 unrar l 2025-01-07_Vul8_183150_Stas.rar
else
 echo "Please install rar or unrar to complete this test"
 exit 1
fi
echo "------------------------------------"
unset results_server_reply
unset results_url
results_server_reply=$(curl --max-time 600 --silent --show-error -X POST -F 'file=@2025-01-07_Vul8_183150_Stas.rar' -F 'workstartemail=' -F 'workendemail=' "http://localhost:$UNMW_FREE_PORT/upload.py")
if [ -z "$results_server_reply" ];then
 echo "$0 test error: empty HTTP server reply"
 exit 1
fi
echo "---- Server reply ---
$results_server_reply
---------------------"
results_url=$(echo "$results_server_reply" | grep 'url=' | head -n1 | awk -F'url=' '{print $2}' | awk -F'"' '{print $1}')
if [ -z "$results_url" ];then
 echo "$0 test error: empty results_url after parsing HTTP server reply"
 exit 1
fi
echo "---- results_url ---
$results_url
---------------------"
echo "Sleep to give the server some time to process the data"
# Wait until no copies of autoprocess.sh are running
# (this assumes no other copies of the script are running)
echo "Waiting for autoprocess.sh to finish..."
while pgrep -f "autoprocess.sh" > /dev/null; do
 sleep 1  # Wait for 1 second before checking again
done
#
echo "*** We are at $PWD"
echo "ls -lhdt *"
ls -lhdt *
#
echo "--- autoprocess.txt ---"
cat autoprocess.txt
echo "-----------------------"
for WEB_UPLOAD_DIR in web_upload_* ;do
 if [ ! -d "$WEB_UPLOAD_DIR" ];then
  echo "No web_upload_* directories found (this is fine)"
  break
 fi
 echo "___ $WEB_UPLOAD_DIR ___"
 for FILE_TO_CAT in "$WEB_UPLOAD_DIR/"*.txt "$WEB_UPLOAD_DIR/"*.log ;do
  ls "$FILE_TO_CAT"
  cat "$FILE_TO_CAT"
 done
done
#
if ! curl --silent --show-error "$results_url" | grep --quiet 'ERROR: too few refereence images for the field Vul8' ;then
 echo "$0 test error: failed to get web run results page via the HTTP server"
 exit 1
else
 echo "Expected error message is fond in HTTP-uploaded results"
fi


# Go back to the work directory
cd "$SCRIPTDIR" || exit 1

# Test the combine reports script
if ! ./combine_reports.sh ;then
 echo "$0 test error: non-zero exit code of combine_reports.sh"
 exit 1
else
 echo "./combine_reports.sh seems to run fine"
fi
# uploads/ is the default location for the processing data (both images and results)
cd "$UPLOADS_DIR" || exit 1
#
LATEST_COMBINED_HTML_REPORT=$(ls -t *_evening_* *_morning_* 2>/dev/null | grep -v summary | head -n 1)
if [ -z "$LATEST_COMBINED_HTML_REPORT" ];then
 echo "$0 test error: empty LATEST_COMBINED_HTML_REPORT"
 exit 1
else
 echo "The latest combined report is:"
 ls -lh "$LATEST_COMBINED_HTML_REPORT"
fi
if ! grep --quiet 'V0615 Vul' "$LATEST_COMBINED_HTML_REPORT" ;then
 echo "$0 test error: cannot find 'V0615 Vul' in LATEST_COMBINED_HTML_REPORT=$LATEST_COMBINED_HTML_REPORT"
 exit 1
else
 echo "Found V0615 Vul in $LATEST_COMBINED_HTML_REPORT"
fi
# Check that the png image previews were actually created
for PNG_FILE_TO_TEST in $(grep 'img src=' "$LATEST_COMBINED_HTML_REPORT" | awk -F"img src=" '{print $2}' | awk -F'"'  '{print $2}' | grep '.png') ;do
 if [ ! -f "$PNG_FILE_TO_TEST" ];then
  echo "$0 test error: cannot find the PNG file $PNG_FILE_TO_TEST"
  exit 1
 fi
 if [ ! -s "$PNG_FILE_TO_TEST" ];then
  echo "$0 test error: empty PNG file $PNG_FILE_TO_TEST"
  exit 1
 fi
 if ! file "$PNG_FILE_TO_TEST" | grep --quiet 'PNG image' ;then
  echo "$0 test error: not a PNG file $PNG_FILE_TO_TEST"
  file "$PNG_FILE_TO_TEST"
  exit 1
 fi
done
echo "PNG files linked in the combined report look fine"
#
LATEST_PROCESSING_SUMMARY_LOG=$(ls -t *_evening_* *_morning_* 2>/dev/null | grep 'summary' | head -n 1)
if [ -z "$LATEST_PROCESSING_SUMMARY_LOG" ];then
 echo "$0 test error: empty LATEST_PROCESSING_SUMMARY_LOG"
 exit 1
else
 echo "The latest combined report is:"
 ls -lh "$LATEST_PROCESSING_SUMMARY_LOG"
fi
if ! grep 'Vul3' "$LATEST_PROCESSING_SUMMARY_LOG" | grep --quiet 'OK' ;then
 echo "$0 test error: cannot find Vul3 OK in LATEST_PROCESSING_SUMMARY_LOG=$LATEST_PROCESSING_SUMMARY_LOG"
 exit 1
else
 echo "Found Vul3 OK in $LATEST_PROCESSING_SUMMARY_LOG"
fi
if ! grep 'Vul8' "$LATEST_PROCESSING_SUMMARY_LOG" | grep --quiet 'ERROR' ;then
 echo "$0 test error: cannot find Vul8 ERROR in LATEST_PROCESSING_SUMMARY_LOG=$LATEST_PROCESSING_SUMMARY_LOG"
 exit 1
else
 echo "Found Vul8 ERROR in $LATEST_PROCESSING_SUMMARY_LOG"
fi

echo "All tests passed with sthttpd HTTP server!"




# Go back to the work directory
cd "$SCRIPTDIR" || exit 1

echo "Now let's test with Python HTTP server"

UNMW_FREE_PORT=$(get_free_port_for_http_server)
if [[ $? -eq 0 ]]; then
    echo "Free port for HTTP server: $UNMW_FREE_PORT"
else
    echo "Failed to find a free port."
    exit 1
fi
# export UNMW_FREE_PORT as local_config.sh needs it
export UNMW_FREE_PORT


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
# by default, Python HTTP server will pass UNMW_FREE_PORT to cgi scripts
# Explicitly specfy port on which the Python HTTP server should run
python3 custom_http_server.py "$UNMW_FREE_PORT" > "$UPLOADS_DIR/custom_http_server.log" 2>&1 &
PYTHON_HTTP_SERVER_PID=$!


### Repeat the Nova Vul test with sthttpd
cd "$UPLOADS_DIR/NMW__NovaVul24_Stas_test/" || exit 1
if [ ! -s NMW__NovaVul24_Stas__WebCheck__NotReal.zip ];then
 echo "$0 test error: failed to find a zip archive with the images"
 exit 1
fi
if ! file NMW__NovaVul24_Stas__WebCheck__NotReal.zip | grep --quiet 'Zip archive' ;then
 echo "$0 test error: NMW__NovaVul24_Stas__WebCheck__NotReal.zip does not look like a ZIP archive"
 exit 1
fi
echo "-- The content of the zip archive --"
unzip -l NMW__NovaVul24_Stas__WebCheck__NotReal.zip
echo "------------------------------------"

# Test if sthttpd HTTP server is running
# (moved after zip file creation to give the server more time to start)
sleep 5  # Give the server some time to start
# Check if the server is running
if ! ps -ef | grep python3 | grep custom_http_server.py ;then
 echo "$0 test error: looks like the Python HTTP server is not running"
 exit 1
fi


# Check if the server is working, serving the content of the current directory
if ! curl --silent --show-error "http://localhost:$UNMW_FREE_PORT/" | grep --quiet 'uploads/' ;then
 echo "$0 test error: something is wrong with the HTTP server"
 exit 1
fi
# Check the results of the previous manual run
if ! curl --silent --show-error "http://localhost:$UNMW_FREE_PORT/$RESULTS_DIR_FROM_URL__MANUALRUN" | grep --quiet 'V0615 Vul' ;then
 echo "$0 test error: failed to get manual run results page via the HTTP server"
 exit 1
else
 echo "$0 successfully got the manual run results page via the HTTP server"
fi

# Upload the results file on server
if [ ! -f NMW__NovaVul24_Stas__WebCheck__NotReal.zip ];then
 echo "$0 test error: canot find NMW__NovaVul24_Stas__WebCheck__NotReal.zip"
 exit 1
else
 echo "$0 test: double-checking that NMW__NovaVul24_Stas__WebCheck__NotReal.zip is stil here"
fi
results_server_reply=$(curl --max-time 600 --silent --show-error -X POST -F 'file=@NMW__NovaVul24_Stas__WebCheck__NotReal.zip' -F 'workstartemail=' -F 'workendemail=' "http://localhost:$UNMW_FREE_PORT/upload.py")
if [ -z "$results_server_reply" ];then
 echo "$0 test error: empty HTTP server reply"
 exit 1
fi
echo "---- Server reply ---
$results_server_reply
---------------------"
results_url=$(echo "$results_server_reply" | grep 'url=' | head -n1 | awk -F'url=' '{print $2}' | awk -F'"' '{print $1}')
if [ -z "$results_url" ];then
 echo "$0 test error: empty results_url after parsing HTTP server reply"
 exit 1
fi
echo "---- results_url ---
$results_url
---------------------"
echo "Sleep to give the server some time to process the data"
# Wait until no copies of autoprocess.sh are running
# (this assumes no other copies of the script are running)
echo "Waiting for autoprocess.sh to finish..."
while pgrep -f "autoprocess.sh" > /dev/null; do
 #echo -n "."
 sleep 1  # Wait for 1 second before checking again
done
#
if ! curl --silent --show-error "$results_url" | grep --quiet 'V0615 Vul' ;then
#if ! curl --silent --show-error "http://localhost:$UNMW_FREE_PORT/$RESULTS_DIR_FROM_URL__MANUALRUN" | grep --quiet 'V0615 Vul' ;then
 echo "$0 test error: failed to get web run results page via the HTTP server"
 exit 1
else
 echo "V0615 Vul is fond in HTTP-uploaded results"
fi

###

echo "All tests passed with Python HTTP server!"


echo "
*********************
* All tests passed! *
*********************
"

# Go back to the work directory
cd "$SCRIPTDIR" || exit 1

# no need to manually stop the server and remove temporary files as thanks to trap 
# cleanup will be called automatically on EXIT, which includes normal termination or errors.
# Stop the server
#cleanup

#!/usr/bin/env bash

# Exit if the script is run via a CGI request
if [[ -n "$REQUEST_METHOD" ]]; then
 echo "This script cannot be run via a web request."
 exit 1
fi

##################################################################
# Check for required external programs before starting the test
##################################################################
echo "Checking for required external programs..."

MISSING_PROGRAMS=""

# List of required programs
# Note: ss/netstat/lsof - at least one is needed for port checking
# Note: rar/unrar - at least one is needed for RAR archive handling
REQUIRED_PROGRAMS="zip dirname readlink seq git make aclocal autoconf automake curl tar grep awk sleep kill cat ps pgrep unzip file sed python3 head tail tr"

for PROG in $REQUIRED_PROGRAMS; do
 if ! command -v "$PROG" &> /dev/null; then
  MISSING_PROGRAMS="$MISSING_PROGRAMS $PROG"
  echo "  $PROG - NOT FOUND"
 else
  echo "  $PROG - found"
 fi
done

# Check for at least one of ss/netstat/lsof (needed for port checking)
if ! command -v ss &> /dev/null && ! command -v netstat &> /dev/null && ! command -v lsof &> /dev/null; then
 MISSING_PROGRAMS="$MISSING_PROGRAMS ss/netstat/lsof(at_least_one)"
 echo "  ss/netstat/lsof - NONE FOUND (at least one required for port checking)"
else
 echo "  ss/netstat/lsof - at least one found"
fi

# Check for at least one of rar/unrar (needed for RAR archive handling)
if ! command -v rar &> /dev/null && ! command -v unrar &> /dev/null; then
 MISSING_PROGRAMS="$MISSING_PROGRAMS rar/unrar(at_least_one)"
 echo "  rar/unrar - NONE FOUND (at least one required for RAR archive handling)"
else
 echo "  rar/unrar - at least one found"
fi

if [ -n "$MISSING_PROGRAMS" ]; then
 echo ""
 echo "ERROR: The following required programs are missing:"
 echo "$MISSING_PROGRAMS"
 echo ""
 echo "Please install these programs before running the test."
 exit 1
fi

echo "All required external programs found!"
echo ""

##################################################################
# Disable IPv6 to work around WSL2 localhost networking issues
# WSL2 has known issues with IPv6 (::1) localhost connections where
# data can be silently lost. See: https://github.com/microsoft/WSL/issues/10803
# This only affects the current session and is safe on native Linux.
##################################################################
echo "Disabling IPv6 to work around WSL2 networking issues..."
if sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null 2>&1 && \
   sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null 2>&1 && \
   sysctl -w net.ipv6.conf.lo.disable_ipv6=1 >/dev/null 2>&1; then
 echo "  IPv6 disabled successfully"
else
 echo "  WARNING: Could not disable IPv6 (may need root privileges)"
 echo "  Continuing anyway - this may cause issues on WSL2"
fi
echo ""

##################################################################
# Check Python scripts for portability and syntax
##################################################################
echo "Checking Python scripts..."

# Check that Python scripts don't have hardcoded version in shebang
# (e.g., #!/usr/bin/env python3.12 would fail on systems without that specific version)
for PY_SCRIPT in upload.py3 filter_report.py custom_http_server.py; do
 if [ -f "$PY_SCRIPT" ]; then
  SHEBANG=$(head -n1 "$PY_SCRIPT")
  if echo "$SHEBANG" | grep -qE '^#!/usr/bin/env python3\.[0-9]+'; then
   echo "  WARNING: $PY_SCRIPT has hardcoded Python version in shebang: $SHEBANG"
   echo "  This may fail on systems without that specific Python version."
   echo "  Consider using '#!/usr/bin/env python3' instead."
   exit 1
  fi
  echo "  $PY_SCRIPT - shebang OK"
 fi
done

# Check that Python scripts can be parsed (syntax check)
for PY_SCRIPT in upload.py3 filter_report.py custom_http_server.py; do
 if [ -f "$PY_SCRIPT" ]; then
  if ! python3 -m py_compile "$PY_SCRIPT" 2>/dev/null; then
   echo "  ERROR: $PY_SCRIPT has Python syntax errors"
   python3 -m py_compile "$PY_SCRIPT"
   exit 1
  fi
  echo "  $PY_SCRIPT - syntax OK"
 fi
done

# Check that required Python modules can be imported
echo "Checking required Python modules..."
PYTHON_MODULES_MISSING=""
for PY_MODULE in cgi cgitb zipfile re os sys; do
 if ! python3 -c "import $PY_MODULE" 2>/dev/null; then
  PYTHON_MODULES_MISSING="$PYTHON_MODULES_MISSING $PY_MODULE"
  echo "  $PY_MODULE - NOT FOUND"
 else
  echo "  $PY_MODULE - found"
 fi
done

# Optional but recommended modules
for PY_MODULE in lxml bs4; do
 if ! python3 -c "import $PY_MODULE" 2>/dev/null; then
  echo "  $PY_MODULE - NOT FOUND (optional but recommended)"
 else
  echo "  $PY_MODULE - found"
 fi
done

if [ -n "$PYTHON_MODULES_MISSING" ]; then
 echo ""
 echo "ERROR: Required Python modules are missing:$PYTHON_MODULES_MISSING"
 echo "For Python 3.13+, install legacy-cgi: pip install legacy-cgi"
 exit 1
fi

echo "Python scripts OK!"
echo ""

##################################################################
# Check bash scripts for syntax errors
##################################################################
echo "Checking bash scripts for syntax errors..."

for BASH_SCRIPT in autoprocess.sh combine_reports.sh wrapper.sh; do
 if [ -f "$BASH_SCRIPT" ]; then
  if ! bash -n "$BASH_SCRIPT" 2>/dev/null; then
   echo "  ERROR: $BASH_SCRIPT has bash syntax errors"
   bash -n "$BASH_SCRIPT"
   exit 1
  fi
  echo "  $BASH_SCRIPT - syntax OK"
 fi
done

echo "Bash scripts OK!"
echo ""

##################################################################
# Check that key scripts are executable
##################################################################
echo "Checking script permissions..."

for SCRIPT in autoprocess.sh combine_reports.sh wrapper.sh upload.py3 filter_report.py; do
 if [ -f "$SCRIPT" ]; then
  if [ ! -x "$SCRIPT" ]; then
   echo "  WARNING: $SCRIPT is not executable, fixing..."
   chmod +x "$SCRIPT"
  fi
  echo "  $SCRIPT - executable"
 fi
done

echo "Script permissions OK!"
echo ""

##################################################################

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

# Debug: verify the symlink was created correctly
echo "DEBUG: Checking upload.py symlink..."
ls -la upload.py upload.py3
file upload.py
if [ -L upload.py ]; then
 echo "DEBUG: upload.py is a symlink pointing to: $(readlink upload.py)"
else
 echo "DEBUG: WARNING - upload.py is NOT a symlink!"
fi

# Create data directory
if [ ! -d uploads ];then
 mkdir "uploads" || exit 1
fi
cd "uploads" || exit 1
UPLOADS_DIR="$PWD"

# Install VaST if it was not installed before
if [ ! -d vast ];then
 git clone --depth 1 https://github.com/kirxkirx/vast.git || exit 1
 cd vast || exit 1
 make || exit 1
else
 cd vast || exit 1
 # Fetch and check if there are updates
 echo "Checking if VaST needs an update"
 if LANG=C git pull 2>&1 | grep --quiet 'Updating' ;then
  echo "Running make..."
  make || exit 1
 else
  echo "Repository is already up-to-date. No action needed."
 fi
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
  kill $STHTTPD_SERVER_PID $(cat "$UPLOADS_DIR/sthttpd_http_server.pid") 2>/dev/null
  echo "Logs of the sthttpd HTTP server..."
  cat "$UPLOADS_DIR/sthttpd_http_server.log"
  rm -fv "$UPLOADS_DIR/sthttpd_http_server.log" 
  if [ -f "$UPLOADS_DIR/sthttpd_http_server.pid" ];then
   rm -fv "$UPLOADS_DIR/sthttpd_http_server.pid"
  fi
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
 git clone --depth 1 https://github.com/blueness/sthttpd.git
 if [ $? -ne 0 ];then
  echo "$0 test error: cannot git clone sthttpd"
  exit 1
 fi
 cd sthttpd || exit 1
 ./autogen.sh || exit 1
 # Detect WSL and disable mmap if running under it
 # WSL2 has known issues with mmap that cause zero-byte responses
 # See: https://github.com/microsoft/WSL/issues/10103
 if grep -qi 'microsoft\|wsl' /proc/version 2>/dev/null; then
  echo "Detected WSL - building sthttpd with mmap disabled"
  CFLAGS="-UHAVE_MMAP" ./configure || exit 1
 else
  ./configure || exit 1
 fi
 make || exit 1
 if [ ! -x src/thttpd ];then
  echo "$0 test error: src/thttpd was not created"
  exit 1
 fi
fi

echo "Run sthttpd"
# Go back to the work directory
cd "$SCRIPTDIR" || exit 1

# Debug: Check CGI script setup before starting sthttpd
echo "DEBUG: Pre-sthttpd CGI diagnostics..."
echo "DEBUG: Current directory: $PWD"
echo "DEBUG: Files in current directory:"
ls -la upload.py upload.py3 2>&1
echo "DEBUG: File types:"
file upload.py upload.py3
echo "DEBUG: Symlink target:"
readlink -f upload.py
echo "DEBUG: First line (shebang) of upload.py3:"
head -n1 upload.py3
echo "DEBUG: Checking if python3 is in PATH:"
which python3
echo "DEBUG: Checking upload.py3 permissions:"
stat upload.py3
echo "DEBUG: Checking if upload.py3 is executable:"
if [ -x upload.py3 ]; then
 echo "DEBUG: upload.py3 IS executable"
else
 echo "DEBUG: upload.py3 is NOT executable - fixing..."
 chmod +x upload.py3
fi
echo "DEBUG: End of pre-sthttpd diagnostics"

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
# Use "**.py" pattern to match all Python scripts as CGI (more flexible for testing)
# On WSL2, add -u root to prevent user switching which causes CGI to fail silently
STHTTPD_EXTRA_ARGS=""
if grep -qi 'microsoft\|wsl' /proc/version 2>/dev/null; then
 echo "Detected WSL - running sthttpd without user switching to fix CGI execution"
 STHTTPD_EXTRA_ARGS="-u root"
fi
sthttpd/src/thttpd -nos -p "$UNMW_FREE_PORT" -d "$PWD" -c "**.py" $STHTTPD_EXTRA_ARGS -l "$UPLOADS_DIR/sthttpd_http_server.log" -i "$UPLOADS_DIR/sthttpd_http_server.pid" &
STHTTPD_SERVER_PID=$!
# STHTTPD_SERVER_PID=$! will work only if the process was started in the background with &
echo "sthttpd PID after starting it is $STHTTPD_SERVER_PID"
# Nice try, but thttpd will change its PID


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
if ! curl --silent --show-error "http://localhost:$UNMW_FREE_PORT/" 2>/dev/null | grep --quiet 'uploads/' ;then
 echo "$0 test error: something is wrong with the HTTP server"
 exit 1
fi
# Check the results of the previous manual run (redirect stderr to suppress broken pipe warnings)
if ! curl --silent --show-error "http://localhost:$UNMW_FREE_PORT/$RESULTS_DIR_FROM_URL__MANUALRUN" 2>/dev/null | grep --quiet 'V0615 Vul' ;then
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

# Debug: Test CGI execution with a simple test script first
echo "DEBUG: Creating a simple test CGI script..."
cat > "$SCRIPTDIR/test_cgi.py" << 'TESTCGI'
#!/usr/bin/env python3
print("Content-Type: text/plain")
print("")
print("CGI TEST OK")
TESTCGI
chmod +x "$SCRIPTDIR/test_cgi.py"

echo "DEBUG: Testing simple CGI script..."
test_cgi_response=$(curl --silent --show-error -w "\nHTTP_CODE:%{http_code}\nSIZE:%{size_download}" "http://localhost:$UNMW_FREE_PORT/test_cgi.py" 2>&1)
echo "DEBUG: test_cgi.py response:"
echo "$test_cgi_response"
echo "DEBUG: sthttpd log after test_cgi.py:"
cat "$UPLOADS_DIR/sthttpd_http_server.log" 2>/dev/null | tail -n3

# Also check directory listing size for comparison
echo "DEBUG: Checking directory listing size..."
dir_listing_size=$(curl --silent --show-error -w "%{size_download}" -o /dev/null "http://localhost:$UNMW_FREE_PORT/" 2>&1)
echo "DEBUG: Directory listing size: $dir_listing_size bytes"

# Debug: Test CGI execution before upload
echo "DEBUG: Testing if CGI script is accessible..."
echo "DEBUG: Attempting GET request to upload.py:"
curl_debug_response=$(curl --silent --show-error -w "\nHTTP_CODE:%{http_code}\nSIZE:%{size_download}" "http://localhost:$UNMW_FREE_PORT/upload.py" 2>&1)
echo "DEBUG: GET /upload.py response info:"
echo "$curl_debug_response" | tail -n2
echo "DEBUG: sthttpd log after GET:"
cat "$UPLOADS_DIR/sthttpd_http_server.log" 2>/dev/null | tail -n5

echo "DEBUG: Now attempting POST upload..."
# Save response to file first to diagnose capture issues
curl --max-time 600 --silent --show-error -w "\nHTTP_CODE:%{http_code}\nSIZE:%{size_download}" -X POST -F 'file=@NMW__NovaVul24_Stas__WebCheck__NotReal.zip' -F 'workstartemail=' -F 'workendemail=' "http://localhost:$UNMW_FREE_PORT/upload.py" -o /tmp/upload_response.txt 2>/tmp/upload_stderr.txt
curl_exit_code=$?
echo "DEBUG: curl exit code: $curl_exit_code"
echo "DEBUG: curl stderr:"
cat /tmp/upload_stderr.txt
echo "DEBUG: Response file size:"
ls -la /tmp/upload_response.txt 2>&1
echo "DEBUG: Response file first 500 chars:"
head -c 500 /tmp/upload_response.txt 2>/dev/null
echo ""
echo "DEBUG: sthttpd log after POST:"
cat "$UPLOADS_DIR/sthttpd_http_server.log" 2>/dev/null | tail -n5

results_server_reply=$(cat /tmp/upload_response.txt 2>/dev/null)
if [ -z "$results_server_reply" ];then
 echo "$0 test error: empty HTTP server reply"
 echo "DEBUG: Full sthttpd log:"
 cat "$UPLOADS_DIR/sthttpd_http_server.log" 2>/dev/null
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
if curl --silent --show-error "$results_url" | grep --quiet 'out of disk space' ;then
 echo "$0 test error: out of disk space"
 exit 1
fi
#
if ! curl --silent --show-error "$results_url" | grep --quiet 'V0615 Vul' ;then
 echo "$0 test error: failed to get web run results page via the HTTP server"
 exit 1
else
 echo "V0615 Vul is found in HTTP-uploaded results"
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
if curl --silent --show-error "$results_url" | grep --quiet 'out of disk space' ;then
 echo "$0 test error: out of disk space"
 exit 1
fi
#
if ! curl --silent --show-error "$results_url" | grep --quiet 'ERROR: too few refereence images for the field Vul8' ;then
 echo "$0 test error: failed to get web run results page via the HTTP server"
 exit 1
else
 echo "Expected error message is found in HTTP-uploaded results"
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
# Make sure we are not looking at filtered list that may not contain known variable or fail without bs4
LATEST_COMBINED_HTML_REPORT=$(ls -t *_evening_* *_morning_* 2>/dev/null | grep -v -e 'summary' -e '_filtered' | head -n 1)
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

LATEST_COMBINED_HTML_REPORT_FILTERED=$(ls -t *_evening_* *_morning_* 2>/dev/null | grep -v -e 'summary' -e '_filtered' | head -n 1)
if [ -z "$LATEST_COMBINED_HTML_REPORT_FILTERED" ];then
 echo "$0 test error: empty LATEST_COMBINED_HTML_REPORT_FILTERED"
 exit 1
else
 echo "The latest combined report is:"
 ls -lh "$LATEST_COMBINED_HTML_REPORT_FILTERED"
 if [ ! -f "$LATEST_COMBINED_HTML_REPORT_FILTERED" ];then
  echo "$0 test error: no such file $LATEST_COMBINED_HTML_REPORT_FILTERED"
  exit 1
 fi
 if [ ! -s "$LATEST_COMBINED_HTML_REPORT_FILTERED" ];then
  echo "$0 test error: empty file $LATEST_COMBINED_HTML_REPORT_FILTERED"
  exit 1
 fi
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
 echo "The latest processing summary is:"
 ls -lh "$LATEST_PROCESSING_SUMMARY_LOG"
fi

# Check that required columns exist in the header
if ! grep --quiet '<th>Field</th>' "$LATEST_PROCESSING_SUMMARY_LOG" ;then
 echo "$0 test error: cannot find 'Field' column header in $LATEST_PROCESSING_SUMMARY_LOG"
 exit 1
fi
if ! grep --quiet '<th>Preview</th>' "$LATEST_PROCESSING_SUMMARY_LOG" ;then
 echo "$0 test error: cannot find 'Preview' column header in $LATEST_PROCESSING_SUMMARY_LOG"
 exit 1
fi
if ! grep --quiet '<th>Status</th>' "$LATEST_PROCESSING_SUMMARY_LOG" ;then
 echo "$0 test error: cannot find 'Status' column header in $LATEST_PROCESSING_SUMMARY_LOG"
 exit 1
fi
if ! grep --quiet '<th>mag.lim.</th>' "$LATEST_PROCESSING_SUMMARY_LOG" ;then
 echo "$0 test error: cannot find 'mag.lim.' column header in $LATEST_PROCESSING_SUMMARY_LOG"
 exit 1
fi
if ! grep --quiet '<th>FWHM(pix)</th>' "$LATEST_PROCESSING_SUMMARY_LOG" ;then
 echo "$0 test error: cannot find 'FWHM(pix)' column header in $LATEST_PROCESSING_SUMMARY_LOG"
 exit 1
fi
echo "All required column headers found in summary"

# Check for Vul3 row with OK status
if ! grep 'Vul3' "$LATEST_PROCESSING_SUMMARY_LOG" | grep --quiet 'OK' ;then
 echo "$0 test error: cannot find Vul3 OK in LATEST_PROCESSING_SUMMARY_LOG=$LATEST_PROCESSING_SUMMARY_LOG"
 exit 1
else
 echo "Found Vul3 OK in $LATEST_PROCESSING_SUMMARY_LOG"
fi

# Check for Vul8 row with ERROR status
if ! grep 'Vul8' "$LATEST_PROCESSING_SUMMARY_LOG" | grep --quiet 'ERROR' ;then
 echo "$0 test error: cannot find Vul8 ERROR in LATEST_PROCESSING_SUMMARY_LOG=$LATEST_PROCESSING_SUMMARY_LOG"
 exit 1
else
 echo "Found Vul8 ERROR in $LATEST_PROCESSING_SUMMARY_LOG"
fi

# Check that Preview column points to existing valid PNG file for ALL Vul3 OK rows
VUL3_PREVIEW_PNG_COUNT=0
while IFS= read -r VUL3_PREVIEW_PNG; do
 if [ -z "$VUL3_PREVIEW_PNG" ];then
  echo "$0 test error: cannot extract Preview PNG path from a Vul3 OK row"
  exit 1
 fi
 if [ ! -f "$VUL3_PREVIEW_PNG" ];then
  echo "$0 test error: Preview PNG file does not exist: $VUL3_PREVIEW_PNG"
  exit 1
 fi
 if ! file "$VUL3_PREVIEW_PNG" | grep --quiet 'PNG image' ;then
  echo "$0 test error: not a valid PNG image: $VUL3_PREVIEW_PNG"
  file "$VUL3_PREVIEW_PNG"
  exit 1
 fi
 echo "Preview PNG valid for Vul3: $VUL3_PREVIEW_PNG"
 VUL3_PREVIEW_PNG_COUNT=$((VUL3_PREVIEW_PNG_COUNT + 1))
done < <(grep 'Vul3' "$LATEST_PROCESSING_SUMMARY_LOG" | grep 'OK' | grep -o 'src="[^"]*\.png"' | sed 's/src="//;s/"//')
if [ "$VUL3_PREVIEW_PNG_COUNT" -eq 0 ];then
 echo "$0 test error: no Vul3 OK rows found for Preview PNG validation"
 exit 1
fi
echo "Validated $VUL3_PREVIEW_PNG_COUNT Preview PNG file(s) for Vul3 OK rows"

# Check FWHM value for Vul3 OK rows - should be empty or a float less than 10
# There may be multiple Vul3 rows, check all of them
VUL3_FWHM_VALID=0
while IFS= read -r VUL3_ROW; do
 # FWHM is in the 9th <td> column
 # Use sed to extract column content, handling nested HTML tags
 VUL3_FWHM=$(echo "$VUL3_ROW" | sed 's/<\/td><td>/\n/g' | sed 's/<[^>]*>//g' | sed -n '9p' | tr -d ' ')
 if [ -z "$VUL3_FWHM" ];then
  echo "FWHM value for a Vul3 row is empty (acceptable)"
  VUL3_FWHM_VALID=1
 elif echo "$VUL3_FWHM" | grep -qE '^[0-9]+\.?[0-9]*$' ;then
  if echo "$VUL3_FWHM" | awk '{exit ($1 >= 10 ? 0 : 1)}' ;then
   echo "$0 test error: FWHM value '$VUL3_FWHM' is >= 10"
   exit 1
  fi
  echo "FWHM value for Vul3 is valid: $VUL3_FWHM"
  VUL3_FWHM_VALID=1
 else
  echo "$0 test error: FWHM value '$VUL3_FWHM' is not a valid number"
  exit 1
 fi
done < <(grep 'Vul3' "$LATEST_PROCESSING_SUMMARY_LOG" | grep 'OK')
if [ "$VUL3_FWHM_VALID" -eq 0 ];then
 echo "$0 test error: no Vul3 OK rows found for FWHM validation"
 exit 1
fi

# Check mag.lim. value for Vul3 OK rows - should be empty or a float (typically 10-20)
while IFS= read -r VUL3_ROW; do
 # mag.lim. is in the 8th <td> column
 # Use sed to extract column content, handling nested HTML tags
 VUL3_MAGLIM=$(echo "$VUL3_ROW" | sed 's/<\/td><td>/\n/g' | sed 's/<[^>]*>//g' | sed -n '8p' | tr -d ' ')
 if [ -n "$VUL3_MAGLIM" ]; then
  if ! echo "$VUL3_MAGLIM" | grep -qE '^[0-9]+\.?[0-9]*$' ; then
   echo "$0 test error: mag.lim. value '$VUL3_MAGLIM' is not a valid number (got text instead?)"
   exit 1
  fi
  echo "mag.lim. value for Vul3 is valid: $VUL3_MAGLIM"
 else
  echo "mag.lim. value for Vul3 is empty (acceptable for error rows)"
 fi
done < <(grep 'Vul3' "$LATEST_PROCESSING_SUMMARY_LOG" | grep 'OK')

# Check Pointing.Offset value for Vul3 OK rows - should be a float or "ERROR"
while IFS= read -r VUL3_ROW; do
 # Pointing.Offset is in the 7th <td> column
 # Use sed to extract column content, handling nested HTML tags
 VUL3_OFFSET=$(echo "$VUL3_ROW" | sed 's/<\/td><td>/\n/g' | sed 's/<[^>]*>//g' | sed -n '7p' | tr -d ' ')
 if [ -n "$VUL3_OFFSET" ]; then
  if [ "$VUL3_OFFSET" = "ERROR" ]; then
   echo "Pointing.Offset value for Vul3 is ERROR (acceptable)"
  elif echo "$VUL3_OFFSET" | grep -qE '^[0-9]+\.?[0-9]*$' ; then
   echo "Pointing.Offset value for Vul3 is valid: $VUL3_OFFSET"
  else
   echo "$0 test error: Pointing.Offset value '$VUL3_OFFSET' is not a valid number or ERROR"
   exit 1
  fi
 fi
done < <(grep 'Vul3' "$LATEST_PROCESSING_SUMMARY_LOG" | grep 'OK')

echo "All numeric columns validated successfully"

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
 sleep 1  # Wait for 1 second before checking again
done
#
if curl --silent --show-error "$results_url" | grep --quiet 'out of disk space' ;then
 echo "$0 test error: out of disk space"
 exit 1
fi
#
if ! curl --silent --show-error "$results_url" | grep --quiet 'V0615 Vul' ;then
 echo "$0 test error: failed to get web run results page via the HTTP server"
 exit 1
else
 echo "V0615 Vul is found in HTTP-uploaded results"
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

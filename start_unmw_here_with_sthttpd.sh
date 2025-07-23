#!/usr/bin/env bash

# Exit if the script is run via a CGI request
if [[ -n "$REQUEST_METHOD" ]]; then
 echo "This script cannot be run via a web request."
 exit 1
fi

# Check for required dependencies
command -v git &> /dev/null || { echo "ERROR: git is required but not installed."; exit 1; }
command -v make &> /dev/null || { echo "ERROR: make is required but not installed."; exit 1; }
command -v curl &> /dev/null || { echo "ERROR: curl is required but not installed."; exit 1; }
command -v gcc &> /dev/null || { echo "ERROR: gcc is required but not installed."; exit 1; }

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

# change to the work directory
SCRIPTDIR=$(dirname "$(readlink -f "$0")")
cd "$SCRIPTDIR" || exit 1

echo "=========================================="
echo "UNMW Server Setup"
echo "=========================================="

echo "Choose HTTP server:"
echo "1) sthttpd (faster, but may have compatibility issues with newer GCC)"
echo "2) Python HTTP server (more compatible, easier to debug)"
echo -n "Enter choice (1 or 2) [default: 1]: "
read -r SERVER_CHOICE

if [ "$SERVER_CHOICE" = "2" ]; then
 USE_PYTHON_SERVER=1
 echo "Using Python HTTP server"
else
 USE_PYTHON_SERVER=0
 echo "Using sthttpd"
fi

# Find a free port
UNMW_FREE_PORT=$(get_free_port_for_http_server)
if [[ $? -eq 0 ]]; then
    echo "Found free port for HTTP server: $UNMW_FREE_PORT"
else
    echo "Failed to find a free port."
    exit 1
fi

# sthttpd has issues with environment variables, so we need port 8080 specifically
if [ "$USE_PYTHON_SERVER" != "1" ] && [ "$UNMW_FREE_PORT" != "8080" ]; then
 echo "WARNING: sthttpd works best on port 8080, but that port is not free."
 echo "The server will run on port $UNMW_FREE_PORT but some functionality may be limited."
 echo "Press Ctrl+C to cancel or Enter to continue..."
 read
fi

# Check if local_config.sh already exists
if [ -f local_config.sh ]; then
 echo "WARNING: local_config.sh already exists!"
 echo "This script will create a backup and overwrite it."
 echo "Press Ctrl+C to cancel or Enter to continue..."
 read
 cp local_config.sh local_config.sh.backup.$(date +%Y%m%d_%H%M%S)
 echo "Backed up existing local_config.sh"
fi

# Create uploads directory if it doesn't exist
if [ ! -d uploads ]; then
 mkdir "uploads" || exit 1
 echo "Created uploads directory"
fi
cd "uploads" || exit 1
UPLOADS_DIR="$PWD"

# Install VaST if it was not installed before
if [ ! -d vast ]; then
 echo "Installing VaST..."
 git clone --depth 1 https://github.com/kirxkirx/vast.git || exit 1
 cd vast || exit 1
 echo "Compiling VaST..."
 make || exit 1
else
 cd vast || exit 1
 # Fetch and check if there are updates
 echo "Checking if VaST needs an update..."
 if LANG=C git pull 2>&1 | grep --quiet 'Updating' ; then
  echo "Running make..."
  make || exit 1
 else
  echo "VaST repository is already up-to-date."
 fi
fi

# Update offline catalogs
echo "Updating VaST offline catalogs (this may take a few minutes)..."
lib/update_offline_catalogs.sh all || exit 1
VAST_INSTALL_DIR="$PWD"
echo "VaST is ready for work at: $VAST_INSTALL_DIR"

# Go back to script directory
cd "$SCRIPTDIR" || exit 1

# Create reference_images directory
REFERENCE_IMAGES_DIR="$UPLOADS_DIR/reference_images"
if [ ! -d "$REFERENCE_IMAGES_DIR" ]; then
 mkdir -p "$REFERENCE_IMAGES_DIR" || exit 1
 echo "Created reference images directory: $REFERENCE_IMAGES_DIR"
else
 echo "Reference images directory already exists: $REFERENCE_IMAGES_DIR"
fi

# Create local_config.sh
cat > local_config.sh << EOF
# Main configuration parameters
export IMAGE_DATA_ROOT="$UPLOADS_DIR"
export DATA_PROCESSING_ROOT="$UPLOADS_DIR"
export VAST_REFERENCE_COPY="$UPLOADS_DIR/vast"

# Use environment variable for port if available (Python server), otherwise hardcode
if [ -n "\$UNMW_FREE_PORT" ]; then
 export URL_OF_DATA_PROCESSING_ROOT="http://localhost:\$UNMW_FREE_PORT/uploads"
else
 export URL_OF_DATA_PROCESSING_ROOT="http://localhost:$UNMW_FREE_PORT/uploads"
fi

export REFERENCE_IMAGES="$REFERENCE_IMAGES_DIR"

# Specify plate-solve service
# as local (requires locally installed astrometry.net code and indexes)
#ASTROMETRYNET_LOCAL_OR_REMOTE="local"
# or remote
#export ASTROMETRYNET_LOCAL_OR_REMOTE="remote"
#export FORCE_PLATE_SOLVE_SERVER="scan.sai.msu.ru"

# Wait to start processing until system parameters are below these values
#export MAX_IOWAIT_PERCENT=3.0                                                                                                                                
#export MAX_CPU_TEMP_C=65.0
#export MAX_SYSTEM_LOAD=3.0

# Raise warning in the processing log on low disk space
# 'util/transients/transient_factory_test31.sh' script will look for that variable
# 2 GB = 2 * 1024 * 1024 KB
#export WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB=2097152

# set this variable to indicate this file has been sourced
# (can be checked in order not to source it twice)
export UNMW_LOCAL_CONFIG_SOURCED=yes
EOF

echo "Created local_config.sh"

# Link the python3 version of the upload handler code
ln -sf upload.py3 upload.py
echo "Created upload.py symlink"

# Set up web interface directory
if [ -d move_to_htdocs ]; then
 echo "Web interface files already available in move_to_htdocs/"
else
 echo "WARNING: move_to_htdocs directory not found. Upload interface may not work."
fi

# Make sure we have the Python HTTP server file available (fallback option)
if [ ! -f custom_http_server.py ]; then
 echo "Creating custom_http_server.py..."
 cat > custom_http_server.py << 'EOF'
#!/usr/bin/env python3

import os
import sys # for sys.exit()
from http.server import HTTPServer, CGIHTTPRequestHandler

# Exit if the script is run via a CGI request
if "REQUEST_METHOD" in os.environ:
    print("This script cannot be run via a web request.", file=sys.stderr)
    sys.exit(1)

class CustomCGIHTTPRequestHandler(CGIHTTPRequestHandler):
    cgi_directories = ["/cgi-bin"]  # Keep the default directories

    def is_cgi(self):
        # Allow specific files like /upload.py to be treated as CGI
        if self.path == "/upload.py":
            self.cgi_info = "", self.path[1:]  # Split path into dir and script
            return True
        return super().is_cgi()
        
    def translate_path(self, path):
        # Get the initial translation (without resolving symlinks)
        untranslated_path = super().translate_path(path)
        
        # Resolve symlinks
        return os.path.realpath(untranslated_path)

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', '-b', default='', metavar='ADDRESS',
                        help='Specify alternate bind address [default: all interfaces]')
    parser.add_argument('port', action='store', default=8080, type=int, nargs='?',
                        help='Specify alternate port [default: 8080]')
    args = parser.parse_args()
    
    # Set the environment variable needed for period search scripts - lk
    os.environ['HTTP_HOST'] = 'kirx.net/ticaariel'
    
    server_address = (args.bind, args.port)
    httpd = HTTPServer(server_address, CustomCGIHTTPRequestHandler)
    print(f"Serving HTTP on {args.bind} port {args.port} (http://{args.bind}:{args.port}/) ...")
    httpd.serve_forever()
EOF
 chmod +x custom_http_server.py
fi

# Install sthttpd if needed (and if not using Python server)
if [ "$USE_PYTHON_SERVER" != "1" ]; then
 if [ ! -d sthttpd ]; then
  echo "Installing sthttpd..."
  git clone --depth 1 https://github.com/blueness/sthttpd.git
  if [ $? -ne 0 ]; then
   echo "ERROR: cannot git clone sthttpd"
   exit 1
  fi
  cd sthttpd || exit 1
  
  # Fix compatibility with newer GCC versions
  echo "Patching sthttpd for newer GCC compatibility..."
  sed -i 's/#include <signal.h>/#define _GNU_SOURCE\n#include <signal.h>/' src/thttpd.c
  
  ./autogen.sh || exit 1
  ./configure || exit 1
  
  # Try to compile with more permissive flags
  if ! make CFLAGS="-g -O2 -Wno-implicit-function-declaration -D_GNU_SOURCE"; then
   echo "ERROR: sthttpd compilation failed even with compatibility flags"
   echo "This may be due to your GCC version being too new for sthttpd."
   echo "Would you like to use the Python HTTP server instead? (y/n)"
   read -r response
   if [[ "$response" =~ ^[Yy]$ ]]; then
    cd "$SCRIPTDIR" || exit 1
    rm -rf sthttpd
    USE_PYTHON_SERVER=1
   else
    exit 1
   fi
  else
   if [ ! -x src/thttpd ]; then
    echo "ERROR: src/thttpd was not created"
    exit 1
   fi
   cd "$SCRIPTDIR" || exit 1
   echo "sthttpd installed successfully"
  fi
 else
  echo "sthttpd is already installed"
 fi
fi

# Check that sthttpd is properly installed (unless we're using Python server)
if [ -z "$USE_PYTHON_SERVER" ]; then
 if [ ! -x sthttpd/src/thttpd ]; then
  echo "ERROR: sthttpd/src/thttpd was not found"
  exit 1
 fi
fi

# Check if server is already running
if [ -f "$UPLOADS_DIR/sthttpd_http_server.pid" ]; then
 OLD_PID=$(cat "$UPLOADS_DIR/sthttpd_http_server.pid")
 if kill -0 "$OLD_PID" 2>/dev/null; then
  echo "WARNING: sthttpd server appears to be already running (PID: $OLD_PID)"
  echo "Kill the old server first or choose a different port."
  exit 1
 else
  echo "Removing stale PID file"
  rm -f "$UPLOADS_DIR/sthttpd_http_server.pid"
 fi
fi

if [ -f "$UPLOADS_DIR/python_http_server.pid" ]; then
 OLD_PID=$(cat "$UPLOADS_DIR/python_http_server.pid")
 if kill -0 "$OLD_PID" 2>/dev/null; then
  echo "WARNING: Python server appears to be already running (PID: $OLD_PID)"
  echo "Kill the old server first or choose a different port."
  exit 1
 else
  echo "Removing stale PID file"
  rm -f "$UPLOADS_DIR/python_http_server.pid"
 fi
fi

# Function to clean up on script exit
cleanup() {
 echo ""
 echo "Shutting down UNMW server..."
 
 # Kill combine_reports background process and any running instances
 if [ -n "$COMBINE_REPORTS_PID" ] && [ "$COMBINE_REPORTS_PID" != "" ]; then
  echo "Stopping combine_reports background process (PID: $COMBINE_REPORTS_PID)..."
  kill -TERM $COMBINE_REPORTS_PID 2>/dev/null
  # Give it a moment to handle the signal
  sleep 2
  # Force kill if still running
  if kill -0 $COMBINE_REPORTS_PID 2>/dev/null; then
   kill -9 $COMBINE_REPORTS_PID 2>/dev/null
  fi
 fi
 
 # Kill any running combine_reports.sh processes
 echo "Stopping any running combine_reports.sh processes..."
 pkill -f "combine_reports.sh" 2>/dev/null
 
 # Kill sthttpd server
 if [ -f "$UPLOADS_DIR/sthttpd_http_server.pid" ]; then
  STHTTPD_PID=$(cat "$UPLOADS_DIR/sthttpd_http_server.pid")
  if kill -0 "$STHTTPD_PID" 2>/dev/null; then
   echo "Stopping sthttpd server (PID: $STHTTPD_PID)..."
   kill -TERM $STHTTPD_PID 2>/dev/null
   # Give it a moment to shut down gracefully
   sleep 2
   # If it's still running, force kill
   if kill -0 "$STHTTPD_PID" 2>/dev/null; then
    echo "Force killing sthttpd server..."
    kill -9 $STHTTPD_PID 2>/dev/null
   fi
  fi
  rm -f "$UPLOADS_DIR/sthttpd_http_server.pid"
 fi
 
 # Kill Python server if running
 if [ -f "$UPLOADS_DIR/python_http_server.pid" ]; then
  PYTHON_PID=$(cat "$UPLOADS_DIR/python_http_server.pid")
  if kill -0 "$PYTHON_PID" 2>/dev/null; then
   echo "Stopping Python server (PID: $PYTHON_PID)..."
   kill -TERM $PYTHON_PID 2>/dev/null
   sleep 2
   if kill -0 "$PYTHON_PID" 2>/dev/null; then
    kill -9 $PYTHON_PID 2>/dev/null
   fi
  fi
  rm -f "$UPLOADS_DIR/python_http_server.pid"
 fi
 
 echo "UNMW server shutdown complete."
 exit 0
}

# Set up signal traps for graceful shutdown
trap cleanup INT TERM

# Start the appropriate server
if [ "$USE_PYTHON_SERVER" = "1" ]; then
 echo "Starting Python HTTP server on port $UNMW_FREE_PORT..."
 UNMW_FREE_PORT="$UNMW_FREE_PORT" python3 custom_http_server.py "$UNMW_FREE_PORT" > "$UPLOADS_DIR/python_http_server.log" 2>&1 &
 SERVER_PID=$!
 echo $SERVER_PID > "$UPLOADS_DIR/python_http_server.pid"
 SERVER_TYPE="Python"
 SERVER_LOG="$UPLOADS_DIR/python_http_server.log"
else
 echo "Starting sthttpd server on port $UNMW_FREE_PORT..."
 sthttpd/src/thttpd -nos -p "$UNMW_FREE_PORT" -d "$PWD" -c "upload.py" -l "$UPLOADS_DIR/sthttpd_http_server.log" -i "$UPLOADS_DIR/sthttpd_http_server.pid" &
 SERVER_TYPE="sthttpd"
 SERVER_LOG="$UPLOADS_DIR/sthttpd_http_server.log"
fi

# Give the server some time to start
sleep 3

# Check if the server is running
if [ "$USE_PYTHON_SERVER" = "1" ]; then
 if ! ps -ef | grep python3 | grep custom_http_server.py | grep -v grep > /dev/null; then
  echo "ERROR: Python HTTP server failed to start"
  echo "Check the log file: $SERVER_LOG"
  exit 1
 fi
else
 if ! ps -ef | grep thttpd | grep -v grep > /dev/null; then
  echo "ERROR: sthttpd server failed to start"
  echo "Check the log file: $SERVER_LOG"
  exit 1
 fi
fi

# Test if the server is responding
if ! curl --silent --connect-timeout 5 "http://localhost:$UNMW_FREE_PORT/" | grep --quiet 'uploads/' ; then
 echo "ERROR: $SERVER_TYPE server is not responding correctly"
 echo "Check the log file: $SERVER_LOG"
 exit 1
fi

# Start combine_reports.sh background process
if [ -x "./combine_reports.sh" ]; then
 echo "Starting combine_reports.sh background process (runs every 2 minutes)..."
 (
  # Set up signal handling for the background process
  cleanup_reports() {
   echo "$(date): Stopping combine_reports background process" >> "$UPLOADS_DIR/combine_reports_background.log"
   # Kill any running combine_reports.sh process
   pkill -f "combine_reports.sh" 2>/dev/null
   exit 0
  }
  trap cleanup_reports INT TERM
  
  cd "$SCRIPTDIR" || exit 1
  echo "$(date): Started combine_reports background process" >> "$UPLOADS_DIR/combine_reports_background.log"
  
  while true; do
   # Use a signal-interruptible sleep (multiple short sleeps instead of one long one)
   for i in $(seq 1 24); do
    sleep 5
    # Check if we should exit (this allows quicker response to signals)
    if ! kill -0 $ 2>/dev/null; then
     exit 0
    fi
   done
   
   if [ -x "./combine_reports.sh" ]; then
    echo "$(date): Running combine_reports.sh" >> "$UPLOADS_DIR/combine_reports_background.log"
    ./combine_reports.sh >> "$UPLOADS_DIR/combine_reports_background.log" 2>&1 &
    COMBINE_REPORTS_CHILD_PID=$!
    
    # Wait for combine_reports.sh to complete, but allow interruption
    while kill -0 $COMBINE_REPORTS_CHILD_PID 2>/dev/null; do
     sleep 1
    done
   fi
  done
 ) &
 COMBINE_REPORTS_PID=$!
else
 echo "WARNING: combine_reports.sh not found or not executable. Skipping background reports."
 COMBINE_REPORTS_PID=""
fi

echo ""
echo "======================================================================="
echo "SUCCESS! UNMW server ($SERVER_TYPE) is now running on port $UNMW_FREE_PORT"
echo "======================================================================="
echo ""
if [ -d move_to_htdocs ]; then
 echo "Upload interface: http://localhost:$UNMW_FREE_PORT/move_to_htdocs/"
else
 echo "Direct upload endpoint: http://localhost:$UNMW_FREE_PORT/upload.py"
fi
echo "Results will appear at: http://localhost:$UNMW_FREE_PORT/uploads/"
echo ""
echo "IMPORTANT: Reference images directory created at:"
echo "  $REFERENCE_IMAGES_DIR"
echo ""
echo "*** YOU MUST COPY YOUR REFERENCE IMAGES TO THIS DIRECTORY ***"
echo "*** BEFORE PROCESSING ANY DATA! ***"
echo ""
echo "Reference images should be:"
echo "  - In FITS format (.fts, .fits, or .fit extension)"
echo "  - Named to match your observing fields"
echo "  - Properly calibrated and aligned"
echo ""
echo "Server information:"
echo "  Server type: $SERVER_TYPE"
echo "  Server logs: $SERVER_LOG"
if [ "$USE_PYTHON_SERVER" = "1" ]; then
 echo "  Server PID file: $UPLOADS_DIR/python_http_server.pid"
else
 echo "  Server PID file: $UPLOADS_DIR/sthttpd_http_server.pid"
fi
echo "  VaST installation: $VAST_INSTALL_DIR"
if [ -n "$COMBINE_REPORTS_PID" ]; then
 echo "  combine_reports.sh PID: $COMBINE_REPORTS_PID"
 echo "  combine_reports.sh log: $UPLOADS_DIR/combine_reports_background.log"
fi
echo ""
echo "To stop the server and all background processes:"
echo "  Press Ctrl+C or send SIGTERM to this process"
echo ""
echo "To monitor server activity:"
echo "  tail -f $SERVER_LOG"
echo "  tail -f $UPLOADS_DIR/autoprocess.txt"
if [ -n "$COMBINE_REPORTS_PID" ]; then
 echo "  tail -f $UPLOADS_DIR/combine_reports_background.log"
fi
echo ""
echo "The server will continue running until you stop it with Ctrl+C."
if [ -n "$COMBINE_REPORTS_PID" ]; then
 echo "combine_reports.sh runs automatically every 2 minutes."
fi
echo "You can now upload .zip or .rar files containing FITS images for processing."
echo ""
echo "For production use, consider:"
echo "  - Installing astrometry.net locally for faster plate solving"
echo "  - Setting up proper firewall rules"
echo "  - Configuring email notifications in local_config.sh"
echo "======================================================================="
echo ""
echo "Server is running. Press Ctrl+C to stop gracefully."

# Keep the script running and wait for signals
while true; do
 sleep 10
 # Check if server is still running
 if [ "$USE_PYTHON_SERVER" = "1" ]; then
  if [ -f "$UPLOADS_DIR/python_http_server.pid" ]; then
   PYTHON_PID=$(cat "$UPLOADS_DIR/python_http_server.pid")
   if ! kill -0 "$PYTHON_PID" 2>/dev/null; then
    echo "ERROR: Python server has stopped unexpectedly"
    cleanup
   fi
  fi
 else
  if [ -f "$UPLOADS_DIR/sthttpd_http_server.pid" ]; then
   STHTTPD_PID=$(cat "$UPLOADS_DIR/sthttpd_http_server.pid")
   if ! kill -0 "$STHTTPD_PID" 2>/dev/null; then
    echo "ERROR: sthttpd server has stopped unexpectedly"
    cleanup
   fi
  fi
 fi
 
 # Check if combine_reports process is still running
 if [ -n "$COMBINE_REPORTS_PID" ] && [ "$COMBINE_REPORTS_PID" != "" ]; then
  if ! kill -0 "$COMBINE_REPORTS_PID" 2>/dev/null; then
   echo "WARNING: combine_reports background process has stopped. Restarting..."
   # Restart the combine_reports background process with signal handling
   (
    cleanup_reports() {
     echo "$(date): Stopping combine_reports background process" >> "$UPLOADS_DIR/combine_reports_background.log"
     pkill -f "combine_reports.sh" 2>/dev/null
     exit 0
    }
    trap cleanup_reports INT TERM
    
    cd "$SCRIPTDIR" || exit 1
    echo "$(date): Restarted combine_reports background process" >> "$UPLOADS_DIR/combine_reports_background.log"
    
    while true; do
     # Use signal-interruptible sleep
     for i in $(seq 1 24); do
      sleep 5
      if ! kill -0 $ 2>/dev/null; then
       exit 0
      fi
     done
     
     if [ -x "./combine_reports.sh" ]; then
      echo "$(date): Running combine_reports.sh" >> "$UPLOADS_DIR/combine_reports_background.log"
      ./combine_reports.sh >> "$UPLOADS_DIR/combine_reports_background.log" 2>&1 &
      COMBINE_REPORTS_CHILD_PID=$!
      
      while kill -0 $COMBINE_REPORTS_CHILD_PID 2>/dev/null; do
       sleep 1
      done
     fi
    done
   ) &
   COMBINE_REPORTS_PID=$!
   echo "Restarted combine_reports background process (PID: $COMBINE_REPORTS_PID)"
  fi
 fi
done

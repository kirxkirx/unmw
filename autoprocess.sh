#!/usr/bin/env bash

# shellcheck disable=SC2086,SC2181,SC2002,SC2162,SC2012,SC2009,SC2126,SC1091

#################################
# Set the safe locale that should be available on any POSIX system
LC_ALL=C
LANGUAGE=C
export LANGUAGE LC_ALL
#################################

# Check the flag that local_config.sh was already sourced
if [ -z "$UNMW_LOCAL_CONFIG_SOURCED" ];then
 # if not - source it
 LOCAL_CONFIG_FILE="$(dirname $(readlink -f "$0"))/local_config.sh"
 if [ -f "$LOCAL_CONFIG_FILE" ];then
  echo source "$LOCAL_CONFIG_FILE"
  source "$LOCAL_CONFIG_FILE"
 fi
fi


# Normally $IMAGE_DATA_ROOT $DATA_PROCESSING_ROOT $URL_OF_DATA_PROCESSING_ROOT are 
# exported in local_config.sh that is sourced by wrapper.sh
if [ -z "$IMAGE_DATA_ROOT" ] || [ -z "$DATA_PROCESSING_ROOT" ] || [ -z "$URL_OF_DATA_PROCESSING_ROOT" ];then
 # Manual setup at vast.sai.msu.ru: 
 # mount --bind /dataX/kirx/NMW_NG_rt3_autumn2019/web_upload /usr/lib/cgi-bin/unmw/uploads
 # mount --bind /dataX/kirx/NMW_NG_rt3_autumn2019/web_upload /var/www/unmw/uploads
 IMAGE_DATA_ROOT="/dataX/kirx/NMW_NG_rt3_autumn2019/web_upload"
 DATA_PROCESSING_ROOT="$IMAGE_DATA_ROOT"
 URL_OF_DATA_PROCESSING_ROOT="http://vast.sai.msu.ru/unmw/uploads"

 # Normally, all this should be sourced from local_config.sh
 if [ "$(hostname)" = "scan" ];then
  IMAGE_DATA_ROOT="/home/NMW_web_upload"
  DATA_PROCESSING_ROOT="/home/NMW_web_upload"
  URL_OF_DATA_PROCESSING_ROOT="http://scan.sai.msu.ru/unmw/uploads"
  # rar is in /opt/bin/
  PATH=$PATH:/opt/bin/
 fi
fi

if [ -z "$VAST_REFERENCE_COPY" ];then
 VAST_REFERENCE_COPY="$DATA_PROCESSING_ROOT"/vast
fi

if [ -z "$1" ];then
 echo "
Usage: 
 $0 path_to_archive_or_directory_containing_new_images

Examples:
 $0 /path/to/images.zip
or
 $0 /path/to/images.rar
or
 $0 /path/to/images/"
 exit 1
fi

INPUT_ZIP_ARCHIVE="$1"

UNIXSEC_START_TOTAL=$(date +%s)

## This function will set the random session key in attempt to avoid 
## file collisions if other instances of the script are running at the same time.
function set_session_key {
 local RANDOMFILE
 if [ -r /dev/urandom ];then
  RANDOMFILE=/dev/urandom
 elif [ -r /dev/random ];then
  RANDOMFILE=/dev/random
 else
  echo "ERROR: cannot find /dev/random" 
  RANDOMFILE=""
 fi
 local SESSION_KEY
 if [ "$RANDOMFILE" != "" ];then
  SESSION_KEY="$$"_$(tr -cd a-zA-Z0-9 < $RANDOMFILE | head -c 8)
 else
  SESSION_KEY="$$"
 fi
 echo "$SESSION_KEY"
}

function is_system_load_low {
 if [ ! -f /proc/loadavg ];then
  # test failed - assume things are fine
  return 0
 fi
 awk -v target=$MAX_SYSTEM_LOAD '{
         if ($1 < target) {
          exit 0
         } else {
          exit 1
         }
        }' /proc/loadavg
 return $?
}

function is_temperature_low {
 command -v sensors &> /dev/null 
 if [ $? -ne 0 ];then
  return 0
 fi
 sensors 2>&1 | grep --quiet 'No sensors'
 if [ $? -eq 0 ];then
  return 0
 fi
 # Every system seem to have its own way of reporting CPU temperature.
 # Most likely you'll need to tweak the expression below when installing the script on a new machine.
 TEMPERATURE=$(sensors 2> /dev/null | grep -e 'Package' -e 'Tctl:' -e 'temp1:' | head -n1 | awk -F'+' '{print $2}' | awk -F'.' '{print $1}')
 if [ -z "$TEMPERATURE" ];then
  return 0
 fi
 if [[ $TEMPERATURE =~ ^[0-9]+$ ]];then
  # The string is an integer number
  #
  # log the temperature value if asked to do so
  if [ -n "$1" ];then
   if [ "$1" = "log" ];then
    #echo "CPU temperature: $TEMPERATURE C"
    # temperature check to print the warning
    echo "$TEMPERATURE" |  awk -v target=$MAX_CPU_TEMP_C '{
         if ($1 < target) {
          printf("CPU temperature: %.0f C\n",$1)
         } else {
          printf("CPU temperature: %.0f C - WARNING\n",$1)
         }
        }'
   fi
  fi
  #
  # actual temperature check
  echo "$TEMPERATURE" |  awk -v target=$MAX_CPU_TEMP_C '{
         if ($1 < target) {
          exit 0
         } else {
          exit 1
         }
        }'
  return $?
 fi
 # The test didn't work after all - assume everything is fine
 return 0
}

function is_cpu_io_wait_low {
 # check if iostat is available
 command -v iostat &> /dev/null 
 if [ $? -ne 0 ];then
  return 0
 fi
 # check that the output format looks like what we expect
 iostat -c | grep 'avg-cpu' | awk '{print $5}' | grep --quiet 'iowait'
 if [ $? -ne 0 ];then
  return 0
 fi
 CPU_IOWAIT_PERCENT=$(iostat -c | grep -A1 'avg-cpu' | grep -v 'avg-cpu' | awk '{print $4}')
 if [ -z "$CPU_IOWAIT_PERCENT" ];then
  return 0
 fi
 if [[ $CPU_IOWAIT_PERCENT =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  # The string is a floating point number 
  echo "$CPU_IOWAIT_PERCENT" |  awk -v max_iowait=$MAX_IOWAIT_PERCENT '{
         if ($1 < max_iowait) {
          exit 0
         } else {
          exit 1
         }
        }'
  return $?
 fi
 # The test didn't work after all - assume everything is fine
 return 0
}

function check_sysrem_processes_are_not_too_many {
 # Get a list of all processes, filter by "util/sysrem", then count the lines.
 # The "-a" option to ps lists all processes. 
 # The "-x" option includes those without a controlling terminal, which could be relevant for some system processes.
 # The grep command filters this list to only include lines that contain "util/sysrem".
 # The -v option to grep excludes lines that contain "grep" itself to prevent counting the grep command as a process.
 # The wc command counts the number of lines.
 #
 # one util/sysrem process my get suck in a wating state (not sure why) and stop the whole system
 # so let's allow more than one util/sysrem process
 num_processes=$(ps ax | grep "util/sysrem" | grep -v grep | wc -l)
 if [ -z "$num_processes" ];then
  return 0
 fi
 if [[ $num_processes =~ ^[0-9]+$ ]];then
  # The string is an integer number
  echo "$num_processes" |  awk -v target=2 '{
         if ($1 < target) {
          exit 0
         } else {
          exit 1
         }
        }'
  return $?
 fi
 # The test didn't work after all - assume everything is fine
 return 0
}

function check_unrar_processes_are_not_too_many {
 # A running unrar process indicates that another processing instance has just started.
 # In this case we proabably don't want to start yet another one
 #
 # Get a list of all processes, filter by "util/sysrem", then count the lines.
 # The "-a" option to ps lists all processes. 
 # The "-x" option includes those without a controlling terminal, which could be relevant for some system processes.
 # The grep command filters this list to only include lines that contain "util/sysrem".
 # The -v option to grep excludes lines that contain "grep" itself to prevent counting the grep command as a process.
 # The wc command counts the number of lines.
 num_processes=$(ps ax | grep -e " unrar " -e " rar " | grep -v grep | wc -l)
 if [ -z "$num_processes" ];then
  return 0
 fi
 if [[ $num_processes =~ ^[0-9]+$ ]];then
  # The string is an integer number
  echo "$num_processes" |  awk -v target=3 '{
         if ($1 < target) {
          exit 0
         } else {
          exit 1
         }
        }'
  return $?
 fi
 # The test didn't work after all - assume everything is fine
 return 0
}

function wait_for_our_turn_to_start_processing {
 if [ -n "$AUTOPROCESS_NO_WAIT" ] && [ "$AUTOPROCESS_NO_WAIT" = "yes" ];then
  return 0
 fi

 # Set base delay
 DELAY=1
 MAX_WAIT_ITERATIONS=30
 #MAX_WAIT_ITERATIONS=12
 # The original idea was that DELAY^MAX_WAIT_ITERATIONS will be approximatelky the duration of the imaging session,
 # so by that time the new images will surely stop coming.

 # exponential backoff
 for WAIT_ITERATION in $(seq 1 $MAX_WAIT_ITERATIONS) ; do
  is_system_load_low && is_temperature_low && check_sysrem_processes_are_not_too_many && check_unrar_processes_are_not_too_many && is_cpu_io_wait_low
  if [ $? -eq 0 ]; then
   return 0
  else
   # Calculate current delay
   # system load changes on 1min timescale, so don't re-check too often as it may take time for the load to rise
   DELAY=$((DELAY * 2))
   DELAY_PLUS_RANDOM=$((DELAY + (RANDOM % 120 + 1)))
   if [ $DELAY_PLUS_RANDOM -gt 2000 ];then
    DELAY=$((RANDOM % 100 + 1))
    DELAY_PLUS_RANDOM=$DELAY
   fi
   echo "Sleeping for $DELAY_PLUS_RANDOM seconds (WAIT_ITERATION=$WAIT_ITERATION)"
   sleep $DELAY_PLUS_RANDOM
  fi 
 done
 
 # if exponential backoff didn't work - wait impatiently
 for WAIT_ITERATION in $(seq 1 1$MAX_WAIT_ITERATIONS) ; do
  is_system_load_low && is_temperature_low && check_sysrem_processes_are_not_too_many && check_unrar_processes_are_not_too_many && is_cpu_io_wait_low
  if [ $? -eq 0 ]; then
   return 0
  else
   # system load changes on 1min timescale, so don't re-check too often as it may take time for the load to rise
   DELAY=$(( RANDOM % 300 + 1 ))
   echo "Sleeping for $DELAY seconds (impatiently)"
   sleep $DELAY
  fi 
 done

 # If we are still here - wait for a random number of seconds then go
 RANDOM_NUMBER_OF_SECONDS=$(( RANDOM % 3600 + 1 ))
 echo "Sleeping for $RANDOM_NUMBER_OF_SECONDS seconds then going no matter what"
 sleep $RANDOM_NUMBER_OF_SECONDS

 return 0
}

# Note that the same function is found in util/transients/transient_factory_test31.sh
function check_free_space() {
    if [ -n "$1" ];then
     dir_to_check="$1"
    else
     dir_to_check="."
    fi
    
    if [ ! -d "$dir_to_check" ];then
     echo "WARNING from check_free_space(): $dir_to_check is not a directory"
     return 0
    fi


    # Check free space in the current directory
    local free_space_kb

    # Use 'df -k .' for portability across Linux, macOS, and FreeBSD
    free_space_kb=$(df -k "$dir_to_check" | awk 'NR==2 {print $4}')

    # Minimum required space in KB (600MB = 600 * 1024 KB)
    local required_space_kb_hardlimit=614400

    # soft limit for minimum required space in KB (2 GB = 2 * 1024 * 1024 KB)
    local required_space_kb_softlimit=2097152
    # Or change it to an externally set value if $WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB is set
    if [ -n "$WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB" ];then
     if [[ "$WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB" =~ ^[0-9]+$ ]] && [ "$WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB" -gt "$required_space_kb_hardlimit" ]; then
      required_space_kb_softlimit="$WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB"
     fi
    fi

    
    if [ "$free_space_kb" -ge "$required_space_kb_softlimit" ]; then
        echo "server $HOSTNAME has sufficient free disk space available: $((free_space_kb / 1024)) MB at $dir_to_check"
        return 0
    elif [ "$free_space_kb" -ge "$required_space_kb_hardlimit" ]; then
        echo "WARNING: server $HOSTNAME is low on disk space, only $((free_space_kb / 1024)) MB free at $dir_to_check"
        return 0
    else
        echo "ERROR: server $HOSTNAME is out of disk space, only $((free_space_kb / 1024)) MB free at $dir_to_check"
        return 1
    fi
}


# Check input
if [ -z "$INPUT_ZIP_ARCHIVE" ];then
 echo "ERROR: no input ZIP archive $INPUT_ZIP_ARCHIVE" 
 exit 1
fi

INPUT_DIR_NOT_ZIP_ARCHIVE=0
if [ -d "$INPUT_ZIP_ARCHIVE" ];then
 echo "The input is a directory $INPUT_ZIP_ARCHIVE"

 #
 # Figure out fits file extension for this dataset
 FITS_FILE_EXT=$(for POSSIBLE_FITS_FILE_EXT in fts fits fit ;do for IMGFILE in "$INPUT_ZIP_ARCHIVE"/*."$POSSIBLE_FITS_FILE_EXT" ;do if [ -f "$IMGFILE" ];then echo "$POSSIBLE_FITS_FILE_EXT"; break; fi ;done ;done)
 if [ -z "$FITS_FILE_EXT" ];then
  FITS_FILE_EXT="fts"
 fi
 export FITS_FILE_EXT
 echo "FITS_FILE_EXT=$FITS_FILE_EXT"
 #


 N_FITS_FILES=$(ls "$INPUT_ZIP_ARCHIVE"/*."$FITS_FILE_EXT" | wc -l)
 if [ $N_FITS_FILES -ge 2 ];then
  INPUT_DIR_NOT_ZIP_ARCHIVE=1
  echo "The input directory contains at lest 2 FITS files "
  ls "$INPUT_ZIP_ARCHIVE"/*."$FITS_FILE_EXT"
  INPUT_IMAGE_DIR_PATH_INSTEAD_OF_ZIP_ARCHIVE="$INPUT_ZIP_ARCHIVE"
 fi
fi

if [ $INPUT_DIR_NOT_ZIP_ARCHIVE -eq 0 ];then
 if [ ! -f "$INPUT_ZIP_ARCHIVE" ];then
  echo "ERROR: input ZIP archive $INPUT_ZIP_ARCHIVE does not exist" 
  exit 1
 fi
 if [ ! -s "$INPUT_ZIP_ARCHIVE" ];then
  echo "ERROR: input ZIP archive $INPUT_ZIP_ARCHIVE is empty" 
  exit 1
 fi
 if [ -d "$INPUT_ZIP_ARCHIVE" ];then
  echo "ERROR: input $INPUT_ZIP_ARCHIVE is a directory, not a ZIP archive" 
  exit 1
 fi
fi

if [ ! -d "$IMAGE_DATA_ROOT" ];then
 echo "ERROR: there is no image data directory $IMAGE_DATA_ROOT"
 exit 1
fi

if [ ! -d "$DATA_PROCESSING_ROOT" ];then
 echo "ERROR: there is no data processing directory $DATA_PROCESSING_ROOT"
 exit 1
fi

if [ ! -d "$VAST_REFERENCE_COPY" ];then
 echo "ERROR: cannot find VaST installation in directory $VAST_REFERENCE_COPY" 
 exit 1
fi



# Make up file names
SESSION_KEY=$(set_session_key) # or SESSION_KEY=`set_session_key`
ZIP_ARCHIVE_FILENAME=$(basename "$INPUT_ZIP_ARCHIVE")
DATASET_NAME=$(basename "$ZIP_ARCHIVE_FILENAME" .zip)
DATASET_NAME=$(basename "$DATASET_NAME" .rar)
if [ $INPUT_DIR_NOT_ZIP_ARCHIVE -eq 1 ];then
 DATASET_NAME="reprocess_$DATASET_NAME"
fi
# set the TEST_RUN flag in order not to send emails etc
TEST_RUN=0
echo "$DATASET_NAME" | grep --quiet -e 'vast_test' -e 'saturn_test' -e 'test' -e 'Test' -e 'TEST'
if [ $? -eq 0 ];then
 TEST_RUN=1
fi
#
# Set variables containg important directlry names readonly as we'll be deleting them and don't want to delete soemthing else
VAST_WORKING_DIR_FILENAME="vast_${DATASET_NAME}_${SESSION_KEY}"
readonly VAST_WORKING_DIR_FILENAME
VAST_RESULTS_DIR_FILENAME="results_$(date +'%Y%m%d_%H%M%S')_${DATASET_NAME}_${SESSION_KEY}"
readonly VAST_RESULTS_DIR_FILENAME
LOCAL_PATH_TO_IMAGES="img_${DATASET_NAME}_${SESSION_KEY}"
readonly LOCAL_PATH_TO_IMAGES


if [ $INPUT_DIR_NOT_ZIP_ARCHIVE -eq 0 ];then
 # First copy ZIP archive with second-epoch images to the $IMAGE_DATA_ROOT
 PATH_TO_ZIP_ARCHIVE=$(dirname "$INPUT_ZIP_ARCHIVE")
 # Moved down as we want to do it after sleep
 #if [ "$PATH_TO_ZIP_ARCHIVE" != "$IMAGE_DATA_ROOT" ];then
 # cp -vf "$INPUT_ZIP_ARCHIVE" "$IMAGE_DATA_ROOT"
 #fi
 # We'll need ABSOLUTE_PATH_TO_ZIP_ARCHIVE to write out the results URL
 ABSOLUTE_PATH_TO_ZIP_ARCHIVE=$(readlink -f "$PATH_TO_ZIP_ARCHIVE")
 #
 # We need to create results_url.txt ASAP as upload.py is waiting for it
 # Place the redirect link
 echo "$URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME/" > "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE/results_url.txt"
 #
else
 ABSOLUTE_PATH_TO_ZIP_ARCHIVE=""
fi
readonly ABSOLUTE_PATH_TO_ZIP_ARCHIVE

# we want this to be after results_url.txt is created
###########################################################################
# this check is slow an unlikely to fail, OK to put after creating results_url.txt that we want ASASP
##### Check if $VAST_REFERENCE_COPY seems to contain a working copy of VaST #####
if [ ! -x "$VAST_REFERENCE_COPY/vast" ];then
 echo "ERROR: cannot find the main VaST executable at $VAST_REFERENCE_COPY/vast"
 exit 1
fi
# Check if vast actually runs
"$VAST_REFERENCE_COPY"/vast --help 2>&1 | grep --quiet 'VaST'
if [ $? -ne 0 ];then
 echo "ERROR: VaST does not seem to run $VAST_REFERENCE_COPY/vast --help"
 exit 1
fi
if [ ! -x "$VAST_REFERENCE_COPY/util/transients/transient_factory_test31.sh" ];then 
 echo "ERROR: cannot find the main VaST data processing script at $VAST_REFERENCE_COPY/util/transients/transient_factory_test31.sh"
 exit 1
fi
##########
###########################################################################
# Set max system stress parameters
# (normally they should be set in local_config.sh)
#
if [ -z "$MAX_IOWAIT_PERCENT" ];then
 MAX_IOWAIT_PERCENT=3.0
fi
#
if [ -z "$MAX_CPU_TEMP_C" ];then
 MAX_CPU_TEMP_C=70.0
fi
#
if [ -z "$MAX_SYSTEM_LOAD" ];then
 if [ -f /proc/cpuinfo ];then
  # Try to guess an appropriate MAX_SYSTEM_LOAD for starting a new process
  # We have "sysrem pile-up" problem on kadar2 server when MAX_SYSTEM_LOAD is threads/2
  MAX_SYSTEM_LOAD=$(cat /proc/cpuinfo | grep -c 'processor' | awk '{threads=$1; print (threads/3 > 3 ? int(threads/3) : 3)}')
  # and since we are at it, set OMP_NUM_THREADS that will be used by vast and sysrem tools
  if [ -z "$OMP_NUM_THREADS" ];then
   OMP_NUM_THREADS=$(cat /proc/cpuinfo | grep -c 'processor' | awk '{threads=$1; print (threads == 1 ? 1 : (threads == 2 ? 2 : (threads/2 > 1 ? int(threads/2) : 1)))}')
   export OMP_NUM_THREADS
  fi
 fi
 # fallback
 if [ -z "$MAX_SYSTEM_LOAD" ];then
  MAX_SYSTEM_LOAD=3.0
 fi
fi
# disable load limit if we are testing in GitHub Actions
if [ -n "$GITHUB_ACTIONS" ] && [ "$GITHUB_ACTIONS" = "true" ];then
 AUTOPROCESS_NO_WAIT="yes"
 # actually this is redundant as we set AUTOPROCESS_NO_WAIT="yes" above
 MAX_IOWAIT_PERCENT=99.0
 MAX_CPU_TEMP_C=99.0
 MAX_SYSTEM_LOAD=99.0
fi
#
###########################################################################
############ Do not start the wait if we are out of disk space ############
if ! check_free_space ;then
 echo "ERROR free disk space check faield before starting to wait for our turn"
 exit 1
fi
############### Delay processing if the server load is high ###############
UNIXSEC_START_WAITLOAD=$(date +%s)

wait_for_our_turn_to_start_processing

echo "Done sleeping"
UNIXSEC_STOP_WAITLOAD=$(date +%s)
###########################################################################

# moved from above
if [ $INPUT_DIR_NOT_ZIP_ARCHIVE -eq 0 ];then
 if [ "$PATH_TO_ZIP_ARCHIVE" != "$IMAGE_DATA_ROOT" ];then
  cp -vf "$INPUT_ZIP_ARCHIVE" "$IMAGE_DATA_ROOT"
 fi
fi

echo "Changing directory to $IMAGE_DATA_ROOT" 
cd "$IMAGE_DATA_ROOT" || exit 1
#
echo -n "Checking write permissions for the current directory ( $PWD ) ... "
touch testfile$$.tmp
if [ $? -eq 0 ];then
 rm -f testfile$$.tmp
 echo "OK"
else
 echo "ERROR: please make sure you have write permissions for the current directory.

Maybe you need something like:
sudo chown -R $USER $PWD"
 exit 1
fi
#

if [ $INPUT_DIR_NOT_ZIP_ARCHIVE -eq 0 ];then
 # Remove image directory with the same name if exist
 if [ -d "$LOCAL_PATH_TO_IMAGES" ];then
  rm -rf "$LOCAL_PATH_TO_IMAGES"
 fi
 mkdir "$LOCAL_PATH_TO_IMAGES"
 ABSOLUTE_PATH_TO_IMAGES="$IMAGE_DATA_ROOT/$LOCAL_PATH_TO_IMAGES"
 echo "Setting archive directory path ABSOLUTE_PATH_TO_IMAGES= $ABSOLUTE_PATH_TO_IMAGES"
else
 #INPUT_IMAGE_DIR_PATH_INSTEAD_OF_ZIP_ARCHIVE=$(basename $INPUT_IMAGE_DIR_PATH_INSTEAD_OF_ZIP_ARCHIVE)
 ABSOLUTE_PATH_TO_IMAGES=$(readlink -f "$INPUT_IMAGE_DIR_PATH_INSTEAD_OF_ZIP_ARCHIVE")
 #### Not sure how the above line worked befor
 if [ -z "$ABSOLUTE_PATH_TO_IMAGES" ];then
  ABSOLUTE_PATH_TO_IMAGES=$(readlink -f $(basename "$INPUT_IMAGE_DIR_PATH_INSTEAD_OF_ZIP_ARCHIVE"))
 fi
 ####
 echo "Setting input directory path ABSOLUTE_PATH_TO_IMAGES= $ABSOLUTE_PATH_TO_IMAGES"
fi # if [ $INPUT_DIR_NOT_ZIP_ARCHIVE -eq 0 ];then
if [ ! -d "$ABSOLUTE_PATH_TO_IMAGES" ];then
 echo "ERROR: cannot find directory $ABSOLUTE_PATH_TO_IMAGES " 
 exit 1
fi
# Remove results directory with the same name if exist
if [ -d "$VAST_RESULTS_DIR_FILENAME" ];then
 rm -rf "$VAST_RESULTS_DIR_FILENAME"
fi
mkdir "$VAST_RESULTS_DIR_FILENAME"

if [ $INPUT_DIR_NOT_ZIP_ARCHIVE -eq 0 ];then
 mv -v "$ZIP_ARCHIVE_FILENAME" "$LOCAL_PATH_TO_IMAGES"

 echo "Changing directory to $ABSOLUTE_PATH_TO_IMAGES" 
 cd "$ABSOLUTE_PATH_TO_IMAGES" || exit 1
 if [ ! -f "$ZIP_ARCHIVE_FILENAME" ];then
  echo "ERROR: cannot find $ABSOLUTE_PATH_TO_IMAGES/$ZIP_ARCHIVE_FILENAME" x
  exit 1
 fi

 # Check if this is a RAR archive
 if file "$ZIP_ARCHIVE_FILENAME" | grep --quiet 'RAR archive' ;then
  command -v rar &> /dev/null
  if [ $? -eq 0 ];then
   rar e "$ZIP_ARCHIVE_FILENAME"
   if [ $? -ne 0 ];then
    echo "ERROR: cannot extradct the RAR archive $ZIP_ARCHIVE_FILENAME" 
    exit 1
   fi
  else
   command -v unrar &> /dev/null
   if [ $? -ne 0 ];then
    echo "ERROR: cannot extradct the RAR archive $ZIP_ARCHIVE_FILENAME" 
    exit 1
   else
    unrar e "$ZIP_ARCHIVE_FILENAME"
    if [ $? -ne 0 ];then
     echo "ERROR: cannot extradct the RAR archive - please install rar or unrar" 
     exit 1
    fi
   fi
  fi
 elif file "$ZIP_ARCHIVE_FILENAME" | grep --quiet 'Zip archive' ;then
  # -n Never overwrite existing files. If a file with the same name exists, it skips extraction of that file without asking for user input.
  # -j Junk the paths (extract all files into the current directory without directory structure).
  unzip -n -j "$ZIP_ARCHIVE_FILENAME"
  if [ $? -ne 0 ];then
   echo "ERROR: cannot extradct the ZIP archive $ZIP_ARCHIVE_FILENAME" 
   exit 1
  fi
 else
  echo "ERROR: unrecognized archive type $ZIP_ARCHIVE_FILENAME" 
  exit 1
 fi
 # remove the archive file
 rm -f "$ZIP_ARCHIVE_FILENAME"
 # set restricitve permissions to all the extracted files, just in case
 chmod -R 0644 *
fi # if [ $INPUT_DIR_NOT_ZIP_ARCHIVE -eq 0 ];then

##### At this point we have input directory with images at $ABSOLUTE_PATH_TO_IMAGES #####


# If not done above
if [ -z "$FITS_FILE_EXT" ];then
 # Figure out fits file extension for this dataset
 FITS_FILE_EXT=$(for POSSIBLE_FITS_FILE_EXT in fts fits fit ;do for IMGFILE in *."$POSSIBLE_FITS_FILE_EXT" ;do if [ -f "$IMGFILE" ];then echo "$POSSIBLE_FITS_FILE_EXT"; break; fi ;done ;done)
 if [ -z "$FITS_FILE_EXT" ];then
  FITS_FILE_EXT="fts"
 fi
 export FITS_FILE_EXT
 echo "FITS_FILE_EXT=$FITS_FILE_EXT"
fi
#

# Rename SF files
echo "Changing directory to $ABSOLUTE_PATH_TO_IMAGES" 
cd "$ABSOLUTE_PATH_TO_IMAGES" || exit 1
#
echo -n "Checking write permissions for the current directory ( $PWD ) ... "
touch testfile$$.tmp
if [ $? -eq 0 ];then
 rm -f testfile$$.tmp
 echo "OK"
else
 echo "ERROR: please make sure you have write permissions for the current directory.

Maybe you need something like:
sudo chown -R $USER $PWD"
 exit 1
fi
#
echo "Renaming the SF files" 
for i in *-SF*."$FITS_FILE_EXT" ;do 
 if [ -f "$i" ];then
  mv "$i" "${i/-SF/}"
 fi 
done
#
echo "Renaming the 2021_ field name files (should be 2021-)"
for i in *"2021_"*."$FITS_FILE_EXT" ;do 
 if [ -f "$i" ];then
  mv "$i" "${i/2021_/2021-}" 
 fi
done
echo "Remofing fd_*.* (old calibrated files)"
for i in fd_*."$FITS_FILE_EXT" ;do
 if [ -f "$i" ];then
  rm -f "$i"
 fi
done




# make a VaST Copy
echo "Changing directory to $DATA_PROCESSING_ROOT" 
cd "$DATA_PROCESSING_ROOT" || exit 1
#
echo -n "Checking write permissions for the current directory ( $PWD ) ... "
touch testfile$$.tmp
if [ $? -eq 0 ];then
 rm -f testfile$$.tmp
 echo "OK"
else
 echo "ERROR: please make sure you have write permissions for the current directory.
Maybe you need something like:
sudo chown -R $USER $PWD"
 exit 1
fi
#

# Make sure we have enough disk space before doing this
if ! check_free_space ;then
 echo "ERROR free disk space check faield before rsync"
 exit 1
fi

echo "Making a copy of $(readlink -f "$VAST_REFERENCE_COPY") to $VAST_WORKING_DIR_FILENAME"
# use rsync instead of cp to ignore large and unneeded files
# '/' tells rsync we want the content of the directory, not the directory itself
#rsync -avz --exclude 'astorb.dat' --exclude 'lib/catalogs' --exclude 'src' --exclude '.git' --exclude '.github' $(readlink -f "$VAST_REFERENCE_COPY")/ "$VAST_WORKING_DIR_FILENAME"
# -z is not needed, it's compression
# --whole-file skips file comparison and increases speed for local transfers
# --omit-dir-times skipping directory timestamp updates can reduce metadata write
# --no-times avoid copying file modification times
rsync -av --whole-file --no-times --omit-dir-times --exclude 'astorb.dat' --exclude 'lib/catalogs' --exclude 'src' --exclude '.git' --exclude '.github' "$(readlink -f "$VAST_REFERENCE_COPY")/" "$VAST_WORKING_DIR_FILENAME"
if [ $? -ne 0 ];then
 echo "ERROR running rsync"
 exit 1
fi
cd "$VAST_WORKING_DIR_FILENAME" || exit 1
# create symlinks
ln -s "$(readlink -f "$VAST_REFERENCE_COPY")/astorb.dat" astorb.dat
cd lib/ || exit 1
ln -s "$(readlink -f "$VAST_REFERENCE_COPY")/lib/catalogs" catalogs
cd .. || exit 1
#

# We should be at $VAST_WORKING_DIR_FILENAME
echo "We are currently at $PWD"

#
if [ -d transient_report ];then
 echo "Removing transient_report"
 rm -rf transient_report
fi
ln -s "../$VAST_RESULTS_DIR_FILENAME" transient_report

# Report that we are ready to go
echo "Reporting the start of work" 
HOST=$(hostname)
HOST="@$HOST"
NAME="$USER$HOST"
DATETIME=$(LANG=C date --utc)
SCRIPTNAME=$(basename $0)
MSG="The script $0 has started on $DATETIME at $PWD with the following parameters:
IMAGE_DATA_ROOT=$IMAGE_DATA_ROOT
DATA_PROCESSING_ROOT=$DATA_PROCESSING_ROOT
VAST_REFERENCE_COPY=$VAST_REFERENCE_COPY
INPUT_ZIP_ARCHIVE=$INPUT_ZIP_ARCHIVE
VAST_WORKING_DIR_FILENAME=$VAST_WORKING_DIR_FILENAME
PATH_TO_ZIP_ARCHIVE=$PATH_TO_ZIP_ARCHIVE
ABSOLUTE_PATH_TO_ZIP_ARCHIVE=$ABSOLUTE_PATH_TO_ZIP_ARCHIVE

Full path to VaST: $DATA_PROCESSING_ROOT/$VAST_WORKING_DIR_FILENAME

The results should appear at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME/
"
echo "
$MSG

" 
if [ -f "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE/workstartemail" ];then
 if [ -n "$CURL_USERNAME_URL_TO_EMAIL_TEAM" ] && [ $TEST_RUN -eq 0 ] ;then
  curl --silent $CURL_USERNAME_URL_TO_EMAIL_TEAM --data-urlencode "name=$NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
 fi
fi
WORKENDEMAIL="off"
if [ -f "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE/workendemail" ] && [ $TEST_RUN -eq 0 ];then
 WORKENDEMAIL="on"
fi
############################################################################
echo "Starting work" 
UNIXSEC_START=$(date +%s)
########################## ACTUAL WORK ##########################
util/transients/transient_factory_test31.sh "$ABSOLUTE_PATH_TO_IMAGES"
SCRIPT_EXIT_CODE=$?
echo "SCRIPT_EXIT_CODE=$SCRIPT_EXIT_CODE"
CPU_TEMERATURE_AT_THE_END_OF_THE_RUN_STRING=$(is_temperature_low log)
#################################################################
if [ ! -f transient_report/index.html ];then
 ERROR_MSG="no transient_report/index.html"
 echo "ERROR: $ERROR_MSG"
 MSG="A VaST error occured: $ERROR_MSG
Please check it at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME"
elif [ ! -s transient_report/index.html ];then
 ERROR_MSG="empty transient_report/index.html"
 echo "ERROR: $ERROR_MSG"
 MSG="A VaST error occured: $ERROR_MSG
Please check it at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME"
else
 # nonempty 'transient_report/index.html' is found
 echo "$CPU_TEMERATURE_AT_THE_END_OF_THE_RUN_STRING" | tee -a transient_report/index.html
 ## Check for extra bright transients and send a special e-mail message
 cat "transient_report/index.html" | grep -v -e 'This object is listed in planets.txt' -e 'This object is listed in comets.txt' -e 'This object is listed in moons.txt' | grep -B1 'galactic' | grep -v -e 'galactic' -e '--' | while read A ;do   
  echo $A | awk '{if ( $5<9.5 && $5>-5.0 ) print "FOUND"}' | grep --quiet "FOUND" 
  if [ $? -eq 0 ];then
   N_NOT_FOUND_IN_CATALOGS=$(grep -A4 "$A" "transient_report/index.html" | grep -c 'not found')
   if [ $N_NOT_FOUND_IN_CATALOGS -ge 3 ];then
    # Allow only for new sources, not flares produce the 'bright candidate' alert
    # (as "flares" are often triggered by misidentification of stars on the reference and new images)
    grep -B6 "$A" "transient_report/index.html" | grep --quiet 'Discovery image 3'
    if [ $? -eq 0 ];then
     continue
    fi
    # If we are still here - we want to raise the 'bright candidate' alert
    BRIGHT_TRANSIENT_NAME=$(grep -B17 "$A" "transient_report/index.html" | grep 'a name=' | awk -F"'" '{print $2}')
    MSG="A bright candidate transient is found

$A

Please check it at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME/#$BRIGHT_TRANSIENT_NAME"
    # Just send this to kirx
    if [ -n "$CURL_USERNAME_URL_TO_EMAIL_KIRX" ] && [ $TEST_RUN -eq 0 ];then
     curl --silent $CURL_USERNAME_URL_TO_EMAIL_KIRX --data-urlencode "name=[NMW bright candidate] $NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
    fi
   fi
  fi  
 done # Check for extra bright transients
 #############
 # Check for errors
 ### Check for the stuck camera
 grep 'ERROR' "transient_report/index.html" | grep 'camera is stuck'
 if [ $? -eq 0 ];then
  FIELD=$(grep 'Processing fields' transient_report/index.html | sed 's:Processing:processing:g' | sed 's:<br>::g')
  MSG="A camera error occured while $FIELD
The cmaera seems to be repeatedly writing the same image!!!
The detailed log output is at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME"
  if [ -n "$CURL_USERNAME_URL_TO_EMAIL_TEAM" ] && [ $TEST_RUN -eq 0 ];then
   curl --silent $CURL_USERNAME_URL_TO_EMAIL_TEAM --data-urlencode "name=[NMW ERROR] $NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
  fi
 else
  ### Check for all other errors
  grep --quiet 'ERROR' "transient_report/index.html"
  if [ $? -eq 0 ] || [ $SCRIPT_EXIT_CODE -ne 0 ] ;then
   ERROR_MSG=$(grep --max-count=1 'ERROR' "transient_report/index.html")
   FIELD=$(grep 'Processing fields' transient_report/index.html | sed 's:Processing:processing:g' | sed 's:<br>::g')
   MSG="An error occured while $FIELD
Please check it at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME"
  fi
 fi # grep 'ERROR' "transient_report/index.html" | grep 'camera is stuck'
fi # if [ ! -f transient_report/index.html ];then
##
UNIXSEC_STOP=$(date +%s)
############################################################################
#
cd ..
# Delete the input archive for security reasons, no matter $SCRIPT_EXIT_CODE
if [ $INPUT_DIR_NOT_ZIP_ARCHIVE -eq 0 ];then
 if [ -n "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE" ];then
  if [ -d "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE" ];then
   if [ ! -f "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE/DO_NOT_DELETE_THIS_DIR" ];then
    # NOTE that results_url.txt should not be deleted with the folder containing it by autoprocess.sh
    # before upload.py gets a chance to read it!
    sleep 3
    #
    rm -rf "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE"
   fi
  fi
 fi
fi # if [ $INPUT_DIR_NOT_ZIP_ARCHIVE -eq 0 ];then
#
# We want to remove the VaST working directory no matter the exit code
echo "Cleaning up"
if [ -n "$VAST_WORKING_DIR_FILENAME" ];then
 if [ -d "$VAST_WORKING_DIR_FILENAME" ];then
  if [ ! -f "$VAST_WORKING_DIR_FILENAME/DO_NOT_DELETE_THIS_DIR" ];then
   rm -rf "$VAST_WORKING_DIR_FILENAME"
  fi
 fi
fi
############################################################################

PROCESSING_TIME=$(echo "$UNIXSEC_STOP $UNIXSEC_START" | awk '{printf "%6.2f", ($1-$2)/60 }')
PROCESSING_TIME_WAITLOAD=$(echo "$UNIXSEC_STOP_WAITLOAD $UNIXSEC_START_WAITLOAD" | awk '{printf "%6.2f", ($1-$2)/60 }')
PROCESSING_TIME_UNPACK=$(echo "$UNIXSEC_START $UNIXSEC_STOP_WAITLOAD" | awk '{printf "%6.2f", ($1-$2)/60 }')
PROCESSING_TIME_TOTAL=$(echo "$UNIXSEC_STOP $UNIXSEC_START_TOTAL" | awk '{printf "%6.2f", ($1-$2)/60 }')
DATETIME=$(LANG=C date --utc)

# report end of work
echo "Reporting the end of work" 
MSG="The script $0 has finished work on $DATETIME at $PWD with the following parameters:
IMAGE_DATA_ROOT=$IMAGE_DATA_ROOT
DATA_PROCESSING_ROOT=$DATA_PROCESSING_ROOT
VAST_REFERENCE_COPY=$VAST_REFERENCE_COPY
INPUT_ZIP_ARCHIVE=$INPUT_ZIP_ARCHIVE
VAST_WORKING_DIR_FILENAME=$VAST_WORKING_DIR_FILENAME
PATH_TO_ZIP_ARCHIVE=$PATH_TO_ZIP_ARCHIVE
ABSOLUTE_PATH_TO_ZIP_ARCHIVE=$ABSOLUTE_PATH_TO_ZIP_ARCHIVE

Full path to VaST: $DATA_PROCESSING_ROOT/$VAST_WORKING_DIR_FILENAME

The processing time was:
$PROCESSING_TIME_TOTAL min  -- Total
$PROCESSING_TIME min  -- VaST
$PROCESSING_TIME_WAITLOAD min  -- wait due to high server load
$PROCESSING_TIME_UNPACK min  -- unpack data and prepare VaST

The script exit code is $SCRIPT_EXIT_CODE

$CPU_TEMERATURE_AT_THE_END_OF_THE_RUN_STRING

The results should appear at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME/
"
echo "
$MSG

"
if [ "$WORKENDEMAIL" = "on" ];then
 if [ -n "$CURL_USERNAME_URL_TO_EMAIL_TEAM" ] && [ $TEST_RUN -eq 0 ];then
  curl --silent $CURL_USERNAME_URL_TO_EMAIL_TEAM --data-urlencode "name=$NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
 fi
fi

echo "########################### $(date +'%Y-%m-%d %H:%M:%S %Z') ###########################
$MSG" >> autoprocess.txt


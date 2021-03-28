#!/usr/bin/env bash


# Normally $IMAGE_DATA_ROOT $DATA_PROCESSING_ROOT $URL_OF_DATA_PROCESSING_ROOT are 
# exported in local_config.sh that is sourced by wrapper.sh
if [ -z "$IMAGE_DATA_ROOT" ] || [ -z "$DATA_PROCESSING_ROOT" ] || [ -z "$URL_OF_DATA_PROCESSING_ROOT" ];then
 # Manual setup at vast.sai.msu.ru: 
 # mount --bind /dataX/kirx/NMW_NG_rt3_autumn2019/web_upload /usr/lib/cgi-bin/unmw/uploads
 # mount --bind /dataX/kirx/NMW_NG_rt3_autumn2019/web_upload /var/www/unmw/uploads
 IMAGE_DATA_ROOT="/dataX/kirx/NMW_NG_rt3_autumn2019/web_upload"
 DATA_PROCESSING_ROOT="$IMAGE_DATA_ROOT"
 URL_OF_DATA_PROCESSING_ROOT="http://vast.sai.msu.ru/unmw/uploads"

 if [ `hostname` == "scan" ];then
  IMAGE_DATA_ROOT="/home/NMW_web_upload"
  DATA_PROCESSING_ROOT="/home/NMW_web_upload"
  URL_OF_DATA_PROCESSING_ROOT="http://scan.sai.msu.ru/unmw/uploads"
  # rar is in /opt/bin/
  PATH=$PATH:/opt/bin/
 fi
fi

VAST_REFERENCE_COPY="$DATA_PROCESSING_ROOT"/vast
INPUT_ZIP_ARCHIVE=$1

UNIXSEC_START_TOTAL=`date +%s`

## This function will set the random session key in attempt to avoid 
## file collisions if other instances of the script are running at the same time.
function set_session_key {
 if [ -r /dev/urandom ];then
  local RANDOMFILE=/dev/urandom
 elif [ -r /dev/random ];then
  local RANDOMFILE=/dev/random
 else
  echo "ERROR: cannot find /dev/random" 
  local RANDOMFILE=""
 fi
 if [ "$RANDOMFILE" != "" ];then
  local SESSION_KEY="$$"_`tr -cd a-zA-Z0-9 < $RANDOMFILE | head -c 8`
 else
  local SESSION_KEY="$$"
 fi
 echo "$SESSION_KEY"
}

# Check input
if [ -z "$INPUT_ZIP_ARCHIVE" ];then
 echo "ERROR: no input ZIP archive $INPUT_ZIP_ARCHIVE" 
 exit 1
fi
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
if [ ! -d "$IMAGE_DATA_ROOT" ];then
 mkdir "$IMAGE_DATA_ROOT"
 if [ $? -ne 0 ];then
  echo "ERROR: cannot create the image data directory $IMAGE_DATA_ROOT" 
  exit 1
 fi
fi
if [ ! -d "$DATA_PROCESSING_ROOT" ];then
 mkdir "$DATA_PROCESSING_ROOT"
 if [ $? -ne 0 ];then
  echo "ERROR: cannot create the data processing directory $DATA_PROCESSING_ROOT" 
  exit 1
 fi
fi

if [ -f "$VAST_REFERENCE_COPY".lock ];then
 echo "Lock file found $VAST_REFERENCE_COPY.lock - another copy of $0 seems to be installin VaST, so we'll just wait"
 sleep 600
elif [ ! -d "$VAST_REFERENCE_COPY" ];then
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
 mkdir "$VAST_REFERENCE_COPY"
 if [ $? -ne 0 ];then
  echo "ERROR: cannot create VaST directory $VAST_REFERENCE_COPY"
  exit 1
 fi
 touch "$VAST_REFERENCE_COPY".lock
 echo "Trying to install VaST in directory $VAST_REFERENCE_COPY" 
 cd "$VAST_REFERENCE_COPY"
 git checkout https://github.com/kirxkirx/vast.git .
 # compile VaST
 make
 if [ $? -eq 0 ];then
  # update offline catalogs
  lib/update_offline_catalogs.sh all
  # manually update the two big ones
  # Tycho-2
  cd `dirname "$VAST_REFERENCE_COPY"`
  if [ ! -d tycho2 ];then
   mkdir tycho2
   cd tycho2
   wget -nH --cut-dirs=4 --no-parent -r -l0 -c -A 'ReadMe,*.gz,robots.txt' "http://scan.sai.msu.ru/~kirx/data/tycho2/"
   for i in tyc2.dat.*gz ;do
    gunzip $i
   done
   cd `dirname "$VAST_REFERENCE_COPY"`
  fi
  # and UCAC5
  cd `dirname "$VAST_REFERENCE_COPY"`
  if [ ! -d UCAC5 ];then
   mkdir UCAC5
   cd UCAC5
   wget -r -Az* -c --no-dir "http://scan.sai.msu.ru/~kirx/data/ucac5"
   cd `dirname "$VAST_REFERENCE_COPY"`
  fi
 fi
 cd `dirname "$VAST_REFERENCE_COPY"`
 BASENAME_VAST_REFERENCE_COPY=`basename $VAST_REFERENCE_COPY`
 if [ "$BASENAME_VAST_REFERENCE_COPY" != "vast" ];then
  mv "vast" "$BASENAME_VAST_REFERENCE_COPY"
 fi
 rm -f "$VAST_REFERENCE_COPY".lock
fi

if [ ! -d "$VAST_REFERENCE_COPY" ];then
 echo "ERROR: cannot find VaST installation in directory $VAST_REFERENCE_COPY" 
 exit 1
fi

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

# Set random delays
RANDOM_TWO_DIGIT_NUMBER=`tr -cd 0-9 < /dev/urandom | head -c 2`
NUMBER_OF_ITERATIONS=30
if [ $RANDOM_TWO_DIGIT_NUMBER -gt $NUMBER_OF_ITERATIONS ];then
 NUMBER_OF_ITERATIONS=$RANDOM_TWO_DIGIT_NUMBER
fi
RANDOM_TWO_DIGIT_NUMBER=`tr -cd 0-9 < /dev/urandom | head -c 2`
if [ $RANDOM_TWO_DIGIT_NUMBER -lt 10 ];then
 RANDOM_TWO_DIGIT_NUMBER=$[$RANDOM_TWO_DIGIT_NUMBER+10]
fi 

# Delay processing if the server load is high
UNIXSEC_START_WAITLOAD=`date +%s`
for LOADWAITITERATION in `seq 1 $NUMBER_OF_ITERATIONS` ;do
 ONEMINUTELOADTIMES100=`uptime | awk -F'load average:' '{print $2}' | awk -F',' '{print $1*100}'`
 if [ -z "$ONEMINUTELOADTIMES100" ];then
  echo "ERROR: getting the load"
  break
 fi
 # if load < 8.00 break
 if [ $ONEMINUTELOADTIMES100 -lt 800 ];then
  break
 else
  #sleep 60
  echo "sleep $RANDOM_TWO_DIGIT_NUMBER  (ONEMINUTELOADTIMES100=$ONEMINUTELOADTIMES100)"
  sleep $RANDOM_TWO_DIGIT_NUMBER
  # wait longer if the load is really high
  if [ $ONEMINUTELOADTIMES100 -gt 1400 ];then
   #sleep 400
   echo "sleep 4$RANDOM_TWO_DIGIT_NUMBER  (ONEMINUTELOADTIMES100=$ONEMINUTELOADTIMES100)"
   sleep 4$RANDOM_TWO_DIGIT_NUMBER
  fi
  # wait longer if the load is really high
  if [ $ONEMINUTELOADTIMES100 -gt 2400 ];then
   #sleep 500
   echo "sleep 5$RANDOM_TWO_DIGIT_NUMBER  (ONEMINUTELOADTIMES100=$ONEMINUTELOADTIMES100)"
   sleep 5$RANDOM_TWO_DIGIT_NUMBER
  fi
 fi
done
echo "Done sleeping"
UNIXSEC_STOP_WAITLOAD=`date +%s`

# Make up file names
SESSION_KEY=$(set_session_key) # or SESSION_KEY=`set_session_key`
ZIP_ARCHIVE_FILENAME=`basename "$INPUT_ZIP_ARCHIVE"`
DATASET_NAME=`basename $ZIP_ARCHIVE_FILENAME .zip`
DATASET_NAME=`basename $DATASET_NAME .rar`
VAST_WORKING_DIR_FILENAME="vast_$DATASET_NAME"_"$SESSION_KEY"
VAST_RESULTS_DIR_FILENAME="results_"`date +"%Y%m%d_%H%M%S"`_"$DATASET_NAME"_"$SESSION_KEY"
LOCAL_PATH_TO_IMAGES="img_$DATASET_NAME"_"$SESSION_KEY"


# First copy and ZIP archive with second-epoch images to the $IMAGE_DATA_ROOT
PATH_TO_ZIP_ARCHIVE=`dirname "$INPUT_ZIP_ARCHIVE"`
if [ "$PATH_TO_ZIP_ARCHIVE" != "$IMAGE_DATA_ROOT" ];then
 cp -vf "$INPUT_ZIP_ARCHIVE" "$IMAGE_DATA_ROOT"
fi
# We'll need ABSOLUTE_PATH_TO_ZIP_ARCHIVE to write out the results URL
ABSOLUTE_PATH_TO_ZIP_ARCHIVE=`readlink -f "$PATH_TO_ZIP_ARCHIVE"`

echo "Changing directory to $IMAGE_DATA_ROOT" 
cd "$IMAGE_DATA_ROOT"
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

# Remove image directory with the same name if exist
if [ -d "$LOCAL_PATH_TO_IMAGES" ];then
 rm -rf "$LOCAL_PATH_TO_IMAGES"
fi
mkdir "$LOCAL_PATH_TO_IMAGES"
ABSOLUTE_PATH_TO_IMAGES="$IMAGE_DATA_ROOT/$LOCAL_PATH_TO_IMAGES"
if [ ! -d "$ABSOLUTE_PATH_TO_IMAGES" ];then
 echo "ERROR: cannot find directory $ABSOLUTE_PATH_TO_IMAGES " 
 exit 1
fi
# Remove results directory with the same name if exist
if [ -d "$VAST_RESULTS_DIR_FILENAME" ];then
 rm -rf "$VAST_RESULTS_DIR_FILENAME"
fi
mkdir "$VAST_RESULTS_DIR_FILENAME"


mv -v "$ZIP_ARCHIVE_FILENAME" "$LOCAL_PATH_TO_IMAGES"

echo "Changing directory to $ABSOLUTE_PATH_TO_IMAGES" 
cd "$ABSOLUTE_PATH_TO_IMAGES"
if [ ! -f "$ZIP_ARCHIVE_FILENAME" ];then
 echo "ERROR: cannot find $ABSOLUTE_PATH_TO_IMAGES/$ZIP_ARCHIVE_FILENAME" 
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
 unzip -j "$ZIP_ARCHIVE_FILENAME"
 if [ $? -ne 0 ];then
  echo "ERROR: cannot extradct the ZIP archive $ZIP_ARCHIVE_FILENAME" 
  exit 1
 fi
else
 echo "ERROR: unrecognized archive type $ZIP_ARCHIVE_FILENAME" 
 exit 1
fi
rm -f "$ZIP_ARCHIVE_FILENAME"

# Rename SF files
echo "Changing directory to $ABSOLUTE_PATH_TO_IMAGES" 
cd "$ABSOLUTE_PATH_TO_IMAGES"
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
for i in *-SF* ;do 
 if [ -f "$i" ];then
  mv "$i" "${i/-SF/}"
 fi 
done

# make a VaST Copy
echo "Changing directory to $DATA_PROCESSING_ROOT" 
cd "$DATA_PROCESSING_ROOT"
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

echo "Making a copy of "`readlink -f "$VAST_REFERENCE_COPY"`" to $VAST_WORKING_DIR_FILENAME" 
# P is to copy symlinks as symlinks
cp -rP `readlink -f "$VAST_REFERENCE_COPY"` "$VAST_WORKING_DIR_FILENAME"
#
echo "Changing directory to $VAST_WORKING_DIR_FILENAME" 
cd "$VAST_WORKING_DIR_FILENAME"

#
if [ -d transient_report ];then
 echo "Removing transient_report"
 rm -rf transient_report
fi
ln -s ../"$VAST_RESULTS_DIR_FILENAME" transient_report

# Place the redirect link
echo "$URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME/" > "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE/results_url.txt"


# Report that we are ready to go
echo "Reporting the start of work" 
HOST=`hostname`
HOST="@$HOST"
NAME="$USER$HOST"
DATETIME=`LANG=C date --utc`
SCRIPTNAME=`basename $0`
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
 if [ ! -z "$CURL_USERNAME_URL_TO_EMAIL_TEAM" ];then
  curl --silent $CURL_USERNAME_URL_TO_EMAIL_TEAM --data-urlencode "name=$NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
 fi
fi
WORKENDEMAIL="off"
if [ -f "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE/workendemail" ];then
 WORKENDEMAIL="on"
fi
############################################################################
echo "Starting work" 
UNIXSEC_START=`date +%s`
########################## ACTUAL WORK ##########################
util/transients/transient_factory_test31.sh "$ABSOLUTE_PATH_TO_IMAGES"
SCRIPT_EXIT_CODE=$?
echo "SCRIPT_EXIT_CODE=$SCRIPT_EXIT_CODE"
#################################################################
if [ ! -f transient_report/index.html ];then
 ERROR_MSG="no transient_report/index.html"
 echo "ERROR: $ERROR_MSG"
 MSG="A VaST error occured: $ERROR_MSG
Please check it at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME"
 # Just send this to kirx
 #if [ ! -z "$CURL_USERNAME_URL_TO_EMAIL_KIRX" ];then
 # curl --silent $CURL_USERNAME_URL_TO_EMAIL_KIRX --data-urlencode "name=[NMW ERROR] $ERROR_MSG   $NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
 #fi
elif [ ! -s transient_report/index.html ];then
 ERROR_MSG="empty transient_report/index.html"
 echo "ERROR: $ERROR_MSG"
 MSG="A VaST error occured: $ERROR_MSG
Please check it at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME"
 # Just send this to kirx
 #if [ ! -z "$CURL_USERNAME_URL_TO_EMAIL_KIRX" ];then
 # curl --silent $CURL_USERNAME_URL_TO_EMAIL_KIRX --data-urlencode "name=[NMW ERROR] $ERROR_MSG   $NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
 #fi
else
 # nonempty 'transient_report/index.html' is found
 ## Check for extra bright transients and send a special e-mail message
 cat "transient_report/index.html" | grep -B1 'galactic' | grep -v -e 'galactic' -e '--' | while read A ;do   
  echo $A | awk '{if ( $5<9.5 && $5>-5.0 ) print "FOUND"}' | grep --quiet "FOUND" 
  if [ $? -eq 0 ];then
   N_NOT_FOUND_IN_CATALOGS=`grep -A4 "$A" "transient_report/index.html" | grep -c 'not found'`
   if [ $N_NOT_FOUND_IN_CATALOGS -ge 3 ];then
    BRIGHT_TRANSIENT_NAME=`grep -B17 "$A" "transient_report/index.html" | grep 'a name=' | awk -F"'" '{print $2}'`
    MSG="A bright candidate transient is found

$A

Please check it at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME/#$BRIGHT_TRANSIENT_NAME"
    # Just send this to kirx
    if [ ! -z "$CURL_USERNAME_URL_TO_EMAIL_KIRX" ];then
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
  FIELD=`grep 'Processing fields' transient_report/index.html | sed 's:Processing:processing:g' | sed 's:<br>::g'`
  MSG="A camera error occured while $FIELD
The cmaera seems to be repeatedly writing the same image!!!
The detailed log output is at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME"
  if [ ! -z "$CURL_USERNAME_URL_TO_EMAIL_TEAM" ];then
   curl --silent $CURL_USERNAME_URL_TO_EMAIL_TEAM --data-urlencode "name=[NMW ERROR] $NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
  fi
 else
  ### Check for all other errors
  grep --quiet 'ERROR' "transient_report/index.html"
  if [ $? -eq 0 ] || [ $SCRIPT_EXIT_CODE -ne 0 ] ;then
   ERROR_MSG=`grep --max-count=1 'ERROR' "transient_report/index.html"`
   FIELD=`grep 'Processing fields' transient_report/index.html | sed 's:Processing:processing:g' | sed 's:<br>::g'`
   MSG="An error occured while $FIELD
Please check it at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME"
   # Just send this to kirx
   #if [ ! -z "$CURL_USERNAME_URL_TO_EMAIL_KIRX" ];then
   # curl --silent $CURL_USERNAME_URL_TO_EMAIL_KIRX --data-urlencode "name=[NMW ERROR] $ERROR_MSG   $NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
   #fi
  fi
 fi # grep 'ERROR' "transient_report/index.html" | grep 'camera is stuck'
fi # if [ ! -f transient_report/index.html ];then
##
UNIXSEC_STOP=`date +%s`
############################################################################
cd ..
if [ $SCRIPT_EXIT_CODE -eq 0 ];then
 echo "Cleaning up"
 if [ ! -z "$VAST_WORKING_DIR_FILENAME" ];then
  if [ -d "$VAST_WORKING_DIR_FILENAME" ];then
   if [ ! -f "$VAST_WORKING_DIR_FILENAME/DO_NOT_DELETE_THIS_DIR" ];then
    rm -rf "$VAST_WORKING_DIR_FILENAME"
   fi
  fi
 fi
 if [ ! -z "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE" ];then
  if [ -d "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE" ];then
   if [ ! -f "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE/DO_NOT_DELETE_THIS_DIR" ];then
    rm -rf "$ABSOLUTE_PATH_TO_ZIP_ARCHIVE"
   fi
  fi
 fi
else
 echo "Skip cleanup for non-zero script exit code"
fi
############################################################################

PROCESSING_TIME=`echo "$UNIXSEC_STOP $UNIXSEC_START" | awk '{printf "%6.2f", ($1-$2)/60 }'`
PROCESSING_TIME_WAITLOAD=`echo "$UNIXSEC_STOP_WAITLOAD $UNIXSEC_START_WAITLOAD" | awk '{printf "%6.2f", ($1-$2)/60 }'`
PROCESSING_TIME_UNPACK=`echo "$UNIXSEC_START $UNIXSEC_STOP_WAITLOAD" | awk '{printf "%6.2f", ($1-$2)/60 }'`
PROCESSING_TIME_TOTAL=`echo "$UNIXSEC_STOP $UNIXSEC_START_TOTAL" | awk '{printf "%6.2f", ($1-$2)/60 }'`
DATETIME=`LANG=C date --utc`

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

The results should appear at $URL_OF_DATA_PROCESSING_ROOT/$VAST_RESULTS_DIR_FILENAME/
"
echo "
$MSG

"
if [ "$WORKENDEMAIL" = "on" ];then
 if [ ! -z "$CURL_USERNAME_URL_TO_EMAIL_TEAM" ];then
  curl --silent $CURL_USERNAME_URL_TO_EMAIL_TEAM --data-urlencode "name=$NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
 fi
fi

echo "########################### "`date +"%Y-%m-%d %H:%M:%S %Z"`" ###########################
$MSG" >> autoprocess.txt


#!/usr/bin/env bash

# You probably want to add this script to /etc/crontab
#*/8     *       *       *       *       www-data        /dataX/cgi-bin/unmw/combine_reports.sh &> /dev/null


## The old way to check if multiple copies of this script are running
##### This does not work for all systems! On some N_RUN is 3 #####
# Check that no other instances of the script are running
N_RUN=`ps ax | grep combine_reports.sh | grep -v grep | grep bash | grep -c combine_reports.sh`
# This is conter-intuitive but the use of the construct N_RUN=`` will create a second copy of "bash ./combine_reports.sh" in the ps output
# So one running copy of the script corresponds to N_RUN=2
#if [ $N_RUN -gt 2 ];then
if [ $N_RUN -gt 3 ];then
 exit 0
fi
##################################################################
# helper functions

# none so far

##################################################################
# change to the work directory
SCRIPTDIR=$(dirname "$(readlink -f "$0")")
cd "$SCRIPTDIR" || exit 1
# source the local settings file if it exist
# it may countain curl e-mail and data processing directory settings
if [ -s local_config.sh ];then
 source local_config.sh
fi
#####
# Silly fix for one local problem
if [ "$HOSTNAME" == "ariel.astro.illinois.edu" ]  && [ "$USER" = "kirill" ] ;then
 source /home/kirill/.bashrc
fi
#####
# uploads/ is the default location for the processing data (both mages and results)
if [ -d "uploads" ];then
 cd "uploads" || exit 1
fi
# DATA_PROCESSING_ROOT may be exported in local_config.sh
# if it is set properly - go there
if [ ! -z "$DATA_PROCESSING_ROOT" ];then
 if [ -d "$DATA_PROCESSING_ROOT" ];then
  cd "$DATA_PROCESSING_ROOT" || exit 1
 fi
fi
#
# URL_OF_DATA_PROCESSING_ROOT specifies where the data processing root is accessible online
# URL_OF_DATA_PROCESSING_ROOT may be exported in local_config.sh
if [ -z "$URL_OF_DATA_PROCESSING_ROOT" ];then
 # if it is not set, go with the default value
 URL_OF_DATA_PROCESSING_ROOT="http://vast.sai.msu.ru/unmw/uploads"
fi


# This script creates a lock file at $DATA_PROCESSING_ROOT/combine_reports.lock and writes its own process ID into that file.
# If another instance of the script runs, it checks the lock file, and if it exists,
# it sends a null signal (kill -0) to the PID contained in the file.
# If the process is still running, kill -0 will succeed and the script will exit, otherwise,
# it will assume that the process is no longer running and will continue execution.

# create a lockfile in the DATA_PROCESSING_ROOT
LOCKFILE="combine_reports.lock"
if [ -e "${LOCKFILE}" ] && kill -0 `cat "${LOCKFILE}"`; then
 echo "Already running."
 exit
fi
# Make sure the lockfile is removed when we exit and when we receive a signal
trap "rm -f ${LOCKFILE}; exit" INT TERM EXIT
echo $$ > "${LOCKFILE}"

# loop through the cameras
for CAMERA in Stas STL-11000M TICA_TESS ;do

#echo "DEBUG CAMERA=$CAMERA"

DAY=`date +%Y%m%d`
HOUR=`date +%H`
EVENING_OR_MORNING="evening"
if [ $HOUR -lt 17 ];then
 EVENING_OR_MORNING="morning"
fi
OUTPUT_COMBINED_HTML_NAME=$DAY"_"$EVENING_OR_MORNING"_"$CAMERA".html"
OUTPUT_FILTERED_HTML_NAME=$DAY"_"$EVENING_OR_MORNING"_"$CAMERA"_filtered.html"
OUTPUT_PROCESSING_SUMMARY_HTML_NAME=$DAY"_"$EVENING_OR_MORNING"_summary.html"

######################################################### 12 hours
INPUT_LIST_OF_RESULT_DIRS=$(find -maxdepth 1 -type d -mmin -720 -name "results*$CAMERA*")

if [ -z "$INPUT_LIST_OF_RESULT_DIRS" ];then
 # nothing to process, continue to the next camera
 continue
fi

# a silly attempt to make sure files are sorted in time and only completed files are listed
LIST_OF_FILES=""
for INPUT_DIR in $INPUT_LIST_OF_RESULT_DIRS ;do
 # check that this report does not look like a test - we don't want them in the ombined list
 if [[ "$INPUT_DIR" == *"_test"* ]]; then
  continue
 fi
 # check that this report has not apeared in a combined report before
 # combine_reports.log may not exist or be empty if this is the first-ever run of combine_reports.sh
 if [ -s combine_reports.log ];then
  grep --quiet "$INPUT_DIR/index.html" combine_reports.log
  if [ $? -eq 0 ];then
   continue
  fi
 fi
 #if [ -f $INPUT_DIR/index.html.combine_reports_lock ];then
 # continue
 #fi
 LIST_OF_FILES="$LIST_OF_FILES $INPUT_DIR/index.html"
done

#echo "DEBUG000 #$LIST_OF_FILES#"

if [ -z "$LIST_OF_FILES" ];then
 # nothing to process, continue to the next camera
 continue
fi

SORTED_LIST_OF_FILES=$(ls -tr $LIST_OF_FILES)

INPUT_LIST_OF_RESULT_DIRS=""
for FILE in $SORTED_LIST_OF_FILES ;do
 grep --quiet 'Processig complete' "$FILE"
 if [ $? -ne 0 ];then
  continue
 fi
 #
 INPUT_LIST_OF_RESULT_DIRS="$INPUT_LIST_OF_RESULT_DIRS "$(dirname "$FILE")
done

if [ -z "$INPUT_LIST_OF_RESULT_DIRS" ];then
 # nothing is completed yet, continue to the next camera
 continue
fi


if [ ! -f "$OUTPUT_COMBINED_HTML_NAME" ];then
 # make head
 for INPUT_DIR in $INPUT_LIST_OF_RESULT_DIRS ;do

  if [ ! -d "$INPUT_DIR" ];then
   echo "ERROR: there is no directory $INPUT_DIR"
   continue
  fi

  if [ ! -f "$INPUT_DIR/index.html" ];then
   echo "ERROR: there is no file $INPUT_DIR/index.html"
   continue
  fi

  # The combined HTML page should have the same HEAD as an individual field results page, so we just copy its head
  # and -A1 is for the floating button
  grep --max-count=1 -B10000 '<BODY>' -A1 "$INPUT_DIR/index.html" > "$OUTPUT_COMBINED_HTML_NAME" && break
 
 done

 ################ 
 if [ ! -f index.html ];then
  # make head
  echo "<HTML>
   <BODY>
   
   <table align='center' width='50%' border='0' class='main'>" > index.html
   if [ -s 'results_comets.txt' ];then
    echo "<tr><td>Summary of comet detections: <a href='results_comets.txt' target='_blank'>results_comets.txt</a></td></tr>" >> index.html
    echo "<tr><td></td></tr>" >> index.html
   fi
 fi
 # Add this summary file to the list
 OUTPUT_COMBINED_HTML_NAME_FOR_THE_TABLE=`basename $OUTPUT_COMBINED_HTML_NAME .html`
 OUTPUT_COMBINED_HTML_NAME_FOR_THE_TABLE="${OUTPUT_COMBINED_HTML_NAME_FOR_THE_TABLE//_/ }"
 echo "<tr><td><a href='$OUTPUT_COMBINED_HTML_NAME' target='_blank'>$OUTPUT_COMBINED_HTML_NAME_FOR_THE_TABLE</a></td></tr>" >> index.html
 #
 OUTPUT_FILTERED_HTML_NAME_FOR_THE_TABLE=`basename $OUTPUT_FILTERED_HTML_NAME .html`
 OUTPUT_FILTERED_HTML_NAME_FOR_THE_TABLE="${OUTPUT_FILTERED_HTML_NAME_FOR_THE_TABLE//_/ }"
 echo "<tr><td><a href='$OUTPUT_FILTERED_HTML_NAME' target='_blank'>$OUTPUT_FILTERED_HTML_NAME_FOR_THE_TABLE</a></td></tr>" >> index.html
 ################
  
 # report that we are writing a new file 
 HOST=`hostname`
 HOST="@$HOST"
 NAME="$USER$HOST"
 DATETIME=`LANG=C date --utc`                                                                                 
 SCRIPTNAME=`basename $0`
 # Yes, I'm being silly
 # Generate a wish-you-well string
 WISHWELLSTRING="Happy observing!"
 MONTECARLO=$[ $RANDOM % 10 ]
 if [ $MONTECARLO -gt 8 ];then
  WISHWELLSTRING="Have fun observing!"
 elif [ $MONTECARLO -gt 7 ];then
  WISHWELLSTRING="Watch the Skies!"
 elif [ $MONTECARLO -gt 6 ];then
  WISHWELLSTRING="Good luck with the observations!"
 elif [ $MONTECARLO -gt 5 ];then
  WISHWELLSTRING="Clear Skies!"
 elif [ $MONTECARLO -gt 4 ];then
  WISHWELLSTRING="Enjoy observing!"
 elif [ $MONTECARLO -gt 3 ];then
  WISHWELLSTRING="Have fun!"
 elif [ $MONTECARLO -gt 2 ];then
  WISHWELLSTRING="Good luck searching for a Nova!"
 elif [ $MONTECARLO -gt 1 ];then
  WISHWELLSTRING="Don't miss a Nova!"
 fi
 #
 MSG="Creating a new combined list of candidates at 
$URL_OF_DATA_PROCESSING_ROOT/$OUTPUT_COMBINED_HTML_NAME

The filtered version of that list (no known variables and asteroids) is at
$URL_OF_DATA_PROCESSING_ROOT/$OUTPUT_FILTERED_HTML_NAME

The corresponding processing summary page: 
$URL_OF_DATA_PROCESSING_ROOT/$OUTPUT_PROCESSING_SUMMARY_HTML_NAME

Processing logs for the individual fields:
$URL_OF_DATA_PROCESSING_ROOT/autoprocess.txt

$WISHWELLSTRING
$SCRIPTNAME $HOST
"

#"(TEST!) The modified observing plan is at $URL_OF_DATA_PROCESSING_ROOT/plan.txt
#The original observing plan is at $URL_OF_DATA_PROCESSING_ROOT/plan_in.txt"
 if [ ! -z "$CURL_USERNAME_URL_TO_EMAIL_TEAM" ];then
  curl --silent $CURL_USERNAME_URL_TO_EMAIL_TEAM --data-urlencode "name=[NMW combined list] $NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
 fi
fi

# Summary file
if [ ! -f "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME" ];then
 # make head
 echo "<html>
<style>
  .main th, .main td {
    text-align: center;
  }
</style>
<body>

<table align='center' width='100%' border='0' class='main'>
<tr><th>Camera</th><th>Obs.Time(UTC)</th><th>Field</th><th>&nbsp;&nbsp;&mdash;&nbsp;&nbsp;</th><th>Status</th><th>Log</th><th>Pointing.Offset(&deg;)</th><th>mag.lim.</th><th>Comments</th></tr>" > "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"

 # Add this summary file to the list
 SUMMARY_FILE_NAME_FOR_THE_TABLE=`basename $OUTPUT_PROCESSING_SUMMARY_HTML_NAME .html`
 SUMMARY_FILE_NAME_FOR_THE_TABLE="${SUMMARY_FILE_NAME_FOR_THE_TABLE//_/ }"
 echo "<tr><td><font color='teal'><a href='$OUTPUT_PROCESSING_SUMMARY_HTML_NAME' target='_blank'>$SUMMARY_FILE_NAME_FOR_THE_TABLE</a></font></td></tr>" >> index.html

 # Also reset the observing plan if this is evening
 if [ "$EVENING_OR_MORNING" = "evening" ];then
  cp plan_in.txt plan.txt
 fi
 #
fi


# make body
for INPUT_DIR in $INPUT_LIST_OF_RESULT_DIRS ;do

# echo "DEBUG001 $INPUT_DIR"

 if [ ! -d "$INPUT_DIR" ];then
  echo "ERROR: there is no directory $INPUT_DIR"
  continue
 fi

 if [ ! -f "$INPUT_DIR/index.html" ];then
  echo "ERROR: there is no file $INPUT_DIR/index.html"
  continue
 fi

 # check file size before doing any grep
 command -v stat &>/dev/null
 if [ $? -eq 0 ];then
  INPUT_HTML_FILE_SIZE_BYTES=`stat --format="%s" "$INPUT_DIR/index.html"`
  INPUT_HTML_FILE_SIZE_MB=`echo "$INPUT_HTML_FILE_SIZE_BYTES" | awk '{printf "%.0f",$1/(1024*1024)}'`
  TEST=`echo "$INPUT_HTML_FILE_SIZE_MB>100" | bc -ql`
  if [ $TEST -eq 1 ];then
   # too large file error
   HOST=`hostname`
   HOST="@$HOST"
   NAME="$USER$HOST"
   DATETIME=`LANG=C date --utc`                                                                                 
   SCRIPTNAME=`basename $0`
   MSG="The combined list of candidates at $URL_OF_DATA_PROCESSING_ROOT/$OUTPUT_COMBINED_HTML_NAME
is too large -- $INPUT_HTML_FILE_SIZE_MB MB. This is very-very wrong!

Reports on the individual fields may be found at $URL_OF_DATA_PROCESSING_ROOT/autoprocess.txt"
   if [ ! -z "$CURL_USERNAME_URL_TO_EMAIL_KIRX" ];then
    curl --silent $CURL_USERNAME_URL_TO_EMAIL_KIRX --data-urlencode "name=[NMW ERROR: large HTML file] $NAME running $SCRIPTNAME" --data-urlencode "message=$MSG" --data-urlencode 'submit=submit'
   fi
   rm -f "${LOCKFILE}"
   exit 1
  fi
 fi

 grep --max-count=1 --quiet 'Processig complete' "$INPUT_DIR/index.html"
 if [ $? -ne 0 ];then
  echo "ERROR: incomplete report in $INPUT_DIR/index.html"
  continue
 fi

 FIELD=`grep 'Processing fields' "$INPUT_DIR/index.html" | sed 's:Processing:processing:g' | sed 's:processing fields::g' | sed 's:<br>::g' | awk '{print $1}'` 
 NUMBER_OF_CANDIDATE_TRANSIENTS=`grep 'script' "$INPUT_DIR/index.html" | grep -c 'printCandidateNameWithAbsLink'`
 # Always include the Galactic Center field Sco6
 if [ $NUMBER_OF_CANDIDATE_TRANSIENTS -lt 50 ] || [ "$FIELD" = "Sco6" ] ;then
  grep --max-count=1 -A100000 'Processing fields' "$INPUT_DIR/index.html" | grep -B100000 'Processig complete' | grep -v -e 'Processing fields' -e 'Processig complete' | sed "s:src=\":src=\"$INPUT_DIR/:g" >> "$OUTPUT_COMBINED_HTML_NAME"
  INCLUDE_REPORT_IN_COMBINED_LIST="OK"
 else
  echo "ERROR: too many candidates in $INPUT_DIR/index.html"
  # too large file error
  HOST=`hostname`
  HOST="@$HOST"
  NAME="$USER$HOST"
  DATETIME=`LANG=C date --utc`                                                                                 
  SCRIPTNAME=`basename $0`
  MSG="Too many candidates ($NUMBER_OF_CANDIDATE_TRANSIENTS) in $URL_OF_DATA_PROCESSING_ROOT/$INPUT_DIR/"
  INCLUDE_REPORT_IN_COMBINED_LIST="ERROR"
 fi
 echo "$INPUT_DIR/index.html" >> combine_reports.log
 
 # Summary file
 # FIELD moved up to give special treatment to Sco6
 #FIELD=`grep 'Processing fields' "$INPUT_DIR/index.html" | sed 's:Processing:processing:g' | sed 's:processing fields::g' | sed 's:<br>::g'` 
 #LAST_IMAGE_DATE=`grep 'Last  image' "$INPUT_DIR/index.html" | head -n1 | awk '{print $4" "$5}'`
 # remove .000 seconds and UTC
 LAST_IMAGE_DATE=`grep 'Last  image' "$INPUT_DIR/index.html" | head -n1 | awk '{print $4" "$5}' | sed 's/\.000/ /g' | sed 's/UTC/ /g'`
 # sed is for the case Record 39: "TIMESYS = 'UTC     '           / Default time system" status=0 to avoid 'UTC
 TIMESYS_OF_LAST_IMAGE_DATE=`grep 'time system' "$INPUT_DIR/index.html" | head -n1 | awk '{print $5}' | sed "s:'::g"`
 LAST_IMAGE_DATE="$LAST_IMAGE_DATE $TIMESYS_OF_LAST_IMAGE_DATE"
 #IMAGE_CENTER_OFFSET_FROM_REF_IMAGE=$(grep 'Angular distance between the image centers' "$INPUT_DIR/index.html" | awk '{if($7+0 > max) max=$7} END{print max}')
 IMAGE_CENTER_OFFSET_FROM_REF_IMAGE=$(grep 'Angular distance between the image centers' "$INPUT_DIR/index.html" | awk 'BEGIN{max=-1} {if($7+0 > max) max=$7} END{if (max == -1) print "ERROR"; else print max}')
 MAG_LIMIT=`grep 'All-image limiting magnitude estimate' "$INPUT_DIR/index.html" | tail -n1 | awk '{print $5}'`
 # remove "UTC" as we have it in the table header
 echo -n "<tr><td>$CAMERA</td><td>${LAST_IMAGE_DATE/ UTC/}</td><td><font color='teal'> $FIELD </font></td><td>&nbsp;&nbsp;&mdash;&nbsp;&nbsp;</td>" >> "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"
 if [ "$INCLUDE_REPORT_IN_COMBINED_LIST" != "OK" ];then
  echo "<td><font color='#FF0033'>ERROR</font></td><td><a href='$INPUT_DIR/' target='_blank'>log</a></td>$IMAGE_CENTER_OFFSET_FROM_REF_IMAGE<td></td><td>$MAG_LIMIT</td><td>too many candidates ($NUMBER_OF_CANDIDATE_TRANSIENTS) to include in the combined list ("`basename $0`")</td></tr>" >> "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"
 else
  grep --quiet 'ERROR' "$INPUT_DIR/index.html" | grep 'stuck camera'
  if [ $? -eq 0 ];then
   FIELD=`grep 'Processing fields' "$INPUT_DIR/index.html" | sed 's:Processing:processing:g' | sed 's:<br>::g' | awk '{print $1}'`
   echo "<td><font color='#FF0033'>CAMERA STUCK</font></td><td><a href='$INPUT_DIR/' target='_blank'>log</a></td><td></td><td></td><td></td></tr>" >> "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"
  else
   ### Check for all other errors
   grep --quiet 'ERROR' "$INPUT_DIR/index.html"
   if [ $? -eq 0 ] ;then
    ERROR_MSG=$(grep --max-count=1 'ERROR' "$INPUT_DIR/index.html")
    echo "<td><font color='#FF0033'>ERROR</font></td><td><a href='$INPUT_DIR/' target='_blank'>log</a></td><td>$IMAGE_CENTER_OFFSET_FROM_REF_IMAGE</td><td></td><td>$ERROR_MSG</td></tr>" >> "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"
   else
    WARNING_MSG=$(grep 'WARNING' "$INPUT_DIR/index.html" | tail -n1)
    echo "<td><font color='green'>OK</font></td><td><a href='$INPUT_DIR/' target='_blank'>log</a></td><td>$IMAGE_CENTER_OFFSET_FROM_REF_IMAGE</td><td>$MAG_LIMIT</td><td>$WARNING_MSG</td></tr>" >> "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"
    ####
    # Re-create filtered list of candidates (no asteroids, no known variables)
    #"$SCRIPTDIR"/filter_report.py "$OUTPUT_COMBINED_HTML_NAME"
    ####
    # Remove this field from the observing plan
    export N_FIELD_FOUND_IN_PLAN=0 
    command -v dos2unix &>/dev/null && command -v unix2dos &>/dev/null
    if [ $? -eq 0 ];then
     cat plan.txt | dos2unix | dos2unix | while read STR ;do 
      if [ $N_FIELD_FOUND_IN_PLAN -eq 0 ];then 
       echo "$STR" | grep --quiet -e "$FIELD"$'\n' -e "$FIELD " && export N_FIELD_FOUND_IN_PLAN=1 && continue 
      fi 
      echo $STR 
     done > plan.tmp
     cat plan.tmp | unix2dos | unix2dos > plan.txt
     rm -f plan.tmp
    fi # if [ $? -eq 0 ];then
    ####
   fi # grep --quiet 'ERROR' "$INPUT_DIR/index.html"
  fi # 'camera is stuck'
 fi # if [ "$INCLUDE_REPORT_IN_COMBINED_LIST" != "OK" ];then
 #
 
done

# Try regenerating the filtered report every time
if [ -s "$OUTPUT_COMBINED_HTML_NAME" ];then
 "$SCRIPTDIR"/filter_report.py "$OUTPUT_COMBINED_HTML_NAME" &
fi

# update results_comets.txt - do the update if we saw a comet that night with this camera
grep --quiet 'comets.txt' "$OUTPUT_COMBINED_HTML_NAME"
if [ $? -eq 0 ];then
 "$SCRIPTDIR"/combine_results_comets.sh &
fi

# wait for the child processes to complete
wait

done # for CAMERA in Stas Nazar ;do

rm -f "${LOCKFILE}"

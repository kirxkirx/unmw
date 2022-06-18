#!/usr/bin/env bash

# You probably want to add this script to /etc/crontab
#*/8     *       *       *       *       www-data        /dataX/cgi-bin/unmw/combine_reports.sh &> /dev/null

# Check that no other instances of the script are running
N_RUN=`ps ax | grep combine_reports.sh | grep -v grep | grep bash | grep -c combine_reports.sh`
# This is conter-intuitive but the use of the construct N_RUN=`` will create a second copy of "bash ./combine_reports.sh" in the ps output
# So one running copy of the script corresponds to N_RUN=2
if [ $N_RUN -gt 2 ];then
# echo "DEBUG DA"
 exit 0
fi

# change to the work directory
SCRIPTDIR=`readlink -f $0`
SCRIPTDIR=`dirname "$SCRIPTDIR"`
cd "$SCRIPTDIR"
# source the local settings file if it exist
# it may countain curl e-mail and data processing directory settings
if [ -s local_config.sh ];then
 source local_config.sh
fi
# uploads/ is the default location for the processing data (both mages and results)
if [ -d "uploads" ];then
 cd "uploads"
fi
# DATA_PROCESSING_ROOT may be exported in local_config.sh
# if it is set properly - go there
if [ ! -z "$DATA_PROCESSING_ROOT" ];then
 if [ -d "$DATA_PROCESSING_ROOT" ];then
  cd "$DATA_PROCESSING_ROOT"
 fi
fi
#
# URL_OF_DATA_PROCESSING_ROOT specifies where the data processing root is accessible online
# URL_OF_DATA_PROCESSING_ROOT may be exported in local_config.sh
if [ ! -z "$URL_OF_DATA_PROCESSING_ROOT" ];then
 # if it is not set, go with the default valeu
 URL_OF_DATA_PROCESSING_ROOT="http://vast.sai.msu.ru/unmw/uploads"
fi

# loop through the cameras
for CAMERA in Stas Nazar Planeta ;do

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

#INPUT_LIST_OF_RESULT_DIRS=`find -maxdepth 1 -type d -mtime -1 -name 'results*'`
######################################################### 12 hours
INPUT_LIST_OF_RESULT_DIRS=`find -maxdepth 1 -type d -mmin -720 -name "results*$CAMERA*"`

if [ -z "$INPUT_LIST_OF_RESULT_DIRS" ];then
 # nothing to process, continue to the next camera
 continue
fi

# a silly attmpt to make sure files are sorted in time and only completed files are listed
LIST_OF_FILES=""
for INPUT_DIR in $INPUT_LIST_OF_RESULT_DIRS ;do
 # check that this reporthas not apeared in a combined report before
 grep --quiet "$INPUT_DIR/index.html" combine_reports.log
 if [ $? -eq 0 ];then
  continue
 fi
 if [ -f $INPUT_DIR/index.html.combine_reports_lock ];then
  continue
 fi
 LIST_OF_FILES="$LIST_OF_FILES $INPUT_DIR/index.html"
done

#echo "DEBUG000 #$LIST_OF_FILES#"

if [ -z "$LIST_OF_FILES" ];then
 # nothing to process, continue to the next camera
 continue
fi

SORTED_LIST_OF_FILES=`ls -tr $LIST_OF_FILES`

INPUT_LIST_OF_RESULT_DIRS=""
for FILE in $SORTED_LIST_OF_FILES ;do
 grep --quiet 'Processig complete' $FILE
 if [ $? -ne 0 ];then
  continue
 fi
 # lock the directory in case second instance of combine_reports.sh will start before we finish
 touch $INPUT_DIR/index.html.combine_reports_lock
 #
 INPUT_LIST_OF_RESULT_DIRS="$INPUT_LIST_OF_RESULT_DIRS "`dirname $FILE`
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

  grep --max-count=1 -B10000 '<BODY>' "$INPUT_DIR/index.html" > "$OUTPUT_COMBINED_HTML_NAME" && break
 
 done

 ################ 
 if [ ! -f index.html ];then
  # make head
  echo "<HTML>
   <BODY>
   
   <table align='center' width='50%' border='0' class='main'>" > index.html
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
 echo "<HTML>
<BODY>

<table align='center' width='100%' border='0' class='main'>
<tr><th>Camera</th><th>Time</th><th>Field</th><th>-</th><th>Status</th><th>Log</th><th>Offset</th><th>Comments</th></tr>" > "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"

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
  MSG="Too mnay candidates ($NUMBER_OF_CANDIDATE_TRANSIENTS) in $URL_OF_DATA_PROCESSING_ROOT/$INPUT_DIR/"
  INCLUDE_REPORT_IN_COMBINED_LIST="ERROR"
 fi
 echo "$INPUT_DIR/index.html" >> combine_reports.log
 
 # Summary file
 # FIELD moved up to give special treatment to Sco6
 #FIELD=`grep 'Processing fields' "$INPUT_DIR/index.html" | sed 's:Processing:processing:g' | sed 's:processing fields::g' | sed 's:<br>::g'` 
 LAST_IMAGE_DATE=`grep 'Last  image' "$INPUT_DIR/index.html" | head -n1 | awk '{print $4" "$5}'`
 TIMESYS_OF_LAST_IMAGE_DATE=`grep 'time system' "$INPUT_DIR/index.html" | head -n1 | awk '{print $5}'`
 LAST_IMAGE_DATE="$LAST_IMAGE_DATE $TIMESYS_OF_LAST_IMAGE_DATE"
 IMAGE_CENTER_OFFSET_FROM_REF_IMAGE=`grep 'Angular distance between the image centers' "$INPUT_DIR/index.html" | sed 's:deg.::g' | tail -n1 | awk '{print $7}'`
 echo -n "<tr><td>$CAMERA</td><td>$LAST_IMAGE_DATE</td><td><font color='teal'> $FIELD </font></td><td>&nbsp;&nbsp;&mdash;&nbsp;&nbsp;</td>" >> "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"
 if [ "$INCLUDE_REPORT_IN_COMBINED_LIST" != "OK" ];then
  echo "<td><font color='#FF0033'>ERROR</font></td><td><a href='$URL_OF_DATA_PROCESSING_ROOT/$INPUT_DIR/' target='_blank'>log</a></td><td></td><td>too many candidates ($NUMBER_OF_CANDIDATE_TRANSIENTS) to include in the combined list ("`basename $0`")</td></tr>" >> "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"
 else
  grep --quiet 'ERROR' "$INPUT_DIR/index.html" | grep 'stuck camera'
  if [ $? -eq 0 ];then
   FIELD=`grep 'Processing fields' "$INPUT_DIR/index.html" | sed 's:Processing:processing:g' | sed 's:<br>::g' | awk '{print $1}'`
   echo "<td><font color='#FF0033'>CAMERA STUCK</font></td><td><a href='$URL_OF_DATA_PROCESSING_ROOT/$INPUT_DIR/' target='_blank'>log</a></td><td></td><td></td></tr>" >> "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"
  else
   ### Check for all other errors
   grep --quiet 'ERROR' "$INPUT_DIR/index.html"
   if [ $? -eq 0 ] ;then
    ERROR_MSG=`grep --max-count=1 'ERROR' "$INPUT_DIR/index.html"`
    echo "<td><font color='#FF0033'>ERROR</font></td><td><a href='$URL_OF_DATA_PROCESSING_ROOT/$INPUT_DIR/' target='_blank'>log</a></td><td>$IMAGE_CENTER_OFFSET_FROM_REF_IMAGE</td><td>$ERROR_MSG</td><tr>" >> "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"
   else
    echo "<td><font color='green'>OK</font></td><td><a href='$URL_OF_DATA_PROCESSING_ROOT/$INPUT_DIR/' target='_blank'>log</a></td><td>$IMAGE_CENTER_OFFSET_FROM_REF_IMAGE</td><td></td><tr>" >> "$OUTPUT_PROCESSING_SUMMARY_HTML_NAME"
    ####
    # Create filtered list of candidates (no asteroids, no known variables)
    "$SCRIPTDIR"/filter_report.py "$OUTPUT_COMBINED_HTML_NAME"
    ####
    # Remove this field from the observing plan
    export N_FIELD_FOUND_IN_PLAN=0 
    cat plan.txt | dos2unix | dos2unix | while read STR ;do 
     if [ $N_FIELD_FOUND_IN_PLAN -eq 0 ];then 
      echo "$STR" | grep --quiet -e "$FIELD"$'\n' -e "$FIELD " && export N_FIELD_FOUND_IN_PLAN=1 && continue 
     fi 
     echo $STR 
    done > plan.tmp
    cat plan.tmp | unix2dos | unix2dos > plan.txt
    rm -f plan.tmp
    ####
   fi # grep --quiet 'ERROR' "$INPUT_DIR/index.html"
  fi # 'camera is stuck'
 fi # if [ "$INCLUDE_REPORT_IN_COMBINED_LIST" != "OK" ];then
 #
 
done

#echo "</BODY>
#</HTML>"

done # for CAMERA in Stas Nazar ;do


#!/usr/bin/env bash
# Closed observatory roof Slack alert; autoprocess.sh sources this file.
#
# VaST's util/transients/transient_factory_test31.sh writes a line containing
# both 'ERROR' and 'OBSERVATORY ROOF CLOSED' into transient_report/index.html
# when the latest new image of a field shows no stars and no sky light above
# the dark level: the observatory roof is closed, but the camera keeps imaging
# (NINA does not know that the roof is closed). autoprocess.sh then calls
# roof_closed_alert, which posts one message to the Slack incoming webhook
# SLACK_WEBHOOK_URL_TEAM naming the camera, the field, the time of the frame
# and the results page, and prints its decision for the autoprocess.txt record.
# The alert is rate-limited per camera (roof_closed_alert_state below), and a
# successful run of the same camera re-arms it (roof_closed_alert_rearm).
#
# Settings (export them from local_config.sh, see local_config.sh_example):
#  ROOF_CLOSED_SLACK_ALERT=no          never send this alert
#  ROOF_CLOSED_ALERT_REPEAT_HOURS=3    at most one alert per camera per this many hours
#  ROOF_CLOSED_ALERT_MAX_AGE_HOURS=6   no alert for a frame taken longer ago than this
#
# Functions only: sourcing this file changes nothing else.
if [ -n "${GATEWAY_INTERFACE:-}" ] || [ -n "${REQUEST_METHOD:-}" ]; then
 printf 'Content-Type: text/plain\n\nERROR: this script must not be run as CGI\n'
 exit 1
fi

function roof_closed_report_camera {
 # The camera of a report: the 'CAMERA_SETTINGS= <camera>' line VaST prints in
 # the processing parameters, or else the first camera of the list below whose
 # name is part of the dataset (archive) name, or else 'unknown'.
 # $1 - report (index.html), $2 - dataset name
 local camera
 local known_camera
 camera=$(grep --max-count=1 '^CAMERA_SETTINGS= ' "$1" 2>/dev/null | awk '{print $2}' | tr -cd 'A-Za-z0-9._+-' | cut -c1-64)
 if [ -z "$camera" ];then
  # The cameras of the loop in combine_reports.sh - keep the two lists in step
  for known_camera in Stas STL-11000M TICA_TESS ED80__Black TTUQ1b1x1 TTUQ2b1x1 ;do
   case "$2" in
    *"$known_camera"*)
     camera="$known_camera"
     break
     ;;
   esac
  done
 fi
 if [ -z "$camera" ];then
  camera="unknown"
 fi
 printf '%s\n' "$camera"
}

function roof_closed_report_field {
 # The (first) field name of the 'Processing fields <FIELD> <br>' line, or nothing
 # $1 - report (index.html)
 grep --max-count=1 'Processing fields' "$1" 2>/dev/null | sed -e 's/.*Processing fields//' -e 's/<br>.*//' | awk '{print $1}' | tr -cd 'A-Za-z0-9._+-' | cut -c1-64
}

function roof_closed_report_frame_jd {
 # The JD of the first 'Last  image: <JD> <DD.MM.YYYY> <HH:MM:SS.sss> UTC' line
 # (for a successful run: the newest image of the run), or nothing
 # $1 - report (index.html)
 grep --max-count=1 'Last  image:' "$1" 2>/dev/null | awk '{
  for (i = 1; i < NF; i++) {
   if ($i == "image:") {
    break
   }
  }
  jd = $(i + 1)
  if (jd ~ /^[0-9]+([.][0-9]+)?$/ && jd + 0 > 2400000 && jd + 0 < 2600000) {
   print jd
  }
 }'
}

function roof_closed_report_frame_time {
 # The date and time of the first 'Last  image:' line as 'DD.MM.YYYY HH:MM:SS UTC'
 # (whole seconds; the time system VaST prints, UTC when it prints none), or nothing
 # $1 - report (index.html)
 grep --max-count=1 'Last  image:' "$1" 2>/dev/null | awk '{
  for (i = 1; i < NF; i++) {
   if ($i == "image:") {
    break
   }
  }
  frame_date = $(i + 2)
  frame_clock = $(i + 3)
  time_system = $(i + 4)
  if (frame_date !~ /^[0-9][0-9][.][0-9][0-9][.][0-9][0-9][0-9][0-9]$/ || frame_clock !~ /^[0-9][0-9]:[0-9][0-9]:[0-9][0-9]/) {
   exit
  }
  if (time_system !~ /^[A-Z][A-Z][A-Z]?$/) {
   time_system = "UTC"
  }
  printf "%s %s %s\n", frame_date, substr(frame_clock, 1, 8), time_system
 }'
}

function roof_closed_slack_escape {
 # Escape the characters Slack mrkdwn treats specially: & < >
 printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

function roof_closed_alert_text {
 # The Slack message
 # $1 - camera, $2 - field (may be empty), $3 - frame time (may be empty), $4 - results URL
 local camera
 local field
 local frame_time
 local url
 local images
 camera=$(roof_closed_slack_escape "$1")
 field=$(roof_closed_slack_escape "$2")
 frame_time=$(roof_closed_slack_escape "$3")
 url=$(roof_closed_slack_escape "$4")
 images="the latest new image"
 if [ -n "$field" ];then
  images="$images of $field"
 fi
 if [ -n "$frame_time" ];then
  images="$images taken $frame_time"
 fi
 printf '%s\n' ":warning: *Observatory roof appears to be CLOSED* - camera *$camera*: no stars and no sky light on $images, but the camera keeps imaging (NINA may not know that the roof is closed). Please check the observatory. Log: $url"
}

function roof_closed_alert_state {
 # Read and update the alert state of one camera under an exclusive lock (python3
 # fcntl.flock works on Linux, FreeBSD and macOS). The state file is the lock: it
 # is never deleted, only rewritten. It holds one line
 #  last_alert_unix=<unix time of the last alert, 0 = re-armed>
 #  alerted_closed_jd=<JD of the closed-roof frame of the last alert>
 #  newest_closed_jd=<JD of the newest closed-roof frame> newest_ok_jd=<JD of the newest good frame>
 # Processing order is not observation order, so the frame JDs decide, not the
 # order in which the runs finish.
 # $1 - state file, $2 - action, then its arguments:
 #  closed FRAME_JD NOW REPEAT_HOURS - a closed-roof run (FRAME_JD 0 = unknown).
 #   Exit 0 and "send previous=<unix time>": the alert is due, the slot is taken
 #   (last_alert_unix=NOW). Exit 1 and the reason not to send:
 #   "recent hours_since_alert=<h>" (an alert went out less than REPEAT_HOURS
 #   ago) or "older_than_ok newest_ok_jd=<JD>" (a good frame taken after this
 #   one was already processed: the roof was open again by then).
 #  ok FRAME_JD NOW - a successful run: records the newest good frame and prints
 #   "rearmed alerted_closed_jd=<JD>" when an alert was sent and this frame was
 #   taken after the closed-roof frame of that alert - the roof was open again
 #   (the next closed-roof run alerts at once) - and "unchanged" otherwise. Never
 #   creates the file ("no_state" without one).
 # A frame JD more than 10 min after NOW comes from a camera clock running ahead:
 # it is treated as unknown and never recorded, and such a JD found in the state
 # is dropped, so that a wrong clock cannot block later alerts.
 #  release NOW PREVIOUS - the POST of the alert failed: give back the slot that
 #   "closed" took at NOW, so that the next closed-roof run tries again.
 # Exit 2 and a message on stderr: unusable state - like nmw_coord_lib.open_lock_file,
 # a symlink, a non-regular file, a hard link or a file of another user is refused.
 python3 -I - "$@" <<'ROOF_CLOSED_ALERT_STATE_PY'
import errno
import fcntl
import math
import os
import stat
import sys
import time


def number(text):
    try:
        value = float(text)
    except ValueError:
        return 0.0
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value


def fail(message):
    sys.stderr.write('roof_closed_alert_state: ' + message + '\n')
    sys.exit(2)


ARGUMENT_COUNTS = {'closed': 3, 'ok': 2, 'release': 2}
if len(sys.argv) < 3 or ARGUMENT_COUNTS.get(sys.argv[2]) != len(sys.argv) - 3:
    fail('usage: STATE_FILE closed FRAME_JD NOW REPEAT_HOURS | ok FRAME_JD NOW | release NOW PREVIOUS')
path, action = sys.argv[1], sys.argv[2]
args = [number(arg) for arg in sys.argv[3:]]
flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
if action == 'closed':
    flags |= os.O_CREAT
try:
    fd = os.open(path, flags, 0o644)
except OSError as error:
    if error.errno == errno.ENOENT and action != 'closed':
        print('no_state')
        sys.exit(0)
    fail('cannot open it: ' + error.strerror)
info = os.fstat(fd)
if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
    os.close(fd)
    fail('refused: not a regular file with a single link owned by this user')
handle = os.fdopen(fd, 'r+b')
deadline = time.time() + 60
while True:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError as error:
        if error.errno not in (errno.EAGAIN, errno.EACCES) or time.time() > deadline:
            fail('cannot lock it: ' + error.strerror)
        time.sleep(0.05)
state = {'last_alert_unix': 0.0, 'alerted_closed_jd': 0.0, 'newest_closed_jd': 0.0, 'newest_ok_jd': 0.0}
for token in handle.read(4096).decode('ascii', 'replace').split():
    key, _, value = token.partition('=')
    if key in state:
        state[key] = number(value)


def frame_jd_or_unknown(jd, now):
    # A frame JD more than 10 min after now comes from a camera clock running ahead:
    # drop such JDs recorded earlier, and treat this frame as one of unknown time (0)
    latest_plausible_jd = (now + 600) / 86400.0 + 2440587.5
    for key in ('alerted_closed_jd', 'newest_closed_jd', 'newest_ok_jd'):
        if state[key] > latest_plausible_jd:
            state[key] = 0.0
    if jd > latest_plausible_jd:
        return 0.0
    return jd


def save():
    handle.seek(0)
    handle.truncate()
    handle.write(('last_alert_unix=%d alerted_closed_jd=%.8f newest_closed_jd=%.8f newest_ok_jd=%.8f\n' % (
        state['last_alert_unix'], state['alerted_closed_jd'], state['newest_closed_jd'],
        state['newest_ok_jd'])).encode('ascii'))
    handle.flush()


stored_state = dict(state)
if action == 'closed':
    jd, now, repeat_hours = args
    jd = frame_jd_or_unknown(jd, now)
    if jd > 0 and state['newest_ok_jd'] > jd:
        if state != stored_state:
            save()
        print('older_than_ok newest_ok_jd=%.5f' % state['newest_ok_jd'])
        sys.exit(1)
    state['newest_closed_jd'] = max(state['newest_closed_jd'], jd)
    since = now - state['last_alert_unix']
    # A last alert more than 10 min in the future means a broken clock, not a recent alert
    if state['last_alert_unix'] > 0 and -600 <= since < repeat_hours * 3600:
        save()
        print('recent hours_since_alert=%.1f' % (max(since, 0) / 3600))
        sys.exit(1)
    previous = state['last_alert_unix']
    state['last_alert_unix'] = now
    # the closed-roof frame of this alert (the newest known one if its time is unknown):
    # a good frame taken after it re-arms the alert, whatever order the runs finish in
    state['alerted_closed_jd'] = jd if jd > 0 else state['newest_closed_jd']
    save()
    print('send previous=%d' % previous)
elif action == 'ok':
    jd = frame_jd_or_unknown(args[0], args[1])
    rearm = jd > 0 and state['last_alert_unix'] > 0 and jd > state['alerted_closed_jd']
    if jd > state['newest_ok_jd'] or rearm or state != stored_state:
        state['newest_ok_jd'] = max(state['newest_ok_jd'], jd)
        if rearm:
            state['last_alert_unix'] = 0
        save()
    if rearm:
        print('rearmed alerted_closed_jd=%.5f' % state['alerted_closed_jd'])
    else:
        print('unchanged')
else:
    reserved, previous = args
    if state['last_alert_unix'] == reserved:
        state['last_alert_unix'] = previous
        save()
        print('released')
    else:
        print('kept')
ROOF_CLOSED_ALERT_STATE_PY
}

function roof_closed_send_slack {
 # POST the text $1 to SLACK_WEBHOOK_URL_TEAM. Prints "<curl exit code> <Slack response>"
 # (Slack answers "ok"); the response is cut to one line of plain text with the
 # webhook URL - a secret - removed, as it goes into the web-visible autoprocess.txt
 local response
 local curl_exit_code
 response=$(curl --silent --show-error --max-time 30 -X POST -H 'Content-type: application/json' \
      --data "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps({"text": sys.stdin.read()}))')" \
      "$SLACK_WEBHOOK_URL_TEAM" 2>&1)
 curl_exit_code=$?
 response="${response//"$SLACK_WEBHOOK_URL_TEAM"/(webhook URL)}"
 response=$(printf '%s' "$response" | tr -c 'A-Za-z0-9 ._:,;()/=+-' ' ' | tr -s ' ' | sed -e 's/^ //' -e 's/ $//' | cut -c1-200)
 printf '%s %s\n' "$curl_exit_code" "$response"
}

function roof_closed_alert {
 # Send the closed-roof Slack alert for a report that has the OBSERVATORY ROOF
 # CLOSED line, if it is due; print the decision as one line for autoprocess.txt.
 # $1 - report (index.html), $2 - results URL
 # Reads the autoprocess.sh variables TEST_RUN, INPUT_DIR_NOT_ZIP_ARCHIVE,
 # DATASET_NAME and DATA_PROCESSING_ROOT, the hook SLACK_WEBHOOK_URL_TEAM and the
 # settings ROOF_CLOSED_SLACK_ALERT, ROOF_CLOSED_ALERT_REPEAT_HOURS and
 # ROOF_CLOSED_ALERT_MAX_AGE_HOURS.
 local report="$1"
 local results_url="$2"
 local camera
 local prefix
 local repeat_hours
 local max_age_hours
 local now
 local frame_jd
 local frame_age_hours
 local state_file
 local state_output
 local state_exit_code
 local state_problem=""
 local previous_alert=""
 local text
 local send_output
 local curl_exit_code
 local slack_response
 local decision
 camera=$(roof_closed_report_camera "$report" "$DATASET_NAME")
 prefix="Closed-roof Slack alert (camera $camera)"
 if [ "${TEST_RUN:-0}" != "0" ];then
  echo "$prefix: suppressed - test run (the dataset name contains 'test')"
  return 0
 fi
 if [ "${INPUT_DIR_NOT_ZIP_ARCHIVE:-0}" != "0" ];then
  echo "$prefix: suppressed - manual reprocessing of an image directory, not an uploaded archive"
  return 0
 fi
 case "$ROOF_CLOSED_SLACK_ALERT" in
  [Nn][Oo]|[Nn]|[Ff][Aa][Ll][Ss][Ee]|[Oo][Ff][Ff]|0)
   echo "$prefix: suppressed - disabled with ROOF_CLOSED_SLACK_ALERT=$ROOF_CLOSED_SLACK_ALERT"
   return 0
   ;;
 esac
 if [ -z "$SLACK_WEBHOOK_URL_TEAM" ];then
  echo "$prefix: not sent - SLACK_WEBHOOK_URL_TEAM is not set"
  return 0
 fi
 repeat_hours="$ROOF_CLOSED_ALERT_REPEAT_HOURS"
 if ! [[ "$repeat_hours" =~ ^[0-9]+([.][0-9]+)?$ ]];then
  repeat_hours=3
 fi
 max_age_hours="$ROOF_CLOSED_ALERT_MAX_AGE_HOURS"
 if ! [[ "$max_age_hours" =~ ^[0-9]+([.][0-9]+)?$ ]];then
  max_age_hours=6
 fi
 now=$(date +%s)
 # A frame of unknown time may well be recent: alert
 frame_jd=$(roof_closed_report_frame_jd "$report")
 if [ -n "$frame_jd" ];then
  frame_age_hours=$(awk -v jd="$frame_jd" -v now="$now" 'BEGIN {printf "%.1f", (now / 86400.0 + 2440587.5 - jd) * 24.0}')
  if awk -v jd="$frame_jd" -v now="$now" -v max="$max_age_hours" 'BEGIN {exit !((now / 86400.0 + 2440587.5 - jd) * 24.0 > max + 0)}' ;then
   echo "$prefix: suppressed - the frame was taken $frame_age_hours h ago (ROOF_CLOSED_ALERT_MAX_AGE_HOURS=$max_age_hours)"
   return 0
  fi
 fi
 # One state file per camera (all unidentified cameras share 'unknown')
 state_file="$DATA_PROCESSING_ROOT/roof_closed_alert_${camera}.state"
 state_output=$(roof_closed_alert_state "$state_file" closed "${frame_jd:-0}" "$now" "$repeat_hours" 2>&1)
 state_exit_code=$?
 case "$state_exit_code:$state_output" in
  "0:send previous="*)
   previous_alert="${state_output##*=}"
   ;;
  "1:recent hours_since_alert="*)
   echo "$prefix: suppressed - an alert for this camera was sent ${state_output##*=} h ago (ROOF_CLOSED_ALERT_REPEAT_HOURS=$repeat_hours)"
   return 0
   ;;
  "1:older_than_ok newest_ok_jd="*)
   echo "$prefix: suppressed - a good frame of this camera taken after this one (JD ${state_output##*=}) was already processed"
   return 0
   ;;
  *)
   # Better an alert too many than none: send without the rate limit
   state_problem=$(printf '%s' "$state_output" | tr '\n' ' ' | cut -c1-200)
   state_problem="; the rate-limit state $state_file is unusable, so the rate limit was not applied ($state_problem)"
   ;;
 esac
 text=$(roof_closed_alert_text "$camera" "$(roof_closed_report_field "$report")" "$(roof_closed_report_frame_time "$report")" "$results_url")
 send_output=$(roof_closed_send_slack "$text")
 curl_exit_code="${send_output%% *}"
 slack_response="${send_output#* }"
 if [ "$curl_exit_code" = "0" ] && [ "$slack_response" = "ok" ];then
  decision="sent (curl exit code 0, Slack response: ok)"
 else
  decision="NOT delivered (curl exit code $curl_exit_code, Slack response: ${slack_response:-none})"
  if [ -n "$previous_alert" ];then
   roof_closed_alert_state "$state_file" release "$now" "$previous_alert" > /dev/null 2>&1
   decision="$decision - the next closed-roof run of this camera will try again"
  fi
 fi
 echo "$prefix: $decision$state_problem"
 return 0
}

function roof_closed_alert_rearm {
 # After a successful run (verdict ok): re-arm the closed-roof alert of the camera
 # when the newest frame of the run was taken after the closed-roof frame of the
 # last alert (the roof was open again since then).
 # Prints a line for autoprocess.txt only when the alert was re-armed.
 # $1 - report (index.html); reads TEST_RUN, INPUT_DIR_NOT_ZIP_ARCHIVE,
 # DATASET_NAME and DATA_PROCESSING_ROOT like roof_closed_alert.
 local camera
 local state_file
 local frame_jd
 local state_output
 if [ "${TEST_RUN:-0}" != "0" ] || [ "${INPUT_DIR_NOT_ZIP_ARCHIVE:-0}" != "0" ];then
  return 0
 fi
 camera=$(roof_closed_report_camera "$1" "$DATASET_NAME")
 state_file="$DATA_PROCESSING_ROOT/roof_closed_alert_${camera}.state"
 # Nothing to re-arm for a camera that never had a closed-roof alert here
 if [ "$camera" = "unknown" ] || [ ! -f "$state_file" ];then
  return 0
 fi
 frame_jd=$(roof_closed_report_frame_jd "$1")
 if [ -z "$frame_jd" ];then
  return 0
 fi
 state_output=$(roof_closed_alert_state "$state_file" ok "$frame_jd" "$(date +%s)" 2>&1)
 case "$state_output" in
  "rearmed alerted_closed_jd=0.00000")
   echo "Closed-roof Slack alert (camera $camera): re-armed - this run's frame (JD $frame_jd) was taken after the last alert (the time of its closed-roof frame is unknown)"
   ;;
  "rearmed alerted_closed_jd="*)
   echo "Closed-roof Slack alert (camera $camera): re-armed - this run's frame (JD $frame_jd) was taken after the closed-roof frame of the last alert (JD ${state_output##*=})"
   ;;
 esac
 return 0
}

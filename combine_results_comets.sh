#!/usr/bin/env bash                                                                                        

# change to the work directory
SCRIPTDIR=$(dirname "$(readlink -f "$0")")
cd "$SCRIPTDIR" || exit 1

if [ ! -d "uploads" ];then
 echo "ERROR: no 'uploads' dir"
 exit 1
fi

#2024 01 01.6422  2460311.1422  12.90  19:38:41.84 +37:43:59.1      11.2     12P/Pons-Brooks
echo "#
#YYYY MM DD.DDDD    JD(UTC)     CVmag  measured_RA measured_Dec predict_mag  comet_name
#" > uploads/results_comets.txt

for i in uploads/*morning*.html uploads/*evening*.html ;do 
 echo "$i" | grep --quiet -e '_filtered.html' -e '_summary.html' -e '2020...._' -e '2021...._' -e '2022...._'
 if [ $? -eq 0 ];then
  continue
 fi
 if [ ! -s "$i" ];then
  continue
 fi
 grep --quiet 'comets.txt' "$i"
 if [ $? -ne 0 ];then
  continue
 fi
 grep -B1 'comets.txt' "$i" | grep -v 'comets.txt' | grep ':' | while read MEASUREMENT ;do
  if [ -z "$MEASUREMENT" ];then
   break
  fi
  COMET_ID=$(grep -A2 "$MEASUREMENT" "$i" | awk -F'" ' '{printf "%s\"%s",$2,$3}' | awk -F'</b>' '{print $1}' | grep -v '^$')
  COMET_DIST_ARCSEC=$(echo "$COMET_ID" | awk -F'comets.txt</font> ' '{print $2}' | awk '{print $1}' | awk -F'"' '{print $1}')
  echo "$COMET_DIST_ARCSEC" | awk '{if( $1>30 ) print "TOO FAR"}' | grep --quiet "TOO FAR" && continue
  COMET_ID=$(echo "$COMET_ID" | awk -F'" ' '{print $2}')
  if [ -z "$COMET_ID" ];then
   continue
  fi
  COMET_MAG=$(echo "$COMET_ID" | awk -F'mag' '{printf "%4.1f", $1}')
  COMET_NAME=$(echo "$COMET_ID" | awk -F'mag' '{print $2}')
  #2024 03 25.6986  2460395.1986  7.40  01:40:57.07 +26:33:21.2
  MEASUREMENT_EDIT=$(echo "$MEASUREMENT" | awk '{printf "%s %s %s  %s  %5.2f  %s %s", $1,$2,$3,$4,$5,$6,$7}')
  #echo " $MEASUREMENT      $COMET_MAG    $COMET_NAME"
  echo " $MEASUREMENT_EDIT      $COMET_MAG    $COMET_NAME"
 done
done | sort -k 9 >> uploads/results_comets.txt

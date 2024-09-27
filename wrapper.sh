#!/usr/bin/env bash

# source the local settings file
# (these are site-specific settings not meant to be uploaded to GitHub)
if [ -s local_config.sh ];then
 source local_config.sh
fi

# You may specify an external plate-solve server here
# but better move it to local_config.sh
if [ -z "$ASTROMETRYNET_LOCAL_OR_REMOTE" ];then
 ASTROMETRYNET_LOCAL_OR_REMOTE="local"
 #export ASTROMETRYNET_LOCAL_OR_REMOTE="remote"
 #export FORCE_PLATE_SOLVE_SERVER="scan.sai.msu.ru"
fi

./autoprocess.sh $1 &>/dev/null &

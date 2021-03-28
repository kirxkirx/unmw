#!/usr/bin/env bash

# source the local settings file
# (these are site-specific settings not meant to be uploaded to GitHub)
if [ -s local_config.sh ];then
 source local_config.sh
fi

# You may specify an external plate-solev server here
#export ASTROMETRYNET_LOCAL_OR_REMOTE="remote"
#export FORCE_PLATE_SOLVE_SERVER="scan.sai.msu.ru"

./autoprocess.sh $1 &>/dev/null &

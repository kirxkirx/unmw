#!/usr/bin/env bash
#
# Install the static web interface pages from move_to_htdocs/ into the
# htdocs directory they are served from, adapting the main page to this
# host's configuration:
#  - the archival forced photometry link is removed from index.html when
#    IMAGE_ARCHIVE_DIR is not set in local_config.sh (the feature is
#    disabled on such hosts, see archive_forced_photometry.py)
#  - the manual upload page link is removed from index.html and the
#    upload.html page itself is not installed (a previously installed copy
#    is removed) unless SHOW_MANUAL_UPLOAD_LINK=yes is set in
#    local_config.sh (hidden by default as the links page may be
#    public-facing; the upload.py endpoint always stays in place - it is
#    required by the automated astrocam-go uploads)
#
# The target directory is taken from the first command line argument or,
# when no argument is given, from HTDOCS_DIR in local_config.sh. When
# neither is set the script does nothing and exits successfully, so it is
# safe to call unconditionally from git_unmw_automated_update.sh: hosts
# that deploy the pages by manually copying move_to_htdocs/ are unaffected.
#
# Usage:
#   ./generate_htdocs.sh [/path/to/htdocs/unmw]
#
# The script only writes the interface files; the only thing it ever
# deletes from the target directory (where the 'uploads' data symlink
# lives) is a previously installed upload.html when the manual upload
# page is disabled.

#################################
# Set the safe locale that should be available on any POSIX system
LC_ALL=C
LANGUAGE=C
export LANGUAGE LC_ALL
#################################

# Guard: refuse to run if invoked as CGI
if [ -n "$GATEWAY_INTERFACE" ] || [ -n "$REQUEST_METHOD" ]; then
    echo "Content-Type: text/plain"
    echo ""
    echo "ERROR: this script must not be run as CGI"
    exit 1
fi

# The installed pages must be readable by the web server no matter how
# restrictive the umask of the invoking user (cron, root shell) is
umask 022

# Remember where we were invoked from (to resolve a relative target path),
# then work from the directory this script lives in (the unmw git checkout)
ORIG_PWD="$PWD"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Check the flag that local_config.sh was already sourced
if [ "$UNMW_LOCAL_CONFIG_SOURCED" != "yes" ];then
 if [ -s local_config.sh ];then
  # shellcheck source=/dev/null
  source local_config.sh
 fi
fi

TARGET_DIR="$1"
if [ -z "$TARGET_DIR" ];then
 TARGET_DIR="$HTDOCS_DIR"
fi
if [ -z "$TARGET_DIR" ];then
 echo "HTDOCS_DIR is not set in local_config.sh and no target directory is given on the command line - nothing to do.
Usage: $0 [/path/to/htdocs/unmw]
(hosts that copy move_to_htdocs/ to htdocs manually do not need this script)"
 exit 0
fi
# Resolve a relative target path against the caller's original working
# directory, not against the checkout directory we changed into above
case "$TARGET_DIR" in
 /*) ;;
 *) TARGET_DIR="$ORIG_PWD/$TARGET_DIR" ;;
esac
if [ ! -d "$TARGET_DIR" ];then
 echo "ERROR: the target directory $TARGET_DIR does not exist" >&2
 exit 1
fi
if [ ! -d move_to_htdocs ];then
 echo "ERROR: cannot find the move_to_htdocs directory in $SCRIPT_DIR" >&2
 exit 1
fi

# Make sure no temporary files are left behind in the web-served target
# directory, whichever way the script exits
trap 'rm -f "$TARGET_DIR"/*.tmp.$$' EXIT

# Decide which optional parts of the interface are enabled on this host.
# Two optional sections of the index.html template are enclosed in marker comments:
#  - ARCHIVE_PHOTOMETRY_SECTION_BEGIN/END: removed when IMAGE_ARCHIVE_DIR
#    is not set (the feature is disabled on this host)
#  - MANUAL_UPLOAD_SECTION_BEGIN/END: removed unless SHOW_MANUAL_UPLOAD_LINK
#    is set to "yes" (hidden by default - the links page may be public-facing)
# A section is filtered out only when exactly one BEGIN and one END marker
# of its pair are present - if the markers are ever lost from the template
# that section is installed unchanged (fail open; for the archival
# photometry link archive_forced_photometry.py itself tells the visitor
# the feature is not configured).
FILTER_ARCHIVE_SECTION=0
FILTER_UPLOAD_SECTION=0
if [ -n "$IMAGE_ARCHIVE_DIR" ];then
 ARCHIVE_LINK_STATE="shown"
 if [ ! -d "$IMAGE_ARCHIVE_DIR" ];then
  echo "WARNING: IMAGE_ARCHIVE_DIR=$IMAGE_ARCHIVE_DIR is set but the directory does not exist (unmounted?) - keeping the archival photometry link anyway" >&2
 fi
else
 N_BEGIN_MARKERS=$(grep -c 'ARCHIVE_PHOTOMETRY_SECTION_BEGIN' move_to_htdocs/index.html)
 N_END_MARKERS=$(grep -c 'ARCHIVE_PHOTOMETRY_SECTION_END' move_to_htdocs/index.html)
 if [ "$N_BEGIN_MARKERS" -eq 1 ] && [ "$N_END_MARKERS" -eq 1 ];then
  ARCHIVE_LINK_STATE="hidden (IMAGE_ARCHIVE_DIR is not set)"
  FILTER_ARCHIVE_SECTION=1
 else
  ARCHIVE_LINK_STATE="shown (cannot filter: expected one BEGIN and one END marker, found $N_BEGIN_MARKERS and $N_END_MARKERS)"
  echo "WARNING: damaged ARCHIVE_PHOTOMETRY_SECTION markers in move_to_htdocs/index.html (found $N_BEGIN_MARKERS BEGIN and $N_END_MARKERS END) - installing the section unfiltered" >&2
 fi
fi
if [ "$SHOW_MANUAL_UPLOAD_LINK" = "yes" ];then
 UPLOAD_LINK_STATE="shown"
else
 N_BEGIN_MARKERS=$(grep -c 'MANUAL_UPLOAD_SECTION_BEGIN' move_to_htdocs/index.html)
 N_END_MARKERS=$(grep -c 'MANUAL_UPLOAD_SECTION_END' move_to_htdocs/index.html)
 if [ "$N_BEGIN_MARKERS" -eq 1 ] && [ "$N_END_MARKERS" -eq 1 ];then
  UPLOAD_LINK_STATE="hidden, upload.html not installed (set SHOW_MANUAL_UPLOAD_LINK=yes in local_config.sh to enable)"
  FILTER_UPLOAD_SECTION=1
 else
  UPLOAD_LINK_STATE="shown (cannot filter: expected one BEGIN and one END marker, found $N_BEGIN_MARKERS and $N_END_MARKERS)"
  echo "WARNING: damaged MANUAL_UPLOAD_SECTION markers in move_to_htdocs/index.html (found $N_BEGIN_MARKERS BEGIN and $N_END_MARKERS END) - installing the section unfiltered" >&2
 fi
fi

# Copy the static pages as they are (index.html is generated below).
# The manual upload page is skipped - and a previously installed copy is
# removed - when the upload link is filtered out of index.html (in the
# damaged-marker fail-open case above the link stays, so the page must
# stay installed too).
# Atomic write: copy to a temporary name, then rename, so a page that is
# being served is never truncated mid-request.
for SOURCE_FILE in move_to_htdocs/* ;do
 if [ ! -f "$SOURCE_FILE" ];then
  continue
 fi
 SOURCE_BASENAME=$(basename "$SOURCE_FILE")
 if [ "$SOURCE_BASENAME" = "index.html" ];then
  continue
 fi
 if [ "$SOURCE_BASENAME" = "upload.html" ] && [ "$FILTER_UPLOAD_SECTION" -eq 1 ];then
  if [ -f "$TARGET_DIR/upload.html" ];then
   if ! rm -f "$TARGET_DIR/upload.html" ;then
    echo "WARNING: failed to remove the previously installed $TARGET_DIR/upload.html" >&2
   fi
  fi
  continue
 fi
 if ! cp "$SOURCE_FILE" "$TARGET_DIR/$SOURCE_BASENAME.tmp.$$" ;then
  echo "ERROR copying $SOURCE_FILE to $TARGET_DIR" >&2
  exit 1
 fi
 if ! mv "$TARGET_DIR/$SOURCE_BASENAME.tmp.$$" "$TARGET_DIR/$SOURCE_BASENAME" ;then
  echo "ERROR renaming $TARGET_DIR/$SOURCE_BASENAME.tmp.$$" >&2
  exit 1
 fi
done

# Generate index.html for this host.
if [ "$FILTER_ARCHIVE_SECTION" -eq 0 ] && [ "$FILTER_UPLOAD_SECTION" -eq 0 ];then
 if ! cp move_to_htdocs/index.html "$TARGET_DIR/index.html.tmp.$$" ;then
  echo "ERROR copying move_to_htdocs/index.html to $TARGET_DIR" >&2
  exit 1
 fi
else
 if ! awk -v filterarchive="$FILTER_ARCHIVE_SECTION" -v filterupload="$FILTER_UPLOAD_SECTION" '
/ARCHIVE_PHOTOMETRY_SECTION_BEGIN/{if(filterarchive)skip=1}
/MANUAL_UPLOAD_SECTION_BEGIN/{if(filterupload)skip=1}
!skip{print}
/ARCHIVE_PHOTOMETRY_SECTION_END/{if(filterarchive)skip=0}
/MANUAL_UPLOAD_SECTION_END/{if(filterupload)skip=0}
' move_to_htdocs/index.html > "$TARGET_DIR/index.html.tmp.$$" ;then
  echo "ERROR generating index.html in $TARGET_DIR" >&2
  exit 1
 fi
fi
if ! mv "$TARGET_DIR/index.html.tmp.$$" "$TARGET_DIR/index.html" ;then
 echo "ERROR renaming $TARGET_DIR/index.html.tmp.$$" >&2
 exit 1
fi

echo "Web interface pages installed to $TARGET_DIR
  archival photometry link: $ARCHIVE_LINK_STATE
  manual upload link: $UPLOAD_LINK_STATE"

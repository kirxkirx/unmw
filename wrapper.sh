#!/usr/bin/env bash

# If something is wrong, this script exits with code 1
# and upload.py is expected to catch that code and take care of removing the uploaded archive file

if [ -z "$1" ]; then
    echo "Usage: $0 uploaded_image_archive_to_be_deleted_after_processing.zip"
    exit 1
fi

INPUT_FILE="$1"

# Check if the input has a .zip or .rar extension
if [[ ! "$INPUT_FILE" =~ \.zip$ && ! "$INPUT_FILE" =~ \.rar$ ]]; then
    echo "Error: Input file must be a .zip or .rar file."
    exit 1
fi

# rar may end up in /opt/bin
# Function to check for binaries and update PATH
check_and_update_path() {
    DIR="$1"
    BINARIES="rar unrar"

    # Check if any of the binaries exist in the directory
    for BIN in $BINARIES; do
        if [ -x "$DIR/$BIN" ]; then
            #echo "$BIN found in $DIR"

            # Check if the directory is in PATH
            case ":$PATH:" in
                *":$DIR:"*)
                    #echo "$DIR is already in PATH."
                    ;;
                *)
                    #echo "$DIR is not in PATH. Adding it now."
                    PATH="$DIR:$PATH"
                    export PATH
                    ;;
            esac

            # Exit the function as we only need one binary to update the PATH
            return
        fi
    done

    #echo "No binaries found in $DIR"
}

# Check /opt/bin
check_and_update_path "/opt/bin"

# Check /usr/local/bin
check_and_update_path "/usr/local/bin"


# Verify the file type using the `file` tool
FILE_TYPE=$(file --mime-type -b "$INPUT_FILE")

if [[ "$INPUT_FILE" =~ \.zip$ ]]; then
    if [[ "$FILE_TYPE" != "application/zip" ]]; then
        echo "Error: File does not appear to be a valid ZIP archive."
        exit 1
    fi
elif [[ "$INPUT_FILE" =~ \.rar$ ]]; then
    if [[ "$FILE_TYPE" != "application/x-rar" && "$FILE_TYPE" != "application/vnd.rar" ]]; then
        echo "Error: File does not appear to be a valid RAR archive."
        exit 1
    fi
else
    echo "Error: Unsupported file format."
    exit 1
fi

# Function to list the contents of a rar file using either `rar` or `unrar`
list_rar_contents() {
    # actually | grep ' ..:.. ' supposed to leave only the lines with time stamps - that's the ones containing files
    if command -v rar &>/dev/null; then
        rar l "$1" | grep ' ..:.. ' | awk '{print $NF}' | grep -vE '^Volume|^Name|^Size' | sed '/^$/d'
    elif command -v unrar &>/dev/null; then
        unrar l "$1" | grep ' ..:.. ' | awk '{print $NF}' | grep -vE '^Volume|^Name|^Size' | sed '/^$/d'
    else
        echo "Error: Neither rar nor unrar is available to list the archive contents."
        exit 1
    fi
}

# Check the file contents
if [[ "$INPUT_FILE" =~ \.zip$ ]]; then
    CONTENTS="$(unzip -l "$INPUT_FILE" | awk '{print $NF}' | grep -v "^$" | tail -n +4 | head -n -2)"
elif [[ "$INPUT_FILE" =~ \.rar$ ]]; then
    CONTENTS="$(list_rar_contents "$INPUT_FILE")"
else
    echo "Error: Unsupported file format."
    exit 1
fi

# Remove directories from the contents
FILES_ONLY="$(echo "$CONTENTS" | grep -v '/$')"

# Filter and count the valid extensions
VALID_FILES_COUNT=$(echo "$FILES_ONLY" | grep -c -E '\.(fts|fit|fits)$')
INVALID_FILES_COUNT=$(echo "$FILES_ONLY" | grep -c -vE '\.(fts|fit|fits)$')

if [[ "$VALID_FILES_COUNT" -lt 2 || "$INVALID_FILES_COUNT" -gt 0 ]]; then
    echo "Error: Archive must contain only .fts, .fit, or .fits files and at least two of them.  VALID_FILES_COUNT=$VALID_FILES_COUNT INVALID_FILES_COUNT=$INVALID_FILES_COUNT"
    exit 1
fi

# Source the local settings file
if [ -s local_config.sh ]; then
    source local_config.sh
fi

# Start the autoprocess script
./autoprocess.sh "$1" &>/dev/null &

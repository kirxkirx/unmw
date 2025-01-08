#!/usr/bin/env bash

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
    if command -v rar &>/dev/null; then
        rar l "$1" | awk '{print $NF}' | grep -vE '^Volume|^Name|^Size' | sed '/^$/d'
    elif command -v unrar &>/dev/null; then
        unrar l "$1" | awk '{print $NF}' | grep -vE '^Volume|^Name|^Size' | sed '/^$/d'
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
VALID_FILES_COUNT=$(echo "$FILES_ONLY" | grep -E '\.(fts|fit|fits)$' | wc -l)
INVALID_FILES_COUNT=$(echo "$FILES_ONLY" | grep -vE '\.(fts|fit|fits)$' | wc -l)

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

#!/bin/sh
set -u

if [ "$#" -eq 0 ] || [ "$1" = "shell" ]; then
  while true; do
    printf "evlab> "
    IFS= read -r line || exit 0
    [ -z "$line" ] && continue

    case "$line" in
      exit|quit) exit 0 ;;
    esac

    set -- $line
    if python /app/cli/evlab.py "$@"; then
      :
    else
      rc=$?
      echo "Command failed with exit code $rc"
    fi
  done
fi

exec python /app/cli/evlab.py "$@"

#!/bin/sh
set -e

if [ "$#" -eq 0 ] || [ "$1" = "shell" ]; then
  while true; do
    printf "evlab> "
    IFS= read -r line || exit 0
    [ -z "$line" ] && continue
    case "$line" in
      exit|quit) exit 0 ;;
    esac
    set -- $line
    python /app/cli/evlab.py "$@"
  done
fi

exec python /app/cli/evlab.py "$@"

#!/usr/bin/env sh
set -e

MSG="${1:-manual_migration}"

echo "[makemigrations] creating revision without database autogenerate"
alembic -c alembic.ini revision -m "$MSG"

echo "[makemigrations] done (edit the generated revision file manually)"

#!/usr/bin/env sh
set -e

echo "[migrate] checking model/schema drift"
if alembic -c alembic.ini check >/tmp/alembic-check.log 2>&1; then
  echo "[migrate] no new migration operations detected"
else
  echo "[migrate] changes detected, autogenerating revision"
  alembic -c alembic.ini revision --autogenerate -m "auto_migration"
fi

echo "[migrate] applying migrations"
alembic -c alembic.ini upgrade head

echo "[migrate] done"

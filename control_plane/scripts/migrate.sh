#!/usr/bin/env sh
set -e

echo "[migrate] validating current alembic state"
if ! alembic -c alembic.ini current >/tmp/alembic-current.log 2>&1; then
  if grep -q "Can't locate revision identified by" /tmp/alembic-current.log; then
    echo "[migrate] detected stale alembic_version reference, resetting to base"
    alembic -c alembic.ini stamp base --purge
  else
    echo "[migrate] unexpected alembic error"
    cat /tmp/alembic-current.log
    exit 1
  fi
fi

echo "[migrate] applying pending committed migrations first"
alembic -c alembic.ini upgrade head

echo "[migrate] checking model/schema drift"
if alembic -c alembic.ini check >/tmp/alembic-check.log 2>&1; then
  echo "[migrate] no new migration operations detected"
else
  echo "[migrate] changes detected, autogenerating revision"
  alembic -c alembic.ini revision --autogenerate -m "auto_migration"

  echo "[migrate] applying auto-generated migration"
  alembic -c alembic.ini upgrade head
fi

echo "[migrate] done"

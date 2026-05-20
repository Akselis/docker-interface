#!/usr/bin/env sh
set -e

mkdir -p alembic/versions

bootstrap_schema_from_models() {
  echo "[migrate] bootstrapping schema directly from SQLAlchemy models"
  python - <<'PY'
import os
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from db.models import Base

db_url = os.environ["DB_URL"]
parsed = make_url(db_url)

sync_driver_by_input_driver = {
    "postgresql": "postgresql+psycopg",
    "postgresql+asyncpg": "postgresql+psycopg",
    "postgresql+psycopg2": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mysql+aiomysql": "mysql+pymysql",
    "sqlite+aiosqlite": "sqlite",
}

if parsed.drivername in sync_driver_by_input_driver:
    parsed = parsed.set(drivername=sync_driver_by_input_driver[parsed.drivername])

sync_url = parsed.render_as_string(hide_password=False)
engine = create_engine(sync_url, pool_pre_ping=True)
try:
    Base.metadata.create_all(engine)
finally:
    engine.dispose()
PY

  alembic -c alembic.ini stamp base >/tmp/alembic-stamp.log 2>&1 || true
}

has_revisions=$(find alembic/versions -maxdepth 1 -type f -name '*.py' | wc -l | tr -d ' ')

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
  if [ "$has_revisions" -eq 0 ]; then
    echo "[migrate] no committed revisions found; using model-based schema bootstrap"
    bootstrap_schema_from_models

    if alembic -c alembic.ini check >/tmp/alembic-check-after-bootstrap.log 2>&1; then
      echo "[migrate] schema bootstrap complete"
    else
      echo "[migrate] schema bootstrap completed but drift still exists"
      cat /tmp/alembic-check-after-bootstrap.log
      exit 1
    fi
  else
    echo "[migrate] changes detected, autogenerating revision"
    alembic -c alembic.ini revision --autogenerate -m "auto_migration"

    echo "[migrate] applying auto-generated migration"
    alembic -c alembic.ini upgrade head
  fi
fi

echo "[migrate] done"

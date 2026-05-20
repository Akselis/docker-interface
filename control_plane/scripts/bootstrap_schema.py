from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.models import Base
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


def _to_sync_url(db_url: str) -> str:
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

    return parsed.render_as_string(hide_password=False)


def main() -> None:
    db_url = os.environ["DB_URL"]
    sync_url = _to_sync_url(db_url)

    engine = create_engine(sync_url, pool_pre_ping=True)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

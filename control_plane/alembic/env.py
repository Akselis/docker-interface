# pyright: reportMissingImports=false
from __future__ import annotations

import os
from importlib import import_module
from logging.config import fileConfig

from constants.const import DATABASE_URL_ENV_VAR
from db.models import Base
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

context = import_module("alembic.context")

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _to_alembic_sync_url(db_url: str) -> str:
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


# Prefer DB_URL from environment and fall back to alembic.ini url if set.
db_url = os.getenv(DATABASE_URL_ENV_VAR)
if db_url:
    config.set_main_option("sqlalchemy.url", _to_alembic_sync_url(db_url))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

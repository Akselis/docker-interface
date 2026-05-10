import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

from constants.const import DATABASE_URL_ENV_VAR
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DB_URL = os.getenv(DATABASE_URL_ENV_VAR)

if not DB_URL:
    raise ValueError(f"{DATABASE_URL_ENV_VAR} environment variable not set")


def _to_async_url(db_url: str) -> str:
    parsed = make_url(db_url)

    async_driver_by_sync_driver = {
        "postgresql": "postgresql+asyncpg",
        "postgresql+psycopg": "postgresql+asyncpg",
        "postgresql+psycopg2": "postgresql+asyncpg",
        "mysql": "mysql+aiomysql",
        "mysql+pymysql": "mysql+aiomysql",
        "sqlite": "sqlite+aiosqlite",
    }

    if parsed.drivername in async_driver_by_sync_driver:
        parsed = parsed.set(drivername=async_driver_by_sync_driver[parsed.drivername])

    return parsed.render_as_string(hide_password=False)


ASYNC_DB_URL = _to_async_url(DB_URL)

engine = create_async_engine(
    ASYNC_DB_URL,
    pool_pre_ping=True,
)

SessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


async def create_session() -> AsyncSession:
    return SessionFactory()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """
    Automatic session lifecycle + transaction handling:
    - begin transaction
    - commit on success
    - rollback on error
    """
    async with SessionFactory() as session:
        await session.begin()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def begin_transaction(session: AsyncSession) -> None:
    await session.begin()


async def commit_transaction(session: AsyncSession) -> None:
    await session.commit()


async def rollback_transaction(session: AsyncSession) -> None:
    await session.rollback()

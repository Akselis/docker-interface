import os

from constants.const import DATABASE_URL_ENV_VAR
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DB_URL = os.getenv(DATABASE_URL_ENV_VAR)

if not DB_URL:
    raise ValueError("DB_URL environment variable not set")

engine = create_engine(DB_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

"""Engine and Session setup, plus helpers that create and reset the database.

- The Engine owns a pool of database connections. Make one per database, per process.
- A Session is one unit of work: it borrows a connection from the pool, keeps track
  of the objects you load and change, and gives the connection back when it closes.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session, sessionmaker

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


def get_database_url(env_var: str = "DATABASE_URL") -> str:
    """Read the connection URL from the environment, or from a .env file."""
    load_dotenv()
    url = os.environ.get(env_var)
    if not url:
        raise RuntimeError(
            f"{env_var} is not set. Copy .env.example to .env and put your "
            "PostgreSQL password in it."
        )
    return url


def make_engine(url: str, *, echo: bool = False) -> Engine:
    # pool_pre_ping checks that a pooled connection is still alive before using it.
    return create_engine(url, echo=echo, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine)


def create_database_if_missing(url: str) -> None:
    """CREATE DATABASE can't run inside a transaction, so this uses an AUTOCOMMIT
    connection to the built-in "postgres" database."""
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            )
            if not exists:
                conn.exec_driver_sql(f'CREATE DATABASE "{target.database}"')
    finally:
        admin.dispose()


def reset_schema(engine: Engine) -> None:
    """Drop and recreate every table by running the Day 3 schema file.

    The SQL file is the source of truth for the schema (triggers, CHECKs, partial
    indexes), so we run it instead of Base.metadata.create_all().
    """
    name = engine.url.database or ""
    # Safety net: never wipe the Day 3 database (or any other one) by mistake.
    if not name.endswith(("_day4", "_test")):
        raise RuntimeError(
            f"Refusing to reset database {name!r}: its name must end in _day4 or _test."
        )
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)

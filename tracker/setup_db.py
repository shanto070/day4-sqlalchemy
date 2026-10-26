"""Create the database if needed, then load the Day 3 schema into it.

Run with `python -m tracker.setup_db`. Warning: this drops and recreates every
table in the DATABASE_URL database.
"""

from tracker.db import (
    create_database_if_missing,
    get_database_url,
    make_engine,
    reset_schema,
)


def main() -> None:
    url = get_database_url()
    create_database_if_missing(url)
    engine = make_engine(url)
    reset_schema(engine)
    engine.dispose()
    print(f"Database {engine.url.database!r} is ready (tables from sql/schema.sql).")


if __name__ == "__main__":
    main()

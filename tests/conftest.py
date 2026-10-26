"""Test fixtures: a real PostgreSQL test database, rolled back after every test.

TEST_DATABASE_URL comes from .env (see .env.example); its name must end in _test.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from tracker import crud
from tracker.db import (
    create_database_if_missing,
    get_database_url,
    make_engine,
    reset_schema,
)
from tracker.models import Project, User


@pytest.fixture(scope="session")
def test_url() -> str:
    return get_database_url("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def engine(test_url: str) -> Iterator[Engine]:
    create_database_if_missing(test_url)
    engine = make_engine(test_url)
    reset_schema(engine)  # fresh tables once per test run
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """One connection and one outer transaction per test, always rolled back."""
    with engine.connect() as connection:
        transaction = connection.begin()
        yield connection
        transaction.rollback()


@pytest.fixture
def session_factory(connection: Connection) -> sessionmaker[Session]:
    # "create_savepoint": a session's commit() only releases a SAVEPOINT inside the
    # outer transaction, so the rollback above still undoes everything.
    return sessionmaker(bind=connection, join_transaction_mode="create_savepoint")


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def alice(session: Session) -> User:
    return crud.create_user(
        session, email="alice@example.com", full_name="Alice Rahman"
    )


@pytest.fixture
def bob(session: Session) -> User:
    return crud.create_user(session, email="bob@example.com", full_name="Bob Karim")


@pytest.fixture
def project(session: Session, alice: User, bob: User) -> Project:
    """Alice owns it, Bob is a member."""
    project = crud.create_project(session, owner_id=alice.id, name="Website Redesign")
    crud.add_member(session, project_id=project.id, user_id=bob.id)
    return project

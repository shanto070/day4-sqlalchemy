"""The pitfalls from tracker/pitfalls.py, measured."""

import uuid

import pytest
from sqlalchemy import exc, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import DetachedInstanceError

from tracker import pitfalls
from tracker.models import Task

N_TASKS = 5


@pytest.fixture
def board(session: Session) -> uuid.UUID:
    project_id = pitfalls.seed_board(session, N_TASKS)
    session.expire_all()  # forget the loaded objects, like a new request would
    return project_id


@pytest.fixture
def task_id(session: Session, board: uuid.UUID) -> uuid.UUID:
    stmt = select(Task.id).where(Task.project_id == board).limit(1)
    return session.scalars(stmt).one()


# ------------------------------------------------------------------ 1. N+1 queries


def test_lazy_loading_in_a_loop_is_n_plus_one(
    session: Session, board: uuid.UUID
) -> None:
    assert pitfalls.load_board_lazily(session, board) == 1 + N_TASKS + N_TASKS


def test_eager_loading_uses_a_fixed_number_of_queries(
    session: Session, board: uuid.UUID
) -> None:
    assert pitfalls.load_board_eagerly(session, board) == 3


def test_raiseload_turns_a_lazy_load_into_an_error(
    session: Session, board: uuid.UUID
) -> None:
    with pytest.raises(exc.InvalidRequestError, match="lazy='raise'"):
        pitfalls.load_board_strictly(session, board)


# -------------------------------------------------------------- 2. Detached objects


def test_lazy_load_after_the_session_closed_raises(
    session_factory: sessionmaker[Session], task_id: uuid.UUID
) -> None:
    with pytest.raises(DetachedInstanceError, match="lazy load"):
        pitfalls.read_labels_after_close(session_factory, task_id)


def test_reading_an_expired_object_after_commit_raises(
    session_factory: sessionmaker[Session], task_id: uuid.UUID
) -> None:
    with pytest.raises(DetachedInstanceError, match="refresh"):
        pitfalls.read_title_after_commit(session_factory, task_id)


def test_eager_loading_before_close_works(
    session_factory: sessionmaker[Session], task_id: uuid.UUID
) -> None:
    assert pitfalls.read_labels_before_close(session_factory, task_id) == ["demo"]


# ---------------------------------------------------------------- 3. Session leaks


@pytest.mark.usefixtures("engine")  # makes sure the test database exists
def test_unclosed_sessions_exhaust_the_pool(test_url: str) -> None:
    engine = pitfalls.small_pool_engine(test_url)
    leaked: list[Session] = []
    try:
        with pytest.raises(exc.TimeoutError):
            pitfalls.open_without_closing(engine, leaked, how_many=3)
        assert len(leaked) == 2
        assert pitfalls.checked_out(engine) == 2
    finally:
        for session in leaked:
            session.close()
        engine.dispose()


@pytest.mark.usefixtures("engine")
def test_with_blocks_return_every_connection(test_url: str) -> None:
    engine = pitfalls.small_pool_engine(test_url)
    try:
        pitfalls.open_and_close(engine, how_many=10)
        assert pitfalls.checked_out(engine) == 0
    finally:
        engine.dispose()

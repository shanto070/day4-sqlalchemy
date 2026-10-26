"""What NOT to do with the ORM, shown with real numbers: `python -m tracker.pitfalls`.

1. N+1 queries       lazy-loading a relationship inside a loop
2. Detached objects  reading data the session never loaded, after it has closed
3. Session leaks     sessions that are never closed, until the pool runs dry
"""

import uuid
from typing import Self

from sqlalchemy import Engine, QueuePool, create_engine, event, exc, select, text
from sqlalchemy.orm import Session, raiseload, selectinload, sessionmaker
from sqlalchemy.orm.exc import DetachedInstanceError

from tracker import crud
from tracker.db import (
    create_database_if_missing,
    get_database_url,
    make_engine,
    make_session_factory,
    reset_schema,
)
from tracker.models import Task


class QueryCounter:
    """Counts the SQL statements a Session sends while the `with` block runs."""

    def __init__(self, session: Session) -> None:
        # Every statement in the session's current transaction uses this connection.
        self.connection = session.connection()
        self.count = 0

    def _count(self, *args: object) -> None:
        self.count += 1

    def __enter__(self) -> Self:
        event.listen(self.connection, "before_cursor_execute", self._count)
        return self

    def __exit__(self, *exc_info: object) -> None:
        event.remove(self.connection, "before_cursor_execute", self._count)


def seed_board(session: Session, n_tasks: int) -> uuid.UUID:
    """A project with n_tasks tasks, each with one label and one comment."""
    owner = crud.create_user(
        session, email="owner@example.com", full_name="Board Owner"
    )
    project = crud.create_project(session, owner_id=owner.id, name="N+1 demo")
    label = crud.create_label(session, project_id=project.id, name="demo")
    for i in range(1, n_tasks + 1):
        task = crud.create_task(
            session, project_id=project.id, created_by=owner.id, title=f"Task {i}"
        )
        crud.add_label_to_task(session, task_id=task.id, label_id=label.id)
        crud.add_comment(
            session, task_id=task.id, author_id=owner.id, body=f"Comment {i}"
        )
    return project.id


# ------------------------------------------------------------------ 1. N+1 queries


def load_board_lazily(session: Session, project_id: uuid.UUID) -> int:
    """BAD: 1 query for the tasks, then 2 more for *each* task. Returns the count."""
    stmt = select(Task).where(Task.project_id == project_id)
    with QueryCounter(session) as counter:
        for task in session.scalars(stmt).all():
            _ = [label.name for label in task.labels]  # lazy load: 1 SELECT per task
            _ = [comment.body for comment in task.comments]  # and 1 more per task
    return counter.count


def load_board_eagerly(session: Session, project_id: uuid.UUID) -> int:
    """GOOD: the same data in 3 queries, however many tasks there are."""
    stmt = (
        select(Task)
        .where(Task.project_id == project_id)
        .options(selectinload(Task.labels), selectinload(Task.comments))
    )
    with QueryCounter(session) as counter:
        for task in session.scalars(stmt).all():
            _ = [label.name for label in task.labels]  # already loaded: no query
            _ = [comment.body for comment in task.comments]
    return counter.count


def load_board_strictly(session: Session, project_id: uuid.UUID) -> None:
    """GUARD: raiseload("*") turns any lazy load you forgot about into an error, so
    an N+1 fails loudly in tests instead of quietly slowing production down."""
    stmt = (
        select(Task)
        .where(Task.project_id == project_id)
        .options(selectinload(Task.labels), raiseload("*"))
    )
    for task in session.scalars(stmt).all():
        _ = task.labels  # fine: loaded up front
        _ = task.comments  # raises InvalidRequestError: not loaded, lazy='raise'


# -------------------------------------------------------------- 2. Detached objects


def read_labels_after_close(
    session_factory: sessionmaker[Session], task_id: uuid.UUID
) -> list[str]:
    """BAD: task.labels was never loaded, and once the session has closed there is
    nothing left to load it with. Raises DetachedInstanceError."""
    with session_factory() as session:
        task = session.get_one(Task, task_id)
    # The session is closed here: `task` is now *detached*.
    return [label.name for label in task.labels]


def read_title_after_commit(
    session_factory: sessionmaker[Session], task_id: uuid.UUID
) -> str:
    """BAD: commit() expires every object in the session (expire_on_commit=True),
    then the with-block closes it. Even a plain column can't be reloaded now."""
    with session_factory.begin() as session:
        task = session.get_one(Task, task_id)
    return task.title


def read_labels_before_close(
    session_factory: sessionmaker[Session], task_id: uuid.UUID
) -> list[str]:
    """GOOD: load everything you need while the session is still open."""
    with session_factory() as session:
        stmt = select(Task).where(Task.id == task_id).options(selectinload(Task.labels))
        task = session.scalars(stmt).one()
    return [label.name for label in task.labels]  # already loaded: no query needed


# ---------------------------------------------------------------- 3. Session leaks


def small_pool_engine(url: str) -> Engine:
    """At most 2 connections, and wait only 1 second for a free one."""
    return create_engine(url, pool_size=2, max_overflow=0, pool_timeout=1)


def checked_out(engine: Engine) -> int:
    """How many of the pool's connections are in use right now."""
    pool = engine.pool
    assert isinstance(pool, QueuePool)
    return pool.checkedout()


def open_without_closing(engine: Engine, leaked: list[Session], how_many: int) -> None:
    """BAD: a Session that is never closed keeps its connection, inside an open
    transaction ("idle in transaction" on the server), for as long as it lives.

    `leaked` stands for whatever keeps sessions alive in a real app: a global, a
    cache, a long-lived object. Here it also lets the caller clean up afterwards.
    """
    for _ in range(how_many):
        session = Session(engine)
        session.execute(text("SELECT 1"))  # autobegin: takes a connection, keeps it
        leaked.append(session)


def open_and_close(engine: Engine, how_many: int) -> None:
    """GOOD: the with-block closes the session even if the code inside raises, and
    the connection goes back to the pool."""
    for _ in range(how_many):
        with Session(engine) as session:
            session.execute(text("SELECT 1"))


def heading(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    url = get_database_url()
    create_database_if_missing(url)
    engine = make_engine(url)
    reset_schema(engine)
    session_factory = make_session_factory(engine)

    n_tasks = 10
    with session_factory.begin() as session:
        project_id = seed_board(session, n_tasks)
        task_id = session.scalars(select(Task.id).limit(1)).one()

    heading("1. N+1 queries")
    with session_factory() as session:
        lazy = load_board_lazily(session, project_id)
    with session_factory() as session:
        eager = load_board_eagerly(session, project_id)
    print(
        f"Lazy loading in a loop: {lazy} queries for {n_tasks} tasks "
        f"(1 + {n_tasks} + {n_tasks})"
    )
    print(f"selectinload():         {eager} queries, however many tasks there are")
    try:
        with session_factory() as session:
            load_board_strictly(session, project_id)
    except exc.InvalidRequestError as error:
        print(f"raiseload('*') stopped a lazy load: {error}")

    heading("2. Detached instances")
    try:
        read_labels_after_close(session_factory, task_id)
    except DetachedInstanceError as error:
        print(f"read_labels_after_close:  DetachedInstanceError: {error}")
    try:
        read_title_after_commit(session_factory, task_id)
    except DetachedInstanceError as error:
        print(f"read_title_after_commit:  DetachedInstanceError: {error}")
    labels = read_labels_before_close(session_factory, task_id)
    print(f"read_labels_before_close: {labels}")

    heading("3. Session leaks")
    small = small_pool_engine(url)
    leaked: list[Session] = []
    try:
        open_without_closing(small, leaked, how_many=3)
    except exc.TimeoutError:
        print(
            f"{len(leaked)} unclosed sessions hold {checked_out(small)} of 2 pool "
            "connections. Session 3 waited 1 second, then got TimeoutError."
        )
    finally:
        for session in leaked:
            session.close()
    open_and_close(small, how_many=10)
    print(
        f"10 sessions in `with` blocks: {checked_out(small)} connections still "
        "checked out"
    )
    small.dispose()
    engine.dispose()


if __name__ == "__main__":
    main()

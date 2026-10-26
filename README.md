# Day 4 — SQLAlchemy 2.0: CRUD for the Task Tracker

CRUD operations for every table of my [Day 3 PostgreSQL schema](https://github.com/shanto070/day3-postgresql)
(users, projects, project members, tasks, labels, comments), written with the **SQLAlchemy 2.0 ORM only**
(no web framework yet). It covers the engine, sessions, models, relationships, lazy vs eager loading and
transactions. It also shows **what not to do**, with measured results: N+1 queries, detached instances and
session leaks.

Stack: Python 3.11 · SQLAlchemy 2.0 · psycopg 3 · PostgreSQL 18 · pytest · ruff · mypy (strict)

## Files

| File | What it does |
|---|---|
| [`sql/schema.sql`](sql/schema.sql) | The Day 3 schema, unchanged (`01_schema.sql`). It is still the source of truth |
| [`tracker/models.py`](tracker/models.py) | ORM models: `DeclarativeBase`, `Mapped[]`, `mapped_column()`, `relationship()` |
| [`tracker/db.py`](tracker/db.py) | Engine, `sessionmaker`, and helpers that create the database and load the schema |
| [`tracker/crud.py`](tracker/crud.py) | 31 CRUD functions for every table, plus `complete_task()` (two writes in one transaction) |
| [`tracker/demo.py`](tracker/demo.py) | `python -m tracker.demo`: runs every operation on a real database (`--echo` prints the SQL) |
| [`tracker/pitfalls.py`](tracker/pitfalls.py) | `python -m tracker.pitfalls`: N+1, detached instances and session leaks, each next to its fix |
| [`tracker/setup_db.py`](tracker/setup_db.py) | `python -m tracker.setup_db`: creates the database and loads the schema |
| [`tests/`](tests/) | 36 pytest tests against real PostgreSQL. Each test is rolled back afterwards |
| [`results/`](results/) | Output of the demo, the pitfalls script and the test run on my machine |

## Setup and run

Requires Python 3.11+ and PostgreSQL 18 (the schema uses `uuidv7()`).

```bash
python -m venv .venv
.venv\Scripts\activate              # Windows  (macOS / Linux: source .venv/bin/activate)
pip install -e ".[dev]"
copy .env.example .env              # then put your PostgreSQL password in .env

python -m tracker.setup_db          # creates task_tracker_day4 and loads sql/schema.sql
python -m tracker.demo              # every CRUD operation (add --echo to see the SQL)
python -m tracker.pitfalls          # what NOT to do, measured
pytest -v                           # 36 tests (creates task_tracker_test by itself)
ruff check . ; ruff format --check . ; mypy tracker tests
```

`setup_db`, `demo` and the tests **drop and recreate** all tables. As a safety net, they refuse to touch any
database whose name doesn't end in `_day4` or `_test`, so the Day 3 `task_tracker` database is never wiped.

## Design decisions

**The SQL file owns the schema; the models describe it.** The tables are created by running Day 3's
`schema.sql`, not by `Base.metadata.create_all()`. That keeps the trigger, the CHECK constraints, the partial
unique index and the composite FK in one place. PostgreSQL still enforces them, and breaking one raises
`IntegrityError`. The tests check this for each constraint.

**2.0 style everywhere:** `DeclarativeBase` with a `type_annotation_map` (`datetime` → `timestamptz`,
`str` → `text`), `Mapped[...]` annotations, reusable `Annotated` primary keys (`UuidPk`, `BigIntPk`),
`select()` + `session.scalars()`, and `session.get()`. There is no legacy `session.query()`.

**The caller owns the transaction.** Every CRUD function takes a `Session`. It never commits, rolls back or
closes it. The caller decides how much work belongs together:

```python
with session_factory.begin() as session:  # BEGIN
    crud.set_task_status(session, task_id, TaskStatus.DONE)
    crud.add_comment(session, task_id=task_id, author_id=user_id, body="Done!")
# COMMIT here, or ROLLBACK of both writes if anything raised
```

The functions call `session.flush()` when they need the database to act now: to get generated ids back, or
to raise a constraint error on the line that caused it. `flush()` sends SQL; only `commit()` makes it permanent.

**Values the database generates come back without extra queries.** Ids are `uuidv7()` server defaults;
`status`, `priority`, `created_at` are server defaults too. `eager_defaults=True` fetches them with
`INSERT ... RETURNING`. `updated_at` is set by Day 3's trigger, and `server_onupdate=FetchedValue()` tells
SQLAlchemy to fetch the new value after every UPDATE (`UPDATE ... RETURNING updated_at`).

**Soft delete, like Day 3.** Users, projects, tasks and comments get `deleted_at`, and every read hides them.
`restore_task()` undoes a delete. Labels and memberships are hard-deleted. Removing a member lets the database's
`ON DELETE SET NULL (assignee_id)` unassign their tasks, and the function then expires the session so loaded
objects don't keep the old value.

### Relationships

| Relationship | Kind | How it's mapped |
|---|---|---|
| `User.owned_projects` ↔ `Project.owner` | one-to-many / many-to-one | `ForeignKey` + `back_populates` |
| `Project.tasks`, `Project.labels`, `Task.comments` | one-to-many | `Task.comments` uses `order_by="Comment.id"` |
| `Project.members` ↔ `ProjectMember` ↔ `User.memberships` | many-to-many **with data** (`role`) | association object (`ProjectMember` class) |
| `Task.labels` ↔ `Label.tasks` | many-to-many, no extra data | plain `Table` as `secondary=` |
| `Task.assignee` | many-to-one through a **composite FK** | `primaryjoin="foreign(Task.assignee_id) == User.id"` |

`tasks.assignee_id` has no FK of its own to `users`. It is part of the composite FK to `project_members`, which
guarantees the assignee is in the task's project. So `Task.assignee` spells out its join condition.

## Lazy vs eager loading

| Strategy | SQL it runs | Use it for |
|---|---|---|
| `lazy="select"` (the default) | one `SELECT` the first time you touch the attribute | single objects, attributes you rarely read |
| `joinedload()` | a `LEFT OUTER JOIN` in the same query | many-to-one (`Task.assignee`, `Comment.author`) |
| `selectinload()` | one extra `SELECT ... WHERE id IN (...)` for all parents | collections (`Task.labels`, `Task.comments`) |
| `raiseload("*")` | none: raises instead of lazy-loading | making a forgotten lazy load fail loudly in tests |

`crud.list_tasks()` always takes 2 queries, and `crud.get_task_details()` always takes 3, however many tasks,
labels or comments there are. The tests count the queries. `get_task_details()` also uses
`selectinload(Task.comments.and_(Comment.deleted_at.is_(None)))` to skip deleted comments while loading.

## What NOT to do (measured)

From [`results/pitfalls_output.txt`](results/pitfalls_output.txt) (10 tasks, each with 1 label and 1 comment):

| Pitfall | What happened | Fix |
|---|---|---|
| **N+1 queries**: reading `task.labels` and `task.comments` in a loop | **21 queries** (1 + 10 + 10), and it grows with every task | `selectinload()` for both: **3 queries**, however many tasks |
| Forgetting one relationship in the options | silently adds N queries again | `raiseload("*")` turns it into `InvalidRequestError` |
| **Detached instance**: lazy-loading `task.labels` after the `with` block closed the session | `DetachedInstanceError: ... lazy load operation of attribute 'labels' cannot proceed` | eager-load what you need while the session is open |
| **Detached + expired**: reading `task.title` after `commit()` and close | `DetachedInstanceError: ... attribute refresh operation cannot proceed`, because `commit()` expires every object | read values before the block ends, or use `expire_on_commit=False` if you really mean it |
| **Session leak**: sessions that are never closed (pool of 2, no overflow) | the 3rd session waits `pool_timeout` and gets `TimeoutError: QueuePool limit ... reached` | `with Session(engine) as session:`. After 10 sessions, **0** connections were still checked out |

Two smaller traps that the tests caught:

- **Expired objects cost queries, even for the id.** After `session.expire_all()`, reading `task.id` runs a
  `SELECT`. My first version of the details test counted 4 queries instead of 3 because of this.
- **The identity map doesn't see database-side changes.** After `ON DELETE SET NULL` ran inside PostgreSQL,
  loaded `Task` objects still held the old `assignee_id` until they were expired.

## Transactions

| Scenario | Where | Result |
|---|---|---|
| `complete_task()`: status → done **and** add a comment, one `session.begin()` | demo + test | both committed |
| The same, but the comment is blank (breaks `comments_body_check`) | demo + test | **both** rolled back; the task is still `in_progress` |
| `session.begin_nested()` around a duplicate label | demo | only the `SAVEPOINT` is rolled back; the comment before it is committed |

## Tests

36 tests in [`tests/`](tests/) run against a real PostgreSQL database (`task_tracker_test`, created and
loaded on the first run). The fixtures in [`conftest.py`](tests/conftest.py) open one connection and one
transaction per test, and bind the sessions to it with `join_transaction_mode="create_savepoint"`. Code under
test can call `commit()` (it only releases a SAVEPOINT), and everything is still rolled back after the test.
Tests that expect a constraint error wrap it in `session.begin_nested()`, so only that statement is undone.

The tests cover all CRUD operations, every CHECK / UNIQUE / FK constraint touched by the CRUD layer, query counts
for the eager-loading functions, both rollback scenarios, and every pitfall above. Output:
[`results/pytest_output.txt`](results/pytest_output.txt).

## Code quality

`ruff check .`, `ruff format --check .` and `mypy --strict` (`mypy tracker tests`) all pass. The SQLAlchemy 2.0
`Mapped[]` annotations type-check with no plugin.

## Key takeaways

- **An Engine per app, a Session per unit of work.** The Engine owns the pool. A Session borrows one connection
  and must give it back, so always open it with `with`.
- **Keep transactions out of CRUD functions.** Functions that take a Session and only `flush()` can be combined
  freely, and the caller commits once.
- **Lazy loading is a default, not a strategy.** Choose `joinedload` for many-to-one and `selectinload` for
  collections whenever you know you'll loop, and count the queries in tests.
- **Objects outlive their sessions.** After `close()` they are detached, and after `commit()` they are expired.
  Load what you need before the session ends.
- **Let the database keep enforcing the rules.** The ORM made the CRUD code shorter, but Day 3's constraints
  still caught every bad write the tests tried.

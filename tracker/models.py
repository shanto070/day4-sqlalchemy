"""ORM models that mirror the Day 3 schema in sql/schema.sql.

The SQL file creates the tables; these classes only describe them to SQLAlchemy.
Rules that live in the database (CHECKs, the partial unique index on email, the
updated_at trigger) are not repeated here, but the database still enforces them:
breaking one raises sqlalchemy.exc.IntegrityError.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    SmallInteger,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"


class MemberRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class Base(DeclarativeBase):
    # Python type -> column type: every datetime in the schema is timestamptz and
    # every string is text.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: DateTime(timezone=True),
        str: Text,
    }


# Reusable primary-key definitions (Annotated + mapped_column, new in 2.0).
UuidPk = Annotated[
    uuid.UUID, mapped_column(primary_key=True, server_default=text("uuidv7()"))
]
BigIntPk = Annotated[
    int, mapped_column(BigInteger, Identity(always=True), primary_key=True)
]


class Timestamped:
    """created_at / updated_at / deleted_at, shared by users, projects, tasks and
    comments."""

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # The set_updated_at() trigger changes this on every UPDATE. FetchedValue() tells
    # SQLAlchemy that the database sets it, so it fetches the new value instead of
    # keeping the old one.
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), server_onupdate=FetchedValue()
    )
    deleted_at: Mapped[datetime | None] = mapped_column()  # NULL = active

    # Get server-generated values (ids, timestamps) back with RETURNING in the same
    # INSERT/UPDATE, instead of a separate SELECT the next time they are read.
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}


class User(Timestamped, Base):
    __tablename__ = "users"

    id: Mapped[UuidPk]
    email: Mapped[str]
    full_name: Mapped[str]

    owned_projects: Mapped[list["Project"]] = relationship(back_populates="owner")
    memberships: Mapped[list["ProjectMember"]] = relationship(back_populates="user")
    created_tasks: Mapped[list["Task"]] = relationship(back_populates="creator")
    comments: Mapped[list["Comment"]] = relationship(back_populates="author")

    def __repr__(self) -> str:
        return f"User({self.email!r})"


class Project(Timestamped, Base):
    __tablename__ = "projects"

    id: Mapped[UuidPk]
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    description: Mapped[str | None]

    owner: Mapped[User] = relationship(back_populates="owned_projects")
    members: Mapped[list["ProjectMember"]] = relationship(back_populates="project")
    tasks: Mapped[list["Task"]] = relationship(back_populates="project")
    labels: Mapped[list["Label"]] = relationship(back_populates="project")

    def __repr__(self) -> str:
        return f"Project({self.name!r})"


class ProjectMember(Base):
    """Association object: a many-to-many junction table that also holds data
    (role, joined_at), so it gets its own class instead of a plain Table."""

    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(server_default=MemberRole.MEMBER)
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())

    project: Mapped[Project] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")

    def __repr__(self) -> str:
        return f"ProjectMember(user_id={self.user_id}, role={self.role!r})"


# A junction table with no extra columns only needs a Table, not a class.
task_labels = Table(
    "task_labels",
    Base.metadata,
    Column("task_id", ForeignKey("tasks.id"), primary_key=True),
    Column("label_id", ForeignKey("labels.id"), primary_key=True),
)


class Task(Timestamped, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # Day 3's composite FK: the assignee must be a member of this task's project.
        ForeignKeyConstraint(
            ["project_id", "assignee_id"],
            ["project_members.project_id", "project_members.user_id"],
        ),
    )

    id: Mapped[UuidPk]
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    assignee_id: Mapped[uuid.UUID | None]
    title: Mapped[str]
    description: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=TaskStatus.TODO)
    priority: Mapped[int] = mapped_column(SmallInteger, server_default="3")
    due_date: Mapped[date | None]
    completed_at: Mapped[datetime | None]

    project: Mapped[Project] = relationship(back_populates="tasks")
    creator: Mapped[User] = relationship(back_populates="created_tasks")
    # tasks.assignee_id has no FK straight to users (it goes through
    # project_members), so we spell out the join and which column is the "foreign" one.
    assignee: Mapped[User | None] = relationship(
        primaryjoin="foreign(Task.assignee_id) == User.id"
    )
    labels: Mapped[list["Label"]] = relationship(
        secondary=task_labels, back_populates="tasks"
    )
    # Ordered by id, not created_at: now() is the same for every row inserted in
    # one transaction, but identity ids always go up.
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="task", order_by="Comment.id"
    )

    def __repr__(self) -> str:
        return f"Task({self.title!r}, status={self.status!r})"


class Label(Base):
    __tablename__ = "labels"

    id: Mapped[BigIntPk]
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str]
    color: Mapped[str] = mapped_column(server_default="#808080")

    project: Mapped[Project] = relationship(back_populates="labels")
    tasks: Mapped[list[Task]] = relationship(
        secondary=task_labels, back_populates="labels"
    )

    def __repr__(self) -> str:
        return f"Label({self.name!r})"


class Comment(Timestamped, Base):
    __tablename__ = "comments"

    id: Mapped[BigIntPk]
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str]

    task: Mapped[Task] = relationship(back_populates="comments")
    author: Mapped[User] = relationship(back_populates="comments")

    def __repr__(self) -> str:
        return f"Comment({self.body[:30]!r})"

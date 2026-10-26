"""Create / read / update / delete functions for every table in the Day 3 schema.

Every function here:
- takes the Session as its first argument and never commits, rolls back or closes
  it. The caller owns the transaction (see `with session_factory.begin()` in
  demo.py), so several calls can succeed or fail together;
- calls session.flush() when it needs the database to act now: to get generated
  ids and timestamps back, or to raise a constraint error on the line that caused it;
- hides soft-deleted rows (deleted_at IS NOT NULL), like Day 3's active_tasks view.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import TypedDict, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from tracker.models import (
    Comment,
    Label,
    MemberRole,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
    Timestamped,
    User,
)


class NotFoundError(LookupError):
    """The row doesn't exist, or it was soft-deleted."""


# The fields a caller may change. total=False: leave a key out to keep its value.
class UserChanges(TypedDict, total=False):
    email: str
    full_name: str


class ProjectChanges(TypedDict, total=False):
    name: str
    description: str | None


class TaskChanges(TypedDict, total=False):
    title: str
    description: str | None
    priority: int
    due_date: date | None


SoftDeletable = TypeVar("SoftDeletable", bound=Timestamped)


def _get_active(
    session: Session, model: type[SoftDeletable], pk: object
) -> SoftDeletable:
    # session.get() looks in the identity map first and only runs a SELECT if the
    # object isn't already loaded in this session.
    obj = session.get(model, pk)
    if obj is None or obj.deleted_at is not None:
        raise NotFoundError(f"{model.__name__} {pk} not found")
    return obj


def _apply(obj: object, changes: Mapping[str, object]) -> None:
    for field, value in changes.items():
        setattr(obj, field, value)


def _soft_delete(session: Session, obj: Timestamped) -> None:
    obj.deleted_at = datetime.now(UTC)
    session.flush()


# --------------------------------------------------------------------------- users


def create_user(session: Session, *, email: str, full_name: str) -> User:
    user = User(email=email, full_name=full_name)
    session.add(user)
    session.flush()  # INSERT ... RETURNING id, created_at, updated_at
    return user


def get_user(session: Session, user_id: uuid.UUID) -> User:
    return _get_active(session, User, user_id)


def get_user_by_email(session: Session, email: str) -> User | None:
    # lower(email) matches Day 3's unique index users_email_active_uq, so the
    # lookup is case-insensitive and can use that index.
    stmt = select(User).where(
        func.lower(User.email) == email.lower(), User.deleted_at.is_(None)
    )
    return session.scalars(stmt).one_or_none()


def list_users(session: Session, *, limit: int = 50, offset: int = 0) -> Sequence[User]:
    stmt = (
        select(User)
        .where(User.deleted_at.is_(None))
        .order_by(User.full_name)
        .limit(limit)
        .offset(offset)
    )
    return session.scalars(stmt).all()


def update_user(session: Session, user_id: uuid.UUID, changes: UserChanges) -> User:
    user = get_user(session, user_id)
    _apply(user, changes)
    session.flush()  # UPDATE ... RETURNING updated_at (set by the trigger)
    return user


def delete_user(session: Session, user_id: uuid.UUID) -> None:
    _soft_delete(session, get_user(session, user_id))


# ------------------------------------------------------------------------ projects


def create_project(
    session: Session,
    *,
    owner_id: uuid.UUID,
    name: str,
    description: str | None = None,
) -> Project:
    project = Project(owner_id=owner_id, name=name, description=description)
    # The owner is a member too, so they can be assigned tasks. Appending to the
    # relationship is enough: SQLAlchemy inserts the project first, then copies its
    # new id into the member row.
    project.members.append(ProjectMember(user_id=owner_id, role=MemberRole.OWNER))
    session.add(project)
    session.flush()
    return project


def get_project(session: Session, project_id: uuid.UUID) -> Project:
    return _get_active(session, Project, project_id)


def list_projects_for_user(session: Session, user_id: uuid.UUID) -> Sequence[Project]:
    stmt = (
        select(Project)
        .join(Project.members)
        .where(ProjectMember.user_id == user_id, Project.deleted_at.is_(None))
        .order_by(Project.name)
    )
    return session.scalars(stmt).all()


def update_project(
    session: Session, project_id: uuid.UUID, changes: ProjectChanges
) -> Project:
    project = get_project(session, project_id)
    _apply(project, changes)
    session.flush()
    return project


def delete_project(session: Session, project_id: uuid.UUID) -> None:
    _soft_delete(session, get_project(session, project_id))


def add_member(
    session: Session,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: MemberRole = MemberRole.MEMBER,
) -> ProjectMember:
    get_project(session, project_id)
    # A clear error instead of a primary-key violation. The primary key still
    # protects us if two requests add the same member at the same moment.
    if session.get(ProjectMember, (project_id, user_id)) is not None:
        raise ValueError(f"User {user_id} is already in project {project_id}")
    member = ProjectMember(project_id=project_id, user_id=user_id, role=role)
    session.add(member)
    session.flush()
    return member


def remove_member(
    session: Session, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    member = session.get(ProjectMember, (project_id, user_id))
    if member is None:
        raise NotFoundError(f"User {user_id} is not in project {project_id}")
    session.delete(member)
    session.flush()
    # The database's ON DELETE SET NULL (assignee_id) has just unassigned this
    # user's tasks in the project. Objects already loaded in this session don't
    # know that, so mark them stale; they reload on their next access.
    session.expire_all()


# --------------------------------------------------------------------------- tasks


def create_task(
    session: Session,
    *,
    project_id: uuid.UUID,
    created_by: uuid.UUID,
    title: str,
    description: str | None = None,
    priority: int = 3,
    due_date: date | None = None,
    assignee_id: uuid.UUID | None = None,
) -> Task:
    get_project(session, project_id)
    task = Task(
        project_id=project_id,
        created_by=created_by,
        title=title,
        description=description,
        priority=priority,
        due_date=due_date,
        assignee_id=assignee_id,
    )
    session.add(task)
    session.flush()  # the composite FK rejects an assignee who isn't a member
    return task


def get_task(session: Session, task_id: uuid.UUID) -> Task:
    return _get_active(session, Task, task_id)


def get_task_details(session: Session, task_id: uuid.UUID) -> Task:
    """One task with everything a detail page shows, loaded up front.

    Always 3 queries, however many labels or comments the task has.
    """
    stmt = (
        select(Task)
        .where(Task.id == task_id, Task.deleted_at.is_(None))
        .options(
            joinedload(Task.creator),
            joinedload(Task.assignee),
            selectinload(Task.labels),
            # .and_() adds a filter to the relationship: skip deleted comments.
            selectinload(Task.comments.and_(Comment.deleted_at.is_(None))).joinedload(
                Comment.author
            ),
        )
    )
    task = session.scalars(stmt).one_or_none()
    if task is None:
        raise NotFoundError(f"Task {task_id} not found")
    return task


def list_tasks(
    session: Session,
    *,
    project_id: uuid.UUID,
    status: TaskStatus | None = None,
    assignee_id: uuid.UUID | None = None,
) -> Sequence[Task]:
    """A project's tasks, with assignees and labels loaded up front.

    Reading task.assignee and task.labels in a loop would otherwise run
    1 + N + N queries (the N+1 problem). With these options it is always 2.
    """
    stmt = (
        select(Task)
        .where(Task.project_id == project_id, Task.deleted_at.is_(None))
        .options(
            # Many-to-one: a LEFT OUTER JOIN to users in the same query.
            joinedload(Task.assignee),
            # Collection: one more SELECT ... WHERE task_id IN (...) for all tasks.
            selectinload(Task.labels),
        )
        .order_by(Task.priority, Task.due_date.nulls_last(), Task.title)
    )
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if assignee_id is not None:
        stmt = stmt.where(Task.assignee_id == assignee_id)
    return session.scalars(stmt).all()


def update_task(session: Session, task_id: uuid.UUID, changes: TaskChanges) -> Task:
    task = get_task(session, task_id)
    _apply(task, changes)
    session.flush()
    return task


def set_task_status(session: Session, task_id: uuid.UUID, status: TaskStatus) -> Task:
    task = get_task(session, task_id)
    task.status = status
    # Day 3's CHECK tasks_done_has_completed_at: a task is 'done' exactly when
    # completed_at is set, so change both together.
    if status == TaskStatus.DONE:
        task.completed_at = task.completed_at or datetime.now(UTC)
    else:
        task.completed_at = None
    session.flush()
    return task


def assign_task(
    session: Session, task_id: uuid.UUID, assignee_id: uuid.UUID | None
) -> Task:
    task = get_task(session, task_id)
    # Set the relationship, not only the id column, so task.assignee and
    # task.assignee_id never disagree inside this session.
    task.assignee = None if assignee_id is None else get_user(session, assignee_id)
    session.flush()  # the composite FK rejects a user who isn't in the project
    return task


def delete_task(session: Session, task_id: uuid.UUID) -> None:
    _soft_delete(session, get_task(session, task_id))


def restore_task(session: Session, task_id: uuid.UUID) -> Task:
    task = session.get(Task, task_id)  # not get_task(): it hides deleted tasks
    if task is None:
        raise NotFoundError(f"Task {task_id} not found")
    task.deleted_at = None
    session.flush()
    return task


def complete_task(
    session: Session, *, task_id: uuid.UUID, user_id: uuid.UUID, note: str
) -> Task:
    """Mark a task done and comment on it: two writes in one unit of work.

    Nothing is committed here. If the comment fails (a blank note breaks the CHECK
    on comments.body), the caller's transaction rolls back the status change too.
    """
    task = set_task_status(session, task_id, TaskStatus.DONE)
    add_comment(session, task_id=task_id, author_id=user_id, body=note)
    return task


# -------------------------------------------------------------------------- labels


def create_label(
    session: Session, *, project_id: uuid.UUID, name: str, color: str = "#808080"
) -> Label:
    label = Label(project_id=project_id, name=name, color=color)
    session.add(label)
    session.flush()  # UNIQUE (project_id, name) and the color CHECK run here
    return label


def list_labels(session: Session, project_id: uuid.UUID) -> Sequence[Label]:
    stmt = select(Label).where(Label.project_id == project_id).order_by(Label.name)
    return session.scalars(stmt).all()


def add_label_to_task(session: Session, *, task_id: uuid.UUID, label_id: int) -> Task:
    task = get_task(session, task_id)
    label = session.get(Label, label_id)
    if label is None:
        raise NotFoundError(f"Label {label_id} not found")
    # The schema can't say "same project" for task_labels, so check it here.
    if label.project_id != task.project_id:
        raise ValueError("A label can only be added to tasks in its own project.")
    if label not in task.labels:
        task.labels.append(label)  # SQLAlchemy inserts the task_labels row
    session.flush()
    return task


def remove_label_from_task(
    session: Session, *, task_id: uuid.UUID, label_id: int
) -> Task:
    task = get_task(session, task_id)
    task.labels = [label for label in task.labels if label.id != label_id]
    session.flush()  # DELETE FROM task_labels WHERE ...
    return task


def delete_label(session: Session, label_id: int) -> None:
    label = session.get(Label, label_id)
    if label is None:
        raise NotFoundError(f"Label {label_id} not found")
    session.delete(label)  # hard delete; its task_labels rows are deleted too
    session.flush()


# ------------------------------------------------------------------------ comments


def add_comment(
    session: Session, *, task_id: uuid.UUID, author_id: uuid.UUID, body: str
) -> Comment:
    get_task(session, task_id)
    comment = Comment(task_id=task_id, author_id=author_id, body=body)
    session.add(comment)
    session.flush()  # CHECK: the body can't be blank
    return comment


def list_comments(session: Session, task_id: uuid.UUID) -> Sequence[Comment]:
    stmt = (
        select(Comment)
        .where(Comment.task_id == task_id, Comment.deleted_at.is_(None))
        .options(joinedload(Comment.author))
        .order_by(Comment.id)
    )
    return session.scalars(stmt).all()


def edit_comment(session: Session, comment_id: int, body: str) -> Comment:
    comment = _get_active(session, Comment, comment_id)
    comment.body = body
    session.flush()
    return comment


def delete_comment(session: Session, comment_id: int) -> None:
    _soft_delete(session, _get_active(session, Comment, comment_id))

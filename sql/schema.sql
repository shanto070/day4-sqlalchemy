-- =====================================================================
-- 01_schema.sql — Task Tracker schema (PostgreSQL 18)
-- Run while connected to the task_tracker database.
-- =====================================================================

-- Start clean so the script can be re-run while learning.
DROP VIEW  IF EXISTS active_tasks;
DROP TABLE IF EXISTS comments, task_labels, labels, tasks, project_members, projects, users CASCADE;
DROP FUNCTION IF EXISTS set_updated_at();


-- ---------------------------------------------------------------------
-- Keeps updated_at correct on every UPDATE, so application code can't forget it.
-- ---------------------------------------------------------------------
CREATE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;


-- ---------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------
CREATE TABLE users (
    id          uuid        PRIMARY KEY DEFAULT uuidv7(),   -- PG 18; use gen_random_uuid() on older versions
    email       text        NOT NULL CHECK (email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
    full_name   text        NOT NULL CHECK (length(trim(full_name)) > 0),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz                                 -- NULL = active (soft delete)
);

-- Unique among ACTIVE users only (partial index), and case-insensitive,
-- so a soft-deleted user's email can be registered again.
CREATE UNIQUE INDEX users_email_active_uq ON users (lower(email)) WHERE deleted_at IS NULL;


-- ---------------------------------------------------------------------
-- projects
-- ---------------------------------------------------------------------
CREATE TABLE projects (
    id          uuid        PRIMARY KEY DEFAULT uuidv7(),
    owner_id    uuid        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    name        text        NOT NULL CHECK (length(trim(name)) > 0),
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);

-- Postgres does NOT index foreign keys automatically.
CREATE INDEX projects_owner_id_idx ON projects (owner_id);


-- ---------------------------------------------------------------------
-- project_members — junction table (many users <-> many projects)
-- ---------------------------------------------------------------------
CREATE TABLE project_members (
    project_id  uuid        NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    user_id     uuid        NOT NULL REFERENCES users (id)    ON DELETE CASCADE,
    role        text        NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member')),
    joined_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, user_id)
);

-- The PK index starts with project_id, so it can't answer "which projects is this user in?".
CREATE INDEX project_members_user_id_idx ON project_members (user_id);


-- ---------------------------------------------------------------------
-- tasks
-- ---------------------------------------------------------------------
CREATE TABLE tasks (
    id           uuid        PRIMARY KEY DEFAULT uuidv7(),
    project_id   uuid        NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    created_by   uuid        NOT NULL REFERENCES users (id)    ON DELETE RESTRICT,
    assignee_id  uuid,                                          -- NULL = unassigned
    title        text        NOT NULL CHECK (length(trim(title)) > 0),
    description  text,
    status       text        NOT NULL DEFAULT 'todo'
                             CHECK (status IN ('todo', 'in_progress', 'in_review', 'done')),
    priority     smallint    NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 4),  -- 1 = urgent, 4 = low
    due_date     date,                                          -- a deadline is a calendar day, not an instant
    completed_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at   timestamptz,

    -- A task is 'done' exactly when it has a completion time.
    CONSTRAINT tasks_done_has_completed_at
        CHECK ((status = 'done') = (completed_at IS NOT NULL)),

    -- The assignee must be a member of the task's project. Removing them from
    -- the project unassigns the task instead of deleting it.
    CONSTRAINT tasks_assignee_is_member_fk
        FOREIGN KEY (project_id, assignee_id)
        REFERENCES project_members (project_id, user_id)
        ON DELETE SET NULL (assignee_id)
);

-- Project board: WHERE project_id = ? [AND status = ?]. Also serves project_id-only
-- queries (leftmost prefix) and the ON DELETE CASCADE from projects.
CREATE INDEX tasks_project_status_idx ON tasks (project_id, status);

-- "My tasks": WHERE assignee_id = ?. Also covers the composite FK lookup above.
CREATE INDEX tasks_assignee_project_idx ON tasks (assignee_id, project_id);

-- FK check when deleting a user.
CREATE INDEX tasks_created_by_idx ON tasks (created_by);

-- Partial index: only open, non-deleted tasks — small, and exactly what
-- "overdue" / "due soon" queries look for.
CREATE INDEX tasks_open_due_date_idx ON tasks (due_date)
    WHERE status <> 'done' AND deleted_at IS NULL;


-- ---------------------------------------------------------------------
-- labels — integer identity key (internal rows, never in a URL)
-- ---------------------------------------------------------------------
CREATE TABLE labels (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id  uuid   NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    name        text   NOT NULL CHECK (length(trim(name)) > 0),
    color       text   NOT NULL DEFAULT '#808080' CHECK (color ~ '^#[0-9A-Fa-f]{6}$'),
    UNIQUE (project_id, name)
);


-- ---------------------------------------------------------------------
-- task_labels — junction table (many tasks <-> many labels)
-- ---------------------------------------------------------------------
CREATE TABLE task_labels (
    task_id   uuid   NOT NULL REFERENCES tasks (id)  ON DELETE CASCADE,
    label_id  bigint NOT NULL REFERENCES labels (id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, label_id)
);

CREATE INDEX task_labels_label_id_idx ON task_labels (label_id);


-- ---------------------------------------------------------------------
-- comments
-- ---------------------------------------------------------------------
CREATE TABLE comments (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id     uuid        NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    author_id   uuid        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    body        text        NOT NULL CHECK (length(trim(body)) > 0),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);

-- A task's comments, oldest first.
CREATE INDEX comments_task_id_created_at_idx ON comments (task_id, created_at);
CREATE INDEX comments_author_id_idx ON comments (author_id);


-- ---------------------------------------------------------------------
-- updated_at triggers
-- ---------------------------------------------------------------------
CREATE TRIGGER users_set_updated_at    BEFORE UPDATE ON users    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER projects_set_updated_at BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER tasks_set_updated_at    BEFORE UPDATE ON tasks    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER comments_set_updated_at BEFORE UPDATE ON comments FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ---------------------------------------------------------------------
-- Soft-delete helper: query this instead of remembering the filter every time.
-- ---------------------------------------------------------------------
CREATE VIEW active_tasks AS
SELECT * FROM tasks WHERE deleted_at IS NULL;

"""SQLite-backed project and task management."""

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


STATUSES = ("pending", "in_progress", "completed", "cancelled")
_UNSET = object()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    directory TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'completed', 'cancelled')),
    codex_session_id TEXT
);
CREATE INDEX IF NOT EXISTS projects_name_key ON projects(name_key);
CREATE TABLE IF NOT EXISTS project_aliases (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    alias TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    PRIMARY KEY (project_id, alias_key)
);
CREATE INDEX IF NOT EXISTS project_aliases_key ON project_aliases(alias_key);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'completed', 'cancelled')),
    codex_session_id TEXT,
    external_message_id TEXT UNIQUE
);
"""


@dataclass(frozen=True)
class Project:
    number: str
    name: str
    directory: str
    aliases: tuple
    status: str
    codex_session_id: Optional[str]


@dataclass(frozen=True)
class Task:
    number: str
    project_number: str
    title: str
    status: str
    codex_session_id: Optional[str]
    external_message_id: Optional[str]


def _required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _optional(value, label):
    return None if value is None else _required(value, label)


def _number_id(number, prefix):
    number = _required(number, "number").upper()
    digits = number[1:]
    if (number.startswith(prefix) and digits.isascii() and digits.isdigit()
            and len(digits) <= 19):
        value = int(digits)
        if 0 < value <= 2**63 - 1 and number == f"{prefix}{value:03d}":
            return value
    return None


class ProjectManager:
    def __init__(self, database, workspace):
        self.database = Path(database).absolute()
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be a directory")
        with closing(self._connect()) as db:
            db.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + "\nCOMMIT;")

    def _connect(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _directory(self, directory):
        path = Path(directory)
        if ".." in path.parts:
            raise ValueError("project directory cannot contain '..'")
        try:
            resolved = (self.workspace / path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("project directory cannot be resolved") from exc
        if not resolved.is_relative_to(self.workspace) or not resolved.is_dir():
            raise ValueError("project directory must be a directory inside the workspace")
        return resolved

    def project_directory(self, project_number):
        """Resolve and validate the stored directory before handing it to a caller."""
        return self._directory(self.get_project(project_number).directory)

    def create_project(self, name, directory, *, aliases=(), codex_session_id=None):
        name = _required(name, "name")
        directory = self._directory(directory).relative_to(self.workspace).as_posix()
        session = _optional(codex_session_id, "codex_session_id")
        if isinstance(aliases, str):
            raise ValueError("aliases must be an iterable of names")
        by_key = {}
        for alias in aliases:
            alias = _required(alias, "alias")
            by_key.setdefault(alias.casefold(), alias)
        with self._write() as db:
            project_id = db.execute(
                "INSERT INTO projects(name, name_key, directory, codex_session_id)"
                " VALUES (?, ?, ?, ?)", (name, name.casefold(), directory, session)
            ).lastrowid
            db.executemany(
                "INSERT INTO project_aliases(project_id, alias, alias_key) VALUES (?, ?, ?)",
                ((project_id, alias, key) for key, alias in by_key.items()),
            )
            return self._project(db, self._get(db, "projects", project_id))

    def find_projects(self, query):
        query = _required(query, "query")
        key = query.casefold()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN")
            rows = db.execute(
                """SELECT * FROM projects
                   WHERE id = ? OR name_key = ? OR id IN (
                       SELECT project_id FROM project_aliases WHERE alias_key = ?
                   )
                   ORDER BY id""",
                (_number_id(query, "P"), key, key),
            ).fetchall()
            return [self._project(db, row) for row in rows]

    def get_project(self, project_number):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN")
            return self._project(db, self._get(db, "projects", _number_id(project_number, "P")))

    def create_task(self, project_number, title, *, external_message_id=None, codex_session_id=None):
        message_id = _optional(external_message_id, "external_message_id")
        with self._write() as db:
            if message_id is not None:
                existing = db.execute(
                    "SELECT * FROM tasks WHERE external_message_id = ?", (message_id,)
                ).fetchone()
                if existing is not None:
                    return self._task(existing)
            project_id = self._get(db, "projects", _number_id(project_number, "P"))["id"]
            title = _required(title, "title")
            session = _optional(codex_session_id, "codex_session_id")
            task_id = db.execute(
                "INSERT INTO tasks(project_id, title, external_message_id, codex_session_id)"
                " VALUES (?, ?, ?, ?)", (project_id, title, message_id, session)
            ).lastrowid
            return self._task(self._get(db, "tasks", task_id))

    def get_task(self, task_number):
        with closing(self._connect()) as db:
            return self._task(self._get(db, "tasks", _number_id(task_number, "T")))

    def update_project(self, project_number, *, status=None, codex_session_id=_UNSET):
        with self._write() as db:
            row = self._update(db, "projects", _number_id(project_number, "P"), status, codex_session_id)
            return self._project(db, row)

    def update_task(self, task_number, *, status=None, codex_session_id=_UNSET):
        with self._write() as db:
            row = self._update(db, "tasks", _number_id(task_number, "T"), status, codex_session_id)
            return self._task(row)

    @staticmethod
    def _get(db, table, row_id):
        row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown record in {table}")
        return row

    def _update(self, db, table, row_id, status, session):
        row = self._get(db, table, row_id)
        status = row["status"] if status is None else status
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        session = row["codex_session_id"] if session is _UNSET else _optional(session, "codex_session_id")
        db.execute(
            f"UPDATE {table} SET status = ?, codex_session_id = ? WHERE id = ?",
            (status, session, row_id),
        )
        return self._get(db, table, row_id)

    @staticmethod
    def _project(db, row):
        aliases = db.execute(
            "SELECT alias FROM project_aliases WHERE project_id = ? ORDER BY alias_key", (row["id"],)
        )
        return Project(f'P{row["id"]:03d}', row["name"], row["directory"],
                       tuple(alias["alias"] for alias in aliases), row["status"], row["codex_session_id"])

    @staticmethod
    def _task(row):
        return Task(f'T{row["id"]:03d}', f'P{row["project_id"]:03d}', row["title"],
                    row["status"], row["codex_session_id"], row["external_message_id"])

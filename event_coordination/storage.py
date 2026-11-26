"""SQLite persistence. Each operation owns a connection and a transaction."""

from contextlib import contextmanager
from importlib.resources import files
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .errors import Conflict, NotFound, ValidationError
from .validation import now, pagination, version


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Store:
    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValidationError("Use a file database; operations use separate connections")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def transaction(self, *, write: bool = False):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self):
        with self.transaction(write=True) as db:
            current = db.execute("PRAGMA user_version").fetchone()[0]
            if current == 1:
                return
            if current != 0:
                raise Conflict(f"Unsupported database schema version: {current}")
            sql = files("event_coordination").joinpath("migrations/001_domain.sql").read_text()
            statement = ""
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise Conflict("Incomplete migration")
            db.execute("PRAGMA user_version = 1")

    def pending_messages(self, limit: int = 100) -> list[dict]:
        """Internal worker API, not a user-facing message listing."""
        pagination(limit)
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM outbox WHERE acknowledged_at IS NULL ORDER BY sequence LIMIT ?",
                (limit,),
            ).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def acknowledge_message(self, message_id: str) -> None:
        """Internal worker API; call only after confirmed external success."""
        with self.transaction(write=True) as db:
            result = db.execute(
                "UPDATE outbox SET acknowledged_at=COALESCE(acknowledged_at, ?) WHERE message_id=?",
                (now(), message_id),
            )
            if result.rowcount != 1:
                raise NotFound("Message not found")


def event_row(db, event_id: str) -> dict:
    row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise NotFound("Event not found")
    return dict(row)


def task_row(db, task_id: str) -> dict:
    row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise NotFound("Task not found")
    return dict(row)


def bump_event(db, event: dict, expected_version: int, **changes) -> int:
    allowed = {"status", "owner_id", "reason"}
    return _bump(db, "events", event, expected_version, allowed, changes)


def bump_task(db, task: dict, expected_version: int, **changes) -> int:
    allowed = {"status", "assignee_id", "note"}
    return _bump(db, "tasks", task, expected_version, allowed, changes)


def _bump(db, table, row, expected, allowed, changes):
    version(expected)
    if row["version"] != expected:
        raise Conflict("Stale version; reload the resource")
    if not changes.keys() <= allowed:
        raise ValueError("Unsupported internal update")
    updates = {**changes, "version": expected + 1, "updated_at": now()}
    assignment = ", ".join(f"{key}=?" for key in updates)
    result = db.execute(
        f"UPDATE {table} SET {assignment} WHERE id=? AND version=?",
        (*updates.values(), row["id"], expected),
    )
    if result.rowcount != 1:
        raise Conflict("Concurrent change; reload the resource")
    return expected + 1


def record(db, actor_id: str, action: str, event_id: str, detail: dict) -> None:
    db.execute(
        "INSERT INTO audit(actor_id,action,event_id,detail,created_at) VALUES (?,?,?,?,?)",
        (actor_id, action, event_id, encode(detail), now()),
    )


def emit(db, topic: str, event_id: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO outbox(message_id,topic,event_id,payload,created_at) VALUES (?,?,?,?,?)",
        (str(uuid4()), topic, event_id, encode(payload), now()),
    )

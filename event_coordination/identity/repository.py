
import sqlite3

from contextlib import contextmanager
from pathlib import Path

from .policy import AgePolicy


class IdentityRepository:
    def __init__(self, path: str | Path, *, age_policy: AgePolicy | None = None):
        self.age_policy = age_policy
        if str(path) == ":memory:":
            raise ValueError("Use a file database")

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=5,
        )
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
        schema = Path(__file__).with_name(
            "schema.sql"
        ).read_text(encoding="utf-8")

        with self.transaction(write=True) as db:
            statement = ""

            for line in schema.splitlines(keepends=True):
                statement += line

                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""

            if statement.strip():
                raise ValueError("Incomplete SQL schema")

            columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
            if "birth_date" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN birth_date TEXT")

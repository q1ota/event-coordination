import sqlite3

from event_coordination.errors import Conflict, ValidationError
from event_coordination.storage import Store
from tests.support import DomainCase


class StorageTests(DomainCase):
    def test_initialization_is_repeatable_and_foreign_keys_enabled(self):
        Store(self.path)
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(1, db.execute("PRAGMA foreign_keys").fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction(write=True) as db:
                db.execute("INSERT INTO participation VALUES ('missing','helper','active',NULL)")

    def test_transaction_rolls_back_and_unknown_schema_is_rejected(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction(write=True) as db:
                db.execute("PRAGMA user_version=99")
                raise RuntimeError("interrupted")
        Store(self.path)
        with self.store.transaction(write=True) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(Conflict):
            Store(self.path)

    def test_separate_connections_require_file_database(self):
        with self.assertRaises(ValidationError):
            Store(":memory:")

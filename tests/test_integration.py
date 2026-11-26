import json
import sqlite3
import subprocess
import sys

from event_coordination.errors import NotFound
from event_coordination.storage import Store
from tests.support import DomainCase


class IntegrationTests(DomainCase):
    def test_public_example_runs_in_a_separate_process_and_persists_workflow(self):
        path = self.path.with_name("example.sqlite")
        result = subprocess.run(
            [sys.executable, "-m", "examples.u1_scenario", "--database", str(path)],
            check=True, capture_output=True, text=True,
        )
        data = json.loads(result.stdout)
        self.assertEqual("synthetic_local_demo", data["mode"])
        self.assertEqual("archived", data["event"]["status"])
        self.assertEqual("done", data["task"]["status"])
        self.assertEqual(0, data["outbox_remaining"])
        self.assertEqual(0, data["external_messages_sent"])
        self.assertGreater(data["outbox_consumed_locally"], 0)
        with sqlite3.connect(path) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            self.assertEqual("archived", db.execute("SELECT status FROM events").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        rerun = subprocess.run(
            [sys.executable, "-m", "examples.u1_scenario", "--database", str(path)],
            capture_output=True, text=True,
        )
        self.assertNotEqual(0, rerun.returncode)
        self.assertIn("must refer to a new file", rerun.stderr)
        with sqlite3.connect(path) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def test_outbox_survives_restart_and_acknowledgment_is_repeatable(self):
        self.propose()
        initial = self.store.pending_messages()[0]
        reopened = Store(self.path)
        self.assertEqual(initial, reopened.pending_messages()[0])
        reopened.acknowledge_message(initial["message_id"])
        reopened.acknowledge_message(initial["message_id"])
        self.assertEqual([], Store(self.path).pending_messages())
        with self.assertRaises(NotFound):
            reopened.acknowledge_message("unknown")

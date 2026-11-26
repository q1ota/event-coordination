from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from unittest.mock import patch

from event_coordination.errors import (
    Conflict, Forbidden, IdempotencyConflict, NotFound, ValidationError,
)
from event_coordination.events import EventService
from event_coordination.storage import Store
from tests.support import DomainCase


class EventTests(DomainCase):
    def setUp(self):
        super().setUp()
        self.events = EventService(self.store, self.directory, self.reports)

    def test_submit_queue_decision_and_restart(self):
        proposal = self.propose()
        event_id = proposal.resource_id
        self.assertEqual("submitted", self.events.get("author", event_id)["status"])
        self.assertEqual("2030-06-01T09:00:00+00:00", self.events.get("author", event_id)["starts_at"])
        self.assertEqual([event_id], [e["id"] for e in self.events.list_mine("author")])
        self.assertEqual([], self.events.list_mine("other"))
        self.assertEqual([event_id], [e["id"] for e in self.events.review_queue("coordinator")])
        approved = self.events.approve(
            "coordinator", "approve", event_id, owner_id="owner", expected_version=1
        )
        self.assertEqual(2, approved.version)
        self.assertEqual([], self.events.review_queue("coordinator"))
        reopened = EventService(Store(self.path), self.directory)
        self.assertEqual("owner", reopened.get("owner", event_id)["owner_id"])
        self.assertEqual(
            approved, reopened.approve(
                "coordinator", "approve", event_id, owner_id="owner", expected_version=1
            )
        )
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            self.assertEqual(2, db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
            self.assertEqual(2, db.execute("SELECT COUNT(*) FROM audit").fetchone()[0])

    def test_repeat_proposal_persists_and_changed_key_is_rejected(self):
        proposal = self.propose()
        self.assertEqual(proposal, self.propose())
        with self.assertRaises(IdempotencyConflict):
            self.events.propose(
                "author", "proposal", title="Changed", place="Hall",
                starts_at="2030-06-01T12:00:00+03:00", territory_id="north",
                direction_id="culture", purpose="Event", needed_help="Help",
            )
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def test_privileges_are_checked_even_on_replay(self):
        approved = self.approve()
        with self.assertRaises(NotFound):
            self.events.get("other", approved.resource_id)
        with self.assertRaises(Forbidden):
            self.events.review_queue("owner")
        with self.assertRaises(Forbidden):
            self.events.approve(
                "author", "illegal", approved.resource_id, owner_id="author", expected_version=2
            )
        self.directory.people["coordinator"] = replace(
            self.directory.people["coordinator"], can_coordinate=False
        )
        with self.assertRaises(Forbidden):
            self.events.approve(
                "coordinator", "approve", approved.resource_id, owner_id="owner", expected_version=1
            )
        self.directory.revoke("author")
        with self.assertRaises(Forbidden):
            self.propose()
        with self.assertRaises(Forbidden):
            self.events.list_mine("unknown")

    def test_rejection_requires_reason_and_is_terminal(self):
        proposal = self.propose()
        with self.assertRaises(ValidationError):
            self.events.reject(
                "coordinator", "reject", proposal.resource_id, reason=" ", expected_version=1
            )
        self.events.reject(
            "coordinator", "reject", proposal.resource_id,
            reason="No location available", expected_version=1,
        )
        event = self.events.get("author", proposal.resource_id)
        self.assertEqual(("rejected", "No location available"), (event["status"], event["reason"]))
        with self.assertRaises(Conflict):
            self.events.approve(
                "coordinator", "approve", proposal.resource_id, owner_id="owner", expected_version=2
            )

    def test_invalid_owner_and_sql_invariant(self):
        proposal = self.propose()
        for target in ("blocked", "missing"):
            with self.subTest(target=target), self.assertRaises(Forbidden):
                self.events.approve(
                    "coordinator", "approve", proposal.resource_id,
                    owner_id=target, expected_version=1,
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction(write=True) as db:
                db.execute("UPDATE events SET status='approved' WHERE id=?", (proposal.resource_id,))
        self.assertEqual("submitted", self.events.get("author", proposal.resource_id)["status"])

    def test_concurrent_decisions_have_only_one_winner(self):
        proposal = self.propose()

        def decide(owner):
            try:
                return self.events.approve(
                    "coordinator", f"approve-{owner}", proposal.resource_id,
                    owner_id=owner, expected_version=1,
                )
            except Conflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, ["owner", "helper"]))
        self.assertEqual(1, sum(r is not None for r in results))
        event = self.events.get("coordinator", proposal.resource_id)
        self.assertIn(event["owner_id"], {"owner", "helper"})
        self.assertEqual(2, event["version"])
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM participation").fetchone()[0])

    def test_outbox_failure_rolls_back_every_write_and_key_can_be_retried(self):
        with patch("event_coordination.events.emit", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                self.propose()
        with self.store.transaction() as db:
            for table in ("events", "audit", "outbox", "commands"):
                self.assertEqual(0, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        self.assertEqual(1, self.propose().version)

    def test_dates_require_timezone_and_input_is_bound(self):
        for value in ("2030-01-01", "not a date", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.events.propose(
                    "author", "date", title="A", place="B", starts_at=value,
                    territory_id="north", direction_id="culture", purpose="C", needed_help="D",
                )
        receipt = self.events.propose(
            "author", "sql", title="'); DROP TABLE events; --", place="Hall",
            starts_at="2030-01-01T00:00:00Z", territory_id="north",
            direction_id="culture", purpose="Event", needed_help="Help",
        )
        self.assertEqual("'); DROP TABLE events; --", self.events.get("author", receipt.resource_id)["title"])

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from event_coordination.contracts import Identity, CompletionEvidence
from event_coordination.storage import Store


class Directory:
    def __init__(self):
        self.people = {
            "author": Identity("author", "Author", True, territory_id="north"),
            "coordinator": Identity("coordinator", "Coordinator", True, True),
            "owner": Identity("owner", "Owner", True, territory_id="north"),
            "helper": Identity(
                "helper", "Helper", True, territory_id="north",
                interests=("culture",), help_types=("transport",),
            ),
            "other": Identity("other", "Other", True, territory_id="south"),
            "blocked": Identity("blocked", "Blocked", False),
        }

    def get(self, user_id):
        return self.people.get(user_id)

    def candidates(self, territory_id, interest, help_type):
        # Deliberately broad: the domain must still verify current admission/filters.
        return list(self.people.values())

    def revoke(self, user_id):
        self.people[user_id] = replace(self.people[user_id], active=False)


class Reports:
    def __init__(self):
        self.evidence = {}

    def get(self, event_id):
        return self.evidence.get(event_id, CompletionEvidence())


class DomainCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "domain.sqlite"
        self.store = Store(self.path)
        self.directory = Directory()
        self.reports = Reports()

    def propose(self, request_id="proposal"):
        from event_coordination.events import EventService
        return EventService(self.store, self.directory).propose(
            "author", request_id, title="Local festival", place="Community hall",
            starts_at="2030-06-01T12:00:00+03:00", territory_id="north",
            direction_id="culture", purpose="Community event", needed_help="Transport",
        )

    def approve(self):
        from event_coordination.events import EventService
        proposal = self.propose()
        return EventService(self.store, self.directory).approve(
            "coordinator", "approve", proposal.resource_id,
            owner_id="owner", expected_version=proposal.version,
        )

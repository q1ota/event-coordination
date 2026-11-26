"""Run the U1 workflow locally; identity/report providers use synthetic data."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from event_coordination.contracts import CompletionEvidence, Identity
from event_coordination.events import EventService
from event_coordination.participation import ParticipationService
from event_coordination.storage import Store
from event_coordination.tasks import TaskService


class DemoDirectory:
    """Only for this example; replace with U2 admission/role checks."""

    def __init__(self):
        self.people = {
            "organizer": Identity("organizer", "Demo organizer", True, territory_id="north"),
            "coordinator": Identity("coordinator", "Demo coordinator", True, True),
            "helper": Identity(
                "helper", "Demo helper", True, territory_id="north",
                interests=("culture",), help_types=("transport",),
            ),
        }

    def get(self, user_id):
        return self.people.get(user_id)

    def candidates(self, territory_id, interest, help_type):
        return list(self.people.values())


class DemoReports:
    """Only for this example; no report is actually sent to a real chat."""

    def __init__(self):
        self.evidence = {}

    def get(self, event_id):
        return self.evidence.get(event_id, CompletionEvidence())


def run(database: Path) -> dict:
    # Refuse to use or overwrite an existing database, including a production file.
    with database.open("x"):
        pass
    store = Store(database)
    directory, reports = DemoDirectory(), DemoReports()
    events = EventService(store, directory, reports)
    members = ParticipationService(store, directory)
    tasks = TaskService(store, directory)
    proposal_input = {
        "title": "Community festival", "place": "Community hall",
        "starts_at": "2030-06-01T12:00:00+03:00",
        "territory_id": "north", "direction_id": "culture",
        "purpose": "Bring the community together", "needed_help": "Transport",
    }
    proposal = events.propose("organizer", "propose", **proposal_input)
    event_id = proposal.resource_id
    approved = events.approve(
        "coordinator", "approve", event_id, owner_id="organizer", expected_version=proposal.version
    )
    candidates = members.find_candidates("organizer", event_id, help_type="transport")
    invited = members.invite(
        "organizer", "invite", event_id, user_id=candidates[0]["user_id"],
        expected_version=approved.version,
    )
    joined = members.request_join("helper", "join", event_id, expected_version=invited.version)
    preparing = events.advance(
        "organizer", "prepare", event_id, status="preparing", expected_version=joined.version
    )
    assigned = tasks.assign(
        "organizer", "assign", event_id, title="Arrange transport", assignee_id="helper",
        due_at="2030-05-31T15:00:00+03:00", expected_version=preparing.version,
    )
    helped = tasks.set_status(
        "helper", "help", assigned.resource_id, status="needs_help",
        note="Need a second driver", expected_version=assigned.version,
    )
    progress = tasks.set_status(
        "helper", "progress", assigned.resource_id, status="in_progress", expected_version=helped.version
    )
    tasks.set_status(
        "helper", "done", assigned.resource_id, status="done", expected_version=progress.version
    )
    started = events.advance(
        "organizer", "start", event_id, status="ongoing",
        expected_version=events.get("organizer", event_id)["version"],
    )
    reports.evidence[event_id] = CompletionEvidence(report_id="demo-report")
    completed = events.complete("organizer", "complete", event_id, expected_version=started.version)
    reports.evidence[event_id] = CompletionEvidence("demo-report", True, True)
    events.archive("organizer", "archive", event_id, expected_version=completed.version)

    # Reopen actual persistence and replay a command using its original parameters.
    reopened = Store(database)
    replayed = EventService(reopened, directory).propose("organizer", "propose", **proposal_input)
    assert replayed == proposal

    # A synthetic in-process consumer acknowledges the outbox; it sends nothing externally.
    delivered = []
    for message in reopened.pending_messages():
        delivered.append(message["message_id"])
        reopened.acknowledge_message(message["message_id"])

    return {
        "mode": "synthetic_local_demo",
        "event": events.get("organizer", event_id),
        "task": tasks.get("helper", assigned.resource_id),
        "proposal_receipt_after_restart": asdict(replayed),
        "outbox_consumed_locally": len(delivered),
        "outbox_remaining": len(reopened.pending_messages()),
        "external_messages_sent": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="New file to retain demo data; must not exist")
    args = parser.parse_args()
    if args.database is None:
        with TemporaryDirectory(prefix="event-coordination-demo-") as folder:
            result = run(Path(folder) / "demo.sqlite")
    else:
        try:
            result = run(args.database)
        except FileExistsError:
            parser.error("--database must refer to a new file")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

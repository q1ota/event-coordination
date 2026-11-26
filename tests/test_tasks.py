from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from event_coordination.contracts import CompletionEvidence
from event_coordination.errors import Conflict, Forbidden, NotFound, ValidationError
from event_coordination.events import EventService
from event_coordination.participation import ParticipationService
from event_coordination.tasks import TaskService
from tests.support import DomainCase


class TaskTests(DomainCase):
    def setUp(self):
        super().setUp()
        self.events = EventService(self.store, self.directory, self.reports)
        self.members = ParticipationService(self.store, self.directory)
        self.tasks = TaskService(self.store, self.directory)
        self.event_id = self.approve().resource_id
        invite = self.members.invite(
            "owner", "invite", self.event_id, user_id="helper", expected_version=2
        )
        joined = self.members.request_join(
            "helper", "accept", self.event_id, expected_version=invite.version
        )
        self.task = self.tasks.assign(
            "owner", "task", self.event_id, title="Arrange transport", assignee_id="helper",
            due_at="2030-05-31T16:00:00+03:00", expected_version=joined.version,
        )

    def event_version(self):
        return self.events.get("owner", self.event_id)["version"]

    def ongoing(self):
        preparing = self.events.advance(
            "owner", "prepare", self.event_id, status="preparing", expected_version=self.event_version()
        )
        return self.events.advance(
            "owner", "start", self.event_id, status="ongoing", expected_version=preparing.version
        )

    def test_statuses_help_and_notifications(self):
        task = self.tasks.get("helper", self.task.resource_id)
        self.assertEqual("assigned", task["status"])
        self.assertEqual("2030-05-31T13:00:00+00:00", task["due_at"])
        running = self.tasks.set_status(
            "helper", "progress", task["id"], status="in_progress", expected_version=1
        )
        help_request = self.tasks.set_status(
            "helper", "help", task["id"], status="needs_help",
            note="A second driver is needed", expected_version=running.version,
        )
        self.assertEqual("helper", self.tasks.get("helper", task["id"])["assignee_id"])
        self.assertEqual(help_request, self.tasks.set_status(
            "helper", "help", task["id"], status="needs_help",
            note="A second driver is needed", expected_version=running.version,
        ))
        done = self.tasks.set_status(
            "helper", "done", task["id"], status="done", expected_version=help_request.version
        )
        self.assertEqual("done", self.tasks.get("helper", task["id"])["status"])
        self.assertIsNone(self.tasks.get("helper", task["id"])["note"])
        messages = [m for m in self.store.pending_messages() if m["topic"].startswith("task.")]
        self.assertFalse(any(m["payload"]["audience"] == "coordination" for m in messages))
        helps = [m for m in messages if m["topic"] == "task.help_requested"]
        self.assertEqual(1, len(helps))
        self.assertEqual("event_owner", helps[0]["payload"]["audience"])
        with self.assertRaises(Conflict):
            self.tasks.set_status(
                "helper", "reopen", task["id"], status="in_progress", expected_version=done.version
            )

    def test_only_executor_can_report_and_only_own_tasks_are_visible(self):
        owner_task = self.tasks.assign(
            "owner", "owner-task", self.event_id, title="Prepare venue", assignee_id="owner",
            due_at="2030-06-01T08:00:00Z", expected_version=self.event_version(),
        )
        self.assertEqual(2, len(self.tasks.list_for_event("owner", self.event_id)))
        self.assertEqual([self.task.resource_id], [
            t["id"] for t in self.tasks.list_for_event("helper", self.event_id)
        ])
        self.assertEqual(1, len(self.tasks.list_mine("helper")))
        self.assertEqual([], self.tasks.list_for_event("author", self.event_id))
        with self.assertRaises(NotFound):
            self.tasks.get("helper", owner_task.resource_id)
        with self.assertRaises(NotFound):
            self.tasks.get("other", self.task.resource_id)
        with self.assertRaises(Forbidden):
            self.tasks.set_status(
                "owner", "fake-done", self.task.resource_id, status="done", expected_version=1
            )
        self.directory.revoke("helper")
        with self.assertRaises(Forbidden):
            self.tasks.list_mine("helper")

    def test_assignment_requires_active_membership_and_valid_deadline(self):
        invited = self.members.invite(
            "owner", "invite-other", self.event_id, user_id="other", expected_version=self.event_version()
        )
        with self.assertRaises(Conflict):
            self.tasks.assign(
                "owner", "not-active", self.event_id, title="Help", assignee_id="other",
                due_at="2030-06-01T00:00:00Z", expected_version=invited.version,
            )
        with self.assertRaises(ValidationError):
            self.tasks.assign(
                "owner", "date", self.event_id, title="Help", assignee_id="helper",
                due_at="2030-06-01", expected_version=invited.version,
            )

    def test_reassignment_removes_previous_executor_access_and_clears_help(self):
        help_request = self.tasks.set_status(
            "helper", "help", self.task.resource_id, status="needs_help",
            note="Unavailable", expected_version=1,
        )
        assigned = self.tasks.reassign(
            "owner", "reassign", self.task.resource_id,
            assignee_id="owner", expected_version=help_request.version,
        )
        task = self.tasks.get("owner", self.task.resource_id)
        self.assertEqual(("assigned", "owner", None), (task["status"], task["assignee_id"], task["note"]))
        self.assertEqual([], self.tasks.list_mine("helper"))
        with self.assertRaises(NotFound):
            self.tasks.set_status(
                "helper", "help", self.task.resource_id, status="needs_help",
                note="Unavailable", expected_version=1,
            )
        self.assertEqual(3, assigned.version)

    def test_open_tasks_prevent_member_removal_until_reassigned(self):
        with self.assertRaises(Conflict):
            self.members.remove(
                "owner", "remove", self.event_id, user_id="helper",
                reason="Leave", expected_version=self.event_version(),
            )
        self.tasks.reassign(
            "owner", "reassign", self.task.resource_id, assignee_id="owner", expected_version=1
        )
        self.members.remove(
            "owner", "remove", self.event_id, user_id="helper",
            reason="Leave", expected_version=self.event_version(),
        )
        self.assertEqual("removed", self.members.my_participation("helper", self.event_id)["status"])

    def test_help_requires_note_and_cancellation_requires_reason(self):
        with self.assertRaises(ValidationError):
            self.tasks.set_status(
                "helper", "help", self.task.resource_id, status="needs_help", expected_version=1
            )
        with self.assertRaises(ValidationError):
            self.tasks.cancel(
                "owner", "cancel", self.task.resource_id, reason="", expected_version=1
            )
        self.tasks.cancel(
            "owner", "cancel", self.task.resource_id, reason="No longer needed", expected_version=1
        )
        task = self.tasks.get("helper", self.task.resource_id)
        self.assertEqual(("cancelled", "No longer needed"), (task["status"], task["note"]))

    def test_stale_concurrent_status_does_not_overwrite(self):
        def update(status):
            try:
                return self.tasks.set_status(
                    "helper", status, self.task.resource_id, status=status, expected_version=1
                )
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ["in_progress", "done"]))
        self.assertEqual(1, sum(result is not None for result in results))
        self.assertEqual(2, self.tasks.get("helper", self.task.resource_id)["version"])

    def test_completion_requires_report_and_explicit_resolution_of_open_tasks(self):
        started = self.ongoing()
        with self.assertRaises(Conflict):
            self.events.complete(
                "owner", "complete", self.event_id, expected_version=started.version,
                cancel_remaining_reason="No longer needed",
            )
        self.reports.evidence[self.event_id] = CompletionEvidence(report_id="report-1")
        with self.assertRaises(Conflict):
            self.events.complete("owner", "complete", self.event_id, expected_version=started.version)
        self.assertEqual("ongoing", self.events.get("owner", self.event_id)["status"])
        completed = self.events.complete(
            "owner", "complete", self.event_id, expected_version=started.version,
            cancel_remaining_reason="Transport was provided by the venue",
        )
        task = self.tasks.get("helper", self.task.resource_id)
        self.assertEqual("cancelled", task["status"])
        self.assertEqual("Transport was provided by the venue", task["note"])
        with self.assertRaises(Conflict):
            self.tasks.assign(
                "owner", "late-task", self.event_id, title="Late", assignee_id="owner",
                due_at="2030-06-02T00:00:00Z", expected_version=completed.version,
            )
        with self.assertRaises(Conflict):
            self.tasks.set_status(
                "helper", "late-status", self.task.resource_id, status="done", expected_version=2
            )
        with self.assertRaises(Conflict):
            self.events.archive("owner", "archive", self.event_id, expected_version=completed.version)
        self.reports.evidence[self.event_id] = CompletionEvidence("report-1", True, False)
        with self.assertRaises(Conflict):
            self.events.archive("owner", "archive", self.event_id, expected_version=completed.version)
        self.reports.evidence[self.event_id] = CompletionEvidence("report-1", True, True)
        self.events.archive("owner", "archive", self.event_id, expected_version=completed.version)
        self.assertEqual("archived", self.events.get("helper", self.event_id)["status"])

    def test_completion_without_integration_is_closed_and_finished_tasks_stay_done(self):
        started = self.ongoing()
        self.tasks.set_status(
            "helper", "done", self.task.resource_id, status="done", expected_version=1
        )
        disconnected = EventService(self.store, self.directory)
        with self.assertRaises(Conflict):
            disconnected.complete("owner", "complete", self.event_id, expected_version=started.version)
        self.reports.evidence[self.event_id] = CompletionEvidence("report-1")
        self.events.complete("owner", "complete", self.event_id, expected_version=started.version)
        self.assertEqual("done", self.tasks.get("helper", self.task.resource_id)["status"])

    def test_cancellation_closes_tasks_and_prevents_new_participation(self):
        cancelled = self.events.cancel(
            "owner", "cancel-event", self.event_id, reason="Venue unavailable",
            expected_version=self.event_version(),
        )
        self.assertEqual("cancelled", self.tasks.get("helper", self.task.resource_id)["status"])
        with self.assertRaises(Conflict):
            self.members.invite(
                "owner", "late-invite", self.event_id, user_id="other", expected_version=cancelled.version
            )
        with self.assertRaises(NotFound):
            self.members.request_join(
                "other", "late-join", self.event_id, expected_version=cancelled.version
            )

    def test_failure_during_completion_rolls_back_tasks_event_and_effects(self):
        started = self.ongoing()
        self.reports.evidence[self.event_id] = CompletionEvidence("report-1")
        before = len(self.store.pending_messages())
        with patch("event_coordination.events.emit", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                self.events.complete(
                    "owner", "complete", self.event_id, expected_version=started.version,
                    cancel_remaining_reason="Not needed",
                )
        self.assertEqual("ongoing", self.events.get("owner", self.event_id)["status"])
        self.assertEqual("assigned", self.tasks.get("helper", self.task.resource_id)["status"])
        self.assertEqual(before, len(self.store.pending_messages()))
        self.events.complete(
            "owner", "complete", self.event_id, expected_version=started.version,
            cancel_remaining_reason="Not needed",
        )

    def test_explicit_problem_escalates_but_ordinary_progress_does_not(self):
        self.events.escalate_problem(
            "owner", "escalate", self.event_id, description="The venue is unavailable",
            expected_version=self.event_version(),
        )
        messages = [
            m for m in self.store.pending_messages()
            if m["topic"] == "event.problem_reported"
        ]
        self.assertEqual(1, len(messages))
        self.assertEqual("coordination", messages[0]["payload"]["audience"])

    def test_transitions_cannot_skip_preparation_or_use_free_form_state(self):
        with self.assertRaises(Conflict):
            self.events.advance(
                "owner", "start", self.event_id, status="ongoing", expected_version=self.event_version()
            )
        with self.assertRaises(ValidationError):
            self.events.advance(
                "owner", "archive", self.event_id, status="archived", expected_version=self.event_version()
            )

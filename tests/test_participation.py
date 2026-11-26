from concurrent.futures import ThreadPoolExecutor

from event_coordination.errors import Conflict, Forbidden, NotFound, ValidationError
from event_coordination.events import EventService
from event_coordination.participation import ParticipationService
from tests.support import DomainCase


class ParticipationTests(DomainCase):
    def setUp(self):
        super().setUp()
        self.events = EventService(self.store, self.directory)
        self.members = ParticipationService(self.store, self.directory)
        self.event_id = self.approve().resource_id

    def test_invitation_requires_acceptance_and_duplicate_is_safe(self):
        invite = self.members.invite(
            "owner", "invite", self.event_id, user_id="helper", expected_version=2
        )
        self.assertEqual("invited", self.members.my_participation("helper", self.event_id)["status"])
        self.assertEqual(invite, self.members.invite(
            "owner", "invite", self.event_id, user_id="helper", expected_version=2
        ))
        joined = self.members.request_join(
            "helper", "accept", self.event_id, expected_version=invite.version
        )
        self.assertEqual("active", self.members.my_participation("helper", self.event_id)["status"])
        self.assertEqual(joined, self.members.request_join(
            "helper", "accept", self.event_id, expected_version=invite.version
        ))
        self.assertEqual(2, len(self.members.list_members("owner", self.event_id)))

    def test_request_acceptance_and_rejection(self):
        request = self.members.request_join("helper", "join", self.event_id, expected_version=2)
        with self.assertRaises(ValidationError):
            self.members.decide(
                "owner", "decision", self.event_id, user_id="helper",
                accept=False, reason=" ", expected_version=request.version,
            )
        rejected = self.members.decide(
            "owner", "decision", self.event_id, user_id="helper",
            accept=False, reason="Team is full", expected_version=request.version,
        )
        self.assertEqual("rejected", self.members.my_participation("helper", self.event_id)["status"])
        with self.assertRaises(NotFound):
            self.events.get("helper", self.event_id)
        request = self.members.request_join(
            "helper", "apply-again", self.event_id, expected_version=rejected.version
        )
        self.members.decide(
            "owner", "accept", self.event_id, user_id="helper",
            accept=True, expected_version=request.version,
        )
        self.assertEqual("active", self.members.my_participation("helper", self.event_id)["status"])

    def test_revocation_denies_invite_acceptance_and_manager_reads(self):
        invitation = self.members.invite(
            "owner", "invite", self.event_id, user_id="helper", expected_version=2
        )
        self.directory.revoke("helper")
        with self.assertRaises(Forbidden):
            self.members.request_join(
                "helper", "accept", self.event_id, expected_version=invitation.version
            )
        with self.assertRaises(Forbidden):
            self.members.list_members("author", self.event_id)
        with self.assertRaises(NotFound):
            self.members.find_candidates("other", self.event_id)
        self.directory.revoke("owner")
        with self.assertRaises(Forbidden):
            self.members.list_members("owner", self.event_id)

    def test_transfer_is_atomic_and_previous_owner_loses_management(self):
        changed = self.members.transfer_owner(
            "owner", "transfer", self.event_id, owner_id="helper", expected_version=2
        )
        self.assertEqual("helper", self.events.get("helper", self.event_id)["owner_id"])
        self.assertEqual("active", self.members.my_participation("helper", self.event_id)["status"])
        with self.assertRaises(Forbidden):
            self.members.invite(
                "owner", "invite", self.event_id, user_id="other", expected_version=changed.version
            )
        with self.assertRaises(Forbidden):
            self.members.transfer_owner(
                "owner", "transfer", self.event_id, owner_id="helper", expected_version=2
            )
        with self.assertRaises(Conflict):
            self.members.remove(
                "coordinator", "remove-owner", self.event_id, user_id="helper",
                reason="Remove", expected_version=changed.version,
            )

    def test_concurrent_transfers_do_not_overwrite_the_winner(self):
        def transfer(target):
            try:
                return self.members.transfer_owner(
                    "coordinator", target, self.event_id, owner_id=target, expected_version=2
                )
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(transfer, ["helper", "other"]))
        self.assertEqual(1, sum(r is not None for r in results))
        self.assertEqual(3, self.events.get("coordinator", self.event_id)["version"])

    def test_candidates_filter_admission_territory_interests_and_help(self):
        self.assertEqual(
            [{"user_id": "helper", "display_name": "Helper"}],
            self.members.find_candidates("owner", self.event_id, help_type="transport"),
        )
        self.assertEqual([], self.members.find_candidates("owner", self.event_id, help_type="cooking"))
        self.directory.revoke("helper")
        self.assertEqual([], self.members.find_candidates("owner", self.event_id))

    def test_removal_revokes_reading_and_reapplication_requires_invitation(self):
        invite = self.members.invite(
            "owner", "invite", self.event_id, user_id="helper", expected_version=2
        )
        joined = self.members.request_join("helper", "accept", self.event_id, expected_version=invite.version)
        removed = self.members.remove(
            "owner", "remove", self.event_id, user_id="helper", reason="Left the team",
            expected_version=joined.version,
        )
        self.assertEqual("removed", self.members.my_participation("helper", self.event_id)["status"])
        with self.assertRaises(NotFound):
            self.events.get("helper", self.event_id)
        with self.assertRaises(Forbidden):
            self.members.request_join("helper", "new-request", self.event_id, expected_version=removed.version)
        with self.assertRaises(Forbidden):
            self.members.request_join("helper", "accept", self.event_id, expected_version=invite.version)
        # A replay cannot return protected membership changes after admission is revoked.
        self.directory.revoke("helper")
        with self.assertRaises(Forbidden):
            self.members.request_join("helper", "accept", self.event_id, expected_version=invite.version)

    def test_invalid_and_stale_transfer_leave_owner_unchanged(self):
        with self.assertRaises(Forbidden):
            self.members.transfer_owner(
                "owner", "change", self.event_id, owner_id="blocked", expected_version=2
            )
        with self.assertRaises(Conflict):
            self.members.transfer_owner(
                "owner", "change", self.event_id, owner_id="helper", expected_version=1
            )
        self.assertEqual("owner", self.events.get("owner", self.event_id)["owner_id"])

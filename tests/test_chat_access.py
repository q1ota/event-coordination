import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from event_coordination.identity.admission import AdmissionPolicy
from event_coordination.identity.bootstrap import BootstrapService
from event_coordination.identity.chat_access import ChatAccessService, ChatKind, U1EventMembership
from event_coordination.identity.gateway import IdentityGateway
from event_coordination.identity.models import Role
from event_coordination.identity.policy import AgePolicy
from event_coordination.identity.registration import RegistrationService
from event_coordination.identity.repository import IdentityRepository
from event_coordination.events import EventService
from event_coordination.participation import ParticipationService
from event_coordination.storage import Store
from event_coordination.identity.directory import SQLiteIdentityDirectory


class ChatAccessTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.repo = IdentityRepository(self.root / "identity.sqlite", age_policy=AgePolicy())
        policy = AdmissionPolicy(16, "v1", "v1", "v1")
        registration = RegistrationService(self.repo)
        registration.add_territory("kazan", "Tatarstan", "Kazan")
        registration.add_territory("other", "Tatarstan", "Other")
        registration.add_direction("culture", "Culture")
        registration.add_direction("sport", "Sport")
        self.gateway = IdentityGateway(self.repo, policy=policy, review_enabled=True,
            grantable_roles=frozenset({Role.TERRITORY_COORDINATOR, Role.COUNCIL_REPRESENTATIVE}))
        self.coordinator = self.pending(1001)
        BootstrapService(self.repo, policy=policy).initialize(1001, operator_ref="TEST", reason="Test")
        self.member = self.pending(1002)
        self.gateway.approve_registration(1001, target_id=self.member, reason="Test")
        self.access = ChatAccessService(self.repo)

    def pending(self, sender):
        user = self.gateway.start_registration(sender)
        self.gateway.update_my_profile(sender, display_name="Synthetic user", territory_id="kazan",
            birth_date=date(2000,1,1), interests=("culture",), help_types=("organization",))
        for kind in ("rules", "personal_data"):
            self.gateway.accept_my_consent(sender, consent_type=kind, version="v1")
        self.gateway.submit_my_registration(sender)
        return user.user_id

    def grant(self, role, scope):
        self.gateway.grant_role(1001, target_id=self.member, role=role, scope_id=scope, reason="Test")

    def test_coordination(self):
        self.assertTrue(self.access.can_access(self.coordinator, ChatKind.COORDINATION))
        self.assertFalse(self.access.can_access(self.member, ChatKind.COORDINATION))

    def test_profile_not_permission(self):
        self.assertFalse(self.access.can_access(self.member, ChatKind.TERRITORY, "kazan"))
        self.assertFalse(self.access.can_access(self.member, ChatKind.COUNCIL, "culture"))

    def test_territory_scope(self):
        self.grant(Role.TERRITORY_COORDINATOR, "kazan")
        self.assertTrue(self.access.can_access(self.member, ChatKind.TERRITORY, "kazan"))
        self.assertFalse(self.access.can_access(self.member, ChatKind.TERRITORY, "other"))

    def test_council_scope(self):
        self.grant(Role.COUNCIL_REPRESENTATIVE, "culture")
        self.assertTrue(self.access.can_access(self.member, ChatKind.COUNCIL, "culture"))
        self.assertFalse(self.access.can_access(self.member, ChatKind.COUNCIL, "sport"))

    def test_multiple_scopes(self):
        self.grant(Role.TERRITORY_COORDINATOR, "kazan")
        self.grant(Role.COUNCIL_REPRESENTATIVE, "culture")
        self.assertTrue(self.access.can_access(self.member, ChatKind.TERRITORY, "kazan"))
        self.assertTrue(self.access.can_access(self.member, ChatKind.COUNCIL, "culture"))

    def test_explicit_access(self):
        access = ChatAccessService(self.repo, confirmed_access=lambda u,k,s: (u,k,s)==(self.member,ChatKind.TERRITORY,"kazan"))
        self.assertTrue(access.can_access(self.member, ChatKind.TERRITORY, "kazan"))
        self.assertFalse(access.can_access(self.member, ChatKind.TERRITORY, "other"))

    def test_unknown_scope_denied_even_with_grant(self):
        access = ChatAccessService(self.repo, confirmed_access=lambda *args: True)
        self.assertFalse(access.can_access(self.member, ChatKind.TERRITORY, "unknown"))

    def test_no_event_reader(self):
        self.assertFalse(self.access.can_access(self.coordinator, ChatKind.EVENT, "event"))

    def test_event_reader_is_authoritative(self):
        access = ChatAccessService(self.repo, event_member=lambda u,e: (u,e)==(self.member,"event"),
                                   confirmed_access=lambda *args: True)
        self.assertTrue(access.can_access(self.member, ChatKind.EVENT, "event"))
        self.assertFalse(access.can_access(self.member, ChatKind.EVENT, "other"))

    def test_pending_user_denied(self):
        user = self.pending(1003)
        access = ChatAccessService(self.repo, event_member=lambda *args: True)
        self.assertFalse(access.can_access(user, ChatKind.EVENT, "event"))

    def test_role_revocation(self):
        self.grant(Role.TERRITORY_COORDINATOR, "kazan")
        self.gateway.revoke_role(1001, target_id=self.member, role=Role.TERRITORY_COORDINATOR, scope_id="kazan", reason="Test")
        self.assertFalse(self.access.can_access(self.member, ChatKind.TERRITORY, "kazan"))

    def test_consent_withdrawal(self):
        self.grant(Role.TERRITORY_COORDINATOR, "kazan")
        self.gateway.withdraw_my_consent(1002, consent_type="personal_data")
        self.assertFalse(self.access.can_access(self.member, ChatKind.TERRITORY, "kazan"))

    def test_sender_and_invalid_inputs(self):
        self.assertTrue(self.access.can_access_sender(1001, ChatKind.COORDINATION))
        self.assertFalse(self.access.can_access_sender(1002, ChatKind.COORDINATION))
        self.assertFalse(self.access.can_access_sender(True, ChatKind.COORDINATION))
        self.assertFalse(self.access.can_access(self.coordinator, "unknown"))
        self.assertFalse(self.access.can_access(self.coordinator, ChatKind.COORDINATION, "extra"))

    def test_real_u1_event_membership(self):
        store = Store(self.root / "events.sqlite")
        directory = SQLiteIdentityDirectory(self.repo)
        events = EventService(store, directory)
        participation = ParticipationService(store, directory)
        event = events.propose(self.member, "proposal", title="Test", place="Kazan",
            starts_at="2030-01-01T10:00:00+00:00", territory_id="kazan", direction_id="culture",
            purpose="Test", needed_help="Test")
        event = events.approve(self.coordinator, "approve", event.resource_id,
            owner_id=self.coordinator, expected_version=event.version)
        access = ChatAccessService(self.repo, event_member=U1EventMembership(store))
        self.assertTrue(access.can_access(self.coordinator, ChatKind.EVENT, event.resource_id))
        self.assertFalse(access.can_access(self.member, ChatKind.EVENT, event.resource_id))
        event = participation.request_join(self.member, "join", event.resource_id, expected_version=event.version)
        self.assertFalse(access.can_access(self.member, ChatKind.EVENT, event.resource_id))
        event = participation.decide(self.coordinator, "accept", event.resource_id, user_id=self.member,
            accept=True, expected_version=event.version)
        self.assertTrue(access.can_access(self.member, ChatKind.EVENT, event.resource_id))
        event = participation.remove(self.coordinator, "remove", event.resource_id, user_id=self.member,
            reason="Test", expected_version=event.version)
        self.assertFalse(access.can_access(self.member, ChatKind.EVENT, event.resource_id))

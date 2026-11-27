import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from event_coordination.contracts import Identity
from event_coordination.identity.admission import (
    AdmissionPolicy,
    AdmissionService,
)
from event_coordination.identity.authorization import (
    AuthorizationError,
)
from event_coordination.identity.directory import (
    SQLiteIdentityDirectory,
)
from event_coordination.identity.external import (
    ExternalIdentityService,
)
from event_coordination.identity.models import Role
from event_coordination.identity.registration import (
    RegistrationService,
    now,
)
from event_coordination.identity.repository import (
    IdentityRepository,
)


class IdentityIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)

        self.path = Path(folder.name) / "identity.sqlite"

        self.repository = IdentityRepository(
            self.path
        )

        self.registration = RegistrationService(
            self.repository
        )

        self.directory = SQLiteIdentityDirectory(
            self.repository
        )

        self.external = ExternalIdentityService(
            self.repository,
            linking_enabled=True,
        )

        self.admission = AdmissionService(
            self.repository,
            policy=AdmissionPolicy(
                adult_age=18,
                rules_version="v1",
                personal_data_version="v1",
                parental_version="v1",
            ),
            review_enabled=True,
        )

        self.registration.add_territory(
            "kazan", "Tatarstan", "Kazan"
        )

        self.registration.add_territory(
            "moscow", "Moscow", "Moscow"
        )

        self.registration.add_direction(
            "culture", "Culture"
        )

        self.registration.add_direction(
            "sport", "Sport"
        )

        coordinator = self.registration.start_registration(
            1001
        )

        self.coordinator = coordinator.user_id

        with self.repository.transaction(write=True) as db:
            db.execute(
                """
                UPDATE users
                SET status = 'active',
                    display_name = 'Coordinator'
                WHERE id = ?
                """,
                (self.coordinator,),
            )

            db.execute(
                """
                INSERT INTO user_roles (
                    id,
                    user_id,
                    role,
                    scope_id,
                    granted_by,
                    granted_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    self.coordinator,
                    Role.GENERAL_COORDINATOR,
                    "",
                    self.coordinator,
                    now(),
                ),
            )

        self.user = self.create_user(1002)

    def create_user(
        self,
        telegram_id,
        territory="kazan",
        interest="culture",
        help_type="transport",
        approve=True,
    ):
        user = self.registration.start_registration(
            telegram_id
        )

        self.registration.update_profile(
            user.user_id,
            display_name="Test Participant",
            territory_id=territory,
            age=20,
            interests=(interest,),
            help_types=(help_type,),
        )

        self.registration.accept_consent(
            user.user_id, "rules", "v1"
        )

        self.registration.accept_consent(
            user.user_id, "personal_data", "v1"
        )

        self.registration.submit_registration(
            user.user_id,
            rules_version="v1",
            personal_data_version="v1",
        )

        if approve:
            self.admission.approve_registration(
                self.coordinator,
                user.user_id,
                reason="Test admission",
            )

        return user.user_id

    def link_external(self):
        self.external.link(
            self.coordinator,
            self.user,
            "test-system",
            "external-100",
            reason="Test linking",
        )

    def test_directory_returns_u1_identity(self):
        identity = self.directory.get(self.user)

        self.assertIsInstance(identity, Identity)
        self.assertEqual(self.user, identity.user_id)
        self.assertTrue(identity.active)

    def test_unknown_identity_returns_none(self):
        self.assertIsNone(
            self.directory.get("unknown-user")
        )

    def test_draft_identity_is_inactive(self):
        user = self.registration.start_registration(
            1003
        )

        identity = self.directory.get(
            user.user_id
        )

        self.assertFalse(identity.active)
        self.assertFalse(identity.can_coordinate)

    def test_pending_user_is_not_active(self):
        user = self.create_user(
            1003,
            approve=False,
        )

        identity = self.directory.get(user)

        self.assertFalse(identity.active)

    def test_general_coordinator_has_permission(self):
        identity = self.directory.get(
            self.coordinator
        )

        self.assertTrue(
            identity.can_coordinate
        )

    def test_participant_has_no_global_permission(self):
        identity = self.directory.get(self.user)

        self.assertFalse(
            identity.can_coordinate
        )

    def test_identity_contains_no_private_profile_fields(self):
        identity = self.directory.get(self.user)

        for field in (
            "telegram_user_id",
            "age",
            "consents",
            "parental_consents",
        ):
            self.assertFalse(
                hasattr(identity, field)
            )

    def test_candidates_filter_by_territory(self):
        other = self.create_user(
            1003,
            territory="moscow",
        )

        candidates = self.directory.candidates(
            "kazan", None, None
        )

        ids = {
            item.user_id
            for item in candidates
        }

        self.assertIn(self.user, ids)
        self.assertNotIn(other, ids)

    def test_candidates_filter_by_interest(self):
        other = self.create_user(
            1003,
            interest="sport",
        )

        candidates = self.directory.candidates(
            "kazan", "culture", None
        )

        ids = {
            item.user_id
            for item in candidates
        }

        self.assertIn(self.user, ids)
        self.assertNotIn(other, ids)

    def test_candidates_filter_by_help_type(self):
        other = self.create_user(
            1003,
            help_type="organization",
        )

        candidates = self.directory.candidates(
            "kazan", None, "transport"
        )

        ids = {
            item.user_id
            for item in candidates
        }

        self.assertIn(self.user, ids)
        self.assertNotIn(other, ids)

    def test_pending_user_is_not_candidate(self):
        pending = self.create_user(
            1003,
            approve=False,
        )

        candidates = self.directory.candidates(
            "kazan", None, None
        )

        ids = {
            item.user_id
            for item in candidates
        }

        self.assertNotIn(pending, ids)

    def test_revocation_is_visible_immediately(self):
        self.assertTrue(
            self.directory.get(self.user).active
        )

        self.admission.revoke_access(
            self.coordinator,
            self.user,
            reason="Test revocation",
        )

        self.assertFalse(
            self.directory.get(self.user).active
        )

        candidates = self.directory.candidates(
            "kazan", None, None
        )

        self.assertNotIn(
            self.user,
            {
                item.user_id
                for item in candidates
            },
        )

    def test_external_identity_can_be_linked(self):
        self.link_external()

        self.assertEqual(
            self.user,
            self.external.resolve(
                "test-system",
                "external-100",
            ),
        )

    def test_external_link_preserves_internal_id(self):
        self.link_external()

        profile = self.registration.start_registration(
            1002
        )

        self.assertEqual(
            self.user,
            profile.user_id,
        )

    def test_duplicate_external_link_is_safe(self):
        self.link_external()
        self.link_external()

        with self.repository.transaction() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM external_identities"
            ).fetchone()[0]

        self.assertEqual(1, count)

    def test_external_identity_cannot_belong_to_two_users(self):
        self.link_external()

        other = self.create_user(1003)

        with self.assertRaises(ValueError):
            self.external.link(
                self.coordinator,
                other,
                "test-system",
                "external-100",
                reason="Test duplicate identity",
            )

    def test_external_identity_can_be_unlinked(self):
        self.link_external()

        result = self.external.unlink(
            self.coordinator,
            self.user,
            "test-system",
            reason="Test unlinking",
        )

        self.assertTrue(result)

        self.assertIsNone(
            self.external.resolve(
                "test-system",
                "external-100",
            )
        )

        self.assertTrue(
            self.directory.get(self.user).active
        )

    def test_revoked_external_identity_is_inactive(self):
        self.link_external()

        self.admission.revoke_access(
            self.coordinator,
            self.user,
            reason="Test revocation",
        )

        self.assertIsNone(
            self.external.resolve(
                "test-system",
                "external-100",
            )
        )

    def test_external_linking_disabled_by_default(self):
        service = ExternalIdentityService(
            self.repository
        )

        with self.assertRaises(AuthorizationError):
            service.link(
                self.coordinator,
                self.user,
                "test-system",
                "external-100",
                reason="Test disabled linking",
            )

    def test_ordinary_user_cannot_link_external_identity(self):
        other = self.create_user(1003)

        with self.assertRaises(AuthorizationError):
            self.external.link(
                self.user,
                other,
                "test-system",
                "external-100",
                reason="Test unauthorized linking",
            )

    def test_identity_persists_after_restart(self):
        self.link_external()

        repository = IdentityRepository(
            self.path
        )

        directory = SQLiteIdentityDirectory(
            repository
        )

        external = ExternalIdentityService(
            repository
        )

        self.assertEqual(
            self.directory.get(self.user),
            directory.get(self.user),
        )

        self.assertEqual(
            self.user,
            external.resolve(
                "test-system",
                "external-100",
            ),
        )

    def test_autonomous_work_without_external_identity(self):
        identity = self.directory.get(self.user)

        self.assertTrue(identity.active)

        self.assertIsNone(
            self.external.resolve(
                "test-system",
                "unknown",
            )
        )

        candidates = self.directory.candidates(
            "kazan", "culture", "transport"
        )

        self.assertIn(identity, candidates)


    def test_u1_event_workflow_with_u2_identity(self):
        from event_coordination.events import EventService
        from event_coordination.storage import Store
        from event_coordination.errors import Forbidden

        store = Store(self.path.parent / "events.sqlite")
        events = EventService(store, self.directory)

        proposal = events.propose(
            self.user,
            "test-proposal",
            title="Test event",
            place="Kazan",
            starts_at="2030-06-01T12:00:00+03:00",
            territory_id="kazan",
            direction_id="culture",
            purpose="Test integration",
            needed_help="Organization",
        )

        event_id = proposal.resource_id

        self.assertEqual(
            "submitted",
            events.get(self.user, event_id)["status"],
        )

        events.approve(
            self.coordinator,
            "test-approval",
            event_id,
            owner_id=self.user,
            expected_version=1,
        )

        self.assertEqual(
            "approved",
            events.get(self.user, event_id)["status"],
        )

        self.admission.revoke_access(
            self.coordinator,
            self.user,
            reason="Test access revocation",
        )

        with self.assertRaises(Forbidden):
            events.get(self.user, event_id)


if __name__ == "__main__":
    unittest.main()

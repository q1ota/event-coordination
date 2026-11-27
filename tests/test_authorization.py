
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from event_coordination.identity.authorization import (
    AuthorizationError,
    AuthorizationService,
)
from event_coordination.identity.models import Role
from event_coordination.identity.registration import (
    RegistrationService,
    now,
)
from event_coordination.identity.repository import IdentityRepository


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)

        self.repository = IdentityRepository(
            Path(folder.name) / "identity.sqlite"
        )

        self.registration = RegistrationService(
            self.repository
        )

        self.authorization = AuthorizationService(
            self.repository,
            grantable_roles=frozenset({Role.PARTICIPANT}),
        )

        self.coordinator = self.create_active_user(1001)
        self.participant = self.create_active_user(1002)
        self.admin = self.create_active_user(1003)

        self.seed_role(
            self.coordinator,
            Role.GENERAL_COORDINATOR,
        )

        self.seed_role(
            self.admin,
            Role.TECH_ADMIN,
        )

    def create_active_user(self, telegram_id):
        profile = self.registration.start_registration(
            telegram_id
        )

        with self.repository.transaction(write=True) as db:
            db.execute(
                "UPDATE users SET status = ? WHERE id = ?",
                ("active", profile.user_id),
            )

        return profile.user_id

    def seed_role(self, user_id, role):
        with self.repository.transaction(write=True) as db:
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
                    user_id,
                    role,
                    "",
                    user_id,
                    now(),
                ),
            )

    def grant_participant(self):
        return self.authorization.grant_role(
            self.coordinator,
            self.participant,
            Role.PARTICIPANT,
            reason="Test assignment",
        )

    def test_coordinator_can_grant_role(self):
        self.grant_participant()

        self.assertTrue(
            self.authorization.has_role(
                self.participant,
                Role.PARTICIPANT,
            )
        )

    def test_duplicate_role_is_prevented(self):
        first = self.grant_participant()
        second = self.grant_participant()

        self.assertEqual(first, second)

    def test_self_assignment_is_forbidden(self):
        with self.assertRaises(AuthorizationError):
            self.authorization.grant_role(
                self.participant,
                self.participant,
                Role.GENERAL_COORDINATOR,
                reason="Test",
            )

    def test_participant_cannot_grant_role(self):
        with self.assertRaises(AuthorizationError):
            self.authorization.grant_role(
                self.participant,
                self.admin,
                Role.PARTICIPANT,
                reason="Test",
            )

    def test_technical_admin_has_no_business_permissions(self):
        with self.assertRaises(AuthorizationError):
            self.authorization.grant_role(
                self.admin,
                self.participant,
                Role.PARTICIPANT,
                reason="Test",
            )

    def test_coordinator_can_revoke_role(self):
        self.grant_participant()

        result = self.authorization.revoke_role(
            self.coordinator,
            self.participant,
            Role.PARTICIPANT,
            reason="Test revocation",
        )

        self.assertTrue(result)

        self.assertFalse(
            self.authorization.has_role(
                self.participant,
                Role.PARTICIPANT,
            )
        )

    def test_repeated_revocation_is_safe(self):
        self.grant_participant()

        for expected in (True, False):
            result = self.authorization.revoke_role(
                self.coordinator,
                self.participant,
                Role.PARTICIPANT,
                reason="Test revocation",
            )

            self.assertEqual(expected, result)

    def test_inactive_user_cannot_receive_role(self):
        user = self.registration.start_registration(1004)

        with self.assertRaises(AuthorizationError):
            self.authorization.grant_role(
                self.coordinator,
                user.user_id,
                Role.PARTICIPANT,
                reason="Test",
            )

    def test_revoked_access_disables_roles(self):
        self.grant_participant()

        with self.repository.transaction(write=True) as db:
            db.execute(
                "UPDATE users SET status = ? WHERE id = ?",
                ("revoked", self.participant),
            )

        self.assertFalse(
            self.authorization.has_role(
                self.participant,
                Role.PARTICIPANT,
            )
        )

    def test_unknown_telegram_user_is_rejected(self):
        with self.assertRaises(AuthorizationError):
            self.authorization.resolve_actor(99999)

    def test_telegram_id_resolves_correct_user(self):
        result = self.authorization.resolve_actor(1002)

        self.assertEqual(self.participant, result)

    def test_role_changes_are_audited(self):
        self.grant_participant()

        self.authorization.revoke_role(
            self.coordinator,
            self.participant,
            Role.PARTICIPANT,
            reason="Test revocation",
        )

        with self.repository.transaction() as db:
            rows = db.execute(
                """
                SELECT action
                FROM authorization_audit
                ORDER BY created_at
                """
            ).fetchall()

        self.assertEqual(
            ["grant_role", "revoke_role"],
            [row["action"] for row in rows],
        )

    def test_user_cannot_read_other_users_roles(self):
        with self.assertRaises(AuthorizationError):
            self.authorization.list_roles(
                self.participant,
                self.coordinator,
            )

    def test_default_policy_denies_role_assignment(self):
        service = AuthorizationService(self.repository)

        with self.assertRaises(AuthorizationError):
            service.grant_role(
                self.coordinator,
                self.participant,
                Role.PARTICIPANT,
                reason="Test",
            )

    def test_revoked_coordinator_cannot_grant_role(self):
        with self.repository.transaction(write=True) as db:
            db.execute(
                "UPDATE users SET status = ? WHERE id = ?",
                ("revoked", self.coordinator),
            )

        with self.assertRaises(AuthorizationError):
            self.grant_participant()

    def test_user_can_read_own_roles(self):
        self.grant_participant()

        roles = self.authorization.list_roles(
            self.participant,
            self.participant,
        )

        self.assertEqual(1, len(roles))
        self.assertEqual(Role.PARTICIPANT, roles[0].role)

    def test_revoked_role_can_be_granted_again(self):
        first = self.grant_participant()

        self.authorization.revoke_role(
            self.coordinator,
            self.participant,
            Role.PARTICIPANT,
            reason="Test revocation",
        )

        second = self.grant_participant()

        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()

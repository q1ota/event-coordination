
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
from event_coordination.identity.repository import (
    IdentityRepository,
)
from event_coordination.identity.scopes import ScopePolicy


class ScopeTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)

        self.repository = IdentityRepository(
            Path(folder.name) / "identity.sqlite"
        )

        self.registration = RegistrationService(
            self.repository
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

        self.authorization = AuthorizationService(
            self.repository,
            grantable_roles=frozenset({
                Role.PARTICIPANT,
                Role.TERRITORY_COORDINATOR,
                Role.COUNCIL_REPRESENTATIVE,
                Role.EVENT_RESPONSIBLE,
            }),
            scope_policy=ScopePolicy(
                event_exists=lambda event_id: event_id in {
                    "event-1",
                    "event-2",
                }
            ),
        )

        self.coordinator = self.create_active_user(1001)
        self.user = self.create_active_user(1002)
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

    def grant(self, role, scope_id):
        return self.authorization.grant_role(
            self.coordinator,
            self.user,
            role,
            scope_id=scope_id,
            reason="Test scope assignment",
        )

    def test_territory_access_is_limited(self):
        self.grant(
            Role.TERRITORY_COORDINATOR,
            "kazan",
        )

        self.assertTrue(
            self.authorization.can_manage_territory(
                self.user, "kazan"
            )
        )

        self.assertFalse(
            self.authorization.can_manage_territory(
                self.user, "moscow"
            )
        )

    def test_direction_access_is_limited(self):
        self.grant(
            Role.COUNCIL_REPRESENTATIVE,
            "culture",
        )

        self.assertTrue(
            self.authorization.can_manage_direction(
                self.user, "culture"
            )
        )

        self.assertFalse(
            self.authorization.can_manage_direction(
                self.user, "sport"
            )
        )

    def test_event_access_is_limited(self):
        self.grant(
            Role.EVENT_RESPONSIBLE,
            "event-1",
        )

        self.assertTrue(
            self.authorization.can_manage_event(
                self.user, "event-1"
            )
        )

        self.assertFalse(
            self.authorization.can_manage_event(
                self.user, "event-2"
            )
        )

    def test_multiple_roles_are_supported(self):
        self.grant(
            Role.TERRITORY_COORDINATOR,
            "kazan",
        )

        self.grant(
            Role.COUNCIL_REPRESENTATIVE,
            "culture",
        )

        self.assertTrue(
            self.authorization.can_manage_territory(
                self.user, "kazan"
            )
        )

        self.assertTrue(
            self.authorization.can_manage_direction(
                self.user, "culture"
            )
        )

        self.assertFalse(
            self.authorization.can_manage_territory(
                self.user, "moscow"
            )
        )

        self.assertFalse(
            self.authorization.can_manage_direction(
                self.user, "sport"
            )
        )

    def test_multiple_territories_are_supported(self):
        self.grant(
            Role.TERRITORY_COORDINATOR,
            "kazan",
        )

        self.grant(
            Role.TERRITORY_COORDINATOR,
            "moscow",
        )

        for territory in ("kazan", "moscow"):
            self.assertTrue(
                self.authorization.can_manage_territory(
                    self.user, territory
                )
            )

    def test_unknown_territory_is_rejected(self):
        with self.assertRaises(ValueError):
            self.grant(
                Role.TERRITORY_COORDINATOR,
                "unknown",
            )

    def test_unknown_direction_is_rejected(self):
        with self.assertRaises(ValueError):
            self.grant(
                Role.COUNCIL_REPRESENTATIVE,
                "unknown",
            )

    def test_unknown_event_is_rejected(self):
        with self.assertRaises(ValueError):
            self.grant(
                Role.EVENT_RESPONSIBLE,
                "unknown",
            )

    def test_missing_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            self.grant(
                Role.TERRITORY_COORDINATOR,
                "",
            )

    def test_global_role_cannot_have_scope(self):
        with self.assertRaises(ValueError):
            self.grant(
                Role.PARTICIPANT,
                "kazan",
            )

    def test_revocation_removes_only_selected_scope(self):
        self.grant(
            Role.TERRITORY_COORDINATOR,
            "kazan",
        )

        self.grant(
            Role.TERRITORY_COORDINATOR,
            "moscow",
        )

        self.authorization.revoke_role(
            self.coordinator,
            self.user,
            Role.TERRITORY_COORDINATOR,
            scope_id="kazan",
            reason="Test scope revocation",
        )

        self.assertFalse(
            self.authorization.can_manage_territory(
                self.user, "kazan"
            )
        )

        self.assertTrue(
            self.authorization.can_manage_territory(
                self.user, "moscow"
            )
        )

    def test_technical_admin_has_no_scope_access(self):
        self.assertFalse(
            self.authorization.can_manage_territory(
                self.admin, "kazan"
            )
        )

        self.assertFalse(
            self.authorization.can_manage_direction(
                self.admin, "culture"
            )
        )

        self.assertFalse(
            self.authorization.can_manage_event(
                self.admin, "event-1"
            )
        )

    def test_scoped_user_cannot_assign_roles(self):
        self.grant(
            Role.TERRITORY_COORDINATOR,
            "kazan",
        )

        with self.assertRaises(AuthorizationError):
            self.authorization.grant_role(
                self.user,
                self.admin,
                Role.TERRITORY_COORDINATOR,
                scope_id="moscow",
                reason="Test unauthorized assignment",
            )

    def test_revoked_user_loses_all_scope_access(self):
        self.grant(
            Role.TERRITORY_COORDINATOR,
            "kazan",
        )

        self.grant(
            Role.COUNCIL_REPRESENTATIVE,
            "culture",
        )

        with self.repository.transaction(write=True) as db:
            db.execute(
                "UPDATE users SET status = ? WHERE id = ?",
                ("revoked", self.user),
            )

        self.assertFalse(
            self.authorization.can_manage_territory(
                self.user, "kazan"
            )
        )

        self.assertFalse(
            self.authorization.can_manage_direction(
                self.user, "culture"
            )
        )

    def test_event_integration_is_required(self):
        service = AuthorizationService(
            self.repository,
            grantable_roles=frozenset({
                Role.EVENT_RESPONSIBLE,
            }),
        )

        with self.assertRaises(PermissionError):
            service.grant_role(
                self.coordinator,
                self.user,
                Role.EVENT_RESPONSIBLE,
                scope_id="event-1",
                reason="Test missing integration",
            )


if __name__ == "__main__":
    unittest.main()

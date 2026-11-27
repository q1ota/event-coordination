
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from event_coordination.identity.admission import (
    AdmissionPolicy,
    AdmissionService,
)
from event_coordination.identity.authorization import (
    AuthorizationError,
    AuthorizationService,
)
from event_coordination.identity.models import (
    RegistrationStatus,
    Role,
)
from event_coordination.identity.registration import (
    RegistrationService,
    now,
)
from event_coordination.identity.repository import (
    IdentityRepository,
)


class AdmissionTests(unittest.TestCase):
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
            self.repository
        )

        self.policy = AdmissionPolicy(
            adult_age=18,
            rules_version="v1",
            personal_data_version="v1",
            parental_version="v1",
        )

        self.admission = AdmissionService(
            self.repository,
            policy=self.policy,
            review_enabled=True,
        )

        self.registration.add_territory(
            "kazan", "Tatarstan", "Kazan"
        )

        self.registration.add_direction(
            "culture", "Culture"
        )

        coordinator = self.registration.start_registration(
            1001
        )

        self.coordinator = coordinator.user_id

        with self.repository.transaction(write=True) as db:
            db.execute(
                """
                UPDATE users
                SET status = 'active'
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

    def create_pending_user(self, telegram_id, age):
        user = self.registration.start_registration(
            telegram_id
        )

        self.registration.update_profile(
            user.user_id,
            display_name="Test User",
            territory_id="kazan",
            age=age,
            interests=("culture",),
            help_types=("organization",),
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

        return user.user_id

    def approve(self, user_id):
        self.admission.approve_registration(
            self.coordinator,
            user_id,
            reason="Test approval",
        )

    def status(self, user_id):
        return self.registration.get_profile(
            user_id
        ).status

    def verify_parental(self, telegram_id):
        consent_id = self.admission.submit_parental_consent(
            telegram_id
        )

        self.admission.review_parental_consent(
            self.coordinator,
            consent_id,
            approved=True,
            reason="Test verification",
        )

        return consent_id

    def test_adult_can_be_approved(self):
        user = self.create_pending_user(1002, 20)

        self.approve(user)

        self.assertEqual(
            RegistrationStatus.ACTIVE,
            self.status(user),
        )

        self.assertTrue(
            self.authorization.has_role(
                user, Role.PARTICIPANT
            )
        )

    def test_minor_without_consent_is_denied(self):
        user = self.create_pending_user(1002, 16)

        with self.assertRaises(AuthorizationError):
            self.approve(user)

        self.assertEqual(
            RegistrationStatus.PENDING,
            self.status(user),
        )

    def test_pending_consent_does_not_grant_access(self):
        user = self.create_pending_user(1002, 16)

        self.admission.submit_parental_consent(1002)

        with self.assertRaises(AuthorizationError):
            self.approve(user)

    def test_minor_with_verified_consent_can_be_approved(self):
        user = self.create_pending_user(1002, 16)

        self.verify_parental(1002)
        self.approve(user)

        self.assertEqual(
            RegistrationStatus.ACTIVE,
            self.status(user),
        )

    def test_duplicate_parental_consent_is_prevented(self):
        self.create_pending_user(1002, 16)

        first = self.admission.submit_parental_consent(
            1002
        )

        second = self.admission.submit_parental_consent(
            1002
        )

        self.assertEqual(first, second)

    def test_adult_does_not_need_parental_consent(self):
        self.create_pending_user(1002, 20)

        with self.assertRaises(ValueError):
            self.admission.submit_parental_consent(1002)

    def test_rejected_parental_consent_denies_access(self):
        user = self.create_pending_user(1002, 16)

        consent_id = self.admission.submit_parental_consent(
            1002
        )

        self.admission.review_parental_consent(
            self.coordinator,
            consent_id,
            approved=False,
            reason="Test rejection",
        )

        with self.assertRaises(AuthorizationError):
            self.approve(user)

    def test_parental_consent_revocation_blocks_access(self):
        user = self.create_pending_user(1002, 16)

        consent_id = self.verify_parental(1002)
        self.approve(user)

        self.admission.revoke_parental_consent(
            self.coordinator,
            consent_id,
            reason="Test revocation",
        )

        self.assertEqual(
            RegistrationStatus.REVOKED,
            self.status(user),
        )

        self.assertFalse(
            self.authorization.has_role(
                user, Role.PARTICIPANT
            )
        )

    def test_registration_can_be_rejected(self):
        user = self.create_pending_user(1002, 20)

        self.admission.reject_registration(
            self.coordinator,
            user,
            reason="Test rejection",
        )

        self.assertEqual(
            RegistrationStatus.REJECTED,
            self.status(user),
        )

    def test_access_can_be_revoked(self):
        user = self.create_pending_user(1002, 20)

        self.approve(user)

        self.admission.revoke_access(
            self.coordinator,
            user,
            reason="Test revocation",
        )

        self.assertEqual(
            RegistrationStatus.REVOKED,
            self.status(user),
        )

        with self.assertRaises(AuthorizationError):
            self.authorization.resolve_actor(1002)

    def test_self_approval_is_forbidden(self):
        user = self.create_pending_user(1002, 20)

        with self.assertRaises(AuthorizationError):
            self.admission.approve_registration(
                user,
                user,
                reason="Test self-approval",
            )

    def test_ordinary_user_cannot_approve_registration(self):
        participant = self.create_pending_user(1002, 20)

        self.approve(participant)

        target = self.create_pending_user(1003, 21)

        with self.assertRaises(AuthorizationError):
            self.admission.approve_registration(
                participant,
                target,
                reason="Test unauthorized approval",
            )

    def test_review_is_disabled_by_default(self):
        user = self.create_pending_user(1002, 20)

        service = AdmissionService(
            self.repository,
            policy=self.policy,
        )

        with self.assertRaises(AuthorizationError):
            service.approve_registration(
                self.coordinator,
                user,
                reason="Test disabled review",
            )

    def test_outdated_consent_blocks_approval(self):
        user = self.create_pending_user(1002, 20)

        service = AdmissionService(
            self.repository,
            policy=AdmissionPolicy(
                adult_age=18,
                rules_version="v2",
                personal_data_version="v1",
                parental_version="v1",
            ),
            review_enabled=True,
        )

        with self.assertRaises(AuthorizationError):
            service.approve_registration(
                self.coordinator,
                user,
                reason="Test outdated consent",
            )

    def test_admission_decision_is_audited(self):
        user = self.create_pending_user(1002, 20)

        self.approve(user)

        with self.repository.transaction() as db:
            row = db.execute(
                """
                SELECT action
                FROM authorization_audit
                WHERE target_id = ?
                """,
                (user,),
            ).fetchone()

        self.assertEqual(
            "approve_registration",
            row["action"],
        )

    def test_approval_without_policy_is_denied(self):
        user = self.create_pending_user(1002, 20)

        service = AdmissionService(
            self.repository,
            review_enabled=True,
        )

        with self.assertRaises(AuthorizationError):
            service.approve_registration(
                self.coordinator,
                user,
                reason="Test missing policy",
            )

    def test_repeated_approval_is_rejected(self):
        user = self.create_pending_user(1002, 20)

        self.approve(user)

        with self.assertRaises(AuthorizationError):
            self.approve(user)


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from event_coordination.identity.admission import AdmissionPolicy
from event_coordination.identity.authorization import AuthorizationError
from event_coordination.identity.bootstrap import BootstrapService
from event_coordination.identity.consent import ConsentService
from event_coordination.identity.directory import SQLiteIdentityDirectory
from event_coordination.identity.gateway import IdentityGateway
from event_coordination.identity.models import RegistrationStatus, Role
from event_coordination.identity.policy import AgePolicy
from event_coordination.identity.registration import RegistrationService
from event_coordination.identity.repository import IdentityRepository


class ConsentTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = IdentityRepository(Path(folder.name) / "identity.sqlite", age_policy=AgePolicy())
        self.policy = AdmissionPolicy(16, "v1", "v1", "v1")
        self.registration = RegistrationService(self.repo)
        self.registration.add_territory("kazan", "Tatarstan", "Kazan")
        self.registration.add_direction("culture", "Culture")
        self.gateway = self.make_gateway(self.policy)
        self.directory = SQLiteIdentityDirectory(self.repo)
        self.coordinator = self.pending(1001)
        BootstrapService(self.repo, policy=self.policy).initialize(1001, operator_ref="TEST", reason="Test")
        self.member = self.pending(1002)
        self.gateway.approve_registration(1001, target_id=self.member, reason="Test")

    def make_gateway(self, policy):
        return IdentityGateway(self.repo, policy=policy, review_enabled=True,
            grantable_roles=frozenset({Role.GENERAL_COORDINATOR}))

    def pending(self, sender):
        user = self.gateway.start_registration(sender)
        self.gateway.update_my_profile(sender, display_name="Synthetic user", territory_id="kazan",
            birth_date=date(2000,1,1), interests=("culture",), help_types=("organization",))
        for kind in ("rules", "personal_data"):
            self.gateway.accept_my_consent(sender, consent_type=kind, version="v1")
        self.gateway.submit_my_registration(sender)
        return user.user_id

    def publish_v2(self):
        policy = AdmissionPolicy(16, "v2", "v1", "v1")
        ConsentService(self.repo, policy=policy).publish_policy(operator_ref="TEST")
        return policy

    def test_withdraw_blocks_gateway_and_directory(self):
        self.gateway.withdraw_my_consent(1002, consent_type="personal_data")
        with self.assertRaises(AuthorizationError):
            self.gateway.resolve_actor(1002)
        self.assertFalse(self.directory.get(self.member).active)
        self.assertNotIn(self.member, [u.user_id for u in self.directory.candidates("kazan", None, None)])

    def test_withdraw_and_reaccept_needs_new_approval(self):
        self.gateway.withdraw_my_consent(1002, consent_type="rules")
        self.gateway.accept_my_consent(1002, consent_type="rules", version="v1")
        self.assertEqual(RegistrationStatus.PENDING, self.gateway.get_my_profile(1002).status)
        self.assertFalse(self.directory.get(self.member).active)
        self.gateway.approve_registration(1001, target_id=self.member, reason="Reviewed again")
        self.assertTrue(self.directory.get(self.member).active)

    def test_old_coordinator_role_does_not_return_after_withdrawal(self):
        self.gateway.grant_role(1001, target_id=self.member, role=Role.GENERAL_COORDINATOR, reason="Test")
        self.gateway.withdraw_my_consent(1002, consent_type="rules")
        self.gateway.accept_my_consent(1002, consent_type="rules", version="v1")
        self.gateway.approve_registration(1001, target_id=self.member, reason="Reviewed again")
        self.assertFalse(self.directory.get(self.member).can_coordinate)

    def test_last_coordinator_can_withdraw(self):
        self.gateway.withdraw_my_consent(1001, consent_type="rules")
        self.assertFalse(self.directory.get(self.coordinator).can_coordinate)
        self.gateway.accept_my_consent(1001, consent_type="rules", version="v1")
        self.assertFalse(self.directory.get(self.coordinator).active)

    def test_cannot_withdraw_other_users_consent(self):
        with self.assertRaises(TypeError):
            self.gateway.withdraw_my_consent(1002, consent_type="rules", user_id=self.coordinator)
        self.assertTrue(self.directory.get(self.coordinator).active)

    def test_history_and_idempotence(self):
        self.gateway.accept_my_consent(1002, consent_type="rules", version="v1")
        self.gateway.withdraw_my_consent(1002, consent_type="rules")
        self.assertFalse(self.gateway.withdraw_my_consent(1002, consent_type="rules"))
        self.gateway.accept_my_consent(1002, consent_type="rules", version="v1")
        with self.repo.transaction() as db:
            actions = [r[0] for r in db.execute("SELECT action FROM consent_history WHERE user_id=? AND consent_type='rules' ORDER BY rowid", (self.member,))]
        self.assertEqual(["accepted", "revoked", "accepted"], actions)

    def test_new_version_blocks_existing_access_even_after_restart(self):
        self.publish_v2()
        directory = SQLiteIdentityDirectory(IdentityRepository(self.repo.path, age_policy=AgePolicy()))
        self.assertFalse(directory.get(self.member).active)
        with self.assertRaises(AuthorizationError):
            self.gateway.resolve_actor(1002)

    def test_version_renewal_restores_unrevoked_role_only(self):
        gateway = self.make_gateway(self.publish_v2())
        gateway.accept_my_consent(1001, consent_type="rules", version="v2")
        self.assertTrue(self.directory.get(self.coordinator).can_coordinate)
        self.assertFalse(self.directory.get(self.member).active)

    def test_stale_gateway_cannot_publish_or_accept_old_version(self):
        self.publish_v2()
        with self.assertRaises(ValueError):
            self.make_gateway(self.policy)
        with self.assertRaises(ValueError):
            self.gateway.accept_my_consent(1002, consent_type="rules", version="v1")

    def test_stale_admission_cannot_approve(self):
        target = self.pending(1003)
        gateway = self.make_gateway(self.publish_v2())
        gateway.accept_my_consent(1001, consent_type="rules", version="v2")
        with self.assertRaises((ValueError, AuthorizationError)):
            self.gateway.approve_registration(1001, target_id=target, reason="Old policy")
        self.assertFalse(self.directory.get(target).active)

    def test_admin_revoked_user_cannot_reactivate_by_accepting(self):
        self.gateway.revoke_access(1001, target_id=self.member, reason="Test")
        with self.assertRaises(PermissionError):
            self.gateway.accept_my_consent(1002, consent_type="rules", version="v1")
        self.assertFalse(self.directory.get(self.member).active)

    def test_withdrawal_rollback(self):
        with self.repo.transaction(write=True) as db:
            db.execute("CREATE TRIGGER fail_withdraw BEFORE INSERT ON authorization_audit WHEN NEW.action='withdraw_consent' BEGIN SELECT RAISE(ABORT,'test failure'); END;")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.gateway.withdraw_my_consent(1002, consent_type="rules")
        self.assertTrue(self.directory.get(self.member).active)

import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from event_coordination.identity.admission import AdmissionPolicy
from event_coordination.identity.authorization import AuthorizationError
from event_coordination.identity.bootstrap import BootstrapService
from event_coordination.identity.gateway import IdentityGateway
from event_coordination.identity.models import RegistrationStatus, Role
from event_coordination.identity.policy import AgePolicy
from event_coordination.identity.registration import RegistrationService
from event_coordination.identity.repository import IdentityRepository


class GatewayTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = IdentityRepository(Path(folder.name) / "identity.sqlite",
            age_policy=AgePolicy(minors_enabled=True))
        self.policy = AdmissionPolicy(16, "v1", "v1", "v1")
        self.registration = RegistrationService(self.repo)
        self.registration.add_territory("kazan", "Tatarstan", "Kazan")
        self.registration.add_direction("culture", "Culture")
        self.gateway = IdentityGateway(self.repo, policy=self.policy, review_enabled=True,
            grantable_roles=frozenset({Role.GENERAL_COORDINATOR}))
        self.coordinator = self.pending(1001)
        BootstrapService(self.repo, policy=self.policy).initialize(1001,
            operator_ref="TEST", reason="Synthetic setup")

    def fields(self):
        return dict(display_name="Synthetic user", territory_id="kazan",
                    birth_date=date(2000, 1, 1), interests=("culture",),
                    help_types=("organization",))

    def pending(self, sender, birth_date=None):
        profile = self.gateway.start_registration(sender)
        fields = self.fields()
        if birth_date is not None:
            fields["birth_date"] = birth_date
        self.gateway.update_my_profile(sender, **fields)
        for kind in ("rules", "personal_data"):
            self.gateway.accept_my_consent(sender, consent_type=kind, version="v1")
        self.gateway.submit_my_registration(sender)
        return profile.user_id

    def active(self, sender=1002):
        target = self.pending(sender)
        self.gateway.approve_registration(1001, target_id=target, reason="Test")
        return target

    def test_explicit_policy_required(self):
        with self.assertRaises(ValueError):
            IdentityGateway(IdentityRepository(self.repo.path), policy=self.policy)

    def test_invalid_sender_rejected(self):
        for sender in (True, 0, -1, "1001", None):
            with self.subTest(sender=sender), self.assertRaises(AuthorizationError):
                self.gateway.start_registration(sender)

    def test_unknown_sender_rejected(self):
        with self.assertRaises(AuthorizationError):
            self.gateway.get_my_profile(9999)

    def test_own_profile_only(self):
        first = self.gateway.start_registration(1002)
        second = self.gateway.start_registration(1003)
        self.gateway.update_my_profile(1002, **self.fields())
        self.assertEqual(self.gateway.get_my_profile(1002).user_id, first.user_id)
        self.assertIsNone(self.registration.get_profile(second.user_id).display_name)

    def test_forged_target_profile_rejected(self):
        victim = self.gateway.start_registration(1003)
        self.gateway.start_registration(1002)
        with self.assertRaises(TypeError):
            self.gateway.update_my_profile(1002, user_id=victim.user_id, **self.fields())
        self.assertIsNone(self.registration.get_profile(victim.user_id).display_name)

    def test_forged_actor_rejected(self):
        self.active()
        target = self.pending(1003)
        with self.assertRaises(TypeError):
            self.gateway.approve_registration(1002, actor_id=self.coordinator,
                target_id=target, reason="Forged actor")
        self.assertEqual(RegistrationStatus.PENDING, self.registration.get_profile(target).status)

    def test_pending_user_cannot_resolve_actor(self):
        self.pending(1002)
        with self.assertRaises(AuthorizationError):
            self.gateway.resolve_actor(1002)

    def test_active_actor_resolves_to_internal_id(self):
        target = self.active()
        self.assertEqual(target, self.gateway.resolve_actor(1002))

    def test_participant_cannot_approve_other_registration(self):
        self.active()
        target = self.pending(1003)
        with self.assertRaises(AuthorizationError):
            self.gateway.approve_registration(1002, target_id=target, reason="Test")

    def test_participant_cannot_grant_coordinator_role(self):
        self.active()
        target = self.active(1003)
        with self.assertRaises(AuthorizationError):
            self.gateway.grant_role(1002, target_id=target,
                role=Role.GENERAL_COORDINATOR, reason="Test")

    def test_coordinator_can_grant_and_revoke_role(self):
        target = self.active()
        self.gateway.grant_role(1001, target_id=target, role=Role.GENERAL_COORDINATOR, reason="Test")
        self.assertIn(Role.GENERAL_COORDINATOR, [r.role for r in self.gateway.list_my_roles(1002)])
        self.gateway.revoke_role(1001, target_id=target, role=Role.GENERAL_COORDINATOR, reason="Test")
        self.assertNotIn(Role.GENERAL_COORDINATOR, [r.role for r in self.gateway.list_my_roles(1002)])

    def test_revoked_access_is_not_cached(self):
        target = self.active()
        self.assertEqual(target, self.gateway.resolve_actor(1002))
        self.gateway.revoke_access(1001, target_id=target, reason="Test")
        with self.assertRaises(AuthorizationError):
            self.gateway.resolve_actor(1002)

    def test_outdated_consent_version_rejected(self):
        self.gateway.start_registration(1002)
        with self.assertRaises(ValueError):
            self.gateway.accept_my_consent(1002, consent_type="rules", version="old")

    def test_no_bootstrap_or_dictionary_admin_in_gateway(self):
        for method in ("initialize", "bootstrap", "add_territory", "add_direction"):
            self.assertFalse(hasattr(self.gateway, method))

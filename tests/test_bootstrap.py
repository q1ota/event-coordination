import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from event_coordination.identity.admission import AdmissionPolicy, AdmissionService
from event_coordination.identity.authorization import AuthorizationError, AuthorizationService
from event_coordination.identity.bootstrap import BootstrapService, main
from event_coordination.identity.directory import SQLiteIdentityDirectory
from event_coordination.identity.models import RegistrationStatus, Role
from event_coordination.identity.policy import AgePolicy
from event_coordination.identity.registration import RegistrationService, now
from event_coordination.identity.repository import IdentityRepository


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "identity.sqlite"
        self.repo = IdentityRepository(self.path, age_policy=AgePolicy(minors_enabled=True))
        self.policy = AdmissionPolicy(16, "v1", "v1", "v1")
        self.registration = RegistrationService(self.repo)
        self.registration.add_territory("kazan", "Tatarstan", "Kazan")
        self.registration.add_direction("culture", "Culture")
        self.bootstrap = BootstrapService(self.repo, policy=self.policy)
        self.auth = AuthorizationService(self.repo, grantable_roles=frozenset({Role.GENERAL_COORDINATOR}))
        self.admission = AdmissionService(self.repo, policy=self.policy, review_enabled=True)

    def pending(self, telegram_id=1001, age=20):
        user = self.registration.start_registration(telegram_id)
        self.registration.update_profile(user.user_id, display_name="Synthetic user",
            territory_id="kazan", age=age, interests=("culture",), help_types=("organization",))
        self.registration.accept_consent(user.user_id, "rules", "v1")
        self.registration.accept_consent(user.user_id, "personal_data", "v1")
        self.registration.submit_registration(user.user_id, rules_version="v1", personal_data_version="v1")
        return user.user_id

    def initialize(self, telegram_id=1001):
        return self.bootstrap.initialize(telegram_id, operator_ref="TEST-OPERATOR", reason="Approved test setup")

    def test_initial_coordinator_is_active_and_can_coordinate(self):
        candidate = self.pending()
        self.assertEqual(candidate, self.initialize())
        self.assertEqual(RegistrationStatus.ACTIVE, self.registration.get_profile(candidate).status)
        self.assertTrue(self.auth.has_role(candidate, Role.PARTICIPANT))
        self.assertTrue(self.auth.has_role(candidate, Role.GENERAL_COORDINATOR))
        self.assertTrue(SQLiteIdentityDirectory(self.repo).get(candidate).can_coordinate)

    def test_audit_and_operator_reference(self):
        candidate = self.pending()
        self.initialize()
        with self.repo.transaction() as db:
            audit = db.execute("SELECT * FROM authorization_audit WHERE action = 'bootstrap_coordinator'").fetchone()
            marker = db.execute("SELECT * FROM identity_bootstrap WHERE singleton = 1").fetchone()
            self.assertEqual(candidate, audit["target_id"])
            self.assertIn("TEST-OPERATOR", audit["reason"])
            self.assertEqual("TEST-OPERATOR", marker["operator_ref"])

    def test_repeat_is_blocked_after_restart(self):
        self.pending()
        self.pending(1002)
        self.initialize()
        restarted = BootstrapService(IdentityRepository(self.path, age_policy=AgePolicy()), policy=self.policy)
        with self.assertRaises(AuthorizationError):
            restarted.initialize(1002, operator_ref="TEST-OPERATOR", reason="Repeated setup")

    def test_repeat_is_blocked_even_after_role_loss(self):
        candidate = self.pending()
        self.pending(1002)
        self.initialize()
        # Synthetic failure: simulate loss of all general coordinator roles.
        with self.repo.transaction(write=True) as db:
            db.execute("UPDATE user_roles SET revoked_at = ?, revoked_by = ? WHERE role = ?",
                       (now(), candidate, Role.GENERAL_COORDINATOR))
        with self.assertRaises(AuthorizationError):
            self.initialize(1002)

    def test_legacy_coordinator_history_blocks_bootstrap(self):
        previous = self.pending()
        self.pending(1002)
        with self.repo.transaction(write=True) as db:
            db.execute("UPDATE users SET status = 'active' WHERE id = ?", (previous,))
            db.execute("INSERT INTO user_roles (id, user_id, role, scope_id, granted_by, granted_at) VALUES (?, ?, ?, '', ?, ?)",
                       (str(uuid4()), previous, Role.GENERAL_COORDINATOR, previous, now()))
        with self.assertRaises(AuthorizationError):
            self.initialize(1002)

    def test_underage_candidate_is_rejected(self):
        candidate = self.pending(age=17)
        with self.assertRaises(AuthorizationError):
            self.initialize()
        self.assertEqual(RegistrationStatus.PENDING, self.registration.get_profile(candidate).status)

    def test_eighteen_year_old_candidate_is_allowed(self):
        candidate = self.pending(age=18)
        self.assertEqual(candidate, self.initialize())

    def test_unknown_and_draft_candidates_are_rejected(self):
        with self.assertRaises(ValueError):
            self.initialize()
        self.registration.start_registration(1001)
        with self.assertRaises(AuthorizationError):
            self.initialize()

    def test_missing_current_consent_is_rejected(self):
        candidate = self.pending()
        with self.repo.transaction(write=True) as db:
            db.execute("UPDATE consents SET revoked_at = ? WHERE user_id = ? AND consent_type = 'rules'", (now(), candidate))
        with self.assertRaises(AuthorizationError):
            self.initialize()

    def test_incomplete_pending_profile_is_rejected(self):
        candidate = self.pending()
        with self.repo.transaction(write=True) as db:
            db.execute("DELETE FROM user_help_types WHERE user_id = ?", (candidate,))
        with self.assertRaises(AuthorizationError):
            self.initialize()

    def test_explicit_policy_and_operator_required(self):
        self.pending()
        with self.assertRaises(ValueError):
            BootstrapService(IdentityRepository(self.path), policy=self.policy)
        with self.assertRaises(ValueError):
            self.bootstrap.initialize(1001, operator_ref="", reason="Test")
        with self.assertRaises(ValueError):
            self.bootstrap.initialize(1001, operator_ref="TEST", reason=" ")

    def test_atomic_rollback_on_audit_failure(self):
        candidate = self.pending()
        with patch.object(AuthorizationService, "_audit", side_effect=RuntimeError("Synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.initialize()
        self.assertEqual(RegistrationStatus.PENDING, self.registration.get_profile(candidate).status)
        with self.repo.transaction() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM identity_bootstrap").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM user_roles").fetchone()[0])
        self.assertEqual(candidate, self.initialize())

    def test_concurrent_initialization_has_one_winner(self):
        self.pending()
        self.pending(1002)
        barrier = Barrier(2)
        def attempt(telegram_id):
            barrier.wait(timeout=5)
            try:
                self.initialize(telegram_id)
                return True
            except AuthorizationError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (1001, 1002)))
        self.assertEqual(1, sum(results))
        with self.repo.transaction() as db:
            count = db.execute("SELECT COUNT(*) FROM user_roles WHERE role = ? AND revoked_at IS NULL", (Role.GENERAL_COORDINATOR,)).fetchone()[0]
            self.assertEqual(1, count)

    def test_last_coordinator_cannot_revoke_own_access_or_role(self):
        candidate = self.pending()
        self.initialize()
        with self.assertRaises(AuthorizationError):
            self.auth.revoke_role(candidate, candidate, Role.GENERAL_COORDINATOR, reason="Test")
        with self.assertRaises(AuthorizationError):
            self.admission.revoke_access(candidate, candidate, reason="Test")
        self.assertTrue(self.auth.has_role(candidate, Role.GENERAL_COORDINATOR))

    def test_normal_appointment_and_revocation_after_bootstrap(self):
        first = self.pending()
        second = self.pending(1002)
        self.initialize()
        self.admission.approve_registration(first, second, reason="Test review")
        self.auth.grant_role(first, second, Role.GENERAL_COORDINATOR, reason="Test assignment")
        self.assertTrue(self.auth.revoke_role(second, first, Role.GENERAL_COORDINATOR, reason="Test handover"))
        self.assertFalse(self.auth.has_role(first, Role.GENERAL_COORDINATOR))
        self.assertTrue(self.auth.has_role(second, Role.GENERAL_COORDINATOR))

    def test_cli_requires_confirmation_and_existing_database(self):
        args = ["--db", str(self.path), "--telegram-id", "1001", "--operator-ref", "TEST",
                "--reason", "Test", "--rules-version", "v1", "--personal-data-version", "v1",
                "--parental-version", "v1"]
        candidate = self.pending()
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                main(args)
            missing = args.copy()
            missing[1] = str(self.path.with_name("missing.sqlite"))
            with self.assertRaises(SystemExit):
                main(missing + ["--confirm-authorized"])
        self.assertFalse(self.path.with_name("missing.sqlite").exists())
        with patch("builtins.print"):
            self.assertEqual(0, main(args + ["--confirm-authorized"]))
        self.assertTrue(self.auth.has_role(candidate, Role.GENERAL_COORDINATOR))

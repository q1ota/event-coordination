import sqlite3
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from event_coordination.identity.admission import AdmissionPolicy, AdmissionService
from event_coordination.identity.authorization import AuthorizationError, AuthorizationService
from event_coordination.identity.directory import SQLiteIdentityDirectory
from event_coordination.identity.models import Role
from event_coordination.identity.policy import AgePolicy, age_on
from event_coordination.identity.registration import RegistrationService, now
from event_coordination.identity.repository import IdentityRepository


class AgePolicyTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "identity.sqlite"
        self.repo = IdentityRepository(self.path, age_policy=AgePolicy(minors_enabled=True))
        self.registration = RegistrationService(self.repo)
        self.registration.add_territory("kazan", "Tatarstan", "Kazan")
        self.registration.add_direction("culture", "Culture")
        self.auth = AuthorizationService(self.repo, grantable_roles=frozenset({
            Role.GENERAL_COORDINATOR, Role.TERRITORY_COORDINATOR, Role.COUNCIL_REPRESENTATIVE
        }))
        self.admission_policy = AdmissionPolicy(16, "v1", "v1", "v1")
        self.admission = AdmissionService(self.repo, policy=self.admission_policy, review_enabled=True)
        self.directory = SQLiteIdentityDirectory(self.repo)
        self.coordinator = self.pending(1001, 25)
        with self.repo.transaction(write=True) as db:
            db.execute("UPDATE users SET status='active' WHERE id=?", (self.coordinator,))
            self.seed_role(db, self.coordinator, Role.GENERAL_COORDINATOR)

    def seed_role(self, db, user_id, role):
        db.execute("""INSERT INTO user_roles
            (id, user_id, role, scope_id, granted_by, granted_at)
            VALUES (?, ?, ?, '', ?, ?)""",
            (str(uuid4()), user_id, role, user_id, now()))

    def pending(self, telegram_id, age):
        user = self.registration.start_registration(telegram_id)
        today = date.today()
        # January 1 avoids invalid leap-day dates; the birthday has already occurred.
        birth = date(today.year - age, 1, 1)
        self.registration.update_profile(user.user_id, display_name="Test User",
            territory_id="kazan", birth_date=birth, interests=("culture",),
            help_types=("organization",))
        for kind in ("rules", "personal_data"):
            self.registration.accept_consent(user.user_id, kind, "v1")
        self.registration.submit_registration(user.user_id, rules_version="v1", personal_data_version="v1")
        return user.user_id

    def approve(self, user_id):
        self.admission.approve_registration(self.coordinator, user_id, reason="Test review")

    def test_age_before_and_on_birthday(self):
        birth = date(2010, 6, 15)
        self.assertEqual(15, age_on(birth, date(2026, 6, 14)))
        self.assertEqual(16, age_on(birth, date(2026, 6, 15)))

    def test_future_date_rejected(self):
        with self.assertRaises(ValueError):
            age_on(date(2027, 1, 1), date(2026, 1, 1))

    def test_invalid_policy(self):
        with self.assertRaises(ValueError):
            AgePolicy(minimum_age=17, parental_consent_age=16)

    def test_age_eleven_rejected(self):
        with self.assertRaises(ValueError):
            self.pending(2001, 11)

    def test_ages_twelve_and_fifteen_require_parental_consent(self):
        for age in (12, 15):
            with self.subTest(age=age):
                telegram_id = 2000 + age
                user = self.pending(telegram_id, age)
                with self.assertRaises(AuthorizationError):
                    self.approve(user)
                consent = self.admission.submit_parental_consent(telegram_id)
                self.admission.review_parental_consent(self.coordinator, consent,
                    approved=True, reason="Synthetic verified consent")
                self.approve(user)
                self.assertTrue(self.directory.get(user).active)

    def test_ages_sixteen_and_seventeen_do_not_require_parental_consent(self):
        for age in (16, 17):
            with self.subTest(age=age):
                user = self.pending(2000 + age, age)
                self.approve(user)
                self.assertTrue(self.directory.get(user).active)
                with self.assertRaises(AuthorizationError):
                    self.auth.grant_role(self.coordinator, user, Role.GENERAL_COORDINATOR,
                        reason="Test grant")

    def test_age_eighteen_can_receive_coordinator_role(self):
        user = self.pending(2018, 18)
        self.approve(user)
        self.auth.grant_role(self.coordinator, user, Role.GENERAL_COORDINATOR, reason="Test grant")
        self.assertTrue(self.directory.get(user).can_coordinate)

    def test_unapproved_minor_policy_blocks_access(self):
        user = self.pending(2016, 16)
        self.approve(user)
        repo = IdentityRepository(self.path, age_policy=AgePolicy())
        self.assertFalse(SQLiteIdentityDirectory(repo).get(user).active)
        self.assertNotIn(user, [x.user_id for x in SQLiteIdentityDirectory(repo).candidates("kazan", None, None)])
        with self.assertRaises(AuthorizationError):
            AuthorizationService(repo).resolve_actor(2016)
        service = AdmissionService(repo, policy=self.admission_policy, review_enabled=True)
        other = self.pending(3017, 17)
        with self.assertRaises(AuthorizationError):
            service.approve_registration(self.coordinator, other, reason="Test review")

    def test_existing_underage_coordinator_role_has_no_effect(self):
        user = self.pending(2017, 17)
        self.approve(user)
        with self.repo.transaction(write=True) as db:
            self.seed_role(db, user, Role.GENERAL_COORDINATOR)
        self.assertFalse(self.auth.has_role(user, Role.GENERAL_COORDINATOR))
        self.assertFalse(self.directory.get(user).can_coordinate)

    def test_policy_threshold_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            AdmissionService(self.repo, policy=AdmissionPolicy(18, "v1", "v1", "v1"))

    def test_birth_date_persists(self):
        user = self.pending(2020, 20)
        profile = RegistrationService(IdentityRepository(self.path)).get_profile(user)
        self.assertEqual(20, profile.age)
        self.assertIsNotNone(profile.birth_date)

    def test_old_schema_migrates_without_losing_age(self):
        path = self.path.with_name("legacy.sqlite")
        db = sqlite3.connect(path)
        try:
            schema = Path(__file__).resolve().parents[1] / "event_coordination" / "identity" / "schema.sql"
            db.executescript(schema.read_text(encoding="utf-8").replace("    birth_date TEXT,\n", ""))
            db.execute("INSERT INTO users (id, telegram_user_id, age, status, created_at, updated_at) VALUES ('old', 9, 20, 'draft', 'x', 'x')")
            db.commit()
        finally:
            db.close()
        profile = RegistrationService(IdentityRepository(path)).get_profile("old")
        self.assertEqual(20, profile.age)
        self.assertIsNone(profile.birth_date)

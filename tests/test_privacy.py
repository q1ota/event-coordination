import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from event_coordination.identity.admission import AdmissionPolicy
from event_coordination.identity.authorization import AuthorizationError
from event_coordination.identity.bootstrap import BootstrapService
from event_coordination.identity.directory import SQLiteIdentityDirectory
from event_coordination.identity.gateway import IdentityGateway
from event_coordination.identity.models import Role, RegistrationStatus
from event_coordination.identity.policy import AgePolicy
from event_coordination.identity.privacy import PrivacyPolicy, PrivacyService
from event_coordination.identity.registration import RegistrationService, now
from event_coordination.identity.repository import IdentityRepository


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "identity.sqlite"
        self.repo = IdentityRepository(self.path, age_policy=AgePolicy())
        self.policy = AdmissionPolicy(16, "v1", "v1", "v1")
        registration = RegistrationService(self.repo)
        registration.add_territory("kazan", "Tatarstan", "Kazan")
        registration.add_direction("culture", "Culture")
        self.gateway = IdentityGateway(self.repo, policy=self.policy, review_enabled=True,
                                       grantable_roles=frozenset({Role.GENERAL_COORDINATOR}))
        self.coordinator = self.pending(1001)
        BootstrapService(self.repo, policy=self.policy).initialize(1001, operator_ref="TEST", reason="Test")
        self.member = self.pending(1002)
        self.gateway.approve_registration(1001, target_id=self.member, reason="Test")
        self.privacy = PrivacyService(self.repo, policy=PrivacyPolicy(erasure_enabled=True))
        self.directory = SQLiteIdentityDirectory(self.repo)

    def pending(self, sender):
        from datetime import date
        user = self.gateway.start_registration(sender)
        self.gateway.update_my_profile(sender, display_name="Synthetic user", territory_id="kazan",
            birth_date=date(2000,1,1), interests=("culture",), help_types=("organization",))
        for kind in ("rules", "personal_data"):
            self.gateway.accept_my_consent(sender, consent_type=kind, version="v1")
        self.gateway.submit_my_registration(sender)
        return user.user_id

    def erase(self):
        return self.privacy.erase_user(self.member, operator_ref="TEST")

    def test_disabled_by_default(self):
        service = PrivacyService(self.repo, policy=PrivacyPolicy())
        with self.assertRaises(PermissionError):
            service.erase_user(self.member, operator_ref="TEST")
        self.assertTrue(self.directory.get(self.member).active)

    def test_profile_fields_erased(self):
        self.assertTrue(self.erase())
        with self.repo.transaction() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (self.member,)).fetchone()
            for field in ("display_name", "territory_id", "age", "birth_date"):
                self.assertIsNone(row[field])
            self.assertLess(row["telegram_user_id"], 0)
            self.assertEqual(row["status"], RegistrationStatus.REVOKED)

    def test_access_revoked(self):
        self.erase()
        with self.assertRaises(AuthorizationError):
            self.gateway.resolve_actor(1002)
        self.assertFalse(self.directory.get(self.member).active)
        self.assertNotIn(self.member, [u.user_id for u in self.directory.candidates("kazan", None, None)])

    def test_repeated_erasure_is_idempotent(self):
        self.assertTrue(self.erase())
        self.assertFalse(self.erase())

    def test_re_registration_has_new_uuid_and_no_access(self):
        self.erase()
        new = self.gateway.start_registration(1002)
        self.assertNotEqual(new.user_id, self.member)
        self.assertEqual(new.status, RegistrationStatus.DRAFT)
        self.assertFalse(self.directory.get(new.user_id).active)

    def test_roles_and_structured_audit_remain(self):
        self.erase()
        with self.repo.transaction() as db:
            roles = db.execute("SELECT revoked_at FROM user_roles WHERE user_id=?", (self.member,)).fetchall()
            self.assertTrue(roles)
            self.assertTrue(all(r[0] is not None for r in roles))
            records = db.execute("SELECT reason FROM authorization_audit WHERE target_id=?", (self.member,)).fetchall()
            self.assertTrue(records)
            self.assertTrue(all(r[0] == "redacted" for r in records))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_personal_records_and_external_links_removed(self):
        with self.repo.transaction(write=True) as db:
            db.execute("INSERT INTO external_identities(provider,external_id,user_id,linked_at) VALUES(?,?,?,?)",
                       ("test", "external-123", self.member, now()))
            cid = str(uuid4())
            db.execute("INSERT INTO parental_consents(id,user_id,version,status,submitted_at) VALUES(?,?,?,?,?)",
                       (cid, self.member, "v1", "pending", now()))
            db.execute("INSERT INTO parental_invitations(consent_id,token_hash,expires_at) VALUES(?,?,?)",
                       (cid, "synthetic-hash", now()))
        self.erase()
        with self.repo.transaction() as db:
            for table in ("user_interests", "user_help_types", "consents", "consent_history", "external_identities", "parental_consents"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (self.member,)).fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT 1 FROM parental_invitations WHERE consent_id=?", (cid,)).fetchone())

    def test_draft_purge_only_expired_drafts(self):
        old = self.gateway.start_registration(1003).user_id
        fresh = self.gateway.start_registration(1004).user_id
        pending = self.pending(1005)
        past = (datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
        with self.repo.transaction(write=True) as db:
            db.execute("UPDATE users SET updated_at=? WHERE id IN (?,?,?)", (past, old, pending, self.member))
        self.assertEqual(self.privacy.purge_expired_drafts(operator_ref="TEST"), (old,))
        self.assertEqual(self.gateway.get_my_profile(1004).user_id, fresh)
        self.assertEqual(self.gateway.get_my_profile(1005).status, RegistrationStatus.PENDING)
        self.assertTrue(self.directory.get(self.member).active)

    def test_erasure_persists_after_reopen(self):
        self.erase()
        repo = IdentityRepository(self.path, age_policy=AgePolicy())
        self.assertFalse(SQLiteIdentityDirectory(repo).get(self.member).active)
        with repo.transaction() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM privacy_erasure WHERE user_id=?", (self.member,)).fetchone())

    def test_erasure_rolls_back_on_failure(self):
        with self.repo.transaction(write=True) as db:
            db.execute("""CREATE TRIGGER fail_erasure BEFORE INSERT ON privacy_erasure
                BEGIN SELECT RAISE(ABORT,'test failure'); END""")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.erase()
        self.assertTrue(self.directory.get(self.member).active)
        self.assertEqual(self.gateway.get_my_profile(1002).user_id, self.member)

    def test_other_users_unchanged(self):
        self.erase()
        self.assertTrue(self.directory.get(self.coordinator).can_coordinate)

    def test_invalid_policy(self):
        for value in (0, -1, True, 4000):
            with self.assertRaises(ValueError):
                PrivacyPolicy(draft_retention_days=value)

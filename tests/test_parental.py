import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from event_coordination.identity.admission import AdmissionPolicy
from event_coordination.identity.bootstrap import BootstrapService
from event_coordination.identity.directory import SQLiteIdentityDirectory
from event_coordination.identity.gateway import IdentityGateway
from event_coordination.identity.models import RegistrationStatus
from event_coordination.identity.parental import ParentalPolicyService
from event_coordination.identity.policy import AgePolicy
from event_coordination.identity.registration import RegistrationService
from event_coordination.identity.repository import IdentityRepository


class ParentalConsentTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = IdentityRepository(Path(folder.name)/"identity.sqlite", age_policy=AgePolicy(minors_enabled=True))
        self.policy = AdmissionPolicy(16,"v1","v1","v1")
        reg = RegistrationService(self.repo)
        reg.add_territory("kazan","Tatarstan","Kazan")
        reg.add_direction("culture","Culture")
        self.gateway = IdentityGateway(self.repo, policy=self.policy, review_enabled=True)
        self.directory = SQLiteIdentityDirectory(self.repo)
        self.coordinator = self.pending(1001,25)
        BootstrapService(self.repo, policy=self.policy).initialize(1001,operator_ref="TEST",reason="Synthetic test")
        self.minor = self.pending(1002,14)
        self.request = self.gateway.submit_my_parental_consent(1002)
        self.token = self.gateway.issue_parental_invitation(1002)

    def pending(self,sender,age):
        user = self.gateway.start_registration(sender)
        self.gateway.update_my_profile(sender,display_name="Synthetic user",territory_id="kazan",
            birth_date=date(date.today().year-age,1,1),interests=("culture",),help_types=("organization",))
        for kind in ("rules","personal_data"):
            self.gateway.accept_my_consent(sender,consent_type=kind,version="v1")
        self.gateway.submit_my_registration(sender)
        return user.user_id

    def submit_form(self, token=None, parent=2001, version="v1"):
        return self.gateway.submit_parental_form(parent, token=token or self.token, version=version,
            relationship="mother", confirms_guardianship=True, accepts_participation=True, accepts_data_terms=True)

    def verify(self,request=None):
        token = self.token if request is None else self.gateway.issue_parental_invitation(1002)
        self.submit_form(token)
        self.gateway.review_parental_consent(1001,consent_id=request or self.request,approved=True,
            reason="Checked by operator",verification_method="electronic_form",verification_ref="TEST-001")

    def approve(self):
        self.gateway.approve_registration(1001,target_id=self.minor,reason="Test")

    def test_pending_has_no_access(self):
        self.assertFalse(self.directory.get(self.minor).active)
        with self.assertRaises(PermissionError): self.approve()

    def test_duplicate_request(self):
        self.assertEqual(self.request,self.gateway.submit_my_parental_consent(1002))

    def test_missing_evidence(self):
        with self.assertRaises(ValueError):
            self.gateway.review_parental_consent(1001,consent_id=self.request,approved=True,reason="Test")

    def test_invalid_method(self):
        with self.assertRaises(ValueError):
            self.gateway.review_parental_consent(1001,consent_id=self.request,approved=True,reason="Test",
                verification_method="self_declared",verification_ref="TEST")

    def test_verified_evidence_and_admission(self):
        self.verify(); self.approve()
        self.assertTrue(self.directory.get(self.minor).active)
        with self.repo.transaction() as db:
            row=db.execute("SELECT * FROM parental_verifications WHERE consent_id=?",(self.request,)).fetchone()
            self.assertEqual(row["verified_by"],self.coordinator)
            self.assertEqual(row["reference"],"TEST-001")

    def test_rejection_and_resubmission(self):
        self.gateway.review_parental_consent(1001,consent_id=self.request,approved=False,reason="Not verified")
        new=self.gateway.submit_my_parental_consent(1002)
        self.assertNotEqual(new,self.request)
        self.verify(new); self.approve()
        self.assertTrue(self.directory.get(self.minor).active)

    def test_revocation_blocks_access(self):
        self.verify(); self.approve()
        self.gateway.revoke_parental_consent(1001,consent_id=self.request,reason="Withdrawn")
        self.assertFalse(self.directory.get(self.minor).active)
        with self.assertRaises(PermissionError): self.gateway.resolve_actor(1002)

    def test_unprivileged_reviewer(self):
        with self.assertRaises(PermissionError):
            self.gateway.review_parental_consent(1002,consent_id=self.request,approved=True,reason="Test",
                verification_method="electronic_form",verification_ref="TEST")

    def test_changed_parental_version_blocks_access(self):
        self.verify(); self.approve()
        policy=AdmissionPolicy(16,"v1","v1","v2")
        ParentalPolicyService(self.repo,policy=policy).publish_policy(operator_ref="TEST")
        self.assertFalse(self.directory.get(self.minor).active)
        with self.assertRaises(PermissionError): self.gateway.resolve_actor(1002)
        with self.assertRaises(PermissionError): IdentityGateway(self.repo,policy=self.policy)

    def test_active_minor_can_renew_after_version_change(self):
        self.verify(); self.approve()
        policy=AdmissionPolicy(16,"v1","v1","v2")
        ParentalPolicyService(self.repo,policy=policy).publish_policy(operator_ref="TEST")
        gateway=IdentityGateway(self.repo,policy=policy,review_enabled=True)
        new=gateway.submit_my_parental_consent(1002)
        self.assertNotEqual(new,self.request)
        token=gateway.issue_parental_invitation(1002)
        gateway.submit_parental_form(2001,token=token,version="v2",relationship="mother",
            confirms_guardianship=True,accepts_participation=True,accepts_data_terms=True)
        gateway.review_parental_consent(1001,consent_id=new,approved=True,reason="Test",
            verification_method="electronic_form",verification_ref="TEST-002")
        self.assertTrue(self.directory.get(self.minor).active)

    def test_sixteen_does_not_need_parental_consent(self):
        user=self.pending(1003,16)
        with self.assertRaises(ValueError): self.gateway.submit_my_parental_consent(1003)
        self.gateway.approve_registration(1001,target_id=user,reason="Test")
        self.assertTrue(self.directory.get(user).active)

    def test_stale_review_denied(self):
        ParentalPolicyService(self.repo,policy=AdmissionPolicy(16,"v1","v1","v2")).publish_policy(operator_ref="TEST")
        with self.assertRaises(PermissionError):
            self.gateway.review_parental_consent(1001,consent_id=self.request,approved=True,reason="Test",
                verification_method="electronic_form",verification_ref="TEST")

    def test_child_cannot_submit_form(self):
        with self.assertRaises(PermissionError): self.submit_form(parent=1002)

    def test_token_single_use(self):
        self.submit_form()
        with self.assertRaises(PermissionError): self.submit_form()

    def test_rotated_token_invalid(self):
        new=self.gateway.issue_parental_invitation(1002)
        with self.assertRaises(PermissionError): self.submit_form()
        self.assertEqual(self.request,self.submit_form(new))

    def test_expired_token_invalid(self):
        with self.repo.transaction(write=True) as db:
            db.execute("UPDATE parental_invitations SET expires_at='2000-01-01T00:00:00+00:00'")
        with self.assertRaises(PermissionError): self.submit_form()

    def test_no_form_no_approval(self):
        with self.assertRaises(PermissionError):
            self.gateway.review_parental_consent(1001,consent_id=self.request,approved=True,reason="Test",
                verification_method="electronic_form",verification_ref="TEST")

    def test_parent_cannot_review_own_form(self):
        self.submit_form(parent=1001)
        with self.assertRaises(PermissionError):
            self.gateway.review_parental_consent(1001,consent_id=self.request,approved=True,reason="Test",
                verification_method="electronic_form",verification_ref="TEST")

    def test_all_confirmations_required(self):
        with self.assertRaises(ValueError):
            self.gateway.submit_parental_form(2001,token=self.token,version="v1",relationship="mother",
                confirms_guardianship=True,accepts_participation=False,accepts_data_terms=True)

    def test_coordinator_can_read_form(self):
        self.submit_form()
        details=self.gateway.get_parental_review_details(1001,consent_id=self.request)
        self.assertEqual(details["parent_sender"],2001)
        with self.assertRaises(PermissionError): self.gateway.get_parental_review_details(1002,consent_id=self.request)

    def test_token_not_stored_in_plaintext(self):
        with self.repo.transaction() as db:
            row=db.execute("SELECT token_hash FROM parental_invitations").fetchone()
            self.assertNotEqual(row["token_hash"],self.token)
            self.assertEqual(len(row["token_hash"]),64)

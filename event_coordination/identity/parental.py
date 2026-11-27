"""Parental consent policy and minimal records of human verification."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe

from .policy import effective_age
from .models import Role


def published_parental_version(db):
    row = db.execute("SELECT version FROM identity_parental_policy WHERE singleton=1").fetchone()
    return None if row is None else row["version"]


def require_parental_version(db, version):
    current = published_parental_version(db)
    if current is not None and current != version:
        raise PermissionError("Parental consent policy is outdated")


def parental_access_valid(db, user, age_policy):
    version = published_parental_version(db)
    if version is None:
        return True  # Legacy standalone API; production gateway initializes policy.
    if age_policy is None:
        return False
    age = effective_age(user)
    if not age_policy.allows_admission(age):
        return False
    if not age_policy.requires_parental_consent(age):
        return True
    return db.execute("""SELECT 1 FROM parental_consents p
        JOIN parental_verifications v ON v.consent_id=p.id
        WHERE p.user_id=? AND p.version=? AND p.status='verified'""",
        (user["id"], version)).fetchone() is not None


def validate_evidence(method, reference):
    if method != "electronic_form":
        raise ValueError("An approved verification method is required")
    if not isinstance(reference, str) or not reference.strip() or len(reference.strip()) > 120:
        raise ValueError("A short verification reference is required")
    return method, reference.strip()


class ParentalPolicyService:
    """Trusted deployment operator only, not a public bot command."""
    def __init__(self, repository, *, policy):
        from .admission import AdmissionPolicy
        if not isinstance(policy, AdmissionPolicy) or repository.age_policy is None:
            raise ValueError("Explicit policies required")
        if policy.adult_age != repository.age_policy.parental_consent_age:
            raise ValueError("Age thresholds differ")
        self.repository = repository
        self.policy = policy

    def ensure_policy(self):
        from .registration import now
        with self.repository.transaction(write=True) as db:
            db.execute("""INSERT INTO identity_parental_policy(singleton,version,updated_at,operator_ref)
                VALUES(1,?,?,?) ON CONFLICT(singleton) DO NOTHING""",
                (self.policy.parental_version, now(), "initial configuration"))
            require_parental_version(db, self.policy.parental_version)

    def publish_policy(self, *, operator_ref):
        from .registration import now
        if not isinstance(operator_ref, str) or not operator_ref.strip():
            raise ValueError("Operator reference required")
        with self.repository.transaction(write=True) as db:
            db.execute("""INSERT INTO identity_parental_policy(singleton,version,updated_at,operator_ref)
                VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET
                version=excluded.version,updated_at=excluded.updated_at,operator_ref=excluded.operator_ref""",
                (self.policy.parental_version, now(), operator_ref.strip()))


def _digest(token):
    if not isinstance(token, str) or not 1 <= len(token) <= 64:
        raise ValueError("Invalid invitation")
    return sha256(token.encode("utf-8")).hexdigest()


def _request(db, digest, repository, version):
    require_parental_version(db, version)
    row = db.execute("""SELECT i.*, p.status AS consent_status, p.version,
        u.telegram_user_id AS child_sender, u.status AS child_status, u.birth_date, u.age
        FROM parental_invitations i JOIN parental_consents p ON p.id=i.consent_id
        JOIN users u ON u.id=p.user_id WHERE i.token_hash=?""", (digest,)).fetchone()
    if row is None or row["consent_status"] != "pending" or row["version"] != version:
        raise PermissionError("Invitation is unavailable")
    if row["child_status"] not in ("pending", "active") or not repository.age_policy.allows_registration(effective_age(row)) or not repository.age_policy.requires_parental_consent(effective_age(row)):
        raise PermissionError("Parental consent is not available")
    if row["submitted_at"] is not None or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        raise PermissionError("Invitation has been used or expired")
    return row


class ElectronicParentalConsent:
    """Trusted transport only. A second account is not proof of legal guardianship."""
    def __init__(self, repository, *, policy, admission):
        self.repository = repository
        self.policy = policy
        self.admission = admission

    @staticmethod
    def _sender(sender_id):
        if type(sender_id) is not int or sender_id <= 0:
            raise PermissionError("Invalid trusted sender")

    def issue(self, sender_id):
        self._sender(sender_id)
        consent_id = self.admission.submit_parental_consent(sender_id)
        token = token_urlsafe(32)
        with self.repository.transaction(write=True) as db:
            require_parental_version(db, self.policy.parental_version)
            row = db.execute("""SELECT p.status, p.version, u.telegram_user_id, u.status AS user_status,
                u.birth_date, u.age FROM parental_consents p JOIN users u ON u.id=p.user_id WHERE p.id=?""", (consent_id,)).fetchone()
            if row is None or row["telegram_user_id"] != sender_id or row["status"] != "pending" or row["version"] != self.policy.parental_version or row["user_status"] not in ("pending", "active") or not self.repository.age_policy.allows_registration(effective_age(row)) or not self.repository.age_policy.requires_parental_consent(effective_age(row)):
                raise PermissionError("Invitation cannot be issued")
            previous = db.execute("SELECT submitted_at FROM parental_invitations WHERE consent_id=?", (consent_id,)).fetchone()
            if previous is not None and previous["submitted_at"] is not None:
                raise PermissionError("Form is already awaiting review")
            expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            db.execute("""INSERT INTO parental_invitations(consent_id,token_hash,expires_at)
                VALUES(?,?,?) ON CONFLICT(consent_id) DO UPDATE SET token_hash=excluded.token_hash,expires_at=excluded.expires_at""",
                (consent_id, _digest(token), expires))
        return token

    def form(self, sender_id, token):
        self._sender(sender_id)
        with self.repository.transaction() as db:
            row = _request(db, _digest(token), self.repository, self.policy.parental_version)
            if sender_id == row["child_sender"]:
                raise PermissionError("Child cannot complete parental form")
            return {"version": row["version"], "expires_at": row["expires_at"]}

    def submit(self, sender_id, token, *, version, relationship, confirms_guardianship, accepts_participation, accepts_data_terms):
        self._sender(sender_id)
        if relationship not in ("mother", "father", "legal_guardian"):
            raise ValueError("Invalid relationship")
        if any(value is not True for value in (confirms_guardianship, accepts_participation, accepts_data_terms)):
            raise ValueError("All confirmations are required")
        with self.repository.transaction(write=True) as db:
            row = _request(db, _digest(token), self.repository, self.policy.parental_version)
            if sender_id == row["child_sender"]:
                raise PermissionError("Child cannot complete parental form")
            if version != row["version"]:
                raise ValueError("Outdated form version")
            db.execute("UPDATE parental_invitations SET parent_sender=?,relationship=?,submitted_at=? WHERE consent_id=?",
                (sender_id, relationship, datetime.now(timezone.utc).isoformat(), row["consent_id"]))
            return row["consent_id"]

    def review_details(self, actor_id, consent_id):
        with self.repository.transaction() as db:
            row = db.execute("""SELECT p.user_id,p.version,p.status,i.parent_sender,i.relationship,i.submitted_at
                FROM parental_consents p JOIN parental_invitations i ON i.consent_id=p.id WHERE p.id=?""", (consent_id,)).fetchone()
            if row is None or row["submitted_at"] is None or row["status"] != "pending":
                raise PermissionError("Submitted form not found")
            require_parental_version(db, self.policy.parental_version)
            if row["version"] != self.policy.parental_version:
                raise PermissionError("Outdated form")
            self.admission._require_reviewer(db, actor_id, row["user_id"])
            return dict(row)

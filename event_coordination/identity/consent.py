"""Versioned mandatory consents. Policy publication is a trusted local operation."""
from datetime import datetime, timezone
from uuid import uuid4

from .models import RegistrationStatus


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def published_versions(db):
    row = db.execute("SELECT rules_version, personal_data_version FROM identity_consent_policy WHERE singleton=1").fetchone()
    return None if row is None else {"rules": row["rules_version"], "personal_data": row["personal_data_version"]}


def require_current_versions(db, rules_version, personal_data_version):
    versions = published_versions(db)
    if versions is not None and versions != {"rules": rules_version, "personal_data": personal_data_version}:
        raise ValueError("Admission consent policy is outdated")


def current_consents_valid(db, user_id):
    versions = published_versions(db)
    if versions is None:
        return True  # Legacy standalone callers until a policy is explicitly published.
    return all(db.execute("""SELECT 1 FROM consents WHERE user_id=? AND consent_type=?
        AND version=? AND revoked_at IS NULL""", (user_id, kind, version)).fetchone()
        is not None for kind, version in versions.items())


def _history(db, user_id, kind, version, action, when):
    db.execute("""INSERT INTO consent_history (id,user_id,consent_type,version,action,occurred_at)
        VALUES (?,?,?,?,?,?)""", (str(uuid4()), user_id, kind, version, action, when))


def _snapshot(db, row):
    # Preserve pre-stage-4 timestamps before updating a legacy consent row.
    for action, when in (("accepted", row["accepted_at"]), ("revoked", row["revoked_at"])):
        if when is not None and db.execute("""SELECT 1 FROM consent_history
            WHERE user_id=? AND consent_type=? AND version=? AND action=? AND occurred_at=?""",
            (row["user_id"], row["consent_type"], row["version"], action, when)).fetchone() is None:
            _history(db, row["user_id"], row["consent_type"], row["version"], action, when)


def accept_in_transaction(db, user_id, kind, version):
    if kind not in {"rules", "personal_data"} or not isinstance(version, str) or not version.strip():
        raise ValueError("Invalid consent")
    version = version.strip()
    versions = published_versions(db)
    if versions is not None and versions[kind] != version:
        raise ValueError("Unknown or outdated consent version")
    old = db.execute("SELECT * FROM consents WHERE user_id=? AND consent_type=? AND version=?",
                     (user_id, kind, version)).fetchone()
    if old is not None:
        _snapshot(db, old)
        if old["revoked_at"] is None:
            return False
    when = timestamp()
    db.execute("""INSERT INTO consents (id,user_id,consent_type,version,accepted_at,revoked_at)
        VALUES (?,?,?,?,?,NULL) ON CONFLICT(user_id,consent_type,version)
        DO UPDATE SET accepted_at=excluded.accepted_at, revoked_at=NULL""",
        (str(uuid4()), user_id, kind, version, when))
    _history(db, user_id, kind, version, "accepted", when)
    return True


class ConsentService:
    def __init__(self, repository, *, policy):
        from .admission import AdmissionPolicy
        if not isinstance(policy, AdmissionPolicy) or repository.age_policy is None:
            raise ValueError("Explicit age and admission policies are required")
        if policy.adult_age != repository.age_policy.parental_consent_age:
            raise ValueError("Admission and age policy thresholds do not match")
        self.repository = repository
        self.policy = policy

    def ensure_policy(self):
        """Initialize once, never replace an existing policy from a stale bot."""
        with self.repository.transaction(write=True) as db:
            db.execute("""INSERT INTO identity_consent_policy
                (singleton,rules_version,personal_data_version,updated_at,operator_ref)
                VALUES (1,?,?,?,?) ON CONFLICT(singleton) DO NOTHING""",
                (self.policy.rules_version, self.policy.personal_data_version, timestamp(), "initial configuration"))
            require_current_versions(db, self.policy.rules_version, self.policy.personal_data_version)

    def publish_policy(self, *, operator_ref):
        """Trusted deployment operator only, never expose as a bot method."""
        if not isinstance(operator_ref, str) or not operator_ref.strip():
            raise ValueError("Operator reference is required")
        with self.repository.transaction(write=True) as db:
            db.execute("""INSERT INTO identity_consent_policy
                (singleton,rules_version,personal_data_version,updated_at,operator_ref)
                VALUES (1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET
                rules_version=excluded.rules_version, personal_data_version=excluded.personal_data_version,
                updated_at=excluded.updated_at, operator_ref=excluded.operator_ref""",
                (self.policy.rules_version, self.policy.personal_data_version, timestamp(), operator_ref.strip()))

    def accept(self, user_id, kind, version):
        versions = {"rules": self.policy.rules_version, "personal_data": self.policy.personal_data_version}
        if kind not in versions or version != versions[kind]:
            raise ValueError("Unknown or outdated consent version")
        with self.repository.transaction(write=True) as db:
            require_current_versions(db, self.policy.rules_version, self.policy.personal_data_version)
            row = db.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None or row["status"] not in (RegistrationStatus.DRAFT, RegistrationStatus.PENDING, RegistrationStatus.ACTIVE):
                raise PermissionError("Consent acceptance is not available in this state")
            return accept_in_transaction(db, user_id, kind, version)

    def withdraw(self, user_id, kind):
        """Withdrawal is permitted even for a suspended or last coordinator."""
        if kind not in {"rules", "personal_data"}:
            raise ValueError("Unknown consent type")
        with self.repository.transaction(write=True) as db:
            user = db.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
            if user is None:
                raise ValueError("User not found")
            rows = db.execute("SELECT * FROM consents WHERE user_id=? AND consent_type=? AND revoked_at IS NULL",
                              (user_id, kind)).fetchall()
            when = timestamp()
            for row in rows:
                _snapshot(db, row)
                db.execute("UPDATE consents SET revoked_at=? WHERE id=?", (when, row["id"]))
                _history(db, user_id, kind, row["version"], "revoked", when)
            changed = bool(rows) or user["status"] == RegistrationStatus.ACTIVE
            if user["status"] == RegistrationStatus.ACTIVE:
                db.execute("UPDATE users SET status=?,updated_at=? WHERE id=?",
                           (RegistrationStatus.PENDING, when, user_id))
            if changed:
                db.execute("UPDATE user_roles SET revoked_by=?,revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                           (user_id, when, user_id))
                from .authorization import AuthorizationService
                AuthorizationService._audit(db, user_id, user_id, "withdraw_consent", None, None, kind)
            return changed

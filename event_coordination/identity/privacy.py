"""Local operator API. Retention and erasure must be authorized before use.

A tombstone preserves internal references; this is pseudonymization, not a
claim that every copy of personal data in the whole application is erased.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import RegistrationStatus
from .registration import now


@dataclass(frozen=True)
class PrivacyPolicy:
    draft_retention_days: int = 30
    erasure_enabled: bool = False

    def __post_init__(self):
        if type(self.draft_retention_days) is not int or not 1 <= self.draft_retention_days <= 3650:
            raise ValueError("Invalid draft retention period")
        if type(self.erasure_enabled) is not bool:
            raise ValueError("Invalid erasure setting")


class PrivacyService:
    """Trusted local operator only. Never expose these methods to bot users."""
    def __init__(self, repository, *, policy: PrivacyPolicy):
        if not isinstance(policy, PrivacyPolicy):
            raise ValueError("Explicit privacy policy required")
        self.repository = repository
        self.policy = policy

    def _require_enabled(self):
        if not self.policy.erasure_enabled:
            raise PermissionError("Erasure policy has not been approved")

    @staticmethod
    def _operator(operator_ref):
        if not isinstance(operator_ref, str) or not operator_ref.strip() or len(operator_ref.strip()) > 120:
            raise ValueError("Operator reference required")
        return operator_ref.strip()

    def _erase(self, db, user_id, operator_ref, reason):
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            raise ValueError("User not found")
        if db.execute("SELECT 1 FROM privacy_erasure WHERE user_id=?", (user_id,)).fetchone():
            return False
        when = now()
        # Never reuse the original Telegram binding or expose a tombstone as a user.
        sentinel = db.execute("SELECT MIN(telegram_user_id) FROM users WHERE telegram_user_id<0").fetchone()[0]
        sentinel = -1 if sentinel is None else sentinel - 1
        db.execute("""UPDATE users SET telegram_user_id=?, display_name=NULL,
            territory_id=NULL, age=NULL, birth_date=NULL, status=?, updated_at=? WHERE id=?""",
            (sentinel, RegistrationStatus.REVOKED, when, user_id))
        db.execute("""UPDATE user_roles SET revoked_by=?, revoked_at=?
            WHERE user_id=? AND revoked_at IS NULL""", (user_id, when, user_id))
        for table in ("user_interests", "user_help_types", "external_identities", "consent_history", "consents"):
            db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        # Remove the child's electronic forms and verification references before the parent row.
        for table in ("parental_invitations", "parental_verifications"):
            db.execute(f"DELETE FROM {table} WHERE consent_id IN (SELECT id FROM parental_consents WHERE user_id=?)", (user_id,))
        db.execute("DELETE FROM parental_consents WHERE user_id=?", (user_id,))
        # Preserve structured role history, but remove potentially identifying free text.
        db.execute("UPDATE authorization_audit SET reason='redacted' WHERE actor_id=? OR target_id=?", (user_id, user_id))
        db.execute("UPDATE parental_consents SET reason=NULL WHERE decided_by=?", (user_id,))
        db.execute("UPDATE parental_verifications SET reference='redacted' WHERE verified_by=?", (user_id,))
        db.execute("INSERT INTO privacy_erasure(user_id,erased_at,operator_ref,reason) VALUES(?,?,?,?)",
                   (user_id, when, operator_ref, reason))
        return True

    def erase_user(self, user_id, *, operator_ref):
        """Run only after checking identity, retention obligations, and linked work in U1/U4."""
        self._require_enabled()
        operator_ref = self._operator(operator_ref)
        with self.repository.transaction(write=True) as db:
            return self._erase(db, user_id, operator_ref, "approved_request")

    def purge_expired_drafts(self, *, operator_ref):
        """Clean only expired DRAFT accounts, never pending or active accounts."""
        self._require_enabled()
        operator_ref = self._operator(operator_ref)
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.policy.draft_retention_days)
        with self.repository.transaction(write=True) as db:
            rows = db.execute("SELECT id,updated_at FROM users WHERE status=?", (RegistrationStatus.DRAFT,)).fetchall()
            erased = []
            for row in rows:
                updated = datetime.fromisoformat(row["updated_at"])
                if updated.tzinfo is None:
                    continue
                if updated <= cutoff and self._erase(db, row["id"], operator_ref, "expired_draft"):
                    erased.append(row["id"])
            return tuple(erased)

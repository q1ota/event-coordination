"""One-time setup by an authorized local deployment operator, not a bot API."""
import argparse
from pathlib import Path
from uuid import uuid4

from .admission import AdmissionPolicy, AdmissionService
from .authorization import AuthorizationError, AuthorizationService
from .models import RegistrationStatus, Role
from .policy import AgePolicy, effective_age
from .registration import now
from .repository import IdentityRepository


class BootstrapService:
    """Trusted local use only. The caller must be authorized by the organization."""

    def __init__(self, repository: IdentityRepository, *, policy: AdmissionPolicy):
        if repository.age_policy is None:
            raise ValueError("An explicit age policy is required")
        if not isinstance(policy, AdmissionPolicy):
            raise ValueError("An explicit admission policy is required")
        self.repository = repository
        self.admission = AdmissionService(repository, policy=policy)
        self.policy = policy

    def initialize(self, telegram_user_id: int, *, operator_ref: str, reason: str) -> str:
        if type(telegram_user_id) is not int or telegram_user_id <= 0:
            raise ValueError("Invalid Telegram ID")
        if not isinstance(operator_ref, str) or not operator_ref.strip():
            raise ValueError("Authorized operator reference is required")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Appointment reason is required")
        operator_ref = operator_ref.strip()
        reason = reason.strip()

        with self.repository.transaction(write=True) as db:
            if db.execute("SELECT 1 FROM identity_bootstrap WHERE singleton = 1").fetchone():
                raise AuthorizationError("Initial coordinator has already been appointed")
            # Never turn bootstrap into an administrative recovery or escalation path.
            if db.execute("SELECT 1 FROM user_roles WHERE role = ? LIMIT 1",
                          (Role.GENERAL_COORDINATOR,)).fetchone():
                raise AuthorizationError("Coordinator history exists; bootstrap is forbidden")

            user = db.execute("SELECT * FROM users WHERE telegram_user_id = ?",
                              (telegram_user_id,)).fetchone()
            if user is None:
                raise ValueError("Candidate has not registered")
            if user["status"] != RegistrationStatus.PENDING:
                raise AuthorizationError("Candidate registration must be pending")
            user_id = user["id"]
            age = effective_age(user)
            if not self.repository.age_policy.allows_role(age, Role.GENERAL_COORDINATOR):
                raise AuthorizationError("Candidate does not meet the coordinator age policy")
            if not (user["display_name"] or "").strip() or not user["territory_id"]:
                raise AuthorizationError("Registration is incomplete")
            for table in ("user_interests", "user_help_types"):
                if db.execute(f"SELECT 1 FROM {table} WHERE user_id = ?", (user_id,)).fetchone() is None:
                    raise AuthorizationError("Registration is incomplete")
            self.admission._check_required_consents(db, user_id, self.policy)

            timestamp = now()
            db.execute("UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
                       (RegistrationStatus.ACTIVE, timestamp, user_id))
            for role in (Role.PARTICIPANT, Role.GENERAL_COORDINATOR):
                db.execute("""
                    INSERT INTO user_roles (id, user_id, role, scope_id, granted_by, granted_at)
                    VALUES (?, ?, ?, '', ?, ?)
                    """, (str(uuid4()), user_id, role, user_id, timestamp))
            # The existing schema requires a registered user FK for the actor.
            # For this special action the actor FK is the candidate, not an ordinary
            # self-grant. The real local operator reference is recorded separately.
            db.execute("""
                INSERT INTO identity_bootstrap (singleton, initialized_at, operator_ref)
                VALUES (1, ?, ?)
                """, (timestamp, operator_ref))
            AuthorizationService._audit(
                db, user_id, user_id, "bootstrap_coordinator", Role.GENERAL_COORDINATOR, "",
                f"Local authorized setup [{operator_ref}]: {reason}",
            )
            return user_id


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="One-time local coordinator appointment")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--telegram-id", type=int, required=True)
    parser.add_argument("--operator-ref", required=True, help="Non-secret authorized operator reference")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--rules-version", required=True)
    parser.add_argument("--personal-data-version", required=True)
    parser.add_argument("--parental-version", required=True)
    parser.add_argument("--minimum-age", type=int, default=12)
    parser.add_argument("--parental-consent-age", type=int, default=16)
    parser.add_argument("--coordinator-minimum-age", type=int, default=18)
    parser.add_argument("--confirm-authorized", action="store_true", help="Confirm approved local operation")
    args = parser.parse_args(argv)
    if not args.confirm_authorized:
        parser.error("Organizational authorization must be confirmed")
    if not args.db.is_file():
        parser.error("Existing identity database is required")
    try:
        age_policy = AgePolicy(args.minimum_age, args.parental_consent_age, args.coordinator_minimum_age)
        policy = AdmissionPolicy(args.parental_consent_age, args.rules_version,
                                 args.personal_data_version, args.parental_version)
        repository = IdentityRepository(args.db, age_policy=age_policy)
        BootstrapService(repository, policy=policy).initialize(
            args.telegram_id, operator_ref=args.operator_ref, reason=args.reason)
    except (ValueError, AuthorizationError) as exc:
        parser.error(str(exc))
    print("Initial coordinator appointed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

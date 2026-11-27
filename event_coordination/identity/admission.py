
from dataclasses import dataclass
from uuid import uuid4

from .authorization import AuthorizationError, AuthorizationService
from .models import RegistrationStatus, Role
from .registration import now
from .repository import IdentityRepository
from .policy import effective_age
from .consent import require_current_versions


@dataclass(frozen=True)
class AdmissionPolicy:
    adult_age: int
    rules_version: str
    personal_data_version: str
    parental_version: str

    def __post_init__(self):
        if type(self.adult_age) is not int:
            raise ValueError("Invalid adult age")

        if not 1 <= self.adult_age <= 120:
            raise ValueError("Invalid adult age")

        versions = (
            self.rules_version,
            self.personal_data_version,
            self.parental_version,
        )

        if any(
            not isinstance(value, str)
            or not value.strip()
            for value in versions
        ):
            raise ValueError("Consent versions are required")


class AdmissionService:
    def __init__(
        self,
        repository: IdentityRepository,
        *,
        policy: AdmissionPolicy | None = None,
        review_enabled: bool = False,
    ):
        self.repository = repository
        self.policy = policy
        self.review_enabled = review_enabled
        age_policy = repository.age_policy
        if (policy is not None and age_policy is not None
                and policy.adult_age != age_policy.parental_consent_age):
            raise ValueError("Admission and age policy thresholds do not match")

        self.authorization = AuthorizationService(
            repository
        )

    def _require_policy(self) -> AdmissionPolicy:
        if self.policy is None:
            raise AuthorizationError(
                "Admission policy is not configured"
            )

        return self.policy

    def _require_reviewer(
        self,
        db,
        actor_id: str,
        target_id: str,
    ) -> None:
        if not self.review_enabled:
            raise AuthorizationError(
                "Admission review is disabled"
            )

        if actor_id == target_id:
            raise AuthorizationError(
                "Self-approval is forbidden"
            )

        self.authorization._require_coordinator(
            db, actor_id
        )

    @staticmethod
    def _get_user(db, user_id: str):
        row = db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            raise ValueError("User not found")

        return row

    @staticmethod
    def _validate_reason(reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Reason is required")

    def _check_required_consents(
        self,
        db,
        user_id: str,
        policy: AdmissionPolicy,
    ) -> None:
        require_current_versions(db, policy.rules_version, policy.personal_data_version)
        required = (
            ("rules", policy.rules_version),
            (
                "personal_data",
                policy.personal_data_version,
            ),
        )

        for consent_type, version in required:
            row = db.execute(
                """
                SELECT id FROM consents
                WHERE user_id = ?
                  AND consent_type = ?
                  AND version = ?
                  AND revoked_at IS NULL
                """,
                (user_id, consent_type, version),
            ).fetchone()

            if row is None:
                raise AuthorizationError(
                    "Required consent is missing"
                )

    def submit_parental_consent(
        self,
        telegram_user_id: int,
    ) -> str:
        policy = self._require_policy()

        if type(telegram_user_id) is not int:
            raise ValueError("Invalid Telegram ID")

        with self.repository.transaction(write=True) as db:
            user = db.execute(
                """
                SELECT * FROM users
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchone()

            if user is None:
                raise ValueError("User not found")

            if user["status"] != RegistrationStatus.PENDING:
                raise AuthorizationError(
                    "Registration must be pending"
                )

            age = effective_age(user)
            age_policy = self.repository.age_policy
            if age_policy is not None and not age_policy.allows_registration(age):
                raise AuthorizationError("Minimum registration age is not met")

            if age is None or age >= policy.adult_age:
                raise ValueError(
                    "Parental consent is not required"
                )

            existing = db.execute(
                """
                SELECT id, version
                FROM parental_consents
                WHERE user_id = ?
                  AND status IN ('pending', 'verified')
                """,
                (user["id"],),
            ).fetchone()

            if existing is not None:
                if existing["version"] != policy.parental_version:
                    raise AuthorizationError(
                        "Outdated parental consent must be revoked"
                    )

                return existing["id"]

            consent_id = str(uuid4())

            db.execute(
                """
                INSERT INTO parental_consents (
                    id,
                    user_id,
                    version,
                    status,
                    submitted_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    consent_id,
                    user["id"],
                    policy.parental_version,
                    "pending",
                    now(),
                ),
            )

            return consent_id

    def review_parental_consent(
        self,
        actor_id: str,
        consent_id: str,
        *,
        approved: bool,
        reason: str,
    ) -> None:
        policy = self._require_policy()
        self._validate_reason(reason)

        if type(approved) is not bool:
            raise ValueError("Invalid decision")

        with self.repository.transaction(write=True) as db:
            consent = db.execute(
                """
                SELECT * FROM parental_consents
                WHERE id = ?
                """,
                (consent_id,),
            ).fetchone()

            if consent is None:
                raise ValueError("Consent not found")

            target_id = consent["user_id"]

            self._require_reviewer(
                db, actor_id, target_id
            )

            user = self._get_user(db, target_id)

            if user["status"] != RegistrationStatus.PENDING:
                raise AuthorizationError(
                    "Registration must be pending"
                )

            if consent["status"] != "pending":
                raise ValueError(
                    "Consent has already been reviewed"
                )

            if consent["version"] != policy.parental_version:
                raise AuthorizationError(
                    "Parental consent version is outdated"
                )

            status = "verified" if approved else "rejected"

            db.execute(
                """
                UPDATE parental_consents
                SET status = ?,
                    decided_by = ?,
                    decided_at = ?,
                    reason = ?
                WHERE id = ?
                """,
                (
                    status,
                    actor_id,
                    now(),
                    reason.strip(),
                    consent_id,
                ),
            )

            self.authorization._audit(
                db,
                actor_id,
                target_id,
                "parental_consent_" + status,
                None,
                None,
                reason,
            )

    def approve_registration(
        self,
        actor_id: str,
        target_id: str,
        *,
        reason: str,
    ) -> None:
        policy = self._require_policy()
        self._validate_reason(reason)

        with self.repository.transaction(write=True) as db:
            self._require_reviewer(
                db, actor_id, target_id
            )

            user = self._get_user(db, target_id)

            if user["status"] != RegistrationStatus.PENDING:
                raise AuthorizationError(
                    "Registration must be pending"
                )

            if (
                not user["display_name"]
                or not user["territory_id"]
                or effective_age(user) is None
            ):
                raise AuthorizationError(
                    "Registration is incomplete"
                )

            for table in (
                "user_interests",
                "user_help_types",
            ):
                row = db.execute(
                    f"SELECT 1 FROM {table} WHERE user_id = ?",
                    (target_id,),
                ).fetchone()

                if row is None:
                    raise AuthorizationError(
                        "Registration is incomplete"
                    )

            self._check_required_consents(
                db, target_id, policy
            )

            age = effective_age(user)
            age_policy = self.repository.age_policy
            if age_policy is not None and not age_policy.allows_admission(age):
                raise AuthorizationError("Age policy does not allow admission")

            if age < policy.adult_age:
                consent = db.execute(
                    """
                    SELECT id FROM parental_consents
                    WHERE user_id = ?
                      AND version = ?
                      AND status = 'verified'
                    """,
                    (
                        target_id,
                        policy.parental_version,
                    ),
                ).fetchone()

                if consent is None:
                    raise AuthorizationError(
                        "Verified parental consent is required"
                    )

            db.execute(
                """
                UPDATE users
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    RegistrationStatus.ACTIVE,
                    now(),
                    target_id,
                ),
            )

            db.execute(
                """
                INSERT INTO user_roles (
                    id,
                    user_id,
                    role,
                    scope_id,
                    granted_by,
                    granted_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    target_id,
                    Role.PARTICIPANT,
                    "",
                    actor_id,
                    now(),
                ),
            )

            self.authorization._audit(
                db,
                actor_id,
                target_id,
                "approve_registration",
                Role.PARTICIPANT,
                "",
                reason,
            )

    def reject_registration(
        self,
        actor_id: str,
        target_id: str,
        *,
        reason: str,
    ) -> None:
        self._require_policy()
        self._validate_reason(reason)

        with self.repository.transaction(write=True) as db:
            self._require_reviewer(
                db, actor_id, target_id
            )

            user = self._get_user(db, target_id)

            if user["status"] != RegistrationStatus.PENDING:
                raise AuthorizationError(
                    "Registration must be pending"
                )

            db.execute(
                """
                UPDATE users
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    RegistrationStatus.REJECTED,
                    now(),
                    target_id,
                ),
            )

            self.authorization._audit(
                db,
                actor_id,
                target_id,
                "reject_registration",
                None,
                None,
                reason,
            )

    def _revoke_access(
        self,
        db,
        actor_id: str,
        target_id: str,
        reason: str,
    ) -> None:
        db.execute(
            """
            UPDATE users
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                RegistrationStatus.REVOKED,
                now(),
                target_id,
            ),
        )

        db.execute(
            """
            UPDATE user_roles
            SET revoked_by = ?,
                revoked_at = ?
            WHERE user_id = ?
              AND revoked_at IS NULL
            """,
            (
                actor_id,
                now(),
                target_id,
            ),
        )

        self.authorization._audit(
            db,
            actor_id,
            target_id,
            "revoke_access",
            None,
            None,
            reason,
        )

    def revoke_access(
        self,
        actor_id: str,
        target_id: str,
        *,
        reason: str,
    ) -> None:
        self._require_policy()
        self._validate_reason(reason)

        with self.repository.transaction(write=True) as db:
            self._require_reviewer(
                db, actor_id, target_id
            )

            user = self._get_user(db, target_id)

            if user["status"] != RegistrationStatus.ACTIVE:
                raise AuthorizationError(
                    "User access is not active"
                )

            self._revoke_access(
                db,
                actor_id,
                target_id,
                reason,
            )

    def revoke_parental_consent(
        self,
        actor_id: str,
        consent_id: str,
        *,
        reason: str,
    ) -> None:
        self._require_policy()
        self._validate_reason(reason)

        with self.repository.transaction(write=True) as db:
            consent = db.execute(
                """
                SELECT * FROM parental_consents
                WHERE id = ?
                """,
                (consent_id,),
            ).fetchone()

            if consent is None:
                raise ValueError("Consent not found")

            target_id = consent["user_id"]

            self._require_reviewer(
                db, actor_id, target_id
            )

            if consent["status"] not in {
                "pending",
                "verified",
            }:
                raise ValueError(
                    "Consent cannot be revoked"
                )

            db.execute(
                """
                UPDATE parental_consents
                SET status = 'revoked',
                    decided_by = ?,
                    decided_at = ?,
                    reason = ?
                WHERE id = ?
                """,
                (
                    actor_id,
                    now(),
                    reason.strip(),
                    consent_id,
                ),
            )

            self.authorization._audit(
                db,
                actor_id,
                target_id,
                "parental_consent_revoked",
                None,
                None,
                reason,
            )

            user = self._get_user(db, target_id)

            if user["status"] == RegistrationStatus.ACTIVE:
                self._revoke_access(
                    db,
                    actor_id,
                    target_id,
                    reason,
                )

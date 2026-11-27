
from uuid import uuid4

from .models import Role, RegistrationStatus, RoleAssignment
from .registration import now
from .repository import IdentityRepository
from .scopes import ScopePolicy
from .policy import effective_age


class AuthorizationError(PermissionError):
    pass


class AuthorizationService:
    def __init__(
        self,
        repository: IdentityRepository,
        *,
        grantable_roles: frozenset[Role] = frozenset(),
        scope_policy: ScopePolicy | None = None,
    ):
        self.repository = repository
        self.grantable_roles = grantable_roles
        self.scope_policy = (
            scope_policy
            if scope_policy is not None
            else ScopePolicy()
        )

    def _require_active(self, db, user_id: str) -> None:
        row = db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            raise AuthorizationError("User not found")

        if row["status"] != RegistrationStatus.ACTIVE:
            raise AuthorizationError("User access is inactive")

        policy = self.repository.age_policy
        if policy is not None and not policy.allows_admission(effective_age(row)):
            raise AuthorizationError("Age policy does not allow access")

    def _role_allowed(self, db, user_id, role) -> bool:
        policy = self.repository.age_policy
        if policy is None:
            return True
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return row is not None and policy.allows_role(effective_age(row), Role(role))

    def _has_role(self, db, user_id, role, scope_id="") -> bool:
        if not self._role_allowed(db, user_id, role):
            return False
        row = db.execute(
            """
            SELECT id FROM user_roles
            WHERE user_id = ?
              AND role = ?
              AND scope_id = ?
              AND revoked_at IS NULL
            """,
            (user_id, role, scope_id),
        ).fetchone()

        return row is not None

    def _require_coordinator(self, db, actor_id: str) -> None:
        self._require_active(db, actor_id)

        if not self._has_role(
            db, actor_id, Role.GENERAL_COORDINATOR
        ):
            raise AuthorizationError(
                "Coordinator permission required"
            )

    def resolve_actor(self, telegram_user_id: int) -> str:
        if type(telegram_user_id) is not int:
            raise AuthorizationError("Invalid Telegram ID")

        with self.repository.transaction() as db:
            row = db.execute(
                """
                SELECT id FROM users
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchone()

            if row is None:
                raise AuthorizationError("Unknown user")

            self._require_active(db, row["id"])

            return row["id"]

    def has_role(
        self,
        user_id: str,
        role: Role,
        scope_id: str = "",
    ) -> bool:

        with self.repository.transaction() as db:
            try:
                self._require_active(db, user_id)
            except AuthorizationError:
                return False

            return self._has_role(
                db, user_id, Role(role), scope_id
            )

    @staticmethod
    def _validate_scope(
        role: Role,
        scope_id: str,
    ) -> None:
        ScopePolicy.validate(role, scope_id)

    def grant_role(
        self,
        actor_id: str,
        target_id: str,
        role: Role,
        *,
        scope_id: str = "",
        reason: str,
    ) -> str:

        role = Role(role)

        if actor_id == target_id:
            raise AuthorizationError(
                "Self-assignment is forbidden"
            )

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Reason is required")

        self._validate_scope(role, scope_id)

        with self.repository.transaction(write=True) as db:
            self._require_coordinator(db, actor_id)
            self._require_active(db, target_id)
            if not self._role_allowed(db, target_id, role):
                raise AuthorizationError("Age policy does not allow this role")

            if role not in self.grantable_roles:
                raise AuthorizationError(
                    "Role assignment policy is not enabled"
                )

            self.scope_policy.require_existing_scope(
                db,
                role,
                scope_id,
            )

            existing = db.execute(
                """
                SELECT id FROM user_roles
                WHERE user_id = ?
                  AND role = ?
                  AND scope_id = ?
                  AND revoked_at IS NULL
                """,
                (target_id, role, scope_id),
            ).fetchone()

            if existing is not None:
                return existing["id"]

            assignment_id = str(uuid4())

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
                    assignment_id,
                    target_id,
                    role,
                    scope_id,
                    actor_id,
                    now(),
                ),
            )

            self._audit(
                db,
                actor_id,
                target_id,
                "grant_role",
                role,
                scope_id,
                reason,
            )

            return assignment_id

    def revoke_role(
        self,
        actor_id: str,
        target_id: str,
        role: Role,
        *,
        scope_id: str = "",
        reason: str,
    ) -> bool:

        role = Role(role)

        if actor_id == target_id:
            raise AuthorizationError(
                "Self-revocation is forbidden"
            )

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Reason is required")

        self._validate_scope(role, scope_id)

        with self.repository.transaction(write=True) as db:
            self._require_coordinator(db, actor_id)

            if role not in self.grantable_roles:
                raise AuthorizationError(
                    "Role revocation policy is not enabled"
                )

            result = db.execute(
                """
                UPDATE user_roles
                SET revoked_by = ?,
                    revoked_at = ?
                WHERE user_id = ?
                  AND role = ?
                  AND scope_id = ?
                  AND revoked_at IS NULL
                """,
                (
                    actor_id,
                    now(),
                    target_id,
                    role,
                    scope_id,
                ),
            )

            if result.rowcount == 0:
                return False

            self._audit(
                db,
                actor_id,
                target_id,
                "revoke_role",
                role,
                scope_id,
                reason,
            )

            return True

    def list_roles(
        self,
        actor_id: str,
        target_id: str,
    ) -> tuple[RoleAssignment, ...]:

        with self.repository.transaction() as db:
            self._require_active(db, actor_id)

            if actor_id != target_id:
                self._require_coordinator(db, actor_id)

            rows = db.execute(
                """
                SELECT * FROM user_roles
                WHERE user_id = ?
                  AND revoked_at IS NULL
                ORDER BY role, scope_id
                """,
                (target_id,),
            ).fetchall()

            return tuple(
                RoleAssignment(
                    id=row["id"],
                    user_id=row["user_id"],
                    role=Role(row["role"]),
                    scope_id=row["scope_id"],
                    granted_by=row["granted_by"],
                    granted_at=row["granted_at"],
                    revoked_by=row["revoked_by"],
                    revoked_at=row["revoked_at"],
                )
                for row in rows
                if self._role_allowed(db, target_id, row["role"])
            )

    def _can_manage(
        self,
        user_id: str,
        role: Role,
        scope_id: str,
    ) -> bool:

        with self.repository.transaction() as db:
            try:
                self._require_active(db, user_id)

                self.scope_policy.require_existing_scope(
                    db,
                    role,
                    scope_id,
                )

            except (PermissionError, ValueError):
                return False

            return self._has_role(
                db,
                user_id,
                role,
                scope_id,
            )

    def can_manage_territory(
        self,
        user_id: str,
        territory_id: str,
    ) -> bool:

        return self._can_manage(
            user_id,
            Role.TERRITORY_COORDINATOR,
            territory_id,
        )

    def can_manage_direction(
        self,
        user_id: str,
        direction_id: str,
    ) -> bool:

        return self._can_manage(
            user_id,
            Role.COUNCIL_REPRESENTATIVE,
            direction_id,
        )

    def can_manage_event(
        self,
        user_id: str,
        event_id: str,
    ) -> bool:

        return self._can_manage(
            user_id,
            Role.EVENT_RESPONSIBLE,
            event_id,
        )

    @staticmethod
    def _audit(
        db,
        actor_id,
        target_id,
        action,
        role,
        scope_id,
        reason,
    ):
        db.execute(
            """
            INSERT INTO authorization_audit (
                id,
                actor_id,
                target_id,
                action,
                role,
                scope_id,
                reason,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                actor_id,
                target_id,
                action,
                role,
                scope_id,
                reason.strip(),
                now(),
            ),
        )


from event_coordination.contracts import Identity, IdentityDirectory

from .models import RegistrationStatus, Role
from .repository import IdentityRepository
from .policy import effective_age
from .consent import current_consents_valid


class SQLiteIdentityDirectory(IdentityDirectory):
    def __init__(
        self,
        repository: IdentityRepository,
    ):
        self.repository = repository

    def _build_identity(self, db, row) -> Identity:
        user_id = row["id"]

        active = (
            row["status"] == RegistrationStatus.ACTIVE
        )

        policy = self.repository.age_policy
        age = effective_age(row)
        if policy is not None:
            active = active and policy.allows_admission(age)

        active = active and current_consents_valid(db, user_id)

        coordinator = db.execute(
            """
            SELECT 1 FROM user_roles
            WHERE user_id = ?
              AND role = ?
              AND scope_id = ''
              AND revoked_at IS NULL
            """,
            (
                user_id,
                Role.GENERAL_COORDINATOR,
            ),
        ).fetchone()

        interests = db.execute(
            """
            SELECT direction_id
            FROM user_interests
            WHERE user_id = ?
            ORDER BY direction_id
            """,
            (user_id,),
        ).fetchall()

        help_types = db.execute(
            """
            SELECT help_type
            FROM user_help_types
            WHERE user_id = ?
            ORDER BY help_type
            """,
            (user_id,),
        ).fetchall()

        return Identity(
            user_id=user_id,
            display_name=row["display_name"] or "",
            active=active,
            can_coordinate=(
                active and coordinator is not None
                and (policy is None or policy.allows_role(age, Role.GENERAL_COORDINATOR))
            ),
            territory_id=row["territory_id"] or "",
            interests=tuple(
                item["direction_id"]
                for item in interests
            ),
            help_types=tuple(
                item["help_type"]
                for item in help_types
            ),
        )

    def get(self, user_id: str) -> Identity | None:
        with self.repository.transaction() as db:
            row = db.execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()

            if row is None:
                return None

            return self._build_identity(db, row)

    def candidates(
        self,
        territory_id: str,
        interest: str | None,
        help_type: str | None,
    ) -> tuple[Identity, ...]:

        with self.repository.transaction() as db:
            rows = db.execute(
                """
                SELECT * FROM users
                WHERE status = ?
                  AND territory_id = ?
                ORDER BY id
                """,
                (
                    RegistrationStatus.ACTIVE,
                    territory_id,
                ),
            ).fetchall()

            result = []

            for row in rows:
                identity = self._build_identity(
                    db, row
                )

                if not identity.active:
                    continue

                if (
                    interest is not None
                    and interest not in identity.interests
                ):
                    continue

                if (
                    help_type is not None
                    and help_type not in identity.help_types
                ):
                    continue

                result.append(identity)

            return tuple(result)

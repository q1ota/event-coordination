
from datetime import datetime, timezone
from uuid import uuid4

from .models import RegistrationStatus, UserProfile
from .repository import IdentityRepository


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RegistrationService:
    def __init__(self, repository: IdentityRepository):
        self.repository = repository

    def add_territory(
        self, territory_id: str, region: str, locality: str
    ) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (territory_id, region, locality)
        ):
            raise ValueError("Territory fields are required")

        with self.repository.transaction(write=True) as db:
            db.execute(
                """
                INSERT INTO territories (id, region, locality)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (territory_id.strip(), region.strip(), locality.strip()),
            )

    def add_direction(self, direction_id: str, name: str) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (direction_id, name)
        ):
            raise ValueError("Direction fields are required")

        with self.repository.transaction(write=True) as db:
            db.execute(
                """
                INSERT INTO directions (id, name)
                VALUES (?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (direction_id.strip(), name.strip()),
            )

    def start_registration(self, telegram_user_id: int) -> UserProfile:
        if type(telegram_user_id) is not int or telegram_user_id <= 0:
            raise ValueError("Invalid Telegram user ID")

        with self.repository.transaction(write=True) as db:
            timestamp = now()

            db.execute(
                """
                INSERT INTO users (
                    id,
                    telegram_user_id,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO NOTHING
                """,
                (
                    str(uuid4()),
                    telegram_user_id,
                    RegistrationStatus.DRAFT,
                    timestamp,
                    timestamp,
                ),
            )

            row = db.execute(
                "SELECT id FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()

            return self._get_profile(db, row["id"])

    def update_profile(
        self,
        user_id: str,
        *,
        display_name: str,
        territory_id: str,
        age: int,
        interests: tuple[str, ...],
        help_types: tuple[str, ...],
    ) -> UserProfile:

        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("Name is required")

        if type(age) is not int or not 0 <= age <= 120:
            raise ValueError("Invalid age")

        interests = self._normalize_values(interests)
        help_types = self._normalize_values(help_types)

        if not interests:
            raise ValueError("Select at least one interest")

        if not help_types:
            raise ValueError("Select at least one help type")

        with self.repository.transaction(write=True) as db:
            self._require_editable(db, user_id)

            territory = db.execute(
                "SELECT id FROM territories WHERE id = ?",
                (territory_id,),
            ).fetchone()

            if territory is None:
                raise ValueError("Unknown territory")

            for direction_id in interests:
                direction = db.execute(
                    "SELECT id FROM directions WHERE id = ?",
                    (direction_id,),
                ).fetchone()

                if direction is None:
                    raise ValueError("Unknown direction")

            db.execute(
                """
                UPDATE users
                SET display_name = ?,
                    territory_id = ?,
                    age = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    display_name.strip(),
                    territory_id,
                    age,
                    now(),
                    user_id,
                ),
            )

            db.execute(
                "DELETE FROM user_interests WHERE user_id = ?",
                (user_id,),
            )

            db.executemany(
                """
                INSERT INTO user_interests (user_id, direction_id)
                VALUES (?, ?)
                """,
                [(user_id, value) for value in interests],
            )

            db.execute(
                "DELETE FROM user_help_types WHERE user_id = ?",
                (user_id,),
            )

            db.executemany(
                """
                INSERT INTO user_help_types (user_id, help_type)
                VALUES (?, ?)
                """,
                [(user_id, value) for value in help_types],
            )

            return self._get_profile(db, user_id)

    def accept_consent(
        self,
        user_id: str,
        consent_type: str,
        version: str,
    ) -> None:

        if consent_type not in {"rules", "personal_data"}:
            raise ValueError("Unknown consent type")

        if not isinstance(version, str) or not version.strip():
            raise ValueError("Consent version is required")

        with self.repository.transaction(write=True) as db:
            self._require_editable(db, user_id)

            db.execute(
                """
                INSERT INTO consents (
                    id,
                    user_id,
                    consent_type,
                    version,
                    accepted_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, consent_type, version)
                DO NOTHING
                """,
                (
                    str(uuid4()),
                    user_id,
                    consent_type,
                    version.strip(),
                    now(),
                ),
            )

    def submit_registration(
        self,
        user_id: str,
        *,
        rules_version: str,
        personal_data_version: str,
    ) -> UserProfile:

        with self.repository.transaction(write=True) as db:
            profile = self._get_profile(db, user_id)

            if profile.status not in {
                RegistrationStatus.DRAFT,
                RegistrationStatus.PENDING,
            }:
                raise ValueError("Registration cannot be submitted")

            if (
                not profile.display_name
                or not profile.territory_id
                or profile.age is None
                or not profile.interests
                or not profile.help_types
            ):
                raise ValueError("Registration is incomplete")

            for consent_type, version in (
                ("rules", rules_version),
                ("personal_data", personal_data_version),
            ):
                consent = db.execute(
                    """
                    SELECT id FROM consents
                    WHERE user_id = ?
                      AND consent_type = ?
                      AND version = ?
                      AND revoked_at IS NULL
                    """,
                    (user_id, consent_type, version),
                ).fetchone()

                if consent is None:
                    raise ValueError("Required consent is missing")

            if profile.status == RegistrationStatus.DRAFT:
                db.execute(
                    """
                    UPDATE users
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        RegistrationStatus.PENDING,
                        now(),
                        user_id,
                    ),
                )

            return self._get_profile(db, user_id)

    def get_profile(self, user_id: str) -> UserProfile:
        with self.repository.transaction() as db:
            return self._get_profile(db, user_id)

    def _require_editable(self, db, user_id: str) -> None:
        profile = self._get_profile(db, user_id)

        if profile.status != RegistrationStatus.DRAFT:
            raise ValueError("Profile is not editable")

    @staticmethod
    def _normalize_values(values) -> tuple[str, ...]:
        if not isinstance(values, (tuple, list)):
            raise ValueError("Expected a sequence of values")

        if any(
            not isinstance(value, str) or not value.strip()
            for value in values
        ):
            raise ValueError("Invalid value")

        return tuple(sorted({value.strip() for value in values}))

    @staticmethod
    def _get_profile(db, user_id: str) -> UserProfile:
        row = db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            raise ValueError("User not found")

        interests = db.execute(
            """
            SELECT direction_id FROM user_interests
            WHERE user_id = ?
            ORDER BY direction_id
            """,
            (user_id,),
        ).fetchall()

        help_types = db.execute(
            """
            SELECT help_type FROM user_help_types
            WHERE user_id = ?
            ORDER BY help_type
            """,
            (user_id,),
        ).fetchall()

        return UserProfile(
            user_id=row["id"],
            telegram_user_id=row["telegram_user_id"],
            display_name=row["display_name"],
            territory_id=row["territory_id"],
            age=row["age"],
            interests=tuple(
                item["direction_id"] for item in interests
            ),
            help_types=tuple(
                item["help_type"] for item in help_types
            ),
            status=RegistrationStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

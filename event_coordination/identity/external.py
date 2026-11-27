
from .authorization import (
    AuthorizationError,
    AuthorizationService,
)
from .registration import now
from .repository import IdentityRepository


class ExternalIdentityService:
    def __init__(
        self,
        repository: IdentityRepository,
        *,
        linking_enabled: bool = False,
    ):
        self.repository = repository
        self.linking_enabled = linking_enabled

        self.authorization = AuthorizationService(
            repository
        )

    @staticmethod
    def _validate(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Identifier is required")

        return value.strip()

    def link(
        self,
        actor_id: str,
        user_id: str,
        provider: str,
        external_id: str,
        *,
        reason: str,
    ) -> None:

        if not self.linking_enabled:
            raise AuthorizationError(
                "External identity linking is disabled"
            )

        provider = self._validate(provider)
        external_id = self._validate(external_id)
        reason = self._validate(reason)

        if provider == "telegram":
            raise ValueError(
                "Telegram identity is managed separately"
            )

        with self.repository.transaction(write=True) as db:
            self.authorization._require_coordinator(
                db, actor_id
            )

            self.authorization._require_active(
                db, user_id
            )

            if actor_id == user_id:
                raise AuthorizationError(
                    "Self-linking is forbidden"
                )

            existing = db.execute(
                """
                SELECT user_id
                FROM external_identities
                WHERE provider = ?
                  AND external_id = ?
                """,
                (provider, external_id),
            ).fetchone()

            if existing is not None:
                if existing["user_id"] != user_id:
                    raise ValueError(
                        "External identity is already linked"
                    )

                return

            existing_provider = db.execute(
                """
                SELECT external_id
                FROM external_identities
                WHERE provider = ?
                  AND user_id = ?
                """,
                (provider, user_id),
            ).fetchone()

            if existing_provider is not None:
                raise ValueError(
                    "User already has a linked identity"
                )

            db.execute(
                """
                INSERT INTO external_identities (
                    provider,
                    external_id,
                    user_id,
                    linked_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    provider,
                    external_id,
                    user_id,
                    now(),
                ),
            )

            self.authorization._audit(
                db,
                actor_id,
                user_id,
                "link_external_identity",
                None,
                None,
                reason,
            )

    def resolve(
        self,
        provider: str,
        external_id: str,
    ) -> str | None:

        provider = self._validate(provider)
        external_id = self._validate(external_id)

        with self.repository.transaction() as db:
            row = db.execute(
                """
                SELECT user_id
                FROM external_identities
                WHERE provider = ?
                  AND external_id = ?
                """,
                (provider, external_id),
            ).fetchone()

            if row is None:
                return None

            try:
                self.authorization._require_active(
                    db, row["user_id"]
                )
            except AuthorizationError:
                return None

            return row["user_id"]

    def unlink(
        self,
        actor_id: str,
        user_id: str,
        provider: str,
        *,
        reason: str,
    ) -> bool:

        if not self.linking_enabled:
            raise AuthorizationError(
                "External identity linking is disabled"
            )

        provider = self._validate(provider)
        reason = self._validate(reason)

        if actor_id == user_id:
            raise AuthorizationError(
                "Self-unlinking is forbidden"
            )

        with self.repository.transaction(write=True) as db:
            self.authorization._require_coordinator(
                db, actor_id
            )

            result = db.execute(
                """
                DELETE FROM external_identities
                WHERE user_id = ?
                  AND provider = ?
                """,
                (user_id, provider),
            )

            if result.rowcount == 0:
                return False

            self.authorization._audit(
                db,
                actor_id,
                user_id,
                "unlink_external_identity",
                None,
                None,
                reason,
            )

            return True

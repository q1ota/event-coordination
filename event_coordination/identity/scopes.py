
from collections.abc import Callable

from .models import Role


SCOPED_ROLES = frozenset({
    Role.EVENT_RESPONSIBLE,
    Role.TERRITORY_COORDINATOR,
    Role.COUNCIL_REPRESENTATIVE,
})


class ScopePolicy:
    def __init__(
        self,
        *,
        event_exists: Callable[[str], bool] | None = None,
    ):
        self.event_exists = event_exists

    @staticmethod
    def validate(role: Role, scope_id: str) -> None:
        role = Role(role)

        if not isinstance(scope_id, str):
            raise ValueError("Invalid scope")

        if role in SCOPED_ROLES:
            if not scope_id or scope_id != scope_id.strip():
                raise ValueError("Valid scope is required")
        elif scope_id:
            raise ValueError(
                "Global role cannot have a scope"
            )

    def require_existing_scope(
        self,
        db,
        role: Role,
        scope_id: str,
    ) -> None:
        role = Role(role)

        self.validate(role, scope_id)

        if role == Role.TERRITORY_COORDINATOR:
            row = db.execute(
                "SELECT id FROM territories WHERE id = ?",
                (scope_id,),
            ).fetchone()

            if row is None:
                raise ValueError("Unknown territory")

        elif role == Role.COUNCIL_REPRESENTATIVE:
            row = db.execute(
                "SELECT id FROM directions WHERE id = ?",
                (scope_id,),
            ).fetchone()

            if row is None:
                raise ValueError("Unknown direction")

        elif role == Role.EVENT_RESPONSIBLE:
            if self.event_exists is None:
                raise PermissionError(
                    "Event integration is not configured"
                )

            if not self.event_exists(scope_id):
                raise ValueError("Unknown event")

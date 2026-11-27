"""Read-only authorization boundary for trusted U3/U4 integration.

A positive decision is not a Telegram invitation or a membership confirmation.
The caller must recheck before external actions and reconcile existing members.
"""
from enum import StrEnum
from collections.abc import Callable

from .authorization import AuthorizationService, AuthorizationError
from .models import Role
from .consent import published_versions
from .parental import published_parental_version


class ChatKind(StrEnum):
    COORDINATION = "coordination"
    TERRITORY = "territory"
    COUNCIL = "council"
    EVENT = "event"


class U1EventMembership:
    """Trusted read adapter for the existing U1 Store; no changes to U1."""
    def __init__(self, store):
        self.store = store

    def __call__(self, user_id: str, event_id: str) -> bool:
        with self.store.transaction() as db:
            row = db.execute("""SELECT e.owner_id, e.status, p.status AS membership
                FROM events e LEFT JOIN participation p
                  ON p.event_id=e.id AND p.user_id=? WHERE e.id=?""",
                (user_id, event_id)).fetchone()
            return bool(row and row["status"] in {"approved", "preparing", "ongoing"}
                        and (row["owner_id"] == user_id or row["membership"] == "active"))


class ChatAccessService:
    def __init__(self, repository, *,
                 event_member: Callable[[str, str], bool] | None = None,
                 confirmed_access: Callable[[str, ChatKind, str], bool] | None = None):
        if repository.age_policy is None:
            raise ValueError("Explicit age policy required")
        self.repository = repository
        self.authorization = AuthorizationService(repository)
        self.event_member = event_member
        self.confirmed_access = confirmed_access

    def _local(self, user_id, kind, scope_id):
        with self.repository.transaction() as db:
            if not published_versions(db) or published_parental_version(db) is None:
                return False, False
            try:
                self.authorization._require_active(db, user_id)
            except AuthorizationError:
                return False, False
            if kind == ChatKind.COORDINATION:
                return True, self.authorization._has_role(db, user_id, Role.GENERAL_COORDINATOR)
            if kind in (ChatKind.TERRITORY, ChatKind.COUNCIL):
                table, role = (("territories", Role.TERRITORY_COORDINATOR)
                               if kind == ChatKind.TERRITORY else ("directions", Role.COUNCIL_REPRESENTATIVE))
                if db.execute(f"SELECT 1 FROM {table} WHERE id=?", (scope_id,)).fetchone() is None:
                    return False, False
                return True, self.authorization._has_role(db, user_id, role, scope_id)
            return True, False

    def can_access(self, user_id: str, kind: ChatKind, scope_id: str = "") -> bool:
        """Internal UUID only, from a trusted process. Never accept caller roles."""
        try:
            kind = ChatKind(kind)
        except (ValueError, TypeError):
            return False
        if not isinstance(user_id, str) or not user_id or not isinstance(scope_id, str):
            return False
        if kind == ChatKind.COORDINATION:
            if scope_id:
                return False
        elif not scope_id or scope_id != scope_id.strip():
            return False
        allowed, local = self._local(user_id, kind, scope_id)
        if not allowed:
            return False
        if local:
            return True
        if kind == ChatKind.EVENT:
            # U1 is authoritative: a scoped U2 role or invitation is insufficient.
            granted = self.event_member is not None and self.event_member(user_id, scope_id) is True
        else:
            # Optional explicit grants from a trusted policy source, not Telegram membership itself.
            granted = self.confirmed_access is not None and self.confirmed_access(user_id, kind, scope_id) is True
        # Do not keep the IAM transaction open while calling the external reader.
        return bool(granted and self._local(user_id, kind, scope_id)[0])

    def can_access_sender(self, sender_id: int, kind: ChatKind, scope_id: str = "") -> bool:
        """U3 supplies the authenticated Telegram sender, not form input."""
        if type(sender_id) is not int or sender_id <= 0:
            return False
        try:
            user_id = self.authorization.resolve_actor(sender_id)
        except AuthorizationError:
            return False
        return self.can_access(user_id, kind, scope_id)

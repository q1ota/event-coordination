from dataclasses import asdict
import hashlib
import json

from .contracts import Identity, IdentityDirectory, Receipt
from .errors import Conflict, Forbidden, IdempotencyConflict, NotFound
from .storage import Store, encode, event_row, task_row
from .validation import text


WORKING_STATES = {"approved", "preparing", "ongoing"}
OPEN_TASK_STATES = {"assigned", "in_progress", "needs_help"}


class Context:
    def __init__(self, db, directory: IdentityDirectory, actor_id: str):
        self.db = db
        self.directory = directory
        self.actor = self.active_identity(actor_id)

    def active_identity(self, user_id: str) -> Identity:
        identity = self.directory.get(user_id)
        if identity is None or identity.user_id != user_id or not identity.active:
            raise Forbidden("User is not admitted")
        return identity

    def coordinate(self):
        if not self.actor.can_coordinate:
            raise Forbidden("Coordination permission required")

    def is_manager(self, event: dict) -> bool:
        return self.actor.can_coordinate or event["owner_id"] == self.actor.user_id

    def membership(self, event_id: str, user_id: str):
        return self.db.execute(
            "SELECT * FROM participation WHERE event_id=? AND user_id=?",
            (event_id, user_id),
        ).fetchone()

    def require_member(self, event_id: str, user_id: str):
        self.active_identity(user_id)
        member = self.membership(event_id, user_id)
        if member is None or member["status"] != "active":
            raise Conflict("An active event participant is required")

    def visible_event(self, event_id: str) -> dict:
        event = event_row(self.db, event_id)
        member = self.membership(event_id, self.actor.user_id)
        if (
            self.is_manager(event)
            or event["author_id"] == self.actor.user_id
            or (member and member["status"] in {"active", "requested", "invited"})
        ):
            return event
        raise NotFound("Event not found")

    def managed_event(self, event_id: str) -> dict:
        event = self.visible_event(event_id)
        if not self.is_manager(event):
            raise Forbidden("Event manager required")
        return event

    def visible_task(self, task_id: str) -> tuple[dict, dict]:
        task = task_row(self.db, task_id)
        event = self.visible_event(task["event_id"])
        if not self.is_manager(event):
            member = self.membership(event["id"], self.actor.user_id)
            if (
                task["assignee_id"] != self.actor.user_id
                or member is None or member["status"] != "active"
            ):
                raise NotFound("Task not found")
        return task, event

    def managed_task(self, task_id: str) -> tuple[dict, dict]:
        task, event = self.visible_task(task_id)
        if not self.is_manager(event):
            raise Forbidden("Event manager required")
        return task, event


def require_working(event: dict):
    if event["status"] not in WORKING_STATES:
        raise Conflict("Event is not open for work")


class Service:
    def __init__(self, store: Store, directory: IdentityDirectory):
        self.store = store
        self.directory = directory

    def _mutate(self, actor_id, request_id, operation, payload, guard, change) -> Receipt:
        actor_id = text(actor_id, "actor_id")
        request_id = text(request_id, "request_id")
        digest = hashlib.sha256(encode(payload).encode("utf-8")).hexdigest()
        with self.store.transaction(write=True) as db:
            ctx = Context(db, self.directory, actor_id)
            subject = guard(ctx)
            old = db.execute(
                "SELECT * FROM commands WHERE actor_id=? AND request_id=?",
                (actor_id, request_id),
            ).fetchone()
            if old:
                if old["operation"] != operation or old["fingerprint"] != digest:
                    raise IdempotencyConflict("Request ID already used for different parameters")
                return Receipt(**json.loads(old["receipt"]))
            receipt = change(ctx, subject)
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?,?)",
                (actor_id, request_id, operation, digest, encode(asdict(receipt))),
            )
            return receipt

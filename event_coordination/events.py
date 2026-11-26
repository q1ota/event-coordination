from uuid import uuid4

from .contracts import CompletionReader, Receipt
from .errors import Conflict
from .service import Context, Service
from .storage import bump_event, emit, event_row, record
from .validation import now, pagination, text, timestamp, version


class EventService(Service):
    def __init__(self, store, directory, completion: CompletionReader | None = None):
        super().__init__(store, directory)
        self.completion = completion

    def propose(
        self, actor_id: str, request_id: str, *, title: str, place: str,
        starts_at: str, territory_id: str, direction_id: str,
        purpose: str, needed_help: str,
    ) -> Receipt:
        payload = {
            "title": text(title, "title"),
            "place": text(place, "place", 500),
            "starts_at": timestamp(starts_at, "starts_at"),
            "territory_id": text(territory_id, "territory_id"),
            "direction_id": text(direction_id, "direction_id"),
            "purpose": text(purpose, "purpose", 4000),
            "needed_help": text(needed_help, "needed_help", 4000),
        }

        def change(ctx, _):
            event_id = str(uuid4())
            created = now()
            ctx.db.execute(
                """INSERT INTO events(
                    id,author_id,title,place,starts_at,territory_id,direction_id,
                    purpose,needed_help,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'submitted',?,?)""",
                (event_id, ctx.actor.user_id, *payload.values(), created, created),
            )
            record(ctx.db, actor_id, "event.submitted", event_id, {})
            emit(ctx.db, "event.submitted", event_id, {"audience": "coordination"})
            return Receipt(event_id, 1)

        return self._mutate(actor_id, request_id, "event.propose", payload, lambda ctx: None, change)

    def get(self, actor_id: str, event_id: str) -> dict:
        with self.store.transaction() as db:
            return Context(db, self.directory, actor_id).visible_event(event_id)

    def list_mine(self, actor_id: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
        pagination(limit, offset)
        with self.store.transaction() as db:
            ctx = Context(db, self.directory, actor_id)
            rows = db.execute(
                """SELECT e.* FROM events e WHERE e.author_id=? OR e.owner_id=?
                OR EXISTS (SELECT 1 FROM participation p WHERE p.event_id=e.id
                    AND p.user_id=? AND p.status IN ('active','invited','requested'))
                ORDER BY e.created_at,e.id LIMIT ? OFFSET ?""",
                (ctx.actor.user_id, ctx.actor.user_id, ctx.actor.user_id, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

    def review_queue(self, actor_id: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
        pagination(limit, offset)
        with self.store.transaction() as db:
            Context(db, self.directory, actor_id).coordinate()
            return [dict(row) for row in db.execute(
                "SELECT * FROM events WHERE status='submitted' ORDER BY created_at,id LIMIT ? OFFSET ?",
                (limit, offset),
            )]

    def approve(
        self, actor_id: str, request_id: str, event_id: str, *,
        owner_id: str, expected_version: int,
    ) -> Receipt:
        payload = {
            "event_id": text(event_id, "event_id"),
            "owner_id": text(owner_id, "owner_id"),
            "expected_version": version(expected_version),
        }

        def guard(ctx):
            ctx.coordinate()
            return event_row(ctx.db, event_id)

        def change(ctx, event):
            if event["status"] != "submitted":
                raise Conflict("Only submitted proposals can be approved")
            ctx.active_identity(owner_id)
            result = bump_event(ctx.db, event, expected_version, status="approved", owner_id=owner_id)
            ctx.db.execute(
                "INSERT INTO participation(event_id,user_id,status) VALUES (?,?,'active')",
                (event_id, owner_id),
            )
            record(ctx.db, actor_id, "event.approved", event_id, {"owner_id": owner_id})
            emit(ctx.db, "event.approved", event_id, {"audience": "event_setup", "owner_id": owner_id})
            return Receipt(event_id, result)

        return self._mutate(actor_id, request_id, "event.approve", payload, guard, change)

    def reject(
        self, actor_id: str, request_id: str, event_id: str, *,
        reason: str, expected_version: int,
    ) -> Receipt:
        payload = {
            "event_id": text(event_id, "event_id"),
            "reason": text(reason, "reason", 4000),
            "expected_version": version(expected_version),
        }

        def guard(ctx):
            ctx.coordinate()
            return event_row(ctx.db, event_id)

        def change(ctx, event):
            if event["status"] != "submitted":
                raise Conflict("Only submitted proposals can be rejected")
            result = bump_event(
                ctx.db, event, expected_version, status="rejected", reason=payload["reason"]
            )
            record(ctx.db, actor_id, "event.rejected", event_id, {"reason": payload["reason"]})
            emit(ctx.db, "event.rejected", event_id, {
                "audience": "user", "user_id": event["author_id"], "reason": payload["reason"],
            })
            return Receipt(event_id, result)

        return self._mutate(actor_id, request_id, "event.reject", payload, guard, change)

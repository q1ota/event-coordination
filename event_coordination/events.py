from uuid import uuid4

from .contracts import CompletionReader, Receipt
from .errors import Conflict, ValidationError
from .service import Context, Service, require_working
from .storage import bump_event, bump_task, emit, event_row, record
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

    def advance(
        self, actor_id: str, request_id: str, event_id: str, *,
        status: str, expected_version: int,
    ) -> Receipt:
        previous = {"preparing": "approved", "ongoing": "preparing"}
        if status not in previous:
            raise ValidationError("Use complete/archive/cancel for terminal transitions")
        payload = {
            "event_id": text(event_id, "event_id"), "status": status,
            "expected_version": version(expected_version),
        }

        def change(ctx, event):
            if event["status"] != previous[status]:
                raise Conflict(f"Transition to {status} requires {previous[status]}")
            result = bump_event(ctx.db, event, expected_version, status=status)
            record(ctx.db, ctx.actor.user_id, "event.advanced", event_id, {"status": status})
            emit(ctx.db, "event.advanced", event_id, {"audience": "event_sync", "status": status})
            return Receipt(event_id, result)

        return self._mutate(
            actor_id, request_id, "event.advance", payload,
            lambda ctx: ctx.managed_event(event_id), change,
        )

    def complete(
        self, actor_id: str, request_id: str, event_id: str, *,
        expected_version: int, cancel_remaining_reason: str | None = None,
    ) -> Receipt:
        if cancel_remaining_reason is not None:
            cancel_remaining_reason = text(cancel_remaining_reason, "cancel_remaining_reason", 4000)
        payload = {
            "event_id": text(event_id, "event_id"),
            "expected_version": version(expected_version),
            "cancel_remaining_reason": cancel_remaining_reason,
        }

        def change(ctx, event):
            if event["status"] != "ongoing":
                raise Conflict("Only an ongoing event can be completed")
            if self.completion is None:
                raise Conflict("Report integration is not connected")
            evidence = self.completion.get(event_id)
            if not evidence.report_id or not evidence.report_id.strip():
                raise Conflict("A saved report is required")
            tasks = self._open_tasks(ctx, event_id)
            if tasks and cancel_remaining_reason is None:
                raise Conflict("Resolve outstanding tasks or explicitly cancel them with a reason")
            result = bump_event(ctx.db, event, expected_version, status="completed")
            self._cancel_tasks(ctx, event_id, tasks, cancel_remaining_reason)
            record(ctx.db, ctx.actor.user_id, "event.completed", event_id, {"report_id": evidence.report_id})
            emit(ctx.db, "event.completed", event_id, {
                "audience": "report_delivery", "report_id": evidence.report_id,
            })
            return Receipt(event_id, result)

        return self._mutate(
            actor_id, request_id, "event.complete", payload,
            lambda ctx: ctx.managed_event(event_id), change,
        )

    def archive(
        self, actor_id: str, request_id: str, event_id: str, *, expected_version: int,
    ) -> Receipt:
        payload = {"event_id": text(event_id, "event_id"), "expected_version": version(expected_version)}

        def change(ctx, event):
            if event["status"] != "completed":
                raise Conflict("Only a completed event can be archived")
            if self.completion is None:
                raise Conflict("Report/chat integration is not connected")
            evidence = self.completion.get(event_id)
            if not (
                evidence.report_id and evidence.report_id.strip()
                and evidence.report_delivered and evidence.chat_archived
            ):
                raise Conflict("Confirmed report delivery and chat archive are required")
            result = bump_event(ctx.db, event, expected_version, status="archived")
            record(ctx.db, ctx.actor.user_id, "event.archived", event_id, {})
            emit(ctx.db, "event.archived", event_id, {"audience": "event_sync"})
            return Receipt(event_id, result)

        return self._mutate(
            actor_id, request_id, "event.archive", payload,
            lambda ctx: ctx.managed_event(event_id), change,
        )

    def cancel(
        self, actor_id: str, request_id: str, event_id: str, *,
        reason: str, expected_version: int,
    ) -> Receipt:
        payload = {
            "event_id": text(event_id, "event_id"), "reason": text(reason, "reason", 4000),
            "expected_version": version(expected_version),
        }

        def change(ctx, event):
            if event["status"] not in {"submitted", "approved", "preparing", "ongoing"}:
                raise Conflict("Event is already closed")
            result = bump_event(
                ctx.db, event, expected_version, status="cancelled", reason=payload["reason"]
            )
            self._cancel_tasks(ctx, event_id, self._open_tasks(ctx, event_id), payload["reason"])
            record(ctx.db, ctx.actor.user_id, "event.cancelled", event_id, {"reason": payload["reason"]})
            emit(ctx.db, "event.cancelled", event_id, {
                "audience": "event_shutdown", "reason": payload["reason"],
            })
            return Receipt(event_id, result)

        return self._mutate(
            actor_id, request_id, "event.cancel", payload,
            lambda ctx: ctx.managed_event(event_id), change,
        )

    def escalate_problem(
        self, actor_id: str, request_id: str, event_id: str, *,
        description: str, expected_version: int,
    ) -> Receipt:
        payload = {
            "event_id": text(event_id, "event_id"),
            "description": text(description, "description", 4000),
            "expected_version": version(expected_version),
        }

        def change(ctx, event):
            require_working(event)
            result = bump_event(ctx.db, event, expected_version)
            record(ctx.db, ctx.actor.user_id, "event.problem_reported", event_id, {
                "description": payload["description"],
            })
            emit(ctx.db, "event.problem_reported", event_id, {
                "audience": "coordination", "description": payload["description"],
            })
            return Receipt(event_id, result)

        return self._mutate(
            actor_id, request_id, "event.escalate_problem", payload,
            lambda ctx: ctx.managed_event(event_id), change,
        )

    @staticmethod
    def _open_tasks(ctx, event_id):
        return [dict(row) for row in ctx.db.execute(
            "SELECT * FROM tasks WHERE event_id=? AND status IN ('assigned','in_progress','needs_help')",
            (event_id,),
        )]

    @staticmethod
    def _cancel_tasks(ctx, event_id, tasks, reason):
        for task in tasks:
            bump_task(ctx.db, task, task["version"], status="cancelled", note=reason)
            record(ctx.db, ctx.actor.user_id, "task.cancelled", event_id, {
                "task_id": task["id"], "reason": reason,
            })
            emit(ctx.db, "task.changed", event_id, {
                "audience": "task_sync", "task_id": task["id"], "status": "cancelled",
            })

from uuid import uuid4

from .contracts import Receipt
from .errors import Conflict, Forbidden, ValidationError
from .service import Context, OPEN_TASK_STATES, Service, require_working
from .storage import bump_event, bump_task, emit, record
from .validation import now, pagination, text, timestamp, version


class TaskService(Service):
    def assign(
        self, actor_id: str, request_id: str, event_id: str, *, title: str,
        assignee_id: str, due_at: str, expected_version: int,
    ) -> Receipt:
        """expected_version is the event version; receipt describes the new task."""
        payload = {
            "event_id": text(event_id, "event_id"), "title": text(title, "title", 1000),
            "assignee_id": text(assignee_id, "assignee_id"),
            "due_at": timestamp(due_at, "due_at"), "expected_version": version(expected_version),
        }

        def change(ctx, event):
            require_working(event)
            ctx.require_member(event_id, assignee_id)
            bump_event(ctx.db, event, expected_version)
            task_id, created = str(uuid4()), now()
            ctx.db.execute(
                """INSERT INTO tasks(
                    id,event_id,title,assignee_id,due_at,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,'assigned',?,?)""",
                (task_id, event_id, payload["title"], assignee_id, payload["due_at"], created, created),
            )
            record(ctx.db, ctx.actor.user_id, "task.assigned", event_id, {
                "task_id": task_id, "assignee_id": assignee_id,
            })
            emit(ctx.db, "task.assigned", event_id, {
                "audience": "user", "user_id": assignee_id, "task_id": task_id,
            })
            return Receipt(task_id, 1)

        return self._mutate(
            actor_id, request_id, "task.assign", payload,
            lambda ctx: ctx.managed_event(event_id), change,
        )

    def set_status(
        self, actor_id: str, request_id: str, task_id: str, *, status: str,
        expected_version: int, note: str | None = None,
    ) -> Receipt:
        if status not in {"in_progress", "needs_help", "done"}:
            raise ValidationError("Executor status must be in_progress, needs_help or done")
        if status == "needs_help":
            note = text(note, "note", 4000)
        elif note is not None:
            raise ValidationError("Only needs_help accepts a note")
        payload = {
            "task_id": text(task_id, "task_id"), "status": status,
            "note": note, "expected_version": version(expected_version),
        }

        def guard(ctx):
            task, event = ctx.visible_task(task_id)
            if task["assignee_id"] != ctx.actor.user_id:
                raise Forbidden("Only the current executor may report progress")
            ctx.require_member(event["id"], ctx.actor.user_id)
            return task, event

        def change(ctx, subject):
            task, event = subject
            require_working(event)
            if task["status"] not in OPEN_TASK_STATES:
                raise Conflict("Task is already closed")
            if task["status"] == status and task["note"] == note:
                raise Conflict("Task already has this status")
            result = bump_task(ctx.db, task, expected_version, status=status, note=note)
            detail = {"task_id": task_id, "status": status}
            record(ctx.db, ctx.actor.user_id, "task.status_changed", event["id"], detail)
            emit(ctx.db, "task.changed", event["id"], {"audience": "task_sync", **detail})
            if status == "needs_help":
                emit(ctx.db, "task.help_requested", event["id"], {
                    "audience": "event_owner", "task_id": task_id, "note": note,
                })
            return Receipt(task_id, result)

        return self._mutate(actor_id, request_id, "task.set_status", payload, guard, change)

    def reassign(
        self, actor_id: str, request_id: str, task_id: str, *,
        assignee_id: str, expected_version: int,
    ) -> Receipt:
        payload = {
            "task_id": text(task_id, "task_id"), "assignee_id": text(assignee_id, "assignee_id"),
            "expected_version": version(expected_version),
        }

        def change(ctx, subject):
            task, event = subject
            require_working(event)
            if task["status"] not in OPEN_TASK_STATES:
                raise Conflict("Task is already closed")
            if task["assignee_id"] == assignee_id:
                raise Conflict("User is already assigned")
            ctx.require_member(event["id"], assignee_id)
            result = bump_task(
                ctx.db, task, expected_version, assignee_id=assignee_id, status="assigned", note=None
            )
            detail = {
                "task_id": task_id, "assignee_id": assignee_id,
                "previous_assignee_id": task["assignee_id"],
            }
            record(ctx.db, ctx.actor.user_id, "task.reassigned", event["id"], detail)
            emit(ctx.db, "task.reassigned", event["id"], {"audience": "task_sync", **detail})
            return Receipt(task_id, result)

        return self._mutate(
            actor_id, request_id, "task.reassign", payload,
            lambda ctx: ctx.managed_task(task_id), change,
        )

    def cancel(
        self, actor_id: str, request_id: str, task_id: str, *,
        reason: str, expected_version: int,
    ) -> Receipt:
        payload = {
            "task_id": text(task_id, "task_id"), "reason": text(reason, "reason", 4000),
            "expected_version": version(expected_version),
        }

        def change(ctx, subject):
            task, event = subject
            require_working(event)
            if task["status"] not in OPEN_TASK_STATES:
                raise Conflict("Task is already closed")
            result = bump_task(
                ctx.db, task, expected_version, status="cancelled", note=payload["reason"]
            )
            record(ctx.db, ctx.actor.user_id, "task.cancelled", event["id"], payload)
            emit(ctx.db, "task.changed", event["id"], {
                "audience": "task_sync", "task_id": task_id, "status": "cancelled",
            })
            return Receipt(task_id, result)

        return self._mutate(
            actor_id, request_id, "task.cancel", payload,
            lambda ctx: ctx.managed_task(task_id), change,
        )

    def get(self, actor_id: str, task_id: str) -> dict:
        with self.store.transaction() as db:
            return Context(db, self.directory, actor_id).visible_task(task_id)[0]

    def list_for_event(self, actor_id: str, event_id: str) -> list[dict]:
        with self.store.transaction() as db:
            ctx = Context(db, self.directory, actor_id)
            event = ctx.visible_event(event_id)
            if ctx.is_manager(event):
                rows = db.execute(
                    "SELECT * FROM tasks WHERE event_id=? ORDER BY due_at,id", (event_id,)
                )
            else:
                member = ctx.membership(event_id, ctx.actor.user_id)
                if member is None or member["status"] != "active":
                    return []
                rows = db.execute(
                    "SELECT * FROM tasks WHERE event_id=? AND assignee_id=? ORDER BY due_at,id",
                    (event_id, ctx.actor.user_id),
                )
            return [dict(row) for row in rows]

    def list_mine(self, actor_id: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
        pagination(limit, offset)
        with self.store.transaction() as db:
            ctx = Context(db, self.directory, actor_id)
            return [dict(row) for row in db.execute(
                """SELECT t.* FROM tasks t JOIN participation p
                ON p.event_id=t.event_id AND p.user_id=t.assignee_id
                WHERE t.assignee_id=? AND p.status='active'
                ORDER BY t.due_at,t.id LIMIT ? OFFSET ?""",
                (ctx.actor.user_id, limit, offset),
            )]

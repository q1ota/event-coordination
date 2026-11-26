from .contracts import Receipt
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .service import Context, Service, require_working
from .storage import bump_event, emit, event_row, record
from .validation import pagination, text, version


class ParticipationService(Service):
    def request_join(
        self, actor_id: str, request_id: str, event_id: str, *, expected_version: int,
    ) -> Receipt:
        """An invitation is accepted by its recipient using the same command."""
        payload = {"event_id": text(event_id, "event_id"), "expected_version": version(expected_version)}

        def guard(ctx):
            event = event_row(ctx.db, event_id)
            member = ctx.membership(event_id, ctx.actor.user_id)
            if member and member["status"] == "removed":
                raise Forbidden("A new invitation is required after removal")
            if event["status"] not in {"approved", "preparing", "ongoing"}:
                # A former participant can replay a receipt; outsiders cannot discover closed events.
                ctx.visible_event(event_id)
            return event

        def change(ctx, event):
            require_working(event)
            member = ctx.membership(event_id, ctx.actor.user_id)
            if member and member["status"] in {"active", "requested"}:
                raise Conflict("Participation already exists")
            if member and member["status"] == "removed":
                raise Forbidden("A new invitation is required after removal")
            state = "active" if member and member["status"] == "invited" else "requested"
            result = bump_event(ctx.db, event, expected_version)
            ctx.db.execute(
                """INSERT INTO participation(event_id,user_id,status) VALUES (?,?,?)
                ON CONFLICT(event_id,user_id) DO UPDATE SET status=excluded.status,reason=NULL""",
                (event_id, ctx.actor.user_id, state),
            )
            record(ctx.db, ctx.actor.user_id, f"participation.{state}", event_id, {})
            emit(ctx.db, f"participation.{state}", event_id, {
                "audience": "event_owner" if state == "requested" else "membership_sync",
                "user_id": ctx.actor.user_id,
            })
            return Receipt(event_id, result)

        return self._mutate(actor_id, request_id, "participation.request", payload, guard, change)

    def invite(
        self, actor_id: str, request_id: str, event_id: str, *,
        user_id: str, expected_version: int,
    ) -> Receipt:
        payload = {
            "event_id": text(event_id, "event_id"), "user_id": text(user_id, "user_id"),
            "expected_version": version(expected_version),
        }

        def change(ctx, event):
            require_working(event)
            ctx.active_identity(user_id)
            member = ctx.membership(event_id, user_id)
            if member and member["status"] in {"active", "invited"}:
                raise Conflict("Participant already active or invited")
            result = bump_event(ctx.db, event, expected_version)
            ctx.db.execute(
                """INSERT INTO participation(event_id,user_id,status) VALUES (?,?,'invited')
                ON CONFLICT(event_id,user_id) DO UPDATE SET status='invited',reason=NULL""",
                (event_id, user_id),
            )
            record(ctx.db, ctx.actor.user_id, "participation.invited", event_id, {"user_id": user_id})
            emit(ctx.db, "participation.invited", event_id, {"audience": "user", "user_id": user_id})
            return Receipt(event_id, result)

        return self._mutate(
            actor_id, request_id, "participation.invite", payload,
            lambda ctx: ctx.managed_event(event_id), change,
        )

    def decide(
        self, actor_id: str, request_id: str, event_id: str, *,
        user_id: str, accept: bool, expected_version: int, reason: str | None = None,
    ) -> Receipt:
        if type(accept) is not bool:
            raise ValidationError("accept must be a boolean")
        if not accept:
            reason = text(reason, "reason", 4000)
        elif reason is not None:
            raise ValidationError("An accepted request must not include a rejection reason")
        payload = {
            "event_id": text(event_id, "event_id"), "user_id": text(user_id, "user_id"),
            "accept": accept, "reason": reason, "expected_version": version(expected_version),
        }

        def change(ctx, event):
            require_working(event)
            member = ctx.membership(event_id, user_id)
            if not member or member["status"] != "requested":
                raise Conflict("A pending participation request is required")
            if accept:
                ctx.active_identity(user_id)
            state = "active" if accept else "rejected"
            result = bump_event(ctx.db, event, expected_version)
            ctx.db.execute(
                "UPDATE participation SET status=?,reason=? WHERE event_id=? AND user_id=?",
                (state, reason, event_id, user_id),
            )
            record(ctx.db, ctx.actor.user_id, f"participation.{state}", event_id, {
                "user_id": user_id, "reason": reason,
            })
            emit(ctx.db, f"participation.{state}", event_id, {
                "audience": "membership_sync" if accept else "user", "user_id": user_id,
            })
            return Receipt(event_id, result)

        return self._mutate(
            actor_id, request_id, "participation.decide", payload,
            lambda ctx: ctx.managed_event(event_id), change,
        )

    def remove(
        self, actor_id: str, request_id: str, event_id: str, *,
        user_id: str, reason: str, expected_version: int,
    ) -> Receipt:
        payload = {
            "event_id": text(event_id, "event_id"), "user_id": text(user_id, "user_id"),
            "reason": text(reason, "reason", 4000), "expected_version": version(expected_version),
        }

        def change(ctx, event):
            require_working(event)
            if event["owner_id"] == user_id:
                raise Conflict("Transfer responsibility before removing its owner")
            member = ctx.membership(event_id, user_id)
            if not member or member["status"] not in {"active", "invited", "requested"}:
                raise Conflict("No current participation to remove")
            open_task = ctx.db.execute(
                """SELECT 1 FROM tasks WHERE event_id=? AND assignee_id=?
                AND status IN ('assigned','in_progress','needs_help') LIMIT 1""",
                (event_id, user_id),
            ).fetchone()
            if open_task:
                raise Conflict("Reassign or cancel outstanding tasks first")
            result = bump_event(ctx.db, event, expected_version)
            ctx.db.execute(
                "UPDATE participation SET status='removed',reason=? WHERE event_id=? AND user_id=?",
                (payload["reason"], event_id, user_id),
            )
            record(ctx.db, ctx.actor.user_id, "participation.removed", event_id, payload)
            emit(ctx.db, "participation.removed", event_id, {
                "audience": "membership_sync", "user_id": user_id,
            })
            return Receipt(event_id, result)

        return self._mutate(
            actor_id, request_id, "participation.remove", payload,
            lambda ctx: ctx.managed_event(event_id), change,
        )

    def transfer_owner(
        self, actor_id: str, request_id: str, event_id: str, *,
        owner_id: str, expected_version: int,
    ) -> Receipt:
        payload = {
            "event_id": text(event_id, "event_id"), "owner_id": text(owner_id, "owner_id"),
            "expected_version": version(expected_version),
        }

        def change(ctx, event):
            require_working(event)
            ctx.active_identity(owner_id)
            if event["owner_id"] == owner_id:
                raise Conflict("This user already owns the event")
            result = bump_event(ctx.db, event, expected_version, owner_id=owner_id)
            ctx.db.execute(
                """INSERT INTO participation(event_id,user_id,status) VALUES (?,?,'active')
                ON CONFLICT(event_id,user_id) DO UPDATE SET status='active',reason=NULL""",
                (event_id, owner_id),
            )
            detail = {"previous_owner_id": event["owner_id"], "owner_id": owner_id}
            record(ctx.db, ctx.actor.user_id, "event.owner_changed", event_id, detail)
            emit(ctx.db, "event.owner_changed", event_id, {
                "audience": "membership_sync", **detail,
            })
            return Receipt(event_id, result)

        return self._mutate(
            actor_id, request_id, "event.transfer_owner", payload,
            lambda ctx: ctx.managed_event(event_id), change,
        )

    def list_members(self, actor_id: str, event_id: str) -> list[dict]:
        with self.store.transaction() as db:
            Context(db, self.directory, actor_id).managed_event(event_id)
            return [dict(row) for row in db.execute(
                "SELECT * FROM participation WHERE event_id=? ORDER BY user_id", (event_id,)
            )]

    def my_participation(self, actor_id: str, event_id: str) -> dict:
        with self.store.transaction() as db:
            ctx = Context(db, self.directory, actor_id)
            member = ctx.membership(event_id, ctx.actor.user_id)
            if member is None:
                raise NotFound("Participation not found")
            return dict(member)

    def find_candidates(
        self, actor_id: str, event_id: str, *, interest: str | None = None,
        help_type: str | None = None, limit: int = 50,
    ) -> list[dict]:
        pagination(limit)
        if interest is not None:
            interest = text(interest, "interest")
        if help_type is not None:
            help_type = text(help_type, "help_type")
        with self.store.transaction() as db:
            ctx = Context(db, self.directory, actor_id)
            event = ctx.managed_event(event_id)
            require_working(event)
            interest = interest or event["direction_id"]
            found = {}
            for candidate in self.directory.candidates(event["territory_id"], interest, help_type):
                person = self.directory.get(candidate.user_id)
                if (
                    person is None or person.user_id != candidate.user_id or not person.active
                    or person.territory_id != event["territory_id"]
                    or interest not in person.interests
                    or (help_type is not None and help_type not in person.help_types)
                ):
                    continue
                member = ctx.membership(event_id, person.user_id)
                if member and member["status"] in {"active", "invited", "removed"}:
                    continue
                found[person.user_id] = {"user_id": person.user_id, "display_name": person.display_name}
            return sorted(found.values(), key=lambda item: item["user_id"])[:limit]

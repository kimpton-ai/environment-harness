"""Advance externally controlled participants without owning their agent loop."""

import json
import time

from .errors import Conflict
from .runner import writer
from .store import uid


def advance(session, environment, principal, *, owner=None):
    """Resolve at most one ready phase under the normal fenced writer lease.

    Return waiting without closing an incomplete coordinated phase. A declared
    deadline and missing-action policy decide whether incomplete phases can advance.
    """
    with writer(session, environment, principal, owner or uid()) as (lease, failures):
        with session.store.transaction() as db:
            row = session.store.environment(db, environment, principal, ("researcher", "worker"))
            session._fence(row, lease)
            if row["status"] != "running":
                return {"status": row["status"], "revision": row["revision"]}
            spec = session._compatible(row)
            scheduler = json.loads(row["scheduler"])
            members = json.loads(row["participants"])
            required = {p for p, member in members.items() if member["active"]}
            if spec.environment.scheduling == "sequential":
                required = {scheduler["actor"]}
            accepted = {
                item["participant"]
                for item in db.execute(
                    "SELECT participant FROM actions WHERE environment=? AND revision=? AND status='accepted'",
                    (environment, row["revision"]),
                )
            }
            complete = required <= accepted
            expired = time.time() >= scheduler["deadline"] or scheduler.get("closed", False)
            event_ready = bool(scheduler["pending_events"])
            ready = complete or (expired and spec.environment.missing_action == "noop")
            if spec.environment.scheduling == "event":
                ready = ready and (event_ready or expired)
            revision = row["revision"]
        if not ready:
            return {"status": "waiting", "revision": revision, "deadline_exceeded": expired}
        if failures:
            raise Conflict("coordinator lost authority")
        if spec.environment.phase_deadline == "coordinator":
            session.close_phase(
                environment,
                principal,
                lease,
                revision=revision,
                reason="decisions_complete" if complete else "deadline",
            )
        result = session.resolve(environment, principal, lease)
        if failures:
            raise Conflict("coordinator lost authority; inspect the committed revision")
        return result

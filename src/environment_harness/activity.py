"""Tenant-scoped reads from the transactional activity outbox."""

import json

from .errors import Forbidden


def events(store, access, after=0, limit=200, *, experiment=None, environment=None):
    access.require("activity.read")
    if after < 0 or not 1 <= limit <= 1000:
        raise ValueError("invalid activity page")
    with store.transaction() as db:
        if experiment is not None:
            owner = db.execute("SELECT tenant FROM experiments WHERE id=?", (experiment,)).fetchone()
            if not owner or owner["tenant"] != access.tenant:
                raise Forbidden("experiment unavailable")
        if environment is not None:
            owner = db.execute(
                "SELECT tenant FROM session_runs WHERE environment=?", (environment,)
            ).fetchone()
            if not owner or owner["tenant"] != access.tenant:
                raise Forbidden("environment session unavailable")
        rows = db.execute(
            "SELECT * FROM event_outbox WHERE tenant=? AND id>? "
            "AND (? IS NULL OR experiment=?) AND (? IS NULL OR environment=?) "
            "ORDER BY id LIMIT ?",
            (access.tenant, after, experiment, experiment, environment, environment, limit),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "topic": row["topic"],
                "experiment": row["experiment"],
                "environment": row["environment"],
                "kind": row["kind"],
                "payload": json.loads(row["body"]),
                "created": row["created"],
            }
            for row in rows
        ]

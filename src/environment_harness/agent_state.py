"""Durable, revision-scoped work for cooperative agent programs."""

import json

from .errors import Conflict


class AgentJournal:
    def __init__(self, session, environment, principal, revision):
        self.session, self.environment, self.principal, self.revision = (
            session,
            environment,
            principal,
            revision,
        )

    def load(self):
        with self.session.store.transaction() as db:
            row = self.session.store.environment(db, self.environment, self.principal, ("agent",))
            if row["revision"] != self.revision:
                raise Conflict("agent journal revision changed")
            participant = json.loads(row["participants"])[self.principal.participant]
            state = participant["agent_state"] or {}
            return state if state.get("revision") == self.revision else {"revision": self.revision}

    def save(self, state, memory):
        if state.get("revision") != self.revision:
            raise Conflict("journal revision mismatch")
        self.session.memory(self.environment, self.principal, memory, state, expected_revision=self.revision)

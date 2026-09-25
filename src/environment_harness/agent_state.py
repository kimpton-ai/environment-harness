"""Durable, revision-scoped work for cooperative agent programs."""

import json

from .errors import Conflict


class AgentJournal:
    def __init__(self, session, environment, access, revision):
        self.session, self.environment, self.access, self.revision = (
            session,
            environment,
            access,
            revision,
        )

    def load(self):
        with self.session.store.transaction() as db:
            row = self.session.store.environment(
                db, self.environment, self.access, "participant.memory.write"
            )
            if row["revision"] != self.revision:
                raise Conflict("agent journal revision changed")
            participant = json.loads(row["participants"])[self.access.participant]
            state = participant["agent_state"] or {}
            return state if state.get("revision") == self.revision else {"revision": self.revision}

    def save(self, state, memory):
        if state.get("revision") != self.revision:
            raise Conflict("journal revision mismatch")
        self.session.memory(self.environment, self.access, memory, state, expected_revision=self.revision)

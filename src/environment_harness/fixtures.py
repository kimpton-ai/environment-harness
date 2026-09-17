"""Synthetic protocol fixture only. No benchmark or supplier mechanics."""

from .contracts import Capabilities, EnvironmentSpec, EventInput, Mode, Transition
from .errors import Unsupported


class SyntheticEnvironment:
    def __init__(self, mode: Mode = "simultaneous"):
        self.spec = EnvironmentSpec(
            id="synthetic-protocol",
            version="1",
            implementation="synthetic-protocol@1",
            scheduling=mode,
            capabilities=Capabilities(checkpoint=True, resume=True, branch=True),
            purposes=("evaluation", "training"),
            missing_action="noop",
            phase_seconds=3600,
            action_schema={
                "type": "object",
                "properties": {"value": {"type": "integer", "minimum": -1, "maximum": 1}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )

    def initialize(self, experiment):
        return {
            "total": 0,
            "turn": 0,
            "private": {p.id: "synthetic-secret-" + p.id for p in experiment.participants},
        }

    def observe(self, state, participant):
        return {
            "total": state["total"],
            "turn": state["turn"],
            "private": state["private"][participant],
            "instruction": "Synthetic fixture: choose an integer from -1 to 1.",
        }

    def resolve(self, state, actions, random, events):
        new = dict(
            state,
            total=state["total"] + sum(a["value"] for a in actions.values() if a),
            turn=state["turn"] + 1,
        )
        return Transition(
            state=new,
            outcomes={
                p: {"executed": a is not None, "value": a["value"] if a else None} for p, a in actions.items()
            },
            rewards={p: float(a["value"]) for p, a in actions.items() if a},
            events=(EventInput(kind="synthetic.total", payload={"total": new["total"]}, audience=("*",)),),
        )

    def intervene(self, state, changes):
        if set(changes) - {"total"} or ("total" in changes and type(changes["total"]) is not int):
            raise Unsupported("synthetic fixture only permits an integer total intervention")
        return dict(state, **changes)


class SyntheticAgent:
    implementation = "synthetic-agent@1"

    def act(self, observation):
        return {"value": 1}

    def checkpoint(self):
        return {}

    def restore(self, state):
        if state != {}:
            raise ValueError("unknown synthetic state")

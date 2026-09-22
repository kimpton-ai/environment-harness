"""Synthetic protocol fixture only. No benchmark or supplier mechanics."""

from pydantic import BaseModel, ConfigDict, Field

from .contracts import Capabilities, EnvironmentSpec, EventInput, Mode, Transition
from .errors import Unsupported


class SyntheticScenarioInput(BaseModel):
    """Typed input for public synthetic environment-session examples."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    starting_total: int = Field(default=0, ge=-100, le=100)


class SyntheticEnvironment:
    scenario_type = SyntheticScenarioInput

    def __init__(self, mode: Mode = "simultaneous"):
        self.operations = {}
        self.spec = EnvironmentSpec(
            id="synthetic-protocol",
            version="1",
            implementation="synthetic-protocol@1",
            scheduling=mode,
            capabilities=Capabilities(checkpoint=True, resume=True, branch=True),
            purposes=("evaluation", "training"),
            missing_action="noop",
            phase_seconds=3600,
            scenario_schema=SyntheticScenarioInput.model_json_schema(),
            action_schema={
                "type": "object",
                "properties": {"value": {"type": "integer", "minimum": -1, "maximum": 1}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )

    def initialize(self, experiment):
        scenario = SyntheticScenarioInput.model_validate(experiment.scenario_input)
        return {
            "total": scenario.starting_total,
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


class SyntheticShowcaseAgent:
    """Deterministic, state-free policy used by the public product showcase."""

    implementation = "synthetic-showcase-agent@1"
    _sequence = (1, 0, -1, 1, 1, -1)

    def __init__(self, offset=0):
        self.offset = offset
        self.config = {"sequence_offset": offset}

    def act(self, observation):
        turn = int(observation["payload"]["turn"])
        return {"value": self._sequence[(turn + self.offset) % len(self._sequence)]}

    def checkpoint(self):
        return {"offset": self.offset}

    def restore(self, state):
        if state not in ({}, {"offset": self.offset}):
            raise ValueError("unknown synthetic showcase state")

"""Synthetic protocol fixture only. No benchmark or supplier mechanics."""

from pydantic import BaseModel, ConfigDict, Field

from .contracts import Capabilities, EnvironmentSpec, EventInput, Mode, Scenario, Transition
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


def shared_experiment(store, *, turns: int = 2, tenant: str = "fixture"):
    """Build the shared experiment fixture consumed by contract gates.

    One store receives an experiment-of-one and a multi-scenario,
    multi-session experiment so every workstream asserts against the same
    frozen Experiment, ScenarioSet, Session, Checkpoint, and Trajectory
    identities. It is deterministic: the same call produces the same digests.
    """

    from .harness import EnvironmentHarness

    harness = EnvironmentHarness(
        store,
        environment=(SyntheticEnvironment,),
        agents={"alice": SyntheticAgent, "bob": SyntheticAgent},
        scoring_versions=("shared-fixture@1",),
        max_concurrency=1,
        tenant=tenant,
    )
    solo = harness.run(
        Scenario(id="experiment-of-one", input=SyntheticScenarioInput(starting_total=0)),
        turns=turns,
    )
    grouped = harness.experiment(
        "shared fixture",
        (
            Scenario(id="low", input=SyntheticScenarioInput(starting_total=-2)),
            Scenario(id="high", input=SyntheticScenarioInput(starting_total=2)),
        ),
        trials=2,
        seed=7,
        turns=turns,
    )
    result = grouped.run()
    checkpoint = solo.checkpoint(exact_agents=True)
    return {
        "harness": harness,
        "solo": solo,
        "experiment": grouped,
        "result": result,
        "checkpoint": checkpoint["id"],
    }

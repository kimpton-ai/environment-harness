"""Run a typed synthetic experiment and print where to review it in the local viewer."""

import argparse
import json

from environment_harness import EnvironmentHarness, Scenario
from environment_harness.fixtures import (
    SyntheticEnvironment,
    SyntheticScenarioInput,
    SyntheticShowcaseAgent,
)


def run_experiment(store, *, turns: int = 3):
    harness = EnvironmentHarness(
        store,
        environment_factory=SyntheticEnvironment,
        agent_factories={
            "alice": lambda: SyntheticShowcaseAgent(0),
            "bob": lambda: SyntheticShowcaseAgent(1),
        },
        max_concurrency=2,
    )
    result = harness.experiment(
        "Synthetic scenario sweep",
        (
            Scenario(
                id="negative-start",
                input=SyntheticScenarioInput(starting_total=-3),
                metadata={"description": "Begin below zero."},
            ),
            Scenario(
                id="positive-start",
                input=SyntheticScenarioInput(starting_total=3),
                metadata={"description": "Begin above zero."},
            ),
        ),
        trials=2,
        seed=42,
        turns=turns,
    ).run()
    return {
        "experiment": result.id,
        "status": result.status,
        "completed": result.completed,
        "total": result.total,
        "sessions": [
            {
                "id": session.id,
                "scenario": session.scenario_id,
                "trial": session.trial + 1,
                "seed": session.seed,
                "status": session.status,
            }
            for session in result.sessions
        ],
        "review": {
            "command": ["environment-harness", "--store", str(store), "serve", "--open"],
            "path": f"/experiment/{result.id}",
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".local/typed-experiment")
    parser.add_argument("--turns", type=int, default=3)
    print(json.dumps(run_experiment(**vars(parser.parse_args())), indent=2))

"""Run a multi-session experiment with a custom operation, scores, and findings.

This is a synthetic SDK example. ``InspectTotal`` stands in for an operation
implemented by a Minecraft, Unreal, browser, or other environment package.
"""

from __future__ import annotations

import argparse
import json

from environment_harness import (
    EnvironmentHarness,
    EnvironmentOperation,
    OperationSpec,
    Scenario,
)
from environment_harness.contracts import Finding, MetricDefinition, ScoreReport
from environment_harness.fixtures import (
    SyntheticEnvironment,
    SyntheticScenarioInput,
    SyntheticShowcaseAgent,
)
from environment_harness.operations import Operations
from environment_harness.runner import run as run_turns

SCORER = "synthetic-operation-review"
SCORER_VERSION = "1"


class InspectTotal(EnvironmentOperation):
    """A small environment-owned operation with only JSON in its frozen spec."""

    endpoint = "synthetic"

    def __init__(self, *, threshold: int = 0):
        self.spec = OperationSpec(
            name="synthetic.inspect-total",
            version="1",
            config={"threshold": threshold},
        )
        self._receipts: dict[str, dict] = {}

    def validate(self, request, manifest):
        payload = request["payload"]
        if set(payload) != {"total"} or type(payload["total"]) is not int:
            raise ValueError("synthetic inspection requires one integer total")
        return payload

    def execute(self, operation_id, request, maximum_cost_micros, *, authority):
        payload = request["payload"]
        authority(payload)
        receipt = {
            "operation_id": operation_id,
            "cost_micros": 0,
            "observed_total": payload["total"],
            "threshold": self.spec.config["threshold"],
            "meets_threshold": payload["total"] >= self.spec.config["threshold"],
        }
        self._receipts[operation_id] = receipt
        return receipt

    def lookup(self, operation_id):
        return self._receipts.get(operation_id)


class ReviewEnvironment(SyntheticEnvironment):
    """The normal synthetic rules plus one package-supplied operation class."""

    def __init__(self):
        super().__init__()
        inspection = InspectTotal(threshold=0)
        self.operations = {inspection.spec.name: inspection}
        self.spec = self.spec.model_copy(
            update={
                "id": "synthetic-operation-review",
                "version": "1",
                "implementation": "synthetic-operation-review@1",
                "operations": (OperationSpec(name=inspection.spec.name, version=inspection.spec.version),),
            }
        )


def run_with_inspection(session, environment, researcher, agents, *, turns):
    """Interleave normal turns with an environment operation in every session."""
    result = run_turns(session, environment, researcher, agents, turns=1)
    observation = session.observe(environment, researcher, "alice")
    # The runtime derives the participant scope; examples never build one.
    agent = session.participant_context(environment, researcher, "alice")
    operations = Operations(session.store)
    operations.prepare(
        environment,
        agent,
        "inspect-after-turn-1",
        endpoint="synthetic",
        operation="synthetic.inspect-total",
        payload={"total": observation["payload"]["total"]},
    )
    lease = session.lease(environment, researcher, "synthetic-inspection")
    try:
        operations.dispatch(
            session,
            environment,
            researcher,
            lease,
            "inspect-after-turn-1",
        )
    finally:
        session.release(environment, researcher, lease)
    if turns > 1:
        result = run_turns(session, environment, researcher, agents, turns=turns - 1)
    return result


def score_session(session):
    """Turn recorded evidence into comparable metrics and one linked finding."""
    evidence = list(session.replay())
    executed = [event for event in evidence if event["kind"] == "action.executed"]
    operation_receipts = [event for event in evidence if event["kind"] == "operation.receipt"]
    inspection = operation_receipts[-1]["payload"]["receipt"]
    final_total = next(
        event["payload"]["total"] for event in reversed(evidence) if event["kind"] == "synthetic.total"
    )
    negative = next(
        (
            event
            for event in executed
            if isinstance(event["payload"].get("outcome"), dict)
            and event["payload"]["outcome"].get("value", 0) < 0
        ),
        None,
    )
    findings = ()
    if negative is not None:
        payload = negative["payload"]
        findings = (
            Finding(
                rule="synthetic.negative-action",
                participant=payload["participant"],
                observation_id=payload["observation_id"],
                action_id=payload["action_id"],
                outcome_event=negative["seq"],
                category="competence",
                status="executed",
                judgment="The synthetic agent decreased the shared counter on this turn.",
                uncertainty="Synthetic demonstration only; this is not a quality or safety judgment.",
            ),
        )
    report = ScoreReport(
        scorer=SCORER,
        version=SCORER_VERSION,
        kind="deterministic",
        evidence_cursor=max(event["seq"] for event in evidence),
        metrics={
            "final_total": final_total,
            "cumulative_reward": sum(
                event["payload"]["reward"]
                for event in executed
                if isinstance(event["payload"].get("reward"), (int, float))
            ),
            "operation_receipts": len(operation_receipts),
            "inspection_passed": int(inspection["meets_threshold"]),
        },
        metric_definitions={
            "final_total": MetricDefinition(id="synthetic.final-total", version="1", unit="count"),
            "cumulative_reward": MetricDefinition(
                id="synthetic.cumulative-reward", version="1", unit="reward"
            ),
            "operation_receipts": MetricDefinition(
                id="environment-harness.operation-receipts", version="1", unit="count"
            ),
            "inspection_passed": MetricDefinition(
                id="synthetic.inspection-passed", version="1", unit="boolean"
            ),
        },
        findings=findings,
        uncertainty="All values come from a deterministic synthetic fixture.",
        provenance={"synthetic": True, "source": "examples/custom_environment_experiment.py"},
    )
    stored = session.report(report)
    return inspection, stored


def run_experiment(store, *, turns: int = 3):
    if turns < 2:
        raise ValueError("the operation example requires at least two turns")
    harness = EnvironmentHarness(
        store,
        environment_factory=ReviewEnvironment,
        agent_factories={
            "alice": lambda: SyntheticShowcaseAgent(0),
            "bob": lambda: SyntheticShowcaseAgent(1),
        },
        scoring_versions=(f"{SCORER}@{SCORER_VERSION}",),
        session_runner=run_with_inspection,
        max_concurrency=2,
    )
    result = harness.experiment(
        "Custom environment operation review",
        (
            Scenario(
                id="below-threshold",
                input=SyntheticScenarioInput(starting_total=-2),
                metadata={"description": "Inspect a counter that begins below zero."},
            ),
            Scenario(
                id="above-threshold",
                input=SyntheticScenarioInput(starting_total=2),
                metadata={"description": "Inspect a counter that begins above zero."},
            ),
        ),
        trials=2,
        seed=42,
        turns=turns,
    ).run()
    sessions = []
    for session in result.sessions:
        inspection, report = score_session(session)
        sessions.append(
            {
                "id": session.id,
                "scenario": session.scenario_id,
                "trial": session.trial + 1,
                "status": session.status,
                "inspection": inspection,
                "report_revision": report["revision"],
                "findings": len(report["report"]["findings"]),
            }
        )
    return {
        "experiment": result.id,
        "status": result.status,
        "completed": result.completed,
        "total": result.total,
        "sessions": sessions,
        "review": {
            "command": ["environment-harness", "--store", str(store), "serve", "--open"],
            "path": f"/experiment/{result.id}",
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".local/custom-environment-experiment")
    parser.add_argument("--turns", type=int, default=3)
    print(json.dumps(run_experiment(**vars(parser.parse_args())), indent=2))

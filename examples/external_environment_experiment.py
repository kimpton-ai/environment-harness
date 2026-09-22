"""Run an experiment that controls a simulator in a separate process.

The simulator stands in for Minecraft, Unreal Engine, a robotics service, or any
other external system. The integration lives entirely in this example: core only
sees an ``EnvironmentOperation`` and its JSON receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from environment_harness import (
    EnvironmentHarness,
    EnvironmentOperation,
    OperationSpec,
    Principal,
    Scenario,
)
from environment_harness.contracts import Finding, MetricDefinition, RunPolicy, ScoreReport
from environment_harness.fixtures import (
    SyntheticEnvironment,
    SyntheticScenarioInput,
    SyntheticShowcaseAgent,
)
from environment_harness.operations import Operations
from environment_harness.runner import run as run_turns

SCORER = "external-simulator-review"
SCORER_VERSION = "1"


class SimulatorClient:
    """Synchronous client for a long-lived external JSON-lines process."""

    def __init__(self):
        worker = Path(__file__).with_name("external_simulator.py")
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            [sys.executable, str(worker)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={"PATH": os.defpath, "PYTHON_DOTENV_DISABLED": "1"},
        )
        hello = self._call("hello")
        if hello["protocol"] != "example-simulator.v1":
            self.close()
            raise RuntimeError("external simulator protocol mismatch")
        self.worker_pid = hello["worker_pid"]

    def _call(self, method, **arguments):
        with self._lock:
            stdin, stdout = self._process.stdin, self._process.stdout
            if stdin is None or stdout is None or self._process.poll() is not None:
                raise RuntimeError("external simulator is unavailable")
            stdin.write(json.dumps({"method": method, "arguments": arguments}) + "\n")
            stdin.flush()
            line = stdout.readline()
            if not line:
                raise RuntimeError("external simulator closed the connection")
            return json.loads(line)["result"]

    def move(self, operation_id: str, *, entity: str, delta: list[int]):
        return self._call("move", operation_id=operation_id, entity=entity, delta=delta)

    def receipt(self, operation_id: str):
        return self._call("lookup", operation_id=operation_id)

    def close(self):
        if self._process.poll() is None:
            try:
                self._call("shutdown")
            finally:
                self._process.wait(timeout=5)
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None:
                stream.close()


class MoveEntity(EnvironmentOperation):
    """Environment-owned operation backed by the external simulator client."""

    endpoint = "external-simulator"

    def __init__(self, client: SimulatorClient):
        self.client = client
        self.spec = OperationSpec(
            name="example.move-entity",
            version="1",
            config={"protocol": "example-simulator.v1", "coordinates": "xyz"},
        )

    def validate(self, request, manifest):
        payload = request["payload"]
        if (
            set(payload) != {"entity", "delta"}
            or not isinstance(payload["entity"], str)
            or not payload["entity"]
            or not isinstance(payload["delta"], list)
            or len(payload["delta"]) != 3
            or any(type(value) is not int for value in payload["delta"])
        ):
            raise ValueError("external move requires an entity and three integer coordinates")
        return payload

    def execute(self, operation_id, request, maximum_cost_micros, *, authority):
        payload = request["payload"]
        authority(payload)
        return self.client.move(operation_id, entity=payload["entity"], delta=payload["delta"])

    def lookup(self, operation_id):
        return self.client.receipt(operation_id)


class ExternalReviewEnvironment(SyntheticEnvironment):
    """Synthetic rules combined with a package-owned external integration."""

    def __init__(self, client: SimulatorClient):
        super().__init__()
        move = MoveEntity(client)
        self.operations = {move.spec.name: move}
        self.spec = self.spec.model_copy(
            update={
                "id": "external-simulator-review",
                "version": "1",
                "implementation": "external-simulator-review@1",
                "operations": (move.spec,),
                "capabilities": self.spec.capabilities.model_copy(update={"external_writes": True}),
            }
        )


def run_with_external_move(session, environment, researcher, agents, *, turns):
    """Run ordinary turns and one fenced external operation per session."""
    result = run_turns(session, environment, researcher, agents, turns=1)
    observation = session.observe(environment, researcher, "alice")
    distance = max(1, abs(int(observation["payload"]["total"])))
    agent = Principal(
        tenant=researcher.tenant,
        subject="alice",
        role="agent",
        environment=environment,
        participant="alice",
    )
    operations = Operations(session.store)
    operations.prepare(
        environment,
        agent,
        "move-after-turn-1",
        endpoint="external-simulator",
        operation="example.move-entity",
        payload={"entity": environment, "delta": [distance, 0, 0]},
        write=True,
    )
    lease = session.lease(environment, researcher, "external-simulator-example")
    try:
        operations.dispatch(session, environment, researcher, lease, "move-after-turn-1")
    finally:
        session.release(environment, researcher, lease)
    if turns > 1:
        result = run_turns(session, environment, researcher, agents, turns=turns - 1)
    return result


def score_session(store, researcher, environment):
    """Score the recorded receipt and link a finding to ordinary turn evidence."""
    evidence = list(store.replay(environment, researcher))
    executed = [event for event in evidence if event["kind"] == "action.executed"]
    receipt_events = [event for event in evidence if event["kind"] == "operation.receipt"]
    receipt = receipt_events[-1]["payload"]["receipt"]
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
            "external_operations": len(receipt_events),
            "distance_moved": receipt["position"][0],
        },
        metric_definitions={
            "external_operations": MetricDefinition(
                id="environment-harness.external-operations", version="1", unit="count"
            ),
            "distance_moved": MetricDefinition(id="example.distance-moved", version="1", unit="coordinate"),
        },
        findings=findings,
        uncertainty="The external simulator is deterministic and synthetic.",
        provenance={
            "synthetic": True,
            "transport": "json-lines-subprocess",
            "source": "examples/external_environment_experiment.py",
        },
    )
    stored = store.report(environment, researcher, report)
    return receipt, stored


def run_experiment(store, *, turns: int = 3):
    if turns < 2:
        raise ValueError("the external operation example requires at least two turns")
    client = SimulatorClient()
    try:
        harness = EnvironmentHarness(
            store,
            environment_factory=lambda: ExternalReviewEnvironment(client),
            agent_factories={
                "alice": lambda: SyntheticShowcaseAgent(0),
                "bob": lambda: SyntheticShowcaseAgent(1),
            },
            scoring_versions=(f"{SCORER}@{SCORER_VERSION}",),
            policy=RunPolicy(external_writes=True),
            session_runner=run_with_external_move,
            max_concurrency=2,
        )
        result = harness.experiment(
            "External simulator operation review",
            (
                Scenario(
                    id="short-move",
                    input=SyntheticScenarioInput(starting_total=-2),
                    metadata={"description": "Request a short external move."},
                ),
                Scenario(
                    id="long-move",
                    input=SyntheticScenarioInput(starting_total=2),
                    metadata={"description": "Request a longer external move."},
                ),
            ),
            trials=2,
            seed=42,
            turns=turns,
        ).run()
        sessions = []
        for session in result.sessions:
            receipt, report = score_session(harness.store, harness.researcher, session.id)
            sessions.append(
                {
                    "id": session.id,
                    "scenario": session.scenario_id,
                    "trial": session.trial + 1,
                    "status": session.status,
                    "receipt": receipt,
                    "report_revision": report["revision"],
                    "findings": len(report["report"]["findings"]),
                }
            )
        return {
            "experiment": result.id,
            "status": result.status,
            "completed": result.completed,
            "total": result.total,
            "driver_pid": os.getpid(),
            "connection": {
                "transport": "json-lines-subprocess",
                "worker_pid": client.worker_pid,
            },
            "sessions": sessions,
            "review": {
                "command": ["environment-harness", "--store", str(store), "serve", "--open"],
                "path": f"/experiment/{result.id}",
            },
        }
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".local/external-environment-experiment")
    parser.add_argument("--turns", type=int, default=3)
    print(json.dumps(run_experiment(**vars(parser.parse_args())), indent=2))

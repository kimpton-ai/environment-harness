"""Record, import, inspect, snapshot, and export synthetic trajectories."""

from __future__ import annotations

import argparse

from environment_harness import EnvironmentHarness, EvidenceStore, Scenario
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.store import encode
from environment_harness.trajectories import (
    SourceRecord,
    SourceRegistration,
    SourceStatusUpdate,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default=".local/trajectory-walkthrough")
    args = parser.parse_args()

    store = EvidenceStore(args.store)
    harness = EnvironmentHarness(
        store,
        environment_factory=SyntheticEnvironment,
        agent_factories={"alice": SyntheticAgent},
    )
    native = harness.run(Scenario(id="walkthrough", input={}), turns=2)

    sources = harness.sources()
    source = sources.register(
        SourceRegistration(
            namespace="com.example.simulator",
            run_id="historical-run-1",
            schema_version="example.trace.v1",
            environment={"id": "example-simulator", "version": "1"},
            participants=("alice",),
            purpose="evaluation",
        )
    )
    record = SourceRecord.create(
        id="historical-outcome-1",
        position="frame-1",
        previous_hash="0" * 64,
        type="com.example.simulator.outcome",
        segment="segment-1",
        participant="alice",
        revision=1,
        time={
            "wallTime": "2026-09-22T15:00:00Z",
            "native": ({"clock": "simulator.frame", "value": 1},),
        },
        data={"result": "success"},
        audience=("*",),
    )
    sources.ingest(source.id, (record,))
    sources.update_status(
        source.id,
        SourceStatusUpdate(
            collection_state="complete",
            execution_state="completed",
            termination={"terminated": True, "truncated": False, "reason": "goal"},
            verified_outcome={"state": "success", "evidence": (record.id,)},
            terminal_position=record.position,
            terminal_hash=record.source_hash,
            backlog=0,
        ),
    )

    native_resource = native.trajectory()
    imported_resource = sources.trajectory(source.id)
    snapshot = sources.freeze(source.id)
    print(
        encode(
            {
                "native": native_resource.metadata.id,
                "native_segments": [segment.id for segment in native_resource.status.segments],
                "imported": imported_resource.metadata.id,
                "imported_outcome": imported_resource.status.verified_outcome.state,
                "snapshot": snapshot.metadata.id,
            }
        )
    )
    for row in sources.export_snapshot(snapshot.metadata.id):
        print(encode(row))


if __name__ == "__main__":
    main()

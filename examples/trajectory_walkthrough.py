"""Record, import, inspect, snapshot, and export synthetic trajectories."""

from __future__ import annotations

import argparse

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.store import encode
from environment_harness.trajectories import (
    SourceRecord,
    SourceRegistration,
    SourceStatusUpdate,
    TrajectoryRepository,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default=".local/trajectory-walkthrough")
    args = parser.parse_args()

    store = EvidenceStore(args.store)
    researcher = Principal(tenant="local", subject="local-researcher", role="researcher")
    environment = SyntheticEnvironment()
    session = EnvironmentSession(store, environment)
    native = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
        ),
        researcher,
    )["id"]
    lease = session.lease(native, researcher, "trajectory-walkthrough")
    session.control(native, researcher, lease, "pause")
    session.resume(native, researcher, lease)
    session.release(native, researcher, lease)

    trajectories = TrajectoryRepository(store)
    source = trajectories.register_source(
        SourceRegistration(
            namespace="com.example.simulator",
            run_id="historical-run-1",
            schema_version="example.trace.v1",
            environment={"id": "example-simulator", "version": "1"},
            participants=("alice",),
            purpose="evaluation",
        ),
        researcher,
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
    trajectories.ingest(source.id, (record,), researcher)
    trajectories.update_source_status(
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
        researcher,
    )

    native_resource = trajectories.get(native, researcher)
    imported_resource = trajectories.get(source.id, researcher)
    snapshot = trajectories.freeze(source.id, researcher)
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
    for row in trajectories.export_snapshot(snapshot.metadata.id, researcher):
        print(encode(row))


if __name__ == "__main__":
    main()

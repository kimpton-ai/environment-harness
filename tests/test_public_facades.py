"""Drive the public local facades end to end.

`EnvironmentHarness`, its `EnvironmentSession` handle, and `harness.sources()`
are the only local-execution surface a caller composes. The rest of the suite
reaches past them into the private runtime and repositories, so these tests
exercise the delegating facades themselves and the guards they inherit.
"""

from __future__ import annotations

import json

import pytest

from environment_harness import BranchRequest, EnvironmentHarness, Scenario
from environment_harness.errors import Conflict, Forbidden
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.training import TrainingOutput, TrainingRepository
from environment_harness.trajectories import (
    SourceRecord,
    SourceRegistration,
    SourceStatusUpdate,
)


def harness(tmp_path, **options):
    return EnvironmentHarness(
        tmp_path,
        environment=SyntheticEnvironment,
        agents={"alice": SyntheticAgent, "bob": SyntheticAgent},
        **options,
    )


def test_environment_session_handle_exposes_every_read_and_command(tmp_path):
    # A runner that stops inside its budget leaves the session resumable, so the
    # handle's cancel path is reachable.
    local = harness(
        tmp_path, session_runner=lambda control, agents, *, turns: control.advance(agents, turns=2)
    )
    session = local.run(Scenario(id="handle", input={}), turns=4)

    assert repr(session) == f"EnvironmentSession(id={session.id!r})"
    assert session.events(after=0, limit=200)
    assert session.reports() == []
    assert [item["id"] for item in session.checkpoint_resources(limit=10)] == []
    assert session.turn_series()["total_turns"] == 2

    stored = session.artifact(b"synthetic-bytes", audience=("alice",), media_type="text/plain")
    assert stored["size"] == len(b"synthetic-bytes")
    assert session.read_artifact(stored["id"]) is not None

    # `environment` is a convenience for the single configured implementation.
    assert local.environment is not None
    assert local.scenario_sets(limit=10) is not None

    cancelled = session.cancel()
    assert cancelled["status"] == "cancelled"


def test_environment_session_rejects_a_non_positive_turn_budget(tmp_path):
    session = harness(tmp_path).run(Scenario(id="budget", input={}), turns=1)
    with pytest.raises(ValueError, match="turns must be positive"):
        session.advance(turns=0)


def test_branching_requires_a_checkpoint_from_the_same_session(tmp_path):
    local = harness(tmp_path)
    parent = local.run(Scenario(id="lineage", input={}), turns=1)
    other = local.run(Scenario(id="other", input={}), turns=1)
    checkpoint = parent.checkpoint()

    child = parent.branch(BranchRequest(checkpoint=checkpoint["id"]))
    assert child.record()["parent"] == parent.id

    # A strict request also accepts the plain mapping form.
    again = parent.branch({"checkpoint": checkpoint["id"], "idempotency_key": "once"})
    assert again.record()["parent"] == parent.id

    with pytest.raises((Conflict, Forbidden)):
        other.branch(BranchRequest(checkpoint=checkpoint["id"]))


def _source(sources, *, purpose="evaluation", split=None, run_id="public-facade"):
    registration = SourceRegistration(
        namespace="com.example.facade",
        run_id=run_id,
        schema_version="facade.v1",
        environment={"id": "facade-simulator", "version": "1"},
        participants=("alice",),
        purpose=purpose,
        **({"split": split} if split else {}),
    )
    source = sources.register(registration)
    record = SourceRecord.create(
        id="frame-1",
        position="1",
        previous_hash="0" * 64,
        type="com.example.facade.outcome",
        segment="segment-1",
        participant="alice",
        revision=0,
        time={"wallTime": "2026-09-22T15:00:00Z", "native": ({"clock": "sim.frame", "value": 1},)},
        data={"result": "success"},
        audience=("*",),
    )
    acknowledgement = sources.ingest(source.id, (record,))
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
    return source, acknowledgement


def test_sources_facade_registers_ingests_inspects_and_freezes(tmp_path):
    sources = harness(tmp_path).sources()
    source, acknowledgement = _source(sources)

    assert acknowledgement.position == "1"
    assert sources.status(source.id).collection_state == "complete"
    assert source.id in {item.id for item in sources.list(limit=100)[0]}
    assert sources.trajectory(source.id).status.collection.state == "complete"
    assert [record.type for record in sources.records(source.id, limit=10).records] == [
        "com.example.facade.outcome"
    ]

    snapshot = sources.freeze(source.id)
    assert sources.snapshot(snapshot.metadata.id).status.complete
    assert [item.metadata.id for item in sources.snapshots(source.id, limit=10)] == [snapshot.metadata.id]
    rows = [json.dumps(row, sort_keys=True) for row in sources.export_snapshot(snapshot.metadata.id)]
    assert rows == [json.dumps(row, sort_keys=True) for row in sources.export_snapshot(snapshot.metadata.id)]


def test_sources_facade_freezes_datasets_and_records_training_receipts(tmp_path):
    local = harness(tmp_path)
    sources = local.sources()
    source, _ = _source(sources, purpose="training", split="training", run_id="trainable")

    dataset = sources.freeze_dataset("facade-dataset", (source.id,))
    assert sources.dataset(dataset.metadata.id).metadata.id == dataset.metadata.id
    assert [item.metadata.id for item in sources.datasets(limit=10)] == [dataset.metadata.id]
    assert list(sources.export_dataset(dataset.metadata.id))
    with pytest.raises(ValueError, match="invalid dataset record page"):
        local.store and TrainingRepository(local.store).dataset_records_page(
            dataset.metadata.id, local._access, limit=0
        )

    class Trainer:
        identity = "com.example.facade-trainer"
        version = "1"

        def validate(self, selected):
            assert selected.metadata.id == dataset.metadata.id

        def train(self, selected, config):
            assert config == {"epochs": 1}
            return TrainingOutput(
                policy={
                    "apiVersion": "environmentharness.dev/v1alpha1",
                    "kind": "Policy",
                    "metadata": {"id": "policy-facade", "createdAt": "2026-09-22T15:00:00Z", "labels": {}},
                    "features": {"required": [], "optional": []},
                    "spec": {
                        "implementation": "facade-trained",
                        "version": "2",
                        "lineage": [],
                        "artifact": {},
                    },
                    "status": {"digest": "0" * 64},
                    "extensions": {},
                },
                metrics={"loss": 0.5},
                limitations=("synthetic",),
            )

    recorded = sources.train(dataset.metadata.id, Trainer(), {"epochs": 1})
    assert sources.training_run(recorded.metadata.id).spec.dataset_id == dataset.metadata.id
    assert [item.metadata.id for item in sources.training_runs(limit=10)] == [recorded.metadata.id]
    assert [item.metadata.id for item in sources.training_runs(dataset=dataset.metadata.id)] == [
        recorded.metadata.id
    ]
    assert local.trajectories(limit=100)[0]


def test_environment_registry_rejects_an_unconfigured_selection(tmp_path):
    local = harness(tmp_path)

    class Unregistered(SyntheticEnvironment):
        pass

    with pytest.raises(ValueError, match="was not supplied to this harness"):
        local.run(Scenario(id="unregistered", input={}), turns=1, environment=Unregistered)


def test_environment_argument_accepts_a_class_a_sequence_or_nothing(tmp_path):
    with pytest.raises(ValueError, match="at least one environment is required"):
        EnvironmentHarness(tmp_path, agents={"alice": SyntheticAgent})
    with pytest.raises(TypeError, match="not an instance"):
        EnvironmentHarness(tmp_path, environment=SyntheticEnvironment(), agents={"alice": SyntheticAgent})


def test_a_legacy_session_row_without_a_reference_falls_back_to_the_default(tmp_path):
    """Rows written before the reference columns still resolve to the one configured factory."""

    local = harness(tmp_path)
    session = local.run(Scenario(id="legacy", input={}), turns=1)
    with local.store.transaction() as db:
        db.execute(
            "UPDATE session_runs SET environment_id=NULL,environment_version=NULL,spec_digest=NULL "
            "WHERE environment=?",
            (session.id,),
        )
    assert local._session_factory(session.id) is SyntheticEnvironment


def test_a_session_whose_factory_is_not_configured_fails_before_running(tmp_path):
    local = harness(tmp_path)
    session = local.run(Scenario(id="mismatch", input={}), turns=1)
    with local.store.transaction() as db:
        db.execute(
            "UPDATE session_runs SET environment_id=?,environment_version=?,spec_digest=? "
            "WHERE environment=?",
            ("other-environment", "9", "f" * 64, session.id),
        )
    with pytest.raises(Conflict, match="no configured environment reproduces"):
        local._session_factory(session.id)


def test_branching_a_grouped_session_refreshes_its_experiment(tmp_path):
    local = harness(tmp_path)
    result = local.experiment("branchable", [Scenario(id="only", input={})], trials=1, turns=1).run()
    parent = result.sessions[0]
    child = parent.branch(BranchRequest(checkpoint=parent.checkpoint()["id"]))
    assert child.record()["parent"] == parent.id
    assert local.hierarchy()["experiments"]

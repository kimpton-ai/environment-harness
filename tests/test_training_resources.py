import json
from types import SimpleNamespace

import pytest
from _credentials import bearer

from environment_harness import AgentSpec, EvidenceStore, ExperimentSpec
from environment_harness.access import _AccessContext
from environment_harness.contracts import RunPolicy
from environment_harness.errors import Conflict, Forbidden
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.runner import run
from environment_harness.runtime import _SessionRuntime
from environment_harness.server import create_app
from environment_harness.training import (
    TrainingOutput,
    TrainingRepository,
    TrajectoryDataset,
)
from environment_harness.trajectories import (
    SourceRecord,
    SourceRegistration,
    SourceStatusUpdate,
    TrajectoryRepository,
)


def completed_session(store, who, *, purpose="training", split="training"):
    environment = SyntheticEnvironment()
    session = _SessionRuntime(store, environment)
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(
                AgentSpec(id="alice", implementation=SyntheticAgent.implementation, policy_version="1"),
            ),
            purpose=purpose,
            split=split,
            policy=RunPolicy(max_turns=1),
        ),
        who,
    )["id"]
    run(session, environment_id, who, {"alice": SyntheticAgent()}, turns=1)
    return environment_id


def test_dataset_freezes_training_trajectories_and_streams_reproducible_export(tmp_path):
    store = EvidenceStore(tmp_path)
    who = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment = completed_session(store, who)
    repository = TrainingRepository(store)

    dataset = repository.freeze_dataset("synthetic-training", (environment,), who)
    before = list(repository.export_dataset(dataset.metadata.id, who))
    store.artifact(environment, who, b"later", audience=("*",))

    assert isinstance(dataset, TrajectoryDataset)
    assert dataset.spec.members[0].trajectory_id == "trajectory-" + environment
    assert dataset.status.record_count > 0
    assert dataset.status.reward_state == "ready"
    assert list(repository.export_dataset(dataset.metadata.id, who)) == before
    with store.transaction() as database:
        stored = json.loads(
            database.execute(
                "SELECT body FROM trajectory_datasets WHERE id=?", (dataset.metadata.id,)
            ).fetchone()[0]
        )
    assert stored["status"]["datasetDigest"] == dataset.status.dataset_digest


def test_dataset_rejects_heldout_evaluation_even_when_snapshot_export_is_allowed(tmp_path):
    store = EvidenceStore(tmp_path)
    who = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment = completed_session(store, who, purpose="evaluation", split="heldout")

    with pytest.raises(Forbidden, match="training-entitled"):
        TrainingRepository(store).freeze_dataset("not-training", (environment,), who)


def imported_training_trajectory(store, who, rewards):
    repository = TrajectoryRepository(store)
    source = repository.register_source(
        SourceRegistration(
            namespace="com.example.training",
            run_id="run-" + str(len(rewards)),
            schema_version="example.trace.v1",
            environment={"id": "example", "version": "1"},
            participants=("alice",),
            purpose="training",
            split="training",
        ),
        who,
    )
    previous = "0" * 64
    records = []
    for index, (record_id, data) in enumerate(rewards, 1):
        record = SourceRecord.create(
            id=record_id,
            position=str(index),
            previous_hash=previous,
            type="environment.reward",
            segment="segment-1",
            participant="alice",
            revision=index,
            time={"wallTime": f"2026-09-22T15:00:0{index}Z", "native": []},
            data=data,
            audience=("*",),
        )
        records.append(record)
        previous = record.source_hash
    repository.ingest(source.id, tuple(records), who)
    repository.update_source_status(
        source.id,
        SourceStatusUpdate(
            collection_state="complete",
            execution_state="completed",
            termination={"terminated": True, "truncated": False, "reason": "complete"},
            verified_outcome={"state": "success", "evidence": (records[-1].id,)},
            terminal_position=records[-1].position,
            terminal_hash=records[-1].source_hash,
            backlog=0,
        ),
        who,
    )
    return source.id


def test_dataset_resolves_delayed_reward_supersession_chains(tmp_path):
    store = EvidenceStore(tmp_path)
    who = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    source = imported_training_trajectory(
        store,
        who,
        (
            ("reward-provisional", {"metric": "task", "value": 0.25, "state": "provisional"}),
            (
                "reward-final",
                {
                    "metric": "task",
                    "value": 1.0,
                    "state": "final",
                    "supersedes": "reward-provisional",
                },
            ),
        ),
    )

    dataset = TrainingRepository(store).freeze_dataset("resolved-rewards", (source,), who)

    assert dataset.status.reward_state == "ready"


def test_dataset_rejects_cyclic_reward_supersession(tmp_path):
    store = EvidenceStore(tmp_path)
    who = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    source = imported_training_trajectory(
        store,
        who,
        (
            ("reward-a", {"metric": "task", "value": 0.25, "supersedes": "reward-b"}),
            ("reward-b", {"metric": "task", "value": 1.0, "supersedes": "reward-a"}),
        ),
    )

    with pytest.raises(Conflict, match="reward chain"):
        TrainingRepository(store).freeze_dataset("cyclic-rewards", (source,), who)


def test_training_integration_records_immutable_result_without_serializing_live_instance(tmp_path):
    store = EvidenceStore(tmp_path)
    who = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment = completed_session(store, who)
    repository = TrainingRepository(store)
    dataset = repository.freeze_dataset("synthetic-training", (environment,), who)

    class Integration:
        identity = "com.example.synthetic-trainer"
        version = "1"

        def __init__(self):
            self.credential = "must-not-be-persisted"

        def validate(self, selected):
            assert selected.metadata.id == dataset.metadata.id

        def train(self, selected, config):
            assert config == {"epochs": 1}
            return TrainingOutput(
                policy={
                    "apiVersion": "environmentharness.dev/v1alpha1",
                    "kind": "Policy",
                    "metadata": {
                        "id": "policy-trained",
                        "createdAt": "2026-09-22T15:00:00Z",
                        "labels": {},
                    },
                    "features": {"required": [], "optional": []},
                    "spec": {
                        "implementation": "synthetic-trained",
                        "version": "2",
                        "lineage": ["policy-alice"],
                        "artifact": None,
                    },
                    "status": {"digest": "a" * 64},
                    "extensions": {},
                },
                metrics={"loss": 0.25},
                artifacts=(),
                limitations=("synthetic only",),
            )

    recorded = repository.run(dataset.metadata.id, Integration(), {"epochs": 1}, who)
    fetched = repository.get_run(recorded.metadata.id, who)

    assert fetched == recorded
    assert fetched.status.state == "completed"
    assert fetched.status.policy.metadata.id == "policy-trained"
    with store.transaction() as database:
        body = database.execute(
            "SELECT body FROM training_runs WHERE id=?", (recorded.metadata.id,)
        ).fetchone()[0]
    assert "must-not-be-persisted" not in body
    assert '"epochs"' not in body


def test_http_can_freeze_and_read_datasets_but_exposes_no_training_execution_route(tmp_path):
    from fastapi.testclient import TestClient

    store = EvidenceStore(tmp_path)
    who = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment_id = completed_session(store, who)
    session = _SessionRuntime(store, SyntheticEnvironment())
    client = TestClient(create_app(session), base_url="http://testserver")
    headers = {"Authorization": "Bearer " + bearer(store, who)}

    created = client.post(
        "/v1/datasets",
        headers=headers,
        json={"name": "api-dataset", "trajectories": [environment_id]},
    )
    dataset_id = created.json()["metadata"]["id"]
    fetched = client.get(f"/v1/datasets/{dataset_id}", headers=headers)
    listed = client.get("/v1/datasets", headers=headers)
    exported = client.get(
        f"/v1/datasets/{dataset_id}/records",
        headers=headers | {"Accept": "application/x-ndjson"},
    )
    runs = client.get(f"/v1/training-runs?dataset={dataset_id}", headers=headers)
    missing_run = client.get("/v1/training-runs/missing", headers=headers)

    assert created.status_code == 201
    assert created.headers["location"] == f"/v1/datasets/{dataset_id}"
    assert fetched.json() == created.json()
    assert listed.json()["items"] == [created.json()]
    assert exported.headers["content-type"].startswith("application/x-ndjson")
    assert runs.json()["items"] == []
    assert missing_run.status_code == 403
    assert client.post("/v1/training-runs", headers=headers, json={}).status_code == 405
    assert client.post("/v1/training-runs/not-executable", headers=headers, json={}).status_code == 405


def test_dataset_authority_and_lifecycle_rejection_paths(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path)
    who = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    # A participant credential is the negative case that survives the removal
    # of the four-role model: it can only observe and act inside its session.
    read_only = _AccessContext(
        tenant="tenant",
        subject="alice",
        policy="participant",
        session="0" * 32,
        participant="alice",
    )
    environment = completed_session(store, who)
    trajectory_repository = TrajectoryRepository(store)
    trajectory = trajectory_repository.get(environment, who)
    snapshot = trajectory_repository.freeze(environment, who)
    repository = TrainingRepository(store)

    with pytest.raises(Forbidden, match="policy denies"):
        repository.freeze_dataset("denied", (environment,), read_only)
    with pytest.raises(ValueError, match="name and trajectories"):
        repository.freeze_dataset("", (), who)

    invalid = (
        (
            trajectory.model_copy(
                update={
                    "status": trajectory.status.model_copy(
                        update={
                            "collection": trajectory.status.collection.model_copy(update={"state": "current"})
                        }
                    )
                }
            ),
            "complete evidence",
        ),
        (
            trajectory.model_copy(
                update={
                    "status": trajectory.status.model_copy(
                        update={
                            "execution": trajectory.status.execution.model_copy(update={"state": "active"})
                        }
                    )
                }
            ),
            "completed execution",
        ),
        (
            trajectory.model_copy(
                update={
                    "status": trajectory.status.model_copy(
                        update={
                            "termination": trajectory.status.termination.model_copy(
                                update={"terminated": False, "truncated": False}
                            )
                        }
                    )
                }
            ),
            "terminal trajectory",
        ),
        (
            trajectory.model_copy(
                update={
                    "status": trajectory.status.model_copy(
                        update={
                            "verified_outcome": trajectory.status.verified_outcome.model_copy(
                                update={"state": "uncertain"}
                            )
                        }
                    )
                }
            ),
            "resolved outcomes",
        ),
    )
    for selected, message in invalid:
        monkeypatch.setattr(repository.trajectories, "get", lambda *_args, selected=selected: selected)
        with pytest.raises(Conflict, match=message):
            repository.freeze_dataset("invalid", (environment,), who)

    incompatible = trajectory.model_copy(
        update={
            "spec": trajectory.spec.model_copy(
                update={
                    "manifest": trajectory.spec.manifest.model_copy(
                        update={
                            "source": trajectory.spec.manifest.source.model_copy(
                                update={"schema_version": "incompatible.v2"}
                            )
                        }
                    )
                }
            )
        }
    )
    monkeypatch.setattr(
        repository.trajectories,
        "get",
        lambda identity, _who: trajectory if identity == "first" else incompatible,
    )
    monkeypatch.setattr(repository.trajectories, "freeze", lambda *_args: snapshot)
    monkeypatch.setattr(repository.trajectories, "stream_records", lambda *_args, **_kwargs: iter(()))
    with pytest.raises(Conflict, match="incompatible trajectory schemas"):
        repository.freeze_dataset("mixed", ("first", "second"), who)


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ([SimpleNamespace(id="reward", type="environment.reward", data=[])], "malformed"),
        (
            [
                SimpleNamespace(
                    id="reward", type="environment.reward", data={"value": 1, "supersedes": "missing"}
                )
            ],
            "incomplete",
        ),
        (
            [
                SimpleNamespace(id="a", type="environment.reward", data={"value": 1}),
                SimpleNamespace(id="b", type="environment.reward", data={"value": 2, "supersedes": "a"}),
                SimpleNamespace(id="c", type="environment.reward", data={"value": 3, "supersedes": "a"}),
            ],
            "ambiguous",
        ),
        (
            [SimpleNamespace(id="action", type="environment.outcome", data={"reward": float("nan")})],
            "non-finite",
        ),
        (
            [
                SimpleNamespace(
                    id="reward", type="environment.reward", data={"value": 1, "state": "retracted"}
                )
            ],
            "unresolved",
        ),
        (
            [SimpleNamespace(id="reward", type="environment.reward", data={"value": float("inf")})],
            "non-finite",
        ),
    ],
)
def test_reward_resolution_rejects_malformed_or_unresolved_values(records, message):
    with pytest.raises(Conflict, match=message):
        TrainingRepository._has_resolved_reward(records)


def test_dataset_and_training_run_lookup_listing_and_conflict_edges(tmp_path, monkeypatch):
    from environment_harness import training as training_module

    store = EvidenceStore(tmp_path)
    who = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    # A participant credential is the negative case that survives the removal
    # of the four-role model: it can only observe and act inside its session.
    read_only = _AccessContext(
        tenant="tenant",
        subject="alice",
        policy="participant",
        session="0" * 32,
        participant="alice",
    )
    environment = completed_session(store, who)
    repository = TrainingRepository(store)
    monkeypatch.setattr(training_module, "_now", lambda: "2026-09-22T15:00:00Z")
    dataset = repository.freeze_dataset("edge-dataset", (environment,), who)

    with pytest.raises(Forbidden, match="policy denies"):
        repository.get_dataset(dataset.metadata.id, read_only)
    with pytest.raises(Forbidden, match="dataset unavailable"):
        repository.get_dataset("missing", who)
    with pytest.raises(Forbidden, match="policy denies"):
        repository.list_datasets(read_only)
    with pytest.raises(ValueError, match="dataset page size"):
        repository.list_datasets(who, limit=0)
    assert repository.list_datasets(who) == (dataset,)

    with store.transaction() as database:
        database.execute("DROP TRIGGER trajectory_datasets_no_update")
        database.execute("UPDATE trajectory_datasets SET body='{}' WHERE id=?", (dataset.metadata.id,))
    with pytest.raises(Conflict, match="dataset identity"):
        repository.freeze_dataset("edge-dataset", (environment,), who)

    class Integration:
        identity = "com.example.mapping-trainer"
        version = "1"

        def validate(self, _selected):
            return None

        def train(self, _selected, _config):
            return {
                "policy": {
                    "apiVersion": "environmentharness.dev/v1alpha1",
                    "kind": "Policy",
                    "metadata": {"id": "policy-edge", "createdAt": "now", "labels": {}},
                    "features": {"required": [], "optional": []},
                    "spec": {"implementation": "edge", "version": "1"},
                    "status": {"digest": "a" * 64},
                    "extensions": {},
                },
                "metrics": {"loss": 0},
            }

    clean_store = EvidenceStore(tmp_path / "runs")
    clean_environment = completed_session(clean_store, who)
    runs = TrainingRepository(clean_store)
    selected = runs.freeze_dataset("runs", (clean_environment,), who)
    with pytest.raises(Forbidden, match="policy denies"):
        runs.run(selected.metadata.id, Integration(), {}, read_only)
    recorded = runs.run(selected.metadata.id, Integration(), {}, who)
    assert runs.list_runs(who) == (recorded,)
    assert runs.list_runs(who, dataset=selected.metadata.id) == (recorded,)
    with pytest.raises(Forbidden, match="policy denies"):
        runs.get_run(recorded.metadata.id, read_only)
    with pytest.raises(Forbidden, match="training run unavailable"):
        runs.get_run("missing", who)
    with pytest.raises(Forbidden, match="policy denies"):
        runs.list_runs(read_only)
    with pytest.raises(ValueError, match="training-run page size"):
        runs.list_runs(who, limit=0)
    with clean_store.transaction() as database:
        database.execute("DROP TRIGGER training_runs_no_update")
        database.execute("UPDATE training_runs SET body='{}' WHERE id=?", (recorded.metadata.id,))
    with pytest.raises(Conflict, match="training-run identity"):
        runs.run(selected.metadata.id, Integration(), {}, who)

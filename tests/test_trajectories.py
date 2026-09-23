import json

import pytest

from environment_harness import (
    AgentSpec,
    EnvironmentSession,
    EvidenceStore,
    ExperimentSpec,
    Policy,
    Principal,
    ResourceRegistry,
    Trajectory,
    TrajectoryRepository,
    TrajectorySnapshot,
)
from environment_harness.errors import Conflict, Forbidden, Unsupported
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.store import digest, encode
from environment_harness.trajectories import (
    API_VERSION,
    SourceRecord,
    SourceRegistration,
    SourceStatusUpdate,
    TrajectoryRecord,
    TrajectorySegment,
)


def test_trajectory_round_trips_one_segment_and_causal_record():
    payload = {
        "apiVersion": "environmentharness.dev/v1alpha1",
        "kind": "Trajectory",
        "metadata": {
            "id": "trajectory-1",
            "createdAt": "2026-09-22T15:00:00Z",
            "labels": {"fixture": "synthetic"},
        },
        "features": {"required": [], "optional": []},
        "spec": {
            "manifest": {
                "environment": {"id": "synthetic", "version": "1"},
                "source": {
                    "namespace": "environment-harness",
                    "runId": "session-1",
                    "schemaVersion": "environment-session.v1",
                },
                "participants": ["alice"],
                "purpose": "evaluation",
            }
        },
        "status": {
            "segments": [
                {
                    "id": "segment-1",
                    "kind": "execution",
                    "sequenceStart": 1,
                    "sequenceEnd": 1,
                    "collection": {"state": "complete"},
                    "execution": {"state": "completed"},
                }
            ],
            "records": [
                {
                    "type": "environment.observation",
                    "id": "record-1",
                    "sequence": 1,
                    "segment": "segment-1",
                    "participant": "alice",
                    "revision": 0,
                    "causes": [],
                    "time": {
                        "wallTime": "2026-09-22T15:00:00Z",
                        "native": [{"clock": "environment.tick", "value": 18}],
                    },
                    "data": {"body": {"position": [1, 2]}},
                    "extensions": {},
                }
            ],
            "collection": {"state": "complete"},
            "execution": {"state": "completed"},
            "termination": {"terminated": True, "truncated": False, "reason": "goal"},
            "verifiedOutcome": {"state": "success", "evidence": ["record-1"]},
            "evidenceHead": "a" * 64,
            "trajectoryDigest": "b" * 64,
        },
        "extensions": {},
    }

    trajectory = Trajectory.model_validate(payload)

    assert trajectory.model_dump(mode="json", by_alias=True) == payload


def test_policy_resource_round_trips_and_native_manifest_synthesizes_legacy_policy(tmp_path):
    policy_payload = {
        "apiVersion": "environmentharness.dev/v1alpha1",
        "kind": "Policy",
        "metadata": {
            "id": "policy-alice-1",
            "createdAt": "2026-09-22T15:00:00Z",
            "labels": {},
        },
        "features": {"required": [], "optional": []},
        "spec": {
            "implementation": "synthetic",
            "version": "1",
            "lineage": [],
            "artifact": None,
        },
        "status": {"digest": "a" * 64},
        "extensions": {},
    }
    assert Policy.model_validate(policy_payload).model_dump(mode="json", by_alias=True) == policy_payload

    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    environment_id = EnvironmentSession(store, environment).create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="legacy-v1"),),
        ),
        researcher,
    )["id"]

    projected = TrajectoryRepository(store).get(environment_id, researcher)

    assert projected.spec.manifest.policies[0].participant == "alice"
    assert projected.spec.manifest.policies[0].version == "legacy-v1"
    assert projected.spec.manifest.policies[0].implementation == "synthetic"


def test_snapshot_freezes_source_score_and_artifact_boundaries():
    payload = {
        "apiVersion": "environmentharness.dev/v1alpha1",
        "kind": "TrajectorySnapshot",
        "metadata": {
            "id": "snapshot-1",
            "createdAt": "2026-09-22T15:05:00Z",
            "labels": {},
        },
        "features": {"required": [], "optional": []},
        "spec": {
            "trajectoryId": "trajectory-1",
            "trajectoryDigest": "b" * 64,
            "sourceBoundary": {"position": "41", "hash": "c" * 64},
            "evidenceHead": "a" * 64,
            "sequenceStart": 1,
            "sequenceEnd": 41,
            "scoreReports": [{"scorer": "synthetic", "revision": 7, "hash": "d" * 64}],
            "artifacts": [{"id": "frame-1", "sha256": "e" * 64}],
            "audience": ["researcher"],
        },
        "status": {
            "recordCount": 41,
            "artifactCount": 1,
            "manifestDigest": "f" * 64,
            "recordsDigest": "1" * 64,
            "snapshotDigest": "2" * 64,
            "complete": True,
        },
        "extensions": {},
    }

    snapshot = TrajectorySnapshot.model_validate(payload)

    assert snapshot.model_dump(mode="json", by_alias=True) == payload


def test_repository_projects_existing_evidence_without_creating_another_journal(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, environment)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    spec = ExperimentSpec(
        environment=environment.spec,
        participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
    )
    environment_id = session.create(spec, researcher)["id"]
    session.observe(environment_id, researcher, "alice")

    trajectory = TrajectoryRepository(store).get(environment_id, researcher)

    assert trajectory.spec.manifest.source.run_id == environment_id
    assert [record.type for record in trajectory.status.records] == [
        "session.created",
        "observation.delivered",
    ]
    assert trajectory.status.records[1].causes == (trajectory.status.records[0].id,)
    assert trajectory.status.evidence_head == list(store.replay(environment_id, researcher))[-1]["hash"]


def test_native_resume_creates_a_continuation_segment_without_breaking_causality(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, environment)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
        ),
        researcher,
    )["id"]
    lease = session.lease(environment_id, researcher, "test")
    session.control(environment_id, researcher, lease, "pause")
    session.resume(environment_id, researcher, lease)
    session.release(environment_id, researcher, lease)

    trajectory = TrajectoryRepository(store).get(environment_id, researcher)

    assert [(segment.id, segment.kind) for segment in trajectory.status.segments] == [
        ("segment-1", "execution"),
        ("segment-2", "continuation"),
    ]
    resumed = next(record for record in trajectory.status.records if record.type == "session.resumed")
    assert resumed.segment == "segment-2"
    assert resumed.causes == (trajectory.status.records[-2].id,)


def test_frozen_snapshot_does_not_change_when_new_evidence_is_appended(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, environment)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    spec = ExperimentSpec(
        environment=environment.spec,
        participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
    )
    environment_id = session.create(spec, researcher)["id"]
    session.observe(environment_id, researcher, "alice")
    repository = TrajectoryRepository(store)

    frozen = repository.freeze(environment_id, researcher)
    exported_before = list(repository.export_snapshot(frozen.metadata.id, researcher))
    store.artifact(environment_id, researcher, b"later evidence", audience=("*",))

    assert repository.get_snapshot(frozen.metadata.id, researcher) == frozen
    assert list(repository.export_snapshot(frozen.metadata.id, researcher)) == exported_before
    with store.transaction() as database:
        body = database.execute(
            "SELECT body FROM trajectory_snapshots WHERE id=?", (frozen.metadata.id,)
        ).fetchone()[0]
        record_count = database.execute(
            "SELECT count(*) FROM trajectory_snapshot_records WHERE snapshot=?",
            (frozen.metadata.id,),
        ).fetchone()[0]
    assert "records" not in json.loads(body)
    assert record_count == frozen.status.record_count


def test_source_registration_is_idempotent_and_conflicting_metadata_fails(tmp_path):
    repository = TrajectoryRepository(EvidenceStore(tmp_path))
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    registration = SourceRegistration(
        namespace="com.example.simulator",
        run_id="run-7",
        schema_version="simulator.trace.v3",
        environment={"id": "simulator", "version": "3"},
        participants=("alice", "bob"),
        purpose="evaluation",
    )

    first = repository.register_source(registration, researcher)

    assert repository.register_source(registration, researcher) == first
    with pytest.raises(Conflict, match="different metadata"):
        repository.register_source(
            registration.model_copy(update={"environment": {"id": "simulator", "version": "4"}}),
            researcher,
        )


def test_source_registration_requires_a_stable_namespaced_identity():
    with pytest.raises(ValueError, match="namespace"):
        SourceRegistration(
            namespace="local simulator",
            run_id="run-7",
            schema_version="simulator.trace.v3",
            environment={"id": "simulator", "version": "3"},
            participants=("alice",),
            purpose="evaluation",
        )


def test_registered_source_status_is_inspectable_before_any_records_arrive(tmp_path):
    repository = TrajectoryRepository(EvidenceStore(tmp_path))
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    receipt = repository.register_source(
        SourceRegistration(
            namespace="com.example.simulator",
            run_id="run-empty",
            schema_version="simulator.trace.v3",
            environment={"id": "simulator", "version": "3"},
            participants=("alice",),
            purpose="evaluation",
        ),
        researcher,
    )

    status = repository.source_status(receipt.id, researcher)

    assert status.source == receipt.id
    assert status.collection_state == "registered"
    assert status.execution_state == "unknown"
    assert status.acknowledged_position is None
    assert status.acknowledged_hash is None
    assert status.gaps == ()


def test_ingestion_retries_identically_and_rejects_conflicts_or_broken_chains(tmp_path):
    repository = TrajectoryRepository(EvidenceStore(tmp_path))
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    source = repository.register_source(
        SourceRegistration(
            namespace="com.example.simulator",
            run_id="run-7",
            schema_version="simulator.trace.v3",
            environment={"id": "simulator", "version": "3"},
            participants=("alice",),
            purpose="evaluation",
        ),
        researcher,
    )
    first = SourceRecord.create(
        id="source-record-1",
        position="1",
        previous_hash="0" * 64,
        type="com.example.simulator.observation",
        segment="segment-1",
        participant="alice",
        revision=0,
        time={
            "wallTime": "2026-09-22T15:00:00Z",
            "native": [{"clock": "simulator.frame", "value": 10}],
        },
        data={"position": [1, 2]},
        audience=("alice",),
    )

    acknowledged = repository.ingest(source.id, (first,), researcher)

    assert repository.ingest(source.id, (first,), researcher) == acknowledged
    assert acknowledged.position == "1"
    assert acknowledged.hash == first.source_hash
    with pytest.raises(Conflict, match="different content"):
        repository.ingest(
            source.id,
            (first.model_copy(update={"data": {"position": [9, 9]}}),),
            researcher,
        )
    with pytest.raises(Conflict, match="source chain"):
        repository.ingest(
            source.id,
            (
                SourceRecord.create(
                    id="source-record-2",
                    position="2",
                    previous_hash="0" * 64,
                    type="com.example.simulator.observation",
                    segment="segment-1",
                    participant="alice",
                    revision=1,
                    time={
                        "wallTime": "2026-09-22T15:00:01Z",
                        "native": [{"clock": "simulator.frame", "value": 11}],
                    },
                    data={"position": [2, 3]},
                    audience=("alice",),
                ),
            ),
            researcher,
        )


def test_imported_source_uses_the_same_trajectory_interface_and_preserves_authority(tmp_path):
    repository = TrajectoryRepository(EvidenceStore(tmp_path))
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    source = repository.register_source(
        SourceRegistration(
            namespace="com.example.simulator",
            run_id="run-8",
            schema_version="simulator.trace.v3",
            environment={"id": "simulator", "version": "3"},
            participants=("alice",),
            purpose="evaluation",
        ),
        researcher,
    )
    record = SourceRecord.create(
        id="source-record-1",
        position="frame-10",
        previous_hash="0" * 64,
        type="com.example.simulator.observation",
        segment="segment-1",
        participant="alice",
        revision=0,
        time={
            "wallTime": "2026-09-22T15:00:00Z",
            "native": [{"clock": "simulator.frame", "value": 10}],
        },
        data={"position": [1, 2]},
        audience=("alice",),
    )
    repository.ingest(source.id, (record,), researcher)

    trajectory = repository.get(source.id, researcher)

    assert trajectory.spec.manifest.source.namespace == "com.example.simulator"
    assert trajectory.status.records[0].type == "com.example.simulator.observation"
    assert trajectory.status.records[0].time.native[0].value == 10
    assert trajectory.status.collection.state == "current"
    assert trajectory.status.execution.state == "unknown"
    assert trajectory.status.verified_outcome.state == "unavailable"


def test_collection_completion_requires_terminal_acknowledgement_and_no_gaps(tmp_path):
    repository = TrajectoryRepository(EvidenceStore(tmp_path))
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    source = repository.register_source(
        SourceRegistration(
            namespace="com.example.simulator",
            run_id="run-9",
            schema_version="simulator.trace.v3",
            environment={"id": "simulator", "version": "3"},
            participants=("alice",),
            purpose="evaluation",
        ),
        researcher,
    )
    record = SourceRecord.create(
        id="source-record-1",
        position="frame-10",
        previous_hash="0" * 64,
        type="com.example.simulator.outcome",
        segment="segment-1",
        participant="alice",
        revision=1,
        time={
            "wallTime": "2026-09-22T15:00:01Z",
            "native": [{"clock": "simulator.frame", "value": 10}],
        },
        data={"result": "success"},
        audience=("*",),
    )
    repository.ingest(source.id, (record,), researcher)

    with pytest.raises(Conflict, match="unresolved gaps"):
        repository.update_source_status(
            source.id,
            SourceStatusUpdate(
                collection_state="complete",
                execution_state="completed",
                termination={"terminated": True, "truncated": False, "reason": "goal"},
                verified_outcome={"state": "success", "evidence": ("source-record-1",)},
                terminal_position="frame-10",
                terminal_hash=record.source_hash,
                backlog=0,
                gaps=("frame-4:frame-6",),
                capture_failures=(),
            ),
            researcher,
        )
    repository.update_source_status(
        source.id,
        SourceStatusUpdate(
            collection_state="complete",
            execution_state="completed",
            termination={"terminated": True, "truncated": False, "reason": "goal"},
            verified_outcome={"state": "success", "evidence": ("source-record-1",)},
            terminal_position="frame-10",
            terminal_hash=record.source_hash,
            backlog=0,
            gaps=(),
            capture_failures=(),
        ),
        researcher,
    )

    trajectory = repository.get(source.id, researcher)
    assert trajectory.status.collection.state == "complete"
    assert trajectory.status.execution.state == "completed"
    assert trajectory.status.termination.terminated is True
    assert trajectory.status.verified_outcome.state == "success"


def test_imported_collection_health_preserves_backlog_gaps_and_acknowledged_boundary(tmp_path):
    repository = TrajectoryRepository(EvidenceStore(tmp_path))
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    source = repository.register_source(
        SourceRegistration(
            namespace="com.example.simulator",
            run_id="run-health",
            schema_version="simulator.trace.v3",
            environment={"id": "simulator", "version": "3"},
            participants=("alice",),
            purpose="evaluation",
        ),
        researcher,
    )
    record = SourceRecord.create(
        id="record-health",
        position="frame-4",
        previous_hash="0" * 64,
        type="com.example.simulator.observation",
        segment="segment-1",
        participant="alice",
        revision=0,
        time={"wallTime": "2026-09-22T15:00:00Z", "native": []},
        data={},
        audience=("alice",),
    )
    repository.ingest(source.id, (record,), researcher)
    repository.update_source_status(
        source.id,
        SourceStatusUpdate(
            collection_state="lagging",
            execution_state="active",
            termination={"terminated": False, "truncated": False, "reason": "running"},
            verified_outcome={"state": "pending", "evidence": ()},
            terminal_position="frame-9",
            terminal_hash="f" * 64,
            backlog=5,
            gaps=("frame-2:frame-3",),
            capture_failures=("telemetry-timeout",),
        ),
        researcher,
    )

    collection = repository.get(source.id, researcher).status.collection
    extras = collection.model_extra or {}

    assert extras["acknowledgedPosition"] == "frame-4"
    assert extras["acknowledgedHash"] == record.source_hash
    assert extras["backlog"] == 5
    assert extras["gaps"] == ["frame-2:frame-3"]
    assert extras["captureFailures"] == ["telemetry-timeout"]


def test_registry_decodes_built_in_resources_and_rejects_duplicate_ownership(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, environment)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
        ),
        researcher,
    )["id"]
    snapshot = TrajectoryRepository(store).freeze(environment_id, researcher)
    registry = ResourceRegistry()

    assert registry.decode(snapshot.model_dump(mode="json", by_alias=True)) == snapshot
    with pytest.raises(Conflict, match="already registered"):
        registry.register(
            "environmentharness.dev/v1alpha1",
            "Trajectory",
            Trajectory,
        )


def test_unified_trajectory_index_pages_native_and_imported_runs(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, environment)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    native_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
        ),
        researcher,
        environment_id="f" * 32,
    )["id"]
    repository = TrajectoryRepository(store)
    imported = repository.register_source(
        SourceRegistration(
            namespace="com.example.simulator",
            run_id="run-index",
            schema_version="simulator.trace.v3",
            environment={"id": "simulator", "version": "3"},
            participants=("alice",),
            purpose="evaluation",
        ),
        researcher,
    )

    first, cursor = repository.list_page(researcher, limit=1)
    second, final_cursor = repository.list_page(researcher, limit=1, cursor=cursor)

    assert {first[0].id, second[0].id} == {native_id, imported.id}
    assert {first[0].origin, second[0].origin} == {"native", "imported"}
    assert final_cursor is None


def test_imported_trajectory_can_be_frozen_and_reexported_without_native_environment(tmp_path):
    store = EvidenceStore(tmp_path)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    repository = TrajectoryRepository(store)
    source = repository.register_source(
        SourceRegistration(
            namespace="com.example.simulator",
            run_id="run-snapshot",
            schema_version="simulator.trace.v3",
            environment={"id": "simulator", "version": "3"},
            participants=("alice",),
            purpose="evaluation",
        ),
        researcher,
    )
    record = SourceRecord.create(
        id="external-1",
        position="offset-1",
        previous_hash="0" * 64,
        type="com.example.simulator.observation",
        segment="segment-1",
        participant="alice",
        revision=0,
        time={"wallTime": "2026-09-22T15:00:00Z", "native": []},
        data={"position": [1, 2]},
        audience=("alice",),
    )
    repository.ingest(source.id, (record,), researcher)

    frozen = repository.freeze(source.id, researcher)
    exported = list(repository.export_snapshot(frozen.metadata.id, researcher))

    assert frozen.spec.trajectory_id == "trajectory-" + source.id
    assert frozen.spec.source_boundary.position == "offset-1"
    assert exported[0]["snapshot"]["metadata"]["id"] == frozen.metadata.id
    assert exported[1]["id"] == "external-1"
    with store.transaction() as database:
        created = database.execute(
            "SELECT created FROM trajectory_snapshots WHERE id=?", (frozen.metadata.id,)
        ).fetchone()[0]
    assert isinstance(created, float)


def test_registry_preserves_unknown_namespaced_records_and_rejects_invalid_types():
    registry = ResourceRegistry()
    raw = {
        "type": "com.example.drone.telemetry",
        "id": "telemetry-1",
        "sequence": 1,
        "segment": "flight-1",
        "participant": None,
        "revision": 0,
        "causes": [],
        "time": {"wallTime": "2026-09-22T15:00:00Z", "native": []},
        "data": {"rotors": [{"rpm": 1500, "future": {"unit": "rpm"}}]},
        "extensions": {"com.example/quality": "simulated"},
        "futureField": {"nested": [1, 2, 3]},
    }

    decoded = registry.decode_record(raw)

    assert decoded.model_dump(mode="json", by_alias=True) == raw
    with pytest.raises(Unsupported, match="namespaced"):
        registry.decode_record(raw | {"type": "future.telemetry"})


def test_registry_rejects_duplicate_record_decoder_registration():
    registry = ResourceRegistry()
    registry.register_record("com.example.telemetry", TrajectoryRecord)

    with pytest.raises(Conflict, match="record contract already registered"):
        registry.register_record("com.example.telemetry", TrajectoryRecord)


def test_snapshot_listing_is_trajectory_scoped_and_tenant_isolated(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    environment_id = EnvironmentSession(store, environment).create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="legacy-v1"),),
        ),
        researcher,
    )["id"]
    session = EnvironmentSession(store, environment)
    session.observe(environment_id, researcher, "alice")
    repository = TrajectoryRepository(store)
    frozen = repository.freeze(environment_id, researcher)

    assert repository.list_snapshots(environment_id, researcher) == [frozen]
    assert repository.list_snapshots("missing", researcher) == []
    assert (
        repository.list_snapshots(
            environment_id,
            Principal(tenant="another-tenant", subject="researcher", role="researcher"),
        )
        == []
    )


def _portable_trajectory_payload():
    return {
        "apiVersion": API_VERSION,
        "kind": "Trajectory",
        "metadata": {"id": "trajectory-edge", "createdAt": "2026-09-22T15:00:00Z", "labels": {}},
        "features": {"required": [], "optional": []},
        "spec": {
            "manifest": {
                "environment": {"id": "synthetic"},
                "source": {
                    "namespace": "environment-harness",
                    "runId": "edge",
                    "schemaVersion": "environment-session.v1",
                },
                "participants": ["alice"],
                "purpose": "evaluation",
            }
        },
        "status": {
            "segments": [
                {
                    "id": "segment-1",
                    "kind": "execution",
                    "sequenceStart": 1,
                    "sequenceEnd": 2,
                    "collection": {"state": "complete"},
                    "execution": {"state": "completed"},
                }
            ],
            "records": [
                {
                    "type": "environment.observation",
                    "id": "record-1",
                    "sequence": 1,
                    "segment": "segment-1",
                    "participant": "alice",
                    "revision": 0,
                    "causes": [],
                    "time": {"wallTime": "2026-09-22T15:00:00Z", "native": []},
                    "data": {},
                    "extensions": {},
                },
                {
                    "type": "agent.action",
                    "id": "record-2",
                    "sequence": 2,
                    "segment": "segment-1",
                    "participant": "alice",
                    "revision": 0,
                    "causes": ["record-1"],
                    "time": {"wallTime": "2026-09-22T15:00:01Z", "native": []},
                    "data": {},
                    "extensions": {},
                },
            ],
            "collection": {"state": "complete"},
            "execution": {"state": "completed"},
            "termination": {"terminated": True, "truncated": False, "reason": "complete"},
            "verifiedOutcome": {"state": "success", "evidence": ["record-2"]},
            "evidenceHead": "a" * 64,
            "trajectoryDigest": "b" * 64,
        },
        "extensions": {},
    }


@pytest.mark.parametrize(
    ("purpose", "split", "message"),
    [
        ("training", "heldout", "training sources"),
        ("evaluation", "training", "evaluation sources"),
    ],
)
def test_source_registration_rejects_inconsistent_purpose_and_split(purpose, split, message):
    with pytest.raises(ValueError, match=message):
        SourceRegistration(
            namespace="com.example.edge",
            run_id="edge",
            schema_version="edge.v1",
            environment={},
            participants=(),
            purpose=purpose,
            split=split,
        )


def test_portable_resources_reject_invalid_boundaries_and_causal_references():
    with pytest.raises(ValueError, match="segment sequence"):
        TrajectorySegment(
            id="segment",
            kind="execution",
            sequenceStart=2,
            sequenceEnd=1,
            collection={"state": "complete"},
            execution={"state": "completed"},
        )

    mutations = (
        (lambda value: value["status"]["records"][1].update(id="record-1"), "duplicate"),
        (lambda value: value["status"]["records"][1].update(sequence=1), "increasing"),
        (lambda value: value["status"]["records"][1].update(segment="missing"), "unknown segment"),
        (lambda value: value["status"]["records"][1].update(causes=["future"]), "earlier record"),
        (
            lambda value: value["status"]["verifiedOutcome"].update(evidence=["missing"]),
            "unknown evidence",
        ),
    )
    for mutate, message in mutations:
        payload = json.loads(json.dumps(_portable_trajectory_payload()))
        mutate(payload)
        with pytest.raises(ValueError, match=message):
            Trajectory.model_validate(payload)

    snapshot = {
        "apiVersion": API_VERSION,
        "kind": "TrajectorySnapshot",
        "metadata": {"id": "snapshot-edge", "createdAt": "now", "labels": {}},
        "features": {"required": [], "optional": []},
        "spec": {
            "trajectoryId": "trajectory-edge",
            "trajectoryDigest": "a" * 64,
            "sourceBoundary": {"position": "2", "hash": "b" * 64},
            "evidenceHead": "b" * 64,
            "sequenceStart": 2,
            "sequenceEnd": 1,
            "scoreReports": [],
            "artifacts": [],
            "audience": ["researcher"],
        },
        "status": {
            "recordCount": 1,
            "artifactCount": 0,
            "manifestDigest": "c" * 64,
            "recordsDigest": "d" * 64,
            "snapshotDigest": "e" * 64,
            "complete": True,
        },
        "extensions": {},
    }
    with pytest.raises(ValueError, match="snapshot sequence"):
        TrajectorySnapshot.model_validate(snapshot)
    snapshot["spec"]["sequenceStart"] = 1
    snapshot["status"]["artifactCount"] = 1
    with pytest.raises(ValueError, match="artifact count"):
        TrajectorySnapshot.model_validate(snapshot)


def test_resource_registry_covers_success_and_rejection_paths():
    registry = ResourceRegistry()
    registry.register("com.example/v1", "Example", Policy)
    with pytest.raises(Unsupported, match="requires apiVersion"):
        registry.decode({})
    with pytest.raises(Unsupported, match="unsupported portable resource"):
        registry.decode({"apiVersion": "missing/v1", "kind": "Missing"})
    payload = _portable_trajectory_payload()
    payload["features"]["required"] = ["com.example/required"]
    with pytest.raises(Unsupported, match="required resource features"):
        registry.decode(payload)
    with pytest.raises(ValueError, match="reverse-domain"):
        registry.register_record("invalid", TrajectoryRecord)
    with pytest.raises(Unsupported, match="requires a type"):
        registry.decode_record({})

    custom = _portable_trajectory_payload()["status"]["records"][0]
    custom["type"] = "com.example.custom.record"
    registry.register_record(custom["type"], TrajectoryRecord)
    assert registry.decode_record(custom).type == custom["type"]
    custom["type"] = "agent.action"
    assert registry.decode_record(custom).type == "agent.action"


def _registered_source(repository, who, run_id="edge-source"):
    return repository.register_source(
        SourceRegistration(
            namespace="com.example.edge",
            run_id=run_id,
            schema_version="edge.v1",
            environment={"id": "edge"},
            participants=("alice",),
            purpose="evaluation",
        ),
        who,
    )


def _source_record(record_id, position, previous_hash, *, segment="segment-1"):
    return SourceRecord.create(
        id=record_id,
        position=position,
        previous_hash=previous_hash,
        type="com.example.edge.observation",
        segment=segment,
        participant="alice",
        revision=int(position),
        time={"wallTime": f"2026-09-22T15:00:0{position}Z", "native": []},
        data={"position": position},
        audience=("alice",),
    )


def test_repository_authority_paging_and_cursor_edges(tmp_path):
    store = EvidenceStore(tmp_path)
    repository = TrajectoryRepository(store)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    worker = Principal(tenant="tenant", subject="worker", role="worker")
    source = _registered_source(repository, researcher)
    first = _source_record("record-1", "1", "0" * 64)
    second = _source_record("record-2", "2", first.source_hash, segment="segment-2")
    repository.ingest(source.id, (first, second), researcher)

    with pytest.raises(Forbidden, match="index authority"):
        repository.list_page(worker)
    with pytest.raises(ValueError, match="page size"):
        repository.list_page(researcher, limit=0)
    with pytest.raises(ValueError, match="cursor"):
        repository.list_page(researcher, cursor="missing")
    with pytest.raises(Forbidden, match="registration authority"):
        repository.register_source(
            SourceRegistration(
                namespace="com.example.denied",
                run_id="denied",
                schema_version="edge.v1",
                environment={},
                participants=(),
                purpose="evaluation",
            ),
            worker,
        )
    with pytest.raises(Forbidden, match="record authority"):
        repository.records_page(source.id, worker)
    with pytest.raises(ValueError, match="record page"):
        repository.records_page(source.id, researcher, after=-1)
    with pytest.raises(Forbidden, match="trajectory unavailable"):
        repository.records_page("missing", researcher)

    first_page = repository.records_page(source.id, researcher, limit=1)
    second_page = repository.records_page(source.id, researcher, after=first_page.cursor, limit=1)

    assert first_page.has_more is True
    assert second_page.records[0].causes == (first_page.records[0].id,)
    assert second_page.records[0].segment == "segment-2"


def test_native_record_paging_tracks_resume_segments(tmp_path):
    store = EvidenceStore(tmp_path)
    environment = SyntheticEnvironment()
    session = EnvironmentSession(store, environment)
    who = Principal(tenant="tenant", subject="researcher", role="researcher")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
        ),
        who,
    )["id"]
    lease = session.lease(environment_id, who, "paging")
    session.control(environment_id, who, lease, "pause")
    session.resume(environment_id, who, lease)
    session.release(environment_id, who, lease)
    repository = TrajectoryRepository(store)
    all_records = repository.records_page(environment_id, who, limit=100).records
    resumed = next(record for record in all_records if record.type == "session.resumed")
    resumed_page = repository.records_page(environment_id, who, after=resumed.sequence - 1, limit=1)

    assert resumed_page.records[0].segment == "segment-2"


def test_ingestion_and_source_status_rejection_paths(tmp_path):
    store = EvidenceStore(tmp_path)
    repository = TrajectoryRepository(store)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    worker = Principal(tenant="tenant", subject="worker", role="worker")
    source = _registered_source(repository, researcher)
    record = _source_record("record-1", "1", "0" * 64)

    with pytest.raises(Forbidden, match="ingestion authority"):
        repository.ingest(source.id, (record,), worker)
    with pytest.raises(ValueError, match="1 to 1000"):
        repository.ingest(source.id, (), researcher)
    with pytest.raises(Forbidden, match="source unavailable"):
        repository.ingest("missing", (record,), researcher)
    with pytest.raises(Conflict, match="hash does not match"):
        repository.ingest(
            source.id,
            (record.model_copy(update={"source_hash": "f" * 64}),),
            researcher,
        )
    with pytest.raises(Forbidden, match="source-status authority"):
        repository.update_source_status(
            source.id,
            SourceStatusUpdate(
                collection_state="current",
                execution_state="active",
                termination={"terminated": False, "truncated": False, "reason": "running"},
                verified_outcome={"state": "pending", "evidence": ()},
                terminal_position="1",
                terminal_hash=record.source_hash,
            ),
            worker,
        )
    with pytest.raises(Forbidden, match="source unavailable"):
        repository.update_source_status(
            "missing",
            SourceStatusUpdate(
                collection_state="current",
                execution_state="active",
                termination={"terminated": False, "truncated": False, "reason": "running"},
                verified_outcome={"state": "pending", "evidence": ()},
                terminal_position="1",
                terminal_hash=record.source_hash,
            ),
            researcher,
        )

    repository.ingest(source.id, (record,), researcher)
    common = {
        "collection_state": "complete",
        "execution_state": "completed",
        "termination": {"terminated": True, "truncated": False, "reason": "complete"},
        "verified_outcome": {"state": "success", "evidence": (record.id,)},
        "terminal_position": record.position,
        "terminal_hash": record.source_hash,
    }
    for update, message in (
        (common | {"capture_failures": ("failure",)}, "capture failures"),
        (common | {"backlog": 1}, "backlog"),
        (common | {"terminal_position": "wrong"}, "boundary"),
        (
            common | {"verified_outcome": {"state": "success", "evidence": ("missing",)}},
            "unavailable source evidence",
        ),
    ):
        with pytest.raises(Conflict, match=message):
            repository.update_source_status(source.id, SourceStatusUpdate(**update), researcher)

    with pytest.raises(Forbidden, match="source-status authority"):
        repository.source_status(source.id, worker)
    with pytest.raises(Forbidden, match="source unavailable"):
        repository.source_status("missing", researcher)


def test_empty_trajectory_and_snapshot_legacy_authority_paths(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path)
    repository = TrajectoryRepository(store)
    who = Principal(tenant="tenant", subject="researcher", role="researcher")
    worker = Principal(tenant="tenant", subject="worker", role="worker")
    environment = SyntheticEnvironment()
    session = EnvironmentSession(store, environment)
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
        ),
        who,
    )["id"]
    monkeypatch.setattr(store, "replay", lambda *_args, **_kwargs: iter(()))
    with pytest.raises(ValueError, match="no evidence"):
        repository.get(environment_id, who)

    source = _registered_source(repository, who, "empty-import")
    with pytest.raises(ValueError, match="no evidence"):
        repository.get(source.id, who)
    with pytest.raises(Forbidden, match="source unavailable"):
        repository.get(source.id, worker)
    with pytest.raises(Forbidden, match="source unavailable"):
        repository.get("missing", who)
    portable = Trajectory.model_validate(_portable_trajectory_payload())
    monkeypatch.setattr(repository, "get", lambda *_args: portable)
    with pytest.raises(Forbidden, match="trajectory unavailable"):
        repository.freeze("missing", who)

    with pytest.raises(Forbidden, match="snapshot authority"):
        repository.get_snapshot("missing", worker)
    with pytest.raises(Forbidden, match="snapshot unavailable"):
        repository.get_snapshot("missing", who)
    with pytest.raises(Forbidden, match="snapshot authority"):
        repository.list_snapshots(environment_id, worker)

    snapshot = _portable_trajectory_payload()
    legacy = {
        "snapshot": {
            "apiVersion": API_VERSION,
            "kind": "TrajectorySnapshot",
            "metadata": {"id": "legacy", "createdAt": "now", "labels": {}},
            "features": {"required": [], "optional": []},
            "spec": {
                "trajectoryId": snapshot["metadata"]["id"],
                "trajectoryDigest": "a" * 64,
                "sourceBoundary": {"position": "1", "hash": "b" * 64},
                "evidenceHead": "b" * 64,
                "sequenceStart": 1,
                "sequenceEnd": 1,
                "scoreReports": [],
                "artifacts": [],
                "audience": ["researcher"],
            },
            "status": {
                "recordCount": 1,
                "artifactCount": 0,
                "manifestDigest": "c" * 64,
                "recordsDigest": "d" * 64,
                "snapshotDigest": "e" * 64,
                "complete": True,
            },
            "extensions": {},
        },
        "records": [{"id": "legacy-record"}],
    }
    with store.transaction() as database:
        database.execute(
            "INSERT INTO trajectory_snapshots VALUES (?,?,?,?,?,?)",
            ("legacy", who.tenant, environment_id, encode(legacy), digest(legacy), 1.0),
        )
    assert list(repository.export_snapshot("legacy", who))[-1] == {"id": "legacy-record"}

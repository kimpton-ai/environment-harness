from _credentials import bearer
from fastapi.testclient import TestClient

from environment_harness import AgentSpec, EvidenceStore, ExperimentSpec
from environment_harness.access import _AccessContext
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.runtime import _SessionRuntime
from environment_harness.server import create_app
from environment_harness.trajectories import SourceRecord, SourceRegistration, TrajectoryRepository


def test_authenticated_api_lists_and_reads_portable_trajectories(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
        ),
        researcher,
    )["id"]
    second_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="bob", implementation="synthetic", policy_version="1"),),
        ),
        researcher,
    )["id"]
    client = TestClient(create_app(session), base_url="http://testserver")
    headers = {"Authorization": "Bearer " + bearer(store, researcher)}

    listed = client.get("/v1/trajectories", headers=headers)
    paged = client.get("/v1/trajectories?limit=1", headers=headers)
    fetched = client.get(f"/v1/trajectories/{environment_id}", headers=headers)

    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()["items"]} == {environment_id, second_id}
    assert paged.headers["x-next-cursor"]
    assert 'rel="next"' in paged.headers["link"]
    assert fetched.status_code == 200
    assert fetched.json()["kind"] == "Trajectory"
    assert client.get("/v1/trajectories").status_code == 401


def test_trajectory_records_are_cursor_paged_without_materializing_the_resource(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
        ),
        researcher,
    )["id"]
    session.observe(environment_id, researcher, "alice")
    client = TestClient(create_app(session), base_url="http://testserver")
    headers = {"Authorization": "Bearer " + bearer(store, researcher)}

    first = client.get(f"/v1/trajectories/{environment_id}/records?limit=1", headers=headers)
    second = client.get(
        f"/v1/trajectories/{environment_id}/records?limit=1&after={first.json()['cursor']}",
        headers=headers,
    )

    assert first.json()["records"][0]["sequence"] == 1
    assert second.json()["records"][0]["sequence"] == 2
    assert first.headers["x-next-cursor"] == "1"
    assert 'rel="next"' in first.headers["link"]


def test_trajectory_snapshot_boundaries_can_be_listed_for_the_viewer(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
        ),
        researcher,
    )["id"]
    session.observe(environment_id, researcher, "alice")
    frozen = TrajectoryRepository(store).freeze(environment_id, researcher)
    client = TestClient(create_app(session), base_url="http://testserver")
    headers = {"Authorization": "Bearer " + bearer(store, researcher)}

    nested = client.get(f"/v1/trajectories/{environment_id}/snapshots", headers=headers)
    indexed = client.get(f"/v1/snapshots?trajectory={environment_id}", headers=headers)

    assert nested.status_code == indexed.status_code == 200
    assert [item["metadata"]["id"] for item in nested.json()["items"]] == [frozen.metadata.id]
    assert [item["metadata"]["id"] for item in indexed.json()["items"]] == [frozen.metadata.id]
    assert client.get(f"/v1/snapshots?trajectory={environment_id}").status_code == 401


def test_source_registration_route_exists_only_in_explicit_ingestion_mode(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    headers = {"Authorization": "Bearer " + bearer(store, researcher)}
    registration = {
        "namespace": "com.example.simulator",
        "run_id": "run-api",
        "schema_version": "simulator.trace.v3",
        "environment": {"id": "simulator", "version": "3"},
        "participants": ["alice"],
        "purpose": "evaluation",
    }

    read_only = TestClient(create_app(session), base_url="http://testserver")
    enabled = TestClient(
        create_app(session, trajectory_ingestion=True),
        base_url="http://testserver",
    )

    # A disabled capability is a declared 501, not a hidden route.
    denied = read_only.post("/v1/sources", headers=headers, json=registration)
    assert denied.status_code == 501
    assert denied.json()["error"]["code"] == "capability_unavailable"
    assert "historical ingestion is disabled" in denied.json()["error"]["message"]
    document = read_only.get("/openapi.json").json()
    assert document["paths"]["/v1/sources"]["post"]["x-capability"] == "historical-ingestion"
    read_only_capabilities = read_only.get("/v1/capabilities", headers=headers).json()
    assert {item["name"]: item["enabled"] for item in read_only_capabilities["capabilities"]}[
        "historical-ingestion"
    ] is False

    assert enabled.post("/v1/sources", json=registration).status_code == 401
    response = enabled.post("/v1/sources", headers=headers, json=registration)
    assert response.status_code == 201
    assert response.json()["namespace"] == "com.example.simulator"
    assert response.headers["location"].startswith("/v1/sources/")
    enabled_capabilities = enabled.get("/v1/capabilities", headers=headers).json()
    assert {item["name"]: item["enabled"] for item in enabled_capabilities["capabilities"]}[
        "historical-ingestion"
    ] is True


def test_read_only_server_can_inspect_but_not_mutate_a_registered_source(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    receipt = TrajectoryRepository(store).register_source(
        SourceRegistration(
            namespace="com.example.simulator",
            run_id="run-status",
            schema_version="simulator.trace.v3",
            environment={"id": "simulator", "version": "3"},
            participants=("alice",),
            purpose="evaluation",
        ),
        researcher,
    )
    client = TestClient(create_app(session), base_url="http://testserver")
    headers = {"Authorization": "Bearer " + bearer(store, researcher)}

    inspected = client.get(f"/v1/sources/{receipt.id}/status", headers=headers)

    assert inspected.status_code == 200
    assert inspected.json()["collection_state"] == "registered"
    assert client.put(f"/v1/sources/{receipt.id}/status", headers=headers, json={}).status_code == 405


def test_configured_ingestion_api_records_and_finalizes_an_imported_trajectory(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    headers = {"Authorization": "Bearer " + bearer(store, researcher)}
    client = TestClient(
        create_app(session, trajectory_ingestion=True),
        base_url="http://testserver",
    )
    registered = client.post(
        "/v1/sources",
        headers=headers,
        json={
            "namespace": "com.example.simulator",
            "run_id": "run-ingest-api",
            "schema_version": "simulator.trace.v3",
            "environment": {"id": "simulator", "version": "3"},
            "participants": ["alice"],
            "purpose": "evaluation",
        },
    ).json()
    record = SourceRecord.create(
        id="source-record-1",
        position="frame-1",
        previous_hash="0" * 64,
        type="com.example.simulator.outcome",
        segment="segment-1",
        participant="alice",
        revision=1,
        time={
            "wallTime": "2026-09-22T15:00:00Z",
            "native": [{"clock": "simulator.frame", "value": 1}],
        },
        data={"result": "success"},
        audience=("*",),
    )

    ingested = client.post(
        f"/v1/sources/{registered['id']}/records",
        headers=headers,
        json={"records": [record.model_dump(mode="json", by_alias=True)]},
    )
    finalized = client.post(
        f"/v1/sources/{registered['id']}/status-reports",
        headers=headers,
        json={
            "collection_state": "complete",
            "execution_state": "completed",
            "termination": {"terminated": True, "truncated": False, "reason": "goal"},
            "verified_outcome": {"state": "success", "evidence": ["source-record-1"]},
            "terminal_position": "frame-1",
            "terminal_hash": record.source_hash,
            "backlog": 0,
            "gaps": [],
            "capture_failures": [],
        },
    )
    fetched = client.get(f"/v1/trajectories/{registered['id']}", headers=headers)

    assert ingested.status_code == 200
    assert ingested.json()["hash"] == record.source_hash
    assert finalized.status_code == 201
    assert fetched.json()["status"]["verifiedOutcome"]["state"] == "success"


def test_evaluation_session_can_freeze_and_export_snapshot_without_training_entitlement(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
            purpose="evaluation",
            split="heldout",
        ),
        researcher,
    )["id"]
    headers = {"Authorization": "Bearer " + bearer(store, researcher)}
    client = TestClient(create_app(session), base_url="http://testserver")

    frozen = client.post(f"/v1/trajectories/{environment_id}/snapshots", headers=headers)
    snapshot_id = frozen.json()["metadata"]["id"]
    fetched = client.get(f"/v1/snapshots/{snapshot_id}", headers=headers)
    exported = client.get(
        f"/v1/snapshots/{snapshot_id}/records",
        headers=headers | {"Accept": "application/x-ndjson"},
    )

    assert frozen.status_code == 201
    assert fetched.json() == frozen.json()
    assert exported.status_code == 200
    rows = [line for line in exported.text.splitlines() if line]
    assert len(rows) == frozen.json()["status"]["recordCount"] + 1
    # Authorized evaluation export needs no training entitlement, while a
    # dataset still requires complete training-entitled evidence.
    assert (
        client.post(
            "/v1/datasets",
            headers=headers,
            json={"name": "denied", "trajectories": [environment_id]},
        ).status_code
        == 403
    )

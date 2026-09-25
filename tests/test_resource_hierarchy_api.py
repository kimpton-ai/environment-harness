"""Contract tests for the canonical short HTTP resource hierarchy."""

from __future__ import annotations

import json

import pytest
from _credentials import bearer
from fastapi.testclient import TestClient

from environment_harness import EnvironmentHarness, EvidenceStore, Scenario
from environment_harness.access import _AccessContext
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment, SyntheticScenarioInput
from environment_harness.server import create_app


def service(tmp_path, *, trajectory_ingestion=False):
    store = EvidenceStore(tmp_path)
    harness = EnvironmentHarness(
        store,
        environment=SyntheticEnvironment,
        agents={"alice": SyntheticAgent, "bob": SyntheticAgent},
        scoring_versions=("hierarchy@1",),
        tenant="tenant",
    )
    experiment = harness.experiment(
        "hierarchy",
        (
            Scenario(id="low", input=SyntheticScenarioInput(starting_total=-1)),
            Scenario(id="high", input=SyntheticScenarioInput(starting_total=1)),
        ),
        trials=1,
        turns=2,
    )
    result = experiment.run()
    client = TestClient(
        create_app(harness, trajectory_ingestion=trajectory_ingestion), base_url="http://testserver"
    )
    headers = {
        "Authorization": "Bearer "
        + bearer(store, _AccessContext(tenant="tenant", subject="ops", policy="admin"))
    }
    return client, harness, experiment, result, headers


def test_experiment_scenario_set_and_session_hierarchy_is_navigable(tmp_path):
    client, _harness, experiment, result, headers = service(tmp_path)

    experiments = client.get("/v1/experiments", headers=headers)
    assert experiments.status_code == 200
    assert [item["metadata"]["id"] for item in experiments.json()["items"]] == [experiment.id]

    fetched = client.get(f"/v1/experiments/{experiment.id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.headers["etag"].strip('"') == fetched.json()["status"]["lockDigest"]
    assert fetched.json()["status"]["progress"]["planned"] == 2

    scenario_set_id = fetched.json()["spec"]["scenarioSet"]["id"]
    sets = client.get("/v1/scenario-sets", headers=headers)
    assert [item["metadata"]["id"] for item in sets.json()["items"]] == [scenario_set_id]
    one = client.get(f"/v1/scenario-sets/{scenario_set_id}", headers=headers)
    assert one.headers["etag"].strip('"') == one.json()["status"]["setDigest"]
    assert [item["id"] for item in one.json()["spec"]["scenarios"]] == ["low", "high"]

    derived = client.get(f"/v1/experiments/{experiment.id}/sessions", headers=headers)
    assert {item["metadata"]["id"] for item in derived.json()["items"]} == {
        session.id for session in result.sessions
    }
    assert client.get("/v1/experiments/missing", headers=headers).status_code == 403
    assert client.get("/v1/scenario-sets/missing", headers=headers).status_code == 403


def test_management_collections_share_one_cursor_envelope(tmp_path):
    client, _harness, _experiment, _result, headers = service(tmp_path)

    first = client.get("/v1/sessions?limit=1", headers=headers)
    assert set(first.json()) == {"items", "nextCursor", "links"}
    cursor = first.json()["nextCursor"]
    assert first.headers["x-next-cursor"] == cursor
    assert first.headers["link"] == f'<{first.json()["links"]["next"]}>; rel="next"'
    assert "limit=1" in first.json()["links"]["next"]

    second = client.get(f"/v1/sessions?limit=1&cursor={cursor}", headers=headers)
    assert second.json()["items"][0]["metadata"]["id"] != first.json()["items"][0]["metadata"]["id"]
    assert client.get("/v1/sessions?cursor=missing", headers=headers).status_code == 422


def test_policies_are_derived_read_only_resources(tmp_path):
    client, _harness, _experiment, _result, headers = service(tmp_path)

    listed = client.get("/v1/policies", headers=headers)
    assert listed.status_code == 200
    identity = listed.json()["items"][0]["metadata"]["id"]
    fetched = client.get(f"/v1/policies/{identity}", headers=headers)
    assert fetched.json()["kind"] == "Policy"
    assert fetched.headers["etag"].strip('"') == fetched.json()["status"]["digest"]
    assert fetched.json()["spec"]["implementation"] == "synthetic-agent@1"
    assert client.get("/v1/policies/missing", headers=headers).status_code == 403
    # Policies are derived: there is no create or delete.
    assert client.post("/v1/policies", headers=headers, json={}).status_code == 405


def test_checkpoints_and_branches_return_created_resources(tmp_path):
    client, _harness, _experiment, result, headers = service(tmp_path)
    session = result.sessions[0].id

    created = client.post(f"/v1/sessions/{session}/checkpoints", headers=headers, json={"exact_agents": True})
    assert created.status_code == 201
    identity = created.json()["metadata"]["id"]
    assert created.headers["location"] == f"/v1/sessions/{session}/checkpoints/{identity}"
    assert created.json()["kind"] == "Checkpoint"
    assert created.json()["status"]["exact"] is True

    fetched = client.get(f"/v1/sessions/{session}/checkpoints/{identity}", headers=headers)
    assert fetched.headers["etag"].strip('"') == fetched.json()["status"]["checkpointDigest"]
    listed = client.get(f"/v1/sessions/{session}/checkpoints", headers=headers)
    assert [item["metadata"]["id"] for item in listed.json()["items"]] == [identity]

    branched = client.post(
        f"/v1/sessions/{session}/branches",
        headers=headers,
        json={"checkpoint": identity, "interventions": {"total": 5}},
    )
    assert branched.status_code == 201
    child = branched.json()["metadata"]["id"]
    assert branched.headers["location"] == f"/v1/sessions/{child}"
    # No Branch resource: the child Session carries the lineage.
    assert branched.json()["kind"] == "Session"
    assert branched.json()["spec"]["lineage"]["parent"] == session
    assert branched.json()["spec"]["lineage"]["checkpoint"] == identity
    assert branched.json()["spec"]["lineage"]["interventions"] == {"total": 5}
    assert client.get("/v1/branches", headers=headers).status_code == 404


def test_scores_are_contextual_on_experiments_sessions_and_trajectories(tmp_path):
    from environment_harness.contracts import ScoreReport

    client, harness, experiment, result, headers = service(tmp_path)
    session = result.sessions[0]
    session.report(
        ScoreReport(
            scorer="hierarchy",
            version="1",
            kind="deterministic",
            evidence_cursor=session.verify()["events"],
            metrics={"total": 1},
            uncertainty="synthetic",
            provenance={"synthetic": True},
        )
    )

    for path in (
        f"/v1/sessions/{session.id}/scores",
        f"/v1/experiments/{experiment.id}/scores",
        f"/v1/trajectories/{session.id}/scores",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, path
        assert len(response.json()["items"]) == 1, path
    assert harness.store.reports(session.id, harness._access)[0]["revision"] == 1


def test_snapshots_and_datasets_negotiate_pages_and_streams(tmp_path):
    client, _harness, _experiment, result, headers = service(tmp_path)
    session = result.sessions[0].id

    frozen = client.post(f"/v1/trajectories/{session}/snapshots", headers=headers)
    identity = frozen.json()["metadata"]["id"]
    indexed = client.get("/v1/snapshots", headers=headers)
    assert [item["metadata"]["id"] for item in indexed.json()["items"]] == [identity]
    assert client.get(f"/v1/snapshots?trajectory={session}", headers=headers).json()["items"]

    page = client.get(f"/v1/snapshots/{identity}/records?limit=2", headers=headers)
    assert page.headers["content-type"].startswith("application/json")
    assert len(page.json()["records"]) == 2 and page.json()["has_more"] is True
    resumed = client.get(f"/v1/snapshots/{identity}/records?after={page.json()['cursor']}", headers=headers)
    assert resumed.json()["records"][0]["sequence"] > page.json()["records"][-1]["sequence"]

    streamed = client.get(
        f"/v1/snapshots/{identity}/records", headers=headers | {"Accept": "application/x-ndjson"}
    )
    rows = [json.loads(line) for line in streamed.text.splitlines() if line]
    assert rows[0]["snapshot"]["metadata"]["id"] == identity
    assert len(rows) == frozen.json()["status"]["recordCount"] + 1


def test_training_dataset_records_page_and_stream(tmp_path):
    store = EvidenceStore(tmp_path)
    harness = EnvironmentHarness(
        store,
        environment=SyntheticEnvironment,
        agents={"alice": SyntheticAgent},
        tenant="tenant",
    )
    # Training datasets require complete, terminal, training-entitled evidence.
    from environment_harness.contracts import ExperimentSpec, RunPolicy
    from environment_harness.runtime import _SessionRuntime

    runtime = _SessionRuntime(store, SyntheticEnvironment())
    from environment_harness.contracts import AgentSpec
    from environment_harness.runner import run

    identity = runtime.create(
        ExperimentSpec(
            environment=SyntheticEnvironment().spec,
            participants=(AgentSpec(id="alice", implementation="synthetic-agent@1", policy_version="1"),),
            purpose="training",
            split="training",
            policy=RunPolicy(max_turns=1),
        ),
        harness._access,
    )["id"]
    run(runtime, identity, harness._access, {"alice": SyntheticAgent()}, turns=1)

    client = TestClient(create_app(harness), base_url="http://testserver")
    headers = {
        "Authorization": "Bearer "
        + bearer(store, _AccessContext(tenant="tenant", subject="ops", policy="admin"))
    }
    created = client.post(
        "/v1/datasets", headers=headers, json={"name": "hierarchy", "trajectories": [identity]}
    )
    assert created.status_code == 201
    dataset = created.json()["metadata"]["id"]
    assert created.headers["location"] == f"/v1/datasets/{dataset}"

    page = client.get(f"/v1/datasets/{dataset}/records?limit=2", headers=headers)
    assert len(page.json()["records"]) == 2 and page.json()["has_more"] is True
    resumed = client.get(f"/v1/datasets/{dataset}/records?after={page.json()['cursor']}", headers=headers)
    assert resumed.json()["records"]
    total = page.json()["records"] + resumed.json()["records"]
    assert len({record["id"] for record in total}) == len(total)

    streamed = client.get(
        f"/v1/datasets/{dataset}/records", headers=headers | {"Accept": "application/x-ndjson"}
    )
    assert streamed.headers["content-type"].startswith("application/x-ndjson")
    assert '"dataset"' in streamed.text


def test_sources_expose_registration_ingestion_records_and_status(tmp_path):
    from environment_harness.trajectories import SourceRecord

    client, _harness, _experiment, _result, headers = service(tmp_path, trajectory_ingestion=True)
    registration = {
        "namespace": "com.example.hierarchy",
        "run_id": "run-1",
        "schema_version": "hierarchy.v1",
        "environment": {"id": "hierarchy", "version": "1"},
        "participants": ["alice"],
        "purpose": "evaluation",
    }
    created = client.post("/v1/sources", headers=headers, json=registration)
    assert created.status_code == 201
    source = created.json()["id"]
    assert created.headers["location"] == f"/v1/sources/{source}"

    record = SourceRecord.create(
        id="record-1",
        position="1",
        previous_hash="0" * 64,
        type="com.example.hierarchy.frame",
        segment="segment-1",
        participant="alice",
        revision=0,
        time={"wallTime": "2026-09-22T15:00:00Z", "native": []},
        data={},
        audience=("*",),
    )
    ingested = client.post(
        f"/v1/sources/{source}/records",
        headers=headers,
        json={"records": [record.model_dump(mode="json", by_alias=True)]},
    )
    assert ingested.status_code == 200 and ingested.json()["accepted"] == 1

    assert [item["source"] for item in client.get("/v1/sources", headers=headers).json()["items"]] == [source]
    assert client.get(f"/v1/sources/{source}", headers=headers).json()["namespace"] == (
        "com.example.hierarchy"
    )
    records = client.get(f"/v1/sources/{source}/records", headers=headers)
    assert [item["id"] for item in records.json()["records"]] == ["record-1"]

    reported = client.post(
        f"/v1/sources/{source}/status-reports",
        headers=headers,
        json={
            "collection_state": "complete",
            "execution_state": "completed",
            "termination": {"terminated": True, "truncated": False, "reason": "goal"},
            "verified_outcome": {"state": "success", "evidence": ["record-1"]},
            "terminal_position": "1",
            "terminal_hash": record.source_hash,
            "backlog": 0,
        },
    )
    assert reported.status_code == 201
    status = client.get(f"/v1/sources/{source}/status", headers=headers)
    assert status.json()["collection_state"] == "complete"


def test_capability_discovery_matches_the_openapi_annotations(tmp_path):
    client, _harness, _experiment, _result, headers = service(tmp_path)

    document = client.get("/openapi.json").json()
    declared = {
        operation["x-capability"]
        for item in document["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict) and "x-capability" in operation
    }
    published = {
        item["name"] for item in client.get("/v1/capabilities", headers=headers).json()["capabilities"]
    }
    assert declared <= published

    from environment_harness.capabilities import CAPABILITY_NAMES

    assert published == set(CAPABILITY_NAMES)
    # A disabled capability reports the same name it returns in its 501.
    denied = client.post("/v1/sources", headers=headers, json={"namespace": "com.example.x"})
    assert denied.status_code in (422, 501)


@pytest.mark.parametrize(
    "path",
    [
        "/v1/environment",
        "/v1/environments",
        "/v1/compare",
        "/v1/activity/events",
        "/v1/activity/snapshot",
        "/v1/trajectory-sources",
        "/v1/trajectory-snapshots",
        "/v1/trajectory-datasets",
    ],
)
def test_removed_0_2_routes_return_not_found(tmp_path, path):
    client, _harness, _experiment, _result, headers = service(tmp_path)
    assert client.get(path, headers=headers).status_code == 404

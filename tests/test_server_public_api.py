import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.contracts import Action, RunPolicy, ScoreReport
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.server import create_app
from environment_harness.store import uid


def service(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, environment)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    spec = ExperimentSpec(
        environment=environment.spec,
        participants=(AgentSpec(id="alice", implementation="synthetic-agent@1", policy_version="1"),),
        scoring_versions=("control@1",),
        policy=RunPolicy(
            max_cost_micros=10,
            allowed_endpoints=("https://synthetic.invalid",),
            allowed_operations=("lookup",),
        ),
    )
    identifier = session.create(spec, researcher)["id"]
    client = TestClient(create_app(session), base_url="http://testserver")
    research_headers = {"Authorization": "Bearer " + store.issue(researcher)}
    agent = Principal(
        tenant="tenant",
        subject="alice",
        role="agent",
        environment=identifier,
        participant="alice",
    )
    agent_headers = {"Authorization": "Bearer " + store.issue(agent)}
    return client, store, session, researcher, spec, identifier, research_headers, agent_headers


ROUTE_ROLE_MATRIX = {
    "health": {"researcher", "worker", "scorer", "agent"},
    "environment-schema": {"researcher", "worker", "scorer", "agent"},
    "create": {"researcher"},
    "list": {"researcher"},
    "get": {"researcher", "worker", "scorer", "agent"},
    "observation": {"researcher", "worker", "scorer", "agent"},
    "actions": {"agent"},
    "events": {"researcher", "worker", "scorer", "agent"},
    "agent-work": {"researcher", "worker", "agent"},
    "commands": {"researcher", "worker"},
    "credentials": {"researcher"},
    "operations": {"agent"},
    "artifacts": {"researcher", "worker", "scorer", "agent"},
    "artifact-read": {"researcher", "worker", "scorer", "agent"},
    "reports": {"researcher", "scorer"},
    "report": {"researcher", "scorer"},
    "export": {"researcher", "worker", "scorer", "agent"},
    "compare": {"researcher", "scorer"},
    "viewer": {"researcher", "worker", "scorer", "agent"},
    "viewer-asset": {"researcher", "worker", "scorer", "agent"},
}
ROLES = ("researcher", "worker", "scorer", "agent")


@pytest.mark.parametrize("case", ROUTE_ROLE_MATRIX)
@pytest.mark.parametrize("role", ROLES)
def test_every_http_route_has_an_explicit_role_decision(tmp_path, case, role):
    client, store, session, researcher, spec, environment, _, _ = service(tmp_path)
    principal = (
        Principal(
            tenant="tenant",
            subject="alice",
            role="agent",
            environment=environment,
            participant="alice",
        )
        if role == "agent"
        else Principal(tenant="tenant", subject=role, role=role)
    )
    headers = {"Authorization": "Bearer " + store.issue(principal)}
    agent = Principal(
        tenant="tenant",
        subject="alice",
        role="agent",
        environment=environment,
        participant="alice",
    )

    if case == "health":
        response = client.get("/health", headers=headers)
    elif case == "environment-schema":
        response = client.get("/v1/environment", headers=headers)
    elif case == "create":
        response = client.post(
            "/v1/environments",
            headers=headers | {"X-Operation-ID": "a" * 32},
            json=spec.model_dump(mode="json"),
        )
    elif case == "list":
        response = client.get("/v1/environments", headers=headers)
    elif case == "get":
        response = client.get(f"/v1/environments/{environment}", headers=headers)
    elif case == "observation":
        response = client.get(
            f"/v1/environments/{environment}/observation?participant=alice", headers=headers
        )
    elif case == "actions":
        observation = session.observe(environment, agent)
        action = Action(
            operation_id=uid(),
            participant="alice",
            observation_id=observation["id"],
            revision=observation["revision"],
            payload={"value": 1},
        )
        response = client.post(
            f"/v1/environments/{environment}/actions",
            headers=headers,
            json=action.model_dump(mode="json"),
        )
    elif case == "events":
        response = client.get(f"/v1/environments/{environment}/events", headers=headers)
    elif case == "agent-work":
        response = client.get(f"/v1/environments/{environment}/agent-work", headers=headers)
    elif case == "commands":
        response = client.post(
            f"/v1/environments/{environment}/commands",
            headers=headers,
            json={"operation": "lease", "arguments": {"owner": "matrix"}},
        )
    elif case == "credentials":
        response = client.post(
            f"/v1/environments/{environment}/credentials",
            headers=headers,
            json={"participant": "alice"},
        )
    elif case == "operations":
        response = client.post(
            f"/v1/environments/{environment}/operations",
            headers=headers,
            json={
                "operation_id": "matrix-operation",
                "endpoint": "https://synthetic.invalid",
                "operation": "lookup",
                "payload": {},
            },
        )
    elif case == "artifacts":
        response = client.post(
            f"/v1/environments/{environment}/artifacts", headers=headers, content=b"matrix"
        )
    elif case == "artifact-read":
        key = store.artifact(environment, researcher, b"matrix", audience=("*",))["id"]
        response = client.get(f"/v1/environments/{environment}/artifacts/{key}", headers=headers)
    elif case == "reports":
        response = client.get(f"/v1/environments/{environment}/reports", headers=headers)
    elif case == "report":
        report = ScoreReport(
            scorer="control",
            version="1",
            kind="deterministic",
            evidence_cursor=store.verify(environment, researcher)["events"],
            metrics={"synthetic": 1},
            uncertainty="synthetic",
            provenance={"synthetic": True},
        )
        response = client.post(
            f"/v1/environments/{environment}/reports",
            headers=headers,
            json=report.model_dump(mode="json"),
        )
    elif case == "export":
        response = client.get(f"/v1/environments/{environment}/export", headers=headers)
    elif case == "compare":
        response = client.post("/v1/compare", headers=headers, json={"environments": [environment]})
    elif case == "viewer":
        response = client.get("/", headers=headers)
    else:
        response = client.get("/viewer/app.js", headers=headers)

    expected = 200 if role in ROUTE_ROLE_MATRIX[case] else 403
    assert response.status_code == expected, (case, role, response.text)


def test_http_negative_credential_matrix(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)
    protected = "/v1/environments"
    assert client.get(protected).status_code == 401
    assert client.get(protected, headers={"Authorization": "Basic value"}).status_code == 401
    assert client.get(protected, headers={"Authorization": "Bearer invalid"}).status_code == 403

    expired = store.issue(researcher)
    revoked = store.issue(researcher)
    with store.transaction() as db:
        db.execute(
            "UPDATE credentials SET expires=0 WHERE hash=?",
            (hashlib.sha256(expired.encode()).hexdigest(),),
        )
        db.execute(
            "UPDATE credentials SET revoked=1 WHERE hash=?",
            (hashlib.sha256(revoked.encode()).hexdigest(),),
        )
    assert client.get(protected, headers={"Authorization": "Bearer " + expired}).status_code == 403
    assert client.get(protected, headers={"Authorization": "Bearer " + revoked}).status_code == 403

    other_tenant = Principal(tenant="other", subject="researcher", role="researcher")
    other_headers = {"Authorization": "Bearer " + store.issue(other_tenant)}
    assert client.get(f"/v1/environments/{environment}", headers=other_headers).status_code == 403

    second = session.create(spec, researcher, environment_id="b" * 32)["id"]
    assert client.get(f"/v1/environments/{second}", headers=agent_headers).status_code == 403
    assert (
        client.get(
            f"/v1/environments/{environment}/observation?participant=bob", headers=agent_headers
        ).status_code
        == 403
    )

    with store.transaction() as db:
        row = store._environment_row(db, environment)
        participants = json.loads(row["participants"])
        participants["alice"]["generation"] += 1
        db.execute(
            "UPDATE environments SET participants=? WHERE id=?",
            (json.dumps(participants, separators=(",", ":"), sort_keys=True), environment),
        )
    assert client.get(f"/v1/environments/{environment}", headers=agent_headers).status_code == 403


def test_http_route_and_role_matrix(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)

    health = client.get("/health")
    assert health.json() == {"status": "ok", "protocol": "environment-session.v1"}
    assert health.headers["cache-control"] == "no-store"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in health.headers["content-security-policy"]
    assert client.get("/v1/environment", headers=headers).json()["id"] == "synthetic-protocol"
    assert client.get("/v1/environments", headers=headers).json()[0]["id"] == environment
    assert client.get(f"/v1/environments/{environment}", headers=headers).json()["id"] == environment
    assert client.get(f"/v1/environments/{environment}/observation", headers=agent_headers).status_code == 200

    http_identifier = "a" * 32
    created = client.post(
        "/v1/environments",
        headers=headers | {"X-Operation-ID": http_identifier},
        json=spec.model_dump(mode="json"),
    )
    assert created.status_code == 200 and created.json()["id"] == http_identifier

    observation = client.get(f"/v1/environments/{environment}/observation", headers=agent_headers).json()
    action = Action(
        operation_id=uid(),
        participant="alice",
        observation_id=observation["id"],
        revision=observation["revision"],
        payload={"value": 1},
    )
    assert (
        client.post(
            f"/v1/environments/{environment}/actions",
            headers=agent_headers,
            json=action.model_dump(mode="json"),
        ).status_code
        == 200
    )

    events = client.get(f"/v1/environments/{environment}/events", headers=headers).json()
    assert events["events"] and events["cursor"] >= 1
    resumed = client.get(
        f"/v1/environments/{environment}/events",
        headers=headers | {"Last-Event-ID": str(events["cursor"])},
    ).json()
    assert resumed == {"events": [], "cursor": events["cursor"]}
    caught_up = client.get(
        f"/v1/environments/{environment}/events",
        headers=headers | {"Accept": "text/event-stream", "Last-Event-ID": str(events["cursor"])},
    )
    assert caught_up.headers["content-type"].startswith("text/event-stream")
    assert caught_up.text == ": caught up\n\n"
    assert (
        client.get(
            f"/v1/environments/{environment}/events", headers=headers | {"Last-Event-ID": "invalid"}
        ).status_code
        == 422
    )
    assert client.get(f"/v1/environments/{environment}/agent-work", headers=headers).status_code == 200


def test_http_commands_credentials_operations_artifacts_and_reports(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)

    lease = client.post(
        f"/v1/environments/{environment}/commands",
        headers=headers,
        json={"operation": "lease", "arguments": {"owner": "http"}},
    ).json()
    assert lease["owner"] == "http"
    released = client.post(
        f"/v1/environments/{environment}/commands",
        headers=headers,
        json={"operation": "release", "arguments": {"lease": lease}},
    )
    assert released.status_code == 200
    assert (
        client.post(
            f"/v1/environments/{environment}/commands",
            headers=headers,
            json={"operation": "unknown", "arguments": {}},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/v1/environments/{environment}/commands",
            headers=headers,
            json={"operation": "lease", "arguments": {"unexpected": True}},
        ).status_code
        == 422
    )

    credential = client.post(
        f"/v1/environments/{environment}/credentials",
        headers=headers,
        json={"participant": "alice", "ttl": 100000},
    )
    assert credential.status_code == 200
    assert store.authenticate(credential.json()["token"]).participant == "alice"
    assert (
        client.post(
            f"/v1/environments/{environment}/credentials",
            headers=headers,
            json={"participant": "missing"},
        ).status_code
        == 403
    )

    prepared = client.post(
        f"/v1/environments/{environment}/operations",
        headers=agent_headers,
        json={
            "operation_id": "http-op",
            "endpoint": "https://synthetic.invalid",
            "operation": "lookup",
            "payload": {"synthetic": True},
            "maximum_cost_micros": 2,
        },
    )
    assert prepared.json() == {"id": "http-op", "status": "prepared"}

    uploaded = client.post(
        f"/v1/environments/{environment}/artifacts",
        headers=agent_headers | {"Content-Type": "application/synthetic"},
        content=b"public SDK artifact",
    )
    assert uploaded.status_code == 200
    downloaded = client.get(
        f"/v1/environments/{environment}/artifacts/{uploaded.json()['id']}", headers=agent_headers
    )
    assert downloaded.content == b"public SDK artifact"
    assert downloaded.headers["content-disposition"].startswith("attachment")

    report = ScoreReport(
        scorer="control",
        version="1",
        kind="deterministic",
        evidence_cursor=store.verify(environment, researcher)["events"],
        metrics={"synthetic": 1},
        uncertainty="synthetic",
        provenance={"synthetic": True},
    )
    assert (
        client.post(
            f"/v1/environments/{environment}/reports",
            headers=headers,
            json=report.model_dump(mode="json"),
        ).status_code
        == 200
    )
    assert len(client.get(f"/v1/environments/{environment}/reports", headers=headers).json()) == 1


def test_http_exports_comparison_viewer_and_invalid_requests(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)

    evidence = client.get(f"/v1/environments/{environment}/export", headers=headers)
    assert evidence.status_code == 200 and '"kind":"session.created"' in evidence.text
    assert (
        client.get(f"/v1/environments/{environment}/export?format=unknown", headers=headers).status_code
        == 422
    )
    assert (
        client.post("/v1/compare", headers=headers, json={"environments": [environment]}).status_code == 200
    )
    for bad in ([], "not-a-list", [str(index) for index in range(101)]):
        assert client.post("/v1/compare", headers=headers, json={"environments": bad}).status_code == 422
    assert client.get("/").status_code == 200
    for asset in ("app.js", "timeline.js", "client.js", "types.js", "style.css"):
        assert client.get(f"/viewer/{asset}").status_code == 200
    assert client.get("/viewer/private.txt").status_code == 404

    assert client.get("/v1/environments", headers={"Authorization": "Basic invalid"}).status_code == 401
    assert client.get("/v1/environments", headers={"Authorization": "Bearer invalid"}).status_code == 403
    other = Principal(tenant="other", subject="researcher", role="researcher")
    other_headers = {"Authorization": "Bearer " + store.issue(other)}
    assert client.get(f"/v1/environments/{environment}", headers=other_headers).status_code == 403
    assert client.get("/health", headers={"Origin": "https://attacker.invalid"}).json() == {
        "error": "cross_origin_denied"
    }
    assert client.get("/v1/environments?limit=0", headers=headers).status_code == 422


def test_http_training_export_entitlement_and_value_error(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)
    assert (
        client.get(f"/v1/environments/{environment}/export?format=training", headers=headers).status_code
        == 403
    )
    assert client.post(
        f"/v1/environments/{environment}/credentials",
        headers=headers,
        json={"participant": "alice", "ttl": "invalid"},
    ).json() == {"error": "invalid_request"}

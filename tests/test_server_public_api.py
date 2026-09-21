import hashlib
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.contracts import Action, RunPolicy, ScoreReport
from environment_harness.errors import HarnessError
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.presentation import SLOTS
from environment_harness.runner import run
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
    "turn-series": {"researcher", "scorer"},
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
    elif case == "turn-series":
        response = client.get(f"/v1/environments/{environment}/turn-series", headers=headers)
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


def test_http_errors_share_one_traceable_envelope(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)

    responses = (
        (client.get("/v1/environments"), 401, "unauthorized"),
        (client.get("/v1/environments", headers={"Authorization": "Bearer invalid"}), 403, "forbidden"),
        (client.get("/v1/environments?limit=0", headers=headers), 422, "invalid_request"),
        (
            client.get(
                f"/v1/environments/{environment}/events",
                headers=headers | {"Last-Event-ID": "invalid"},
            ),
            422,
            "invalid_request",
        ),
        (client.get("/viewer/private.txt"), 404, "not_found"),
        (client.put("/health"), 405, "method_not_allowed"),
        (client.get("/health", headers={"Origin": "https://attacker.invalid"}), 403, "cross_origin_denied"),
    )
    for response, status, code in responses:
        assert response.status_code == status
        assert set(response.json()) == {"error"}
        error = response.json()["error"]
        assert error["code"] == code
        assert error["status"] == status
        assert isinstance(error["message"], str) and error["message"]
        assert error["request_id"] == response.headers["x-request-id"]
        assert len(error["request_id"]) == 32
        assert datetime.fromisoformat(error["timestamp"].replace("Z", "+00:00")).tzinfo is not None

    validation = responses[2][0].json()["error"]
    assert validation["details"] == [
        {
            "field": "query.limit",
            "message": "Input should be greater than or equal to 1",
            "type": "greater_than_equal",
        }
    ]


def test_http_unexpected_errors_are_logged_and_redacted(tmp_path, caplog):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("private supplier failure")

    session.list_page = explode
    client = TestClient(create_app(session), base_url="http://testserver", raise_server_exceptions=False)
    with caplog.at_level("ERROR", logger="environment_harness.server"):
        response = client.get("/v1/environments", headers=headers)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["message"] == "Internal server error"
    assert "private supplier failure" not in response.text
    assert f"request_id={error['request_id']}" in caplog.text


def test_openapi_documents_the_shared_error_envelope(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)
    document = client.get("/openapi.json").json()

    assert {"ApiError", "ErrorDetail", "ErrorEnvelope"} <= set(document["components"]["schemas"])
    responses = document["paths"]["/v1/environments"]["get"]["responses"]
    for status in ("401", "403", "405", "409", "413", "422", "500", "503"):
        assert responses[status]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorEnvelope"
        }


def test_openapi_is_a_public_authenticated_api_reference(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)
    document = client.get("/openapi.json").json()

    assert document["info"]["title"] == "EnvironmentHarness HTTP API"
    assert document["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "description": "Opaque EnvironmentHarness credential issued for a scoped principal.",
        "scheme": "bearer",
        "bearerFormat": "opaque",
    }
    assert document["paths"]["/health"]["get"].get("security") is None
    assert document["paths"]["/v1/environments"]["get"]["security"] == [{"BearerAuth": []}]
    assert document["paths"]["/v1/environments"]["get"]["x-roles"] == ["researcher"]
    assert document["paths"]["/v1/environments/{environment}/actions"]["post"]["x-roles"] == ["agent"]
    commands = document["paths"]["/v1/environments/{environment}/commands"]["post"]
    assert commands["x-roles"] == ["researcher", "worker", "agent"]
    assert "advance" in commands["x-command-operations"]
    command_schema = document["components"]["schemas"]["CommandOperation"]
    assert set(command_schema["enum"]) == set(commands["x-command-operations"])
    assert document["paths"]["/v1/environments/{environment}/credentials"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/CredentialRequest"}
    assert document["paths"]["/v1/environments/{environment}/operations"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/OperationIntentRequest"}
    assert {tag["name"] for tag in document["tags"]} >= {
        "Service",
        "Environment sessions",
        "Evidence",
        "Activity",
        "Evaluation",
    }
    assert "/home" not in document["paths"]
    assert "/session/{environment}" not in document["paths"]


def test_environment_session_list_uses_a_stable_bounded_cursor(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)
    for digit in ("1", "2", "3"):
        response = client.post(
            "/v1/environments",
            headers=headers | {"X-Operation-ID": digit * 32},
            json=spec.model_dump(mode="json"),
        )
        assert response.status_code == 200

    first = client.get("/v1/environments?limit=2", headers=headers)
    cursor = first.headers["x-next-cursor"]
    second = client.get(f"/v1/environments?limit=2&cursor={cursor}", headers=headers)

    first_ids = [item["id"] for item in first.json()]
    second_ids = [item["id"] for item in second.json()]
    assert len(first_ids) == len(second_ids) == 2
    assert set(first_ids).isdisjoint(second_ids)
    assert first_ids + second_ids == sorted(first_ids + second_ids, reverse=True)
    assert f"cursor={cursor}" in first.headers["link"]
    assert 'rel="next"' in first.headers["link"]
    assert "x-next-cursor" not in second.headers
    assert client.get("/v1/environments?cursor=not-a-cursor", headers=headers).status_code == 422


def test_interactive_api_reference_has_route_scoped_asset_policy(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)

    docs = client.get("/docs")
    docs_policy = docs.headers["content-security-policy"]
    health_policy = client.get("/health").headers["content-security-policy"]

    assert docs.status_code == 200
    assert docs_policy == (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: https://fastapi.tiangolo.com; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
    )
    assert health_policy == (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "object-src 'none'; frame-ancestors 'none'"
    )
    assert client.get("/session/example").status_code == 200
    assert client.get("/experiment/example").status_code == 200


def test_http_boundaries_reject_oversize_invalid_and_unavailable_requests(tmp_path, monkeypatch):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)

    oversized = client.post("/v1/compare", content=b"x" * 16777217, headers=headers)
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "request_too_large"
    invalid_control = client.post(
        f"/v1/environments/{environment}/commands",
        headers=headers,
        json={"operation": "control", "arguments": {"lease": {}, "command": "unknown"}},
    )
    assert invalid_control.status_code == 422
    assert invalid_control.json()["error"]["code"] == "invalid_request"
    assert (
        client.get("/v1/activity/events", headers=headers | {"Last-Event-ID": "invalid"}).status_code == 422
    )

    def unavailable(*_args, **_kwargs):
        raise HarnessError("synthetic unavailable")

    monkeypatch.setattr(store, "activity", unavailable)
    failure = client.get("/v1/activity/events", headers=headers)
    assert failure.status_code == 503
    assert failure.json()["error"]["code"] == "harness_error"


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


def test_turn_series_bounds_long_sessions_and_preserves_range_endpoints(tmp_path):
    client, store, session, researcher, spec, environment, headers, _ = service(tmp_path)
    run(session, environment, researcher, {"alice": SyntheticAgent()}, turns=30)

    response = client.get(f"/v1/environments/{environment}/turn-series?max_points=20", headers=headers)
    assert response.status_code == 200
    projection = response.json()
    assert projection["total_turns"] == 30
    assert projection["range"] == {"start_turn": 1, "end_turn": 30}
    cumulative = next(item for item in projection["series"] if item["id"] == "reward:cumulative")
    assert cumulative["source_points"] == 30
    assert cumulative["downsampled"] is True
    assert len(cumulative["points"]) <= 20
    assert [cumulative["points"][0]["turn"], cumulative["points"][-1]["turn"]] == [1, 30]

    window = client.get(
        f"/v1/environments/{environment}/turn-series?start_turn=10&end_turn=15&max_points=20",
        headers=headers,
    ).json()
    assert window["range"] == {"start_turn": 10, "end_turn": 15}
    assert all(10 <= point["turn"] <= 15 for item in window["series"] for point in item["points"])
    after_session = client.get(
        f"/v1/environments/{environment}/turn-series?start_turn=40&end_turn=50&max_points=20",
        headers=headers,
    ).json()
    assert after_session["range"] == {"start_turn": 40, "end_turn": 50}
    assert all(not item["points"] for item in after_session["series"])
    assert (
        client.get(
            f"/v1/environments/{environment}/turn-series?start_turn=15&end_turn=10",
            headers=headers,
        ).status_code
        == 422
    )


def test_turn_series_ignores_non_numeric_signals_and_counts_blocked_attempts(monkeypatch):
    import environment_harness.evaluation as evaluation

    slots = {slot: [] for slot in SLOTS}
    slots["observation"] = [{"payload": {"value": 1}}]
    events = [
        {
            "revision": 0,
            "kind": "action.attempted",
            "payload": {"receipt": {"status": "blocked"}},
            "audience": ["alice"],
        },
        {
            "revision": 1,
            "kind": "synthetic.signal",
            "payload": {"boolean": True, "infinite": float("inf"), "count": 3},
            "audience": ["*"],
        },
    ]

    class Transaction:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    class Store:
        def transaction(self):
            return Transaction()

        def environment(self, *_args):
            return {"participants": '["alice"]'}

        def replay(self, *_args):
            return iter(events)

    monkeypatch.setattr(
        evaluation,
        "build_timeline",
        lambda *_args: [{"revision": 0, "participants": {"alice": slots}}],
    )
    monkeypatch.setattr(evaluation, "next_revision", lambda _turn: 1)

    projection = evaluation.turn_series(
        Store(), "environment", Principal(tenant="tenant", subject="researcher", role="researcher")
    )

    assert (
        next(item for item in projection["series"] if item["id"] == "signal:synthetic.signal:count")[
            "points"
        ][0]["value"]
        == 3
    )
    assert all("boolean" not in item["id"] and "infinite" not in item["id"] for item in projection["series"])


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
    waiting = client.post(
        f"/v1/environments/{environment}/commands",
        headers=headers,
        json={"operation": "advance", "arguments": {}},
    )
    assert waiting.status_code == 200
    assert waiting.json()["status"] == "waiting"
    assert (
        client.post(
            f"/v1/environments/{environment}/commands",
            headers=headers,
            json={"operation": "unknown", "arguments": {}},
        ).json()["error"]["code"]
        == "invalid_request"
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
            json={"participant": "alice", "unexpected": True},
        ).json()["error"]["code"]
        == "invalid_request"
    )
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
    viewer = client.get("/")
    assert viewer.status_code == 200
    assert "What am I looking at?" in viewer.text
    assert "It does not run agents or change the environment." in viewer.text
    assert "No environment sessions yet" in viewer.text
    assert "Use the same <code>--store</code> and <code>--tenant</code> values" in viewer.text
    for asset in ("app.js", "timeline.js", "client.js", "types.js", "style.css"):
        assert client.get(f"/viewer/{asset}").status_code == 200
    assert client.get("/viewer/private.txt").status_code == 404

    assert client.get("/v1/environments", headers={"Authorization": "Basic invalid"}).status_code == 401
    assert client.get("/v1/environments", headers={"Authorization": "Bearer invalid"}).status_code == 403
    other = Principal(tenant="other", subject="researcher", role="researcher")
    other_headers = {"Authorization": "Bearer " + store.issue(other)}
    assert client.get(f"/v1/environments/{environment}", headers=other_headers).status_code == 403
    assert (
        client.get("/health", headers={"Origin": "https://attacker.invalid"}).json()["error"]["code"]
        == "cross_origin_denied"
    )
    assert client.get("/v1/environments?limit=0", headers=headers).status_code == 422


def test_http_training_export_entitlement_and_value_error(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)
    assert (
        client.get(f"/v1/environments/{environment}/export?format=training", headers=headers).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/environments/{environment}/credentials",
            headers=headers,
            json={"participant": "alice", "ttl": "invalid"},
        ).json()["error"]["code"]
        == "invalid_request"
    )

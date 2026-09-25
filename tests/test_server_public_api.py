import hashlib
import json
from datetime import datetime

import pytest
from _credentials import bearer
from fastapi.testclient import TestClient

from environment_harness import AgentSpec, EvidenceStore, ExperimentSpec
from environment_harness.access import _AccessContext
from environment_harness.contracts import Action, RunPolicy, ScoreReport
from environment_harness.errors import HarnessError
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.presentation import SLOTS
from environment_harness.runner import run
from environment_harness.runtime import _SessionRuntime
from environment_harness.server import create_app
from environment_harness.store import uid


def service(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
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
    research_headers = {"Authorization": "Bearer " + bearer(store, researcher)}
    agent = _AccessContext(
        tenant="tenant",
        subject="alice",
        policy="participant",
        session=identifier,
        participant="alice",
    )
    agent_headers = {"Authorization": "Bearer " + bearer(store, agent)}
    return client, store, session, researcher, spec, identifier, research_headers, agent_headers


# Expected status for each route under every server-owned credential policy.
# There is no public role model: a caller sends only a bearer credential and the
# server decides from the policy it persisted beside that credential's hash.
ROUTE_POLICY_MATRIX = {
    "health": {"management": 200, "viewer": 200, "participant": 200},
    "capabilities": {"management": 200, "viewer": 200, "participant": 200},
    "create": {"management": 201, "viewer": 403, "participant": 403},
    "list": {"management": 200, "viewer": 200, "participant": 200},
    "experiment-list": {"management": 200, "viewer": 200, "participant": 403},
    "scenario-set-list": {"management": 200, "viewer": 200, "participant": 403},
    "policy-list": {"management": 200, "viewer": 200, "participant": 200},
    "snapshot-list": {"management": 200, "viewer": 200, "participant": 403},
    "source-list": {"management": 200, "viewer": 200, "participant": 403},
    "checkpoint-list": {"management": 200, "viewer": 200, "participant": 200},
    "trajectory-list": {"management": 200, "viewer": 200, "participant": 403},
    "trajectory-get": {"management": 200, "viewer": 200, "participant": 403},
    "get": {"management": 200, "viewer": 200, "participant": 200},
    "observation": {"management": 200, "viewer": 200, "participant": 200},
    "actions": {"management": 403, "viewer": 403, "participant": 200},
    "events": {"management": 200, "viewer": 200, "participant": 200},
    "turn-series": {"management": 200, "viewer": 200, "participant": 403},
    "agent-work": {"management": 200, "viewer": 200, "participant": 200},
    "commands": {"management": 202, "viewer": 403, "participant": 403},
    "credentials": {"management": 200, "viewer": 403, "participant": 403},
    "operations": {"management": 403, "viewer": 403, "participant": 200},
    "artifacts": {"management": 200, "viewer": 403, "participant": 200},
    "artifact-read": {"management": 200, "viewer": 200, "participant": 200},
    "reports": {"management": 200, "viewer": 200, "participant": 403},
    "report": {"management": 201, "viewer": 403, "participant": 403},
    "compare": {"management": 200, "viewer": 200, "participant": 403},
    "viewer": {"management": 200, "viewer": 200, "participant": 200},
    "viewer-asset": {"management": 200, "viewer": 200, "participant": 200},
}
POLICIES = ("management", "viewer", "participant")


@pytest.mark.parametrize("case", ROUTE_POLICY_MATRIX)
@pytest.mark.parametrize("policy", POLICIES)
def test_every_http_route_has_an_explicit_credential_policy_decision(tmp_path, case, policy):
    client, store, session, researcher, spec, environment, _, _ = service(tmp_path)
    principal = (
        _AccessContext(
            tenant="tenant",
            subject="alice",
            policy="participant",
            session=environment,
            participant="alice",
        )
        if policy == "participant"
        else _AccessContext(tenant="tenant", subject=policy, policy=policy)
    )
    headers = {"Authorization": "Bearer " + bearer(store, principal)}
    agent = _AccessContext(
        tenant="tenant",
        subject="alice",
        policy="participant",
        session=environment,
        participant="alice",
    )

    if case == "health":
        response = client.get("/health", headers=headers)
    elif case == "capabilities":
        response = client.get("/v1/capabilities", headers=headers)
    elif case == "create":
        response = client.post(
            "/v1/experiments",
            headers=headers | {"X-Operation-ID": "a" * 32},
            json=spec.model_dump(mode="json"),
        )
    elif case == "list":
        response = client.get("/v1/sessions", headers=headers)
    elif case == "experiment-list":
        response = client.get("/v1/experiments", headers=headers)
    elif case == "scenario-set-list":
        response = client.get("/v1/scenario-sets", headers=headers)
    elif case == "policy-list":
        response = client.get("/v1/policies", headers=headers)
    elif case == "snapshot-list":
        response = client.get("/v1/snapshots", headers=headers)
    elif case == "source-list":
        response = client.get("/v1/sources", headers=headers)
    elif case == "checkpoint-list":
        response = client.get(f"/v1/sessions/{environment}/checkpoints", headers=headers)
    elif case == "trajectory-list":
        response = client.get("/v1/trajectories", headers=headers)
    elif case == "trajectory-get":
        response = client.get(f"/v1/trajectories/{environment}", headers=headers)
    elif case == "get":
        response = client.get(f"/v1/sessions/{environment}", headers=headers)
    elif case == "observation":
        response = client.get(f"/v1/sessions/{environment}/participants/alice/observation", headers=headers)
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
            f"/v1/sessions/{environment}/participants/alice/actions",
            headers=headers,
            json=action.model_dump(mode="json"),
        )
    elif case == "events":
        response = client.get(f"/v1/sessions/{environment}/evidence", headers=headers)
    elif case == "turn-series":
        response = client.get(f"/v1/sessions/{environment}/turn-series", headers=headers)
    elif case == "agent-work":
        response = client.get(f"/v1/sessions/{environment}/invocations", headers=headers)
    elif case == "commands":
        response = client.post(
            f"/v1/sessions/{environment}/commands",
            headers=headers,
            json={"operation": "lease", "arguments": {"owner": "matrix"}},
        )
    elif case == "credentials":
        response = client.post(
            f"/v1/sessions/{environment}/participants/alice/credentials",
            headers=headers,
            json={"ttl": 900},
        )
    elif case == "operations":
        response = client.post(
            f"/v1/sessions/{environment}/operations",
            headers=headers,
            json={
                "operation_id": "matrix-operation",
                "endpoint": "https://synthetic.invalid",
                "operation": "lookup",
                "payload": {},
            },
        )
    elif case == "artifacts":
        response = client.post(f"/v1/sessions/{environment}/artifacts", headers=headers, content=b"matrix")
    elif case == "artifact-read":
        key = store.artifact(environment, researcher, b"matrix", audience=("*",))["id"]
        response = client.get(f"/v1/sessions/{environment}/artifacts/{key}", headers=headers)
    elif case == "reports":
        response = client.get(f"/v1/sessions/{environment}/scores", headers=headers)
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
            f"/v1/sessions/{environment}/scores",
            headers=headers,
            json=report.model_dump(mode="json"),
        )
    elif case == "compare":
        response = client.post("/v1/comparisons", headers=headers, json={"sessions": [environment]})
    elif case == "viewer":
        response = client.get("/", headers=headers)
    else:
        response = client.get("/viewer/app.js", headers=headers)

    expected = ROUTE_POLICY_MATRIX[case][policy]
    assert response.status_code == expected, (case, policy, response.text)


def test_http_negative_credential_matrix(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)
    protected = "/v1/sessions"
    assert client.get(protected).status_code == 401
    assert client.get(protected, headers={"Authorization": "Basic value"}).status_code == 401
    # An invalid credential is an authentication failure, never authorization.
    assert client.get(protected, headers={"Authorization": "Bearer invalid"}).status_code == 401

    expired = bearer(store, researcher)
    revoked = bearer(store, researcher)
    with store.transaction() as db:
        db.execute(
            "UPDATE credentials SET expires=0 WHERE hash=?",
            (hashlib.sha256(expired.encode()).hexdigest(),),
        )
        db.execute(
            "UPDATE credentials SET revoked=1 WHERE hash=?",
            (hashlib.sha256(revoked.encode()).hexdigest(),),
        )
    assert client.get(protected, headers={"Authorization": "Bearer " + expired}).status_code == 401
    assert client.get(protected, headers={"Authorization": "Bearer " + revoked}).status_code == 401

    other_tenant = _AccessContext(tenant="other", subject="researcher", policy="trusted-local")
    other_headers = {"Authorization": "Bearer " + bearer(store, other_tenant)}
    assert client.get(f"/v1/sessions/{environment}", headers=other_headers).status_code == 403

    second = session.create(spec, researcher, environment_id="b" * 32)["id"]
    assert client.get(f"/v1/sessions/{second}", headers=agent_headers).status_code == 403
    # Participant scope is in the route: another participant is out of bounds.
    assert (
        client.get(
            f"/v1/sessions/{environment}/participants/bob/observation", headers=agent_headers
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
    assert client.get(f"/v1/sessions/{environment}", headers=agent_headers).status_code == 403


def test_http_errors_share_one_traceable_envelope(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)

    responses = (
        (client.get("/v1/sessions"), 401, "unauthorized"),
        (client.get("/v1/sessions", headers={"Authorization": "Bearer invalid"}), 401, "unauthorized"),
        (client.get("/v1/sessions?limit=0", headers=headers), 422, "validation_error"),
        (
            client.get(
                f"/v1/sessions/{environment}/evidence",
                headers=headers | {"Last-Event-ID": "invalid"},
            ),
            422,
            "validation_error",
        ),
        (client.get("/viewer/private.txt"), 404, "not_found"),
        (client.put("/health"), 405, "method_not_allowed"),
        (client.get("/health", headers={"Origin": "https://attacker.invalid"}), 403, "forbidden"),
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

    session.observe = explode
    client = TestClient(create_app(session), base_url="http://testserver", raise_server_exceptions=False)
    with caplog.at_level("ERROR", logger="environment_harness.server"):
        response = client.get(f"/v1/sessions/{environment}/participants/alice/observation", headers=headers)

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
    responses = document["paths"]["/v1/sessions"]["get"]["responses"]
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
        "description": (
            "Opaque EnvironmentHarness credential. The server resolves it to an identity and "
            "one of its fixed management, viewer, or participant access policies."
        ),
        "scheme": "bearer",
        "bearerFormat": "opaque",
    }
    assert document["paths"]["/health"]["get"].get("security") is None
    assert document["paths"]["/v1/sessions"]["get"]["security"] == [{"BearerAuth": []}]
    # OpenAPI uses only the standard HTTP bearer scheme: no custom role,
    # permission, or principal-kind extension is published.
    serialized = json.dumps(document)
    assert "x-roles" not in serialized and "x-principal" not in serialized
    assert "permissions" not in document.get("components", {}).get("schemas", {})
    commands = document["paths"]["/v1/sessions/{session_id}/commands"]["post"]
    assert "advance" in commands["x-command-operations"]
    command_schema = document["components"]["schemas"]["CommandOperation"]
    assert set(command_schema["enum"]) == set(commands["x-command-operations"])
    credentials = document["paths"]["/v1/sessions/{session_id}/participants/{participant_id}/credentials"][
        "post"
    ]
    assert credentials["requestBody"]["content"]["application/json"]["schema"]["anyOf"][0] == {
        "$ref": "#/components/schemas/CredentialRequest"
    }
    assert credentials["x-capability"] == "participant-credentials"
    assert document["paths"]["/v1/sessions/{session_id}/operations"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/OperationIntentRequest"}
    assert {tag["name"] for tag in document["tags"]} >= {
        "Service",
        "Experiments",
        "Sessions",
        "Sources",
        "Evidence",
        "Activity",
        "Evaluation",
    }
    # The removed `/v1/environments` surface and viewer shell stay out of the
    # published contract.
    assert not any(path.startswith("/v1/environment") for path in document["paths"])
    assert "/overview" not in document["paths"]
    assert "/sessions/{session_id}" not in document["paths"]


def test_environment_session_list_uses_a_stable_bounded_cursor(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)
    for digit in ("1", "2", "3"):
        response = client.post(
            "/v1/experiments",
            headers=headers | {"X-Operation-ID": digit * 32},
            json=spec.model_dump(mode="json"),
        )
        assert response.status_code == 201
        assert response.headers["location"] == f"/v1/sessions/{digit * 32}"

    first = client.get("/v1/sessions?limit=2", headers=headers)
    cursor = first.headers["x-next-cursor"]
    second = client.get(f"/v1/sessions?limit=2&cursor={cursor}", headers=headers)

    # Management collections share one typed envelope with an opaque cursor.
    assert set(first.json()) == {"items", "nextCursor", "links"}
    assert first.json()["nextCursor"] == cursor
    assert first.json()["links"]["next"].endswith(f"cursor={cursor}")
    first_ids = [item["metadata"]["id"] for item in first.json()["items"]]
    second_ids = [item["metadata"]["id"] for item in second.json()["items"]]
    assert len(first_ids) == len(second_ids) == 2
    assert set(first_ids).isdisjoint(second_ids)
    assert f"cursor={cursor}" in first.headers["link"]
    assert second.json()["links"]["self"].endswith(f"cursor={cursor}")
    assert 'rel="next"' in first.headers["link"]
    assert "x-next-cursor" not in second.headers
    assert client.get("/v1/sessions?cursor=not-a-cursor", headers=headers).status_code == 422


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
    # Canonical browser routes use plural nouns.
    assert client.get("/overview").status_code == 200
    assert client.get("/sessions/example").status_code == 200
    assert client.get("/sessions/example/turns").status_code == 200
    assert client.get("/experiments/example").status_code == 200
    assert client.get("/experiments/example/scenarios").status_code == 200
    assert client.get("/experiments/example/scenarios/one").status_code == 200
    assert client.get("/experiments/example/sessions").status_code == 200
    assert client.get("/trajectories/source-example").status_code == 200
    assert client.get("/trajectories/source-example/records").status_code == 200
    assert client.get("/experiments/example/unknown").status_code == 404
    assert client.get("/sessions/example/unknown").status_code == 404


def test_activity_openapi_records_json_response_contracts(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)

    document = client.get("/openapi.json").json()

    assert document["paths"]["/v1/activity"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/ActivityPage"}
    assert document["paths"]["/v1/experiments/{experiment_id}/activity"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {"$ref": "#/components/schemas/ActivityPage"}
    assert document["paths"]["/v1/sessions/{session_id}/activity"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ActivityPage"}
    assert document["paths"]["/v1/activity/hierarchy"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ActivityHierarchy"}


def test_http_boundaries_reject_oversize_invalid_and_unavailable_requests(tmp_path, monkeypatch):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)

    oversized = client.post("/v1/comparisons", content=b"x" * 16777217, headers=headers)
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "payload_too_large"
    invalid_control = client.post(
        f"/v1/sessions/{environment}/commands",
        headers=headers,
        json={"operation": "control", "arguments": {"lease": {}, "command": "unknown"}},
    )
    assert invalid_control.status_code == 422
    assert invalid_control.json()["error"]["code"] == "validation_error"
    assert client.get("/v1/activity", headers=headers | {"Last-Event-ID": "invalid"}).status_code == 422

    def unavailable(*_args, **_kwargs):
        raise HarnessError("synthetic unavailable")

    monkeypatch.setattr(store, "activity", unavailable)
    failure = client.get("/v1/activity", headers=headers)
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
    listed = client.get("/v1/sessions", headers=headers).json()
    assert listed["items"][0]["metadata"]["id"] == environment
    # EnvironmentSpec is embedded in the Session, not a separate resource.
    resource = client.get(f"/v1/sessions/{environment}", headers=headers).json()
    assert resource["metadata"]["id"] == environment
    assert resource["spec"]["environment"]["id"] == "synthetic-protocol"
    assert (
        client.get(
            f"/v1/sessions/{environment}/participants/alice/observation", headers=agent_headers
        ).status_code
        == 200
    )

    http_identifier = "a" * 32
    created = client.post(
        "/v1/experiments",
        headers=headers | {"X-Operation-ID": http_identifier},
        json=spec.model_dump(mode="json"),
    )
    assert created.status_code == 201 and created.json()["id"] == http_identifier
    assert created.headers["location"] == f"/v1/sessions/{http_identifier}"

    observation = client.get(
        f"/v1/sessions/{environment}/participants/alice/observation", headers=agent_headers
    ).json()
    action = Action(
        operation_id=uid(),
        participant="alice",
        observation_id=observation["id"],
        revision=observation["revision"],
        payload={"value": 1},
    )
    assert (
        client.post(
            f"/v1/sessions/{environment}/participants/alice/actions",
            headers=agent_headers,
            json=action.model_dump(mode="json"),
        ).status_code
        == 200
    )

    events = client.get(f"/v1/sessions/{environment}/evidence", headers=headers).json()
    assert events["events"] and events["cursor"] >= 1
    resumed = client.get(
        f"/v1/sessions/{environment}/evidence",
        headers=headers | {"Last-Event-ID": str(events["cursor"])},
    ).json()
    assert resumed == {"events": [], "cursor": events["cursor"]}
    caught_up = client.get(
        f"/v1/sessions/{environment}/evidence",
        headers=headers | {"Accept": "text/event-stream", "Last-Event-ID": str(events["cursor"])},
    )
    assert caught_up.headers["content-type"].startswith("text/event-stream")
    assert caught_up.text == ": caught up\n\n"
    assert (
        client.get(
            f"/v1/sessions/{environment}/evidence", headers=headers | {"Last-Event-ID": "invalid"}
        ).status_code
        == 422
    )
    assert client.get(f"/v1/sessions/{environment}/invocations", headers=headers).status_code == 200


def test_turn_series_bounds_long_sessions_and_preserves_range_endpoints(tmp_path):
    client, store, session, researcher, spec, environment, headers, _ = service(tmp_path)
    run(session, environment, researcher, {"alice": SyntheticAgent()}, turns=30)

    response = client.get(f"/v1/sessions/{environment}/turn-series?max_points=20", headers=headers)
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
        f"/v1/sessions/{environment}/turn-series?start_turn=10&end_turn=15&max_points=20",
        headers=headers,
    ).json()
    assert window["range"] == {"start_turn": 10, "end_turn": 15}
    assert all(10 <= point["turn"] <= 15 for item in window["series"] for point in item["points"])
    after_session = client.get(
        f"/v1/sessions/{environment}/turn-series?start_turn=40&end_turn=50&max_points=20",
        headers=headers,
    ).json()
    assert after_session["range"] == {"start_turn": 40, "end_turn": 50}
    assert all(not item["points"] for item in after_session["series"])
    assert (
        client.get(
            f"/v1/sessions/{environment}/turn-series?start_turn=15&end_turn=10",
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
        Store(), "environment", _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
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
        f"/v1/sessions/{environment}/commands",
        headers=headers,
        json={"operation": "lease", "arguments": {"owner": "http"}},
    ).json()["result"]
    assert lease["owner"] == "http"
    released = client.post(
        f"/v1/sessions/{environment}/commands",
        headers=headers,
        json={"operation": "release", "arguments": {"lease": lease}},
    )
    assert released.status_code == 202
    waiting = client.post(
        f"/v1/sessions/{environment}/commands",
        headers=headers,
        json={"operation": "advance", "arguments": {}},
    )
    assert waiting.status_code == 202
    assert waiting.json()["result"]["status"] == "waiting"
    assert (
        client.post(
            f"/v1/sessions/{environment}/commands",
            headers=headers,
            json={"operation": "unknown", "arguments": {}},
        ).json()["error"]["code"]
        == "validation_error"
    )
    assert (
        client.post(
            f"/v1/sessions/{environment}/commands",
            headers=headers,
            json={"operation": "lease", "arguments": {"unexpected": True}},
        ).status_code
        == 422
    )

    credential = client.post(
        f"/v1/sessions/{environment}/participants/alice/credentials",
        headers=headers,
        json={"ttl": 100000},
    )
    assert credential.status_code == 200
    assert store.authenticate(credential.json()["token"]).participant == "alice"
    assert (
        client.post(
            f"/v1/sessions/{environment}/participants/alice/credentials",
            headers=headers,
            json={"unexpected": True},
        ).json()["error"]["code"]
        == "validation_error"
    )
    # The participant is named by the route, so an unknown one is forbidden.
    assert (
        client.post(
            f"/v1/sessions/{environment}/participants/missing/credentials",
            headers=headers,
            json={},
        ).status_code
        == 403
    )

    prepared = client.post(
        f"/v1/sessions/{environment}/operations",
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
        f"/v1/sessions/{environment}/artifacts",
        headers=agent_headers | {"Content-Type": "application/synthetic"},
        content=b"public SDK artifact",
    )
    assert uploaded.status_code == 200
    downloaded = client.get(
        f"/v1/sessions/{environment}/artifacts/{uploaded.json()['id']}", headers=agent_headers
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
            f"/v1/sessions/{environment}/scores",
            headers=headers,
            json=report.model_dump(mode="json"),
        ).status_code
        == 201
    )
    assert len(client.get(f"/v1/sessions/{environment}/scores", headers=headers).json()["items"]) == 1


def test_http_exports_comparison_viewer_and_invalid_requests(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)

    # The corrected export is a frozen snapshot, not a session verb path.
    assert client.get(f"/v1/sessions/{environment}/export", headers=headers).status_code == 404
    frozen = client.post(f"/v1/trajectories/{environment}/snapshots", headers=headers)
    assert frozen.status_code == 201
    identity = frozen.json()["metadata"]["id"]
    assert frozen.headers["location"] == f"/v1/snapshots/{identity}"
    streamed = client.get(
        f"/v1/snapshots/{identity}/records",
        headers=headers | {"Accept": "application/x-ndjson"},
    )
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("application/x-ndjson")
    assert '"type":"session.created"' in streamed.text
    paged = client.get(f"/v1/snapshots/{identity}/records", headers=headers)
    assert paged.headers["content-type"].startswith("application/json")
    assert paged.json()["records"]
    assert (
        client.post("/v1/comparisons", headers=headers, json={"sessions": [environment]}).status_code == 200
    )
    for bad in ([], "not-a-list", [str(index) for index in range(101)]):
        assert client.post("/v1/comparisons", headers=headers, json={"sessions": bad}).status_code == 422
    viewer = client.get("/")
    assert viewer.status_code == 200
    assert "What am I looking at?" in viewer.text
    assert "It does not run agents or change the environment." in viewer.text
    assert "No environment sessions yet" in viewer.text
    assert "Use the same <code>--store</code> and <code>--tenant</code> values" in viewer.text
    for asset in ("app.js", "timeline.js", "client.js", "types.js", "style.css"):
        assert client.get(f"/viewer/{asset}").status_code == 200
    assert client.get("/viewer/private.txt").status_code == 404

    assert client.get("/v1/sessions", headers={"Authorization": "Basic invalid"}).status_code == 401
    assert client.get("/v1/sessions", headers={"Authorization": "Bearer invalid"}).status_code == 401
    other = _AccessContext(tenant="other", subject="researcher", policy="trusted-local")
    other_headers = {"Authorization": "Bearer " + bearer(store, other)}
    assert client.get(f"/v1/sessions/{environment}", headers=other_headers).status_code == 403
    assert (
        client.get("/health", headers={"Origin": "https://attacker.invalid"}).json()["error"]["code"]
        == "forbidden"
    )
    assert client.get("/v1/sessions?limit=0", headers=headers).status_code == 422


def test_http_training_export_entitlement_and_value_error(tmp_path):
    client, store, session, researcher, spec, environment, headers, agent_headers = service(tmp_path)
    # Evaluation evidence cannot be relabeled as a training dataset.
    assert (
        client.post(
            "/v1/datasets",
            headers=headers,
            json={"name": "denied", "trajectories": [environment]},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/sessions/{environment}/participants/alice/credentials",
            headers=headers,
            json={"ttl": "invalid"},
        ).json()["error"]["code"]
        == "validation_error"
    )

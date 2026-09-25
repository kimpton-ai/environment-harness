import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from environment_harness.access import _AccessContext
from environment_harness.adapters.legacy import LegacyEnvironment
from environment_harness.adapters.ors import ORSClient, ORSError, read_result
from environment_harness.adapters.remote import PROTOCOL, HTTPWorkerTransport, RemoteEnvironment
from environment_harness.contracts import Action, AgentSpec, ExperimentSpec
from environment_harness.coordinator import advance
from environment_harness.errors import Forbidden, Unavailable
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.runtime import _SessionRuntime
from environment_harness.store import EvidenceStore
from environment_harness.worker import dispatch
from environment_harness.worker_server import create_worker_app

TOKEN = "synthetic-worker-credential-for-local-tests"


@pytest.fixture
def http_server():
    requests = []
    replies = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((self.command, self.path, dict(self.headers), json.loads(body) if body else None))
            status, payload = replies.pop(0)
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, replies
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_transport_and_ors_session_lifecycle(http_server):
    origin, requests, replies = http_server
    worker = HTTPWorkerTransport(origin, TOKEN, allow_loopback=True)
    replies.append((200, b'{"result":1}'))
    assert worker({"method": "spec"}) == {"result": 1}
    assert requests[-1][1] == "/v1/worker/call"
    replies.append((500, b"private supplier error"))
    with pytest.raises(Unavailable, match="transport failed"):
        worker({})
    client = ORSClient(origin, TOKEN, allow_loopback=True)
    replies.append((200, b'{"sid":"session-1"}'))
    sid = client.create_session()
    operations = [
        (client.environments, (), {}, "/list_environments"),
        (client.splits, ("test env",), {}, "/test%20env/splits"),
        (client.tasks, ("test", "train"), {"start": 2, "stop": 4}, "/test/task_range"),
        (client.initialize, (sid, "test"), {"split": "train", "index": 2}, "/create"),
        (client.initialize, (sid, "test"), {"task_spec": {"seed": 4}}, "/create"),
        (client.prompt, (sid, "test"), {}, "/test/prompt"),
        (client.tools, (sid, "test"), {}, "/test/task_tools"),
        (client.ping, (sid,), {}, "/ping"),
        (client.close, (sid,), {}, "/delete"),
    ]
    for method, args, kwargs, path in operations:
        replies.append((200, b"{}"))
        assert method(*args, **kwargs) == {}
        assert requests[-1][1] == path
        assert requests[-1][2]["Authorization"] == "Bearer " + TOKEN
    output = {"ok": True, "output": {"blocks": [], "finished": False, "reward": 0}}
    replies.append((200, b"".join(events(("task_id", "task-1"), ("end", json.dumps(output))))))
    assert client.call(sid, "test", "move", {"x": 1}).task_id == "task-1"
    assert requests[-1][2]["X-Session-Id"] == sid
    replies.append((200, b"".join(events(("end", json.dumps(output))))))
    assert client.call(sid, "test", "move", {"x": 1}, task_id="task-1").reward == 0
    assert requests[-1][3]["task_id"] == "task-1"
    replies.append((503, b"private"))
    with pytest.raises(ORSError) as error:
        client.call(sid, "test", "move", {})
    assert error.value.outcome_unknown and error.value.session_id == sid
    replies.append((200, b'{"sid":"bad sid"}'))
    with pytest.raises(ORSError, match="invalid session"):
        client.create_session()


def test_transport_limits(http_server):
    origin, _, replies = http_server
    with pytest.raises(ValueError):
        HTTPWorkerTransport(origin, TOKEN, allow_loopback=True, timeout=0)
    worker = HTTPWorkerTransport(origin, TOKEN, allow_loopback=True, max_bytes=10)
    with pytest.raises(Unavailable, match="request exceeds"):
        worker({"large": "a" * 20})
    replies.append((200, b"a" * 20))
    with pytest.raises(Unavailable, match="response exceeds"):
        worker({})


def test_legacy_adapter_freezes_a_distinct_version_without_rewriting_spec():
    implementation = SyntheticEnvironment()
    native = implementation.spec
    legacy = native.model_dump(mode="json") | {"protocol": "world-session.v1"}
    implementation.spec = SimpleNamespace(model_dump=lambda **kwargs: legacy)
    env = LegacyEnvironment(implementation, version="2")
    assert env.spec.protocol == "environment-session.v1"
    assert env.legacy_spec["protocol"] == "world-session.v1"
    legacy["implementation"] = "mutated"
    assert env.legacy_spec["implementation"] == native.implementation
    experiment = ExperimentSpec(
        environment=env.spec, participants=(AgentSpec(id="a", implementation="external", policy_version="1"),)
    )
    state = env.initialize(experiment)
    assert env.observe(state, "a")["total"] == 0
    assert env.resolve(state, {"a": {"value": 1}}, random.Random(1), []).state["total"] == 1
    assert env.intervene(state, {"total": 2})["total"] == 2
    with pytest.raises(ValueError):
        env.initialize(experiment.model_copy(update={"environment": native}))
    with pytest.raises(ValueError):
        LegacyEnvironment(implementation, version="1")


def rpc_client(env):
    client = TestClient(create_worker_app(env, TOKEN))

    def send(body):
        response = client.post("/v1/worker/call", json=body, headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == 200, response.text
        return response.json()

    return client, send


def test_remote_environment_roundtrip_restores_state_and_rng():
    env = SyntheticEnvironment()
    _, send = rpc_client(env)
    remote = RemoteEnvironment(send, expected_spec=env.spec)
    experiment = ExperimentSpec(
        environment=env.spec, participants=(AgentSpec(id="alice", implementation="a", policy_version="1"),)
    )
    state = remote.initialize(experiment)
    assert remote.observe(state, "alice") == env.observe(state, "alice")
    left, right = random.Random(7), random.Random(7)
    result = remote.resolve(state, {"alice": {"value": 3}}, left, [])
    expected = env.resolve(state, {"alice": {"value": 3}}, right, [])
    assert result == expected
    assert left.getstate() == right.getstate()
    assert remote.intervene(state, {"total": 9}) == env.intervene(state, {"total": 9})


def test_worker_boundary_and_receipt_identity():
    client, send = rpc_client(SyntheticEnvironment())
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"Authorization": "Bearer " + TOKEN}).json()["protocol"] == PROTOCOL
    request = {"protocol": PROTOCOL, "id": "a" * 32, "method": "spec", "arguments": {}}
    assert client.post("/v1/worker/call", json=request).status_code == 401
    headers = {"Authorization": "Bearer " + TOKEN}
    assert (
        client.post("/v1/worker/call", json={**request, "method": "delete"}, headers=headers).status_code
        == 422
    )
    failed = client.post("/v1/worker/call", json={**request, "method": "initialize"}, headers=headers)
    assert failed.status_code == 422
    assert failed.json()["error"] == "worker_failure"
    with pytest.raises(ValueError):
        create_worker_app(SyntheticEnvironment(), "short")
    with pytest.raises(Unavailable):
        RemoteEnvironment(lambda body: {**send(body), "id": "b" * 32})
    with pytest.raises(Unavailable):
        RemoteEnvironment(
            send, expected_spec=SyntheticEnvironment().spec.model_copy(update={"version": "other"})
        )
    with pytest.raises(ValueError):
        dispatch(SyntheticEnvironment(), "unknown", {})


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com",
        "https://user:secret@example.com",  # pragma: allowlist secret (synthetic rejection fixture)
        "https://example.com#fragment",
        "https://example.com?a=b",
        "https://",
    ],
)
def test_transport_rejects_unsafe_origins(endpoint):
    with pytest.raises(ValueError):
        HTTPWorkerTransport(endpoint, TOKEN)
    with pytest.raises(ValueError):
        ORSClient(endpoint, TOKEN)


def test_external_agents_advance_without_hosted_model(tmp_path):
    env = SyntheticEnvironment(mode="simultaneous")
    env.spec = env.spec.model_copy(update={"phase_deadline": "coordinator"})
    session = _SessionRuntime(EvidenceStore(tmp_path), env)
    who = _AccessContext(tenant="synthetic", subject="owner", policy="trusted-local")
    experiment = ExperimentSpec(
        environment=env.spec,
        participants=tuple(
            AgentSpec(id=p, implementation="external", policy_version="1") for p in ("alice", "bob")
        ),
    )
    identity = session.create(experiment, who)["id"]
    first = None
    for participant in ("alice", "bob"):
        principal = session.participant_context(identity, who, participant)
        observation = session.observe(identity, principal)
        assert observation["payload"]["total"] == 0
        action = Action(
            operation_id=participant,
            participant=participant,
            observation_id=observation["id"],
            revision=0,
            payload={"value": 1},
        )
        assert session.submit(identity, principal, action)["status"] == "accepted"
        assert session.submit(identity, principal, action)["status"] == "accepted"
        if first is None:
            first = action
            assert advance(session, identity, who)["status"] == "waiting"
        with pytest.raises(Forbidden):
            advance(session, identity, principal)
    assert advance(session, identity, who)["revision"] == 1
    assert session.observe(identity, who, "alice")["payload"]["total"] == 2
    assert advance(session, identity, who)["status"] == "waiting"
    assert session.store.verify(identity, who)["events"] > 0
    session.cancel(identity, who)
    assert advance(session, identity, who)["status"] == "cancelled"


def events(*records):
    for kind, value in records:
        yield f"event: {kind}\r\n".encode()
        yield f"data: {value}\r\n".encode()
        yield b"\r\n"


def test_ors_chunked_stream_keeps_receipt_and_payload():
    output = {
        "blocks": [{"type": "text", "text": "synthetic result"}],
        "reward": 0.75,
        "finished": True,
        "metadata": {"source": "synthetic"},
    }
    raw = json.dumps({"ok": True, "output": output})
    receipts = []
    result = read_result(
        events(("task_id", "operation-1"), ("chunk", raw[:20]), ("end", raw[20:])),
        session_id="episode",
        on_task=receipts.append,
    )
    assert result.output == output
    assert result.reward == 0.75 and result.finished
    assert receipts == ["operation-1"]
    assert read_result(events(("end", raw)), task_id="operation-1") == result


@pytest.mark.parametrize(
    "records",
    [
        (("task_id", "operation-1"),),
        (("task_id", "operation-1"), ("error", "private supplier exception")),
        (("end", "{}"),),
        (("chunk", "{}"),),
        (("task_id", "operation-1"), ("task_id", "operation-2")),
        (("task_id", "operation-1"), ("end", "not json")),
        (("unknown", "payload"),),
    ],
)
def test_ors_uncertain_results_never_become_success(records):
    with pytest.raises(ORSError) as caught:
        read_result(events(*records), session_id="episode")
    assert caught.value.outcome_unknown
    assert caught.value.session_id == "episode"
    assert "private supplier exception" not in str(caught.value)


@pytest.mark.parametrize(
    "output",
    [
        {},
        {"blocks": [], "finished": "true"},
        {"blocks": [], "finished": True, "reward": float("nan")},
        {"blocks": [], "finished": True, "reward": True},
    ],
)
def test_ors_rejects_malformed_tool_outputs(output):
    with pytest.raises(ORSError):
        read_result(events(("task_id", "t"), ("end", json.dumps({"ok": True, "output": output}))))


def test_ors_limits_and_explicit_failure():
    with pytest.raises(ORSError):
        read_result([b"x" * 100], max_bytes=20)
    with pytest.raises(ORSError) as caught:
        read_result(events(("task_id", "t"), ("end", '{"ok":false}')))
    assert not caught.value.outcome_unknown
    with pytest.raises(ORSError):
        read_result([b"\xff\n"])
    client = ORSClient("https://example.invalid", TOKEN)
    with pytest.raises(ValueError):
        client.initialize("s", "env")
    with pytest.raises(ValueError):
        client.initialize("s", "env", task_spec={}, split="train")
    with pytest.raises(ValueError):
        client.initialize("s", "env", task_spec={}, split="train", index=0)


def test_ors_bounds_both_directions_and_keeps_stream_failure(http_server, monkeypatch):
    import environment_harness.adapters.ors as ors

    origin, requests, replies = http_server
    client = ORSClient(origin, TOKEN, allow_loopback=True)
    monkeypatch.setattr(ors, "MAX_BYTES", 32)
    with pytest.raises(ValueError, match="request exceeds"):
        client._request("POST", "/create", {"large": "x" * 64})
    assert not requests
    replies.append((200, b"x" * 33))
    with pytest.raises(ORSError) as oversized:
        client.environments()
    assert not oversized.value.outcome_unknown
    monkeypatch.setattr(ors, "MAX_BYTES", 1024)
    replies.append((200, b"event: task_id\ndata: original\n\nevent: error\ndata: private\n\n"))
    with pytest.raises(ORSError) as uncertain:
        client.call("session", "env", "move", {})
    assert uncertain.value.task_id == "original" and uncertain.value.outcome_unknown
    result = read_result(
        [
            b": heartbeat\n",
            *events(("task_id", "t"), ("end", '{"ok":true,"output":{"blocks":[],"finished":true}}')),
        ]
    )
    assert result.finished


def test_worker_limits_and_entrypoint_remove_bootstrap_credential(monkeypatch):
    import runpy
    import sys

    import uvicorn
    from fastapi import HTTPException

    from environment_harness import plugins, worker_server

    env = SyntheticEnvironment()
    app = create_worker_app(env, TOKEN)
    headers = {"Authorization": "Bearer " + TOKEN}
    monkeypatch.setattr(worker_server, "MAX_BYTES", 128)
    with TestClient(app) as client:
        assert client.post("/v1/worker/call", content=b"x" * 129, headers=headers).status_code == 413
        response = client.post(
            "/v1/worker/call",
            json={"protocol": PROTOCOL, "id": "a" * 32, "method": "spec", "arguments": {}},
            headers=headers,
        )
        assert response.status_code == 422 and response.json()["error"] == "worker_failure"
    endpoint = next(route.endpoint for route in app.routes if route.path == "/v1/worker/call")
    with pytest.raises(HTTPException) as denied:
        endpoint(None, authorization="Bearer incorrect")
    assert denied.value.status_code == 401
    started = []
    monkeypatch.setattr(plugins, "environment", lambda name: env)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.append(kwargs))
    monkeypatch.setenv("ENVIRONMENT_WORKER_TOKEN", TOKEN)
    monkeypatch.setattr(sys, "argv", ["worker", "synthetic", "--port", "8123"])
    runpy.run_module("environment_harness.worker_server", run_name="__main__")
    import os

    assert "ENVIRONMENT_WORKER_TOKEN" not in os.environ
    assert started == [{"host": "0.0.0.0", "port": 8123, "access_log": False}]


@pytest.mark.parametrize(
    "mode,loss",
    [("sequential", None), ("event", None), ("simultaneous", "before"), ("simultaneous", "after")],
)
def test_coordinator_respects_scheduling_and_lost_authority(tmp_path, monkeypatch, mode, loss):
    from contextlib import contextmanager

    from environment_harness import coordinator
    from environment_harness.errors import Conflict

    env = SyntheticEnvironment(mode=mode)
    env.spec = env.spec.model_copy(update={"phase_deadline": "coordinator"})
    session = _SessionRuntime(EvidenceStore(tmp_path), env)
    who = _AccessContext(tenant="synthetic", subject="owner", policy="trusted-local")
    identity = session.create(
        ExperimentSpec(
            environment=env.spec,
            participants=(AgentSpec(id="alice", implementation="external", policy_version="1"),),
        ),
        who,
    )["id"]
    agent = who.replace(policy="participant", subject="alice", participant="alice", session=identity)
    observation = session.observe(identity, agent)
    session.submit(
        identity,
        agent,
        Action(
            operation_id="one",
            participant="alice",
            observation_id=observation["id"],
            revision=0,
            payload={"value": 1},
        ),
    )
    if mode == "event":
        assert advance(session, identity, who)["status"] == "waiting"
        lease = session.lease(identity, who, "event-input")
        session.external_event(
            identity, who, lease, source="synthetic", cursor=1, event_time=1, payload={"signal": True}
        )
        session.release(identity, who, lease)
    failures = []
    original = coordinator.writer

    @contextmanager
    def fenced(*args, **kwargs):
        with original(*args, **kwargs) as (lease, errors):
            if loss == "before":
                failures.append(RuntimeError("synthetic lost writer"))
            yield lease, failures

    monkeypatch.setattr(coordinator, "writer", fenced)
    resolve = session.resolve

    def commit(*args, **kwargs):
        result = resolve(*args, **kwargs)
        if loss == "after":
            failures.append(RuntimeError("synthetic lost writer"))
        return result

    monkeypatch.setattr(session, "resolve", commit)
    if loss:
        with pytest.raises(Conflict, match="lost authority"):
            advance(session, identity, who)
        assert session.get(identity, who)["revision"] == (1 if loss == "after" else 0)
    else:
        assert advance(session, identity, who)["revision"] == 1

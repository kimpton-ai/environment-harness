import asyncio
import base64
import io
import json
import random
import sys
import types
import urllib.error

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from environment_harness import backends, hosted, plugins, receipts, worker
from environment_harness.adapters.programs import HTTPAgent, InstrumentedModel, MCPTools
from environment_harness.client import EnvironmentClient, NoRedirect, ServiceError
from environment_harness.contracts import AgentSpec, ExperimentSpec, Principal
from environment_harness.errors import Conflict, Forbidden, HarnessError, Unsupported
from environment_harness.fixtures import SyntheticEnvironment


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit):
        assert limit == 16777217
        return self.body


def test_remote_client_validates_transport_and_builds_public_requests(monkeypatch):
    for endpoint in (
        "http://example.test",
        "https://user@example.test",
        "https://example.test?secret=yes",
        "https://example.test/#fragment",
    ):
        with pytest.raises(ValueError, match="HTTPS endpoint"):
            EnvironmentClient(endpoint, "token")

    client = EnvironmentClient("http://127.0.0.1:8000/", "token", allow_loopback=True, timeout=4)
    seen = []

    class Opener:
        def open(self, request, timeout):
            seen.append((request, timeout))
            return Response(b'{"ok":true}')

    client.opener = Opener()
    assert client.request("POST", "/v1/test", {"value": 1}, operation_id="op") == {"ok": True}
    request, timeout = seen.pop()
    assert timeout == 4
    assert request.full_url == "http://127.0.0.1:8000/v1/test"
    assert request.get_header("Authorization") == "Bearer token"
    assert request.get_header("X-operation-id") == "op"
    assert request.data == b'{"value":1}'

    monkeypatch.setattr(
        client, "request", lambda method, path, body=None, **kwargs: (method, path, body, kwargs)
    )
    spec = ExperimentSpec(
        environment=SyntheticEnvironment().spec,
        participants=(AgentSpec(id="a", implementation="test", policy_version="1"),),
    )
    assert client.create(spec)[0:3] == ("POST", "/v1/environments", spec.model_dump(mode="json"))
    assert client.list(limit=25, cursor="a" * 32)[1].endswith("/v1/environments?limit=25&cursor=" + "a" * 32)
    assert client.get("a/b")[1].endswith("a%2Fb")
    assert client.observe("env", "a/b")[1].endswith("?participant=a%2Fb")
    assert client.submit("env", {"value": 1})[1].endswith("/actions")
    assert client.command("env", "cancel", reason="test")[2]["arguments"] == {"reason": "test"}
    assert client.agent_work("env")[1].endswith("/agent-work")
    assert client.cancel("env")[2]["operation"] == "cancel"
    assert client.advance("env")[2]["operation"] == "advance"
    assert client.credentials("env", "a", ttl=30)[2] == {"participant": "a", "ttl": 30}
    assert client.reports("env")[1].endswith("/reports")
    assert client.events("env", 7)[1].endswith("events?after=7")


def test_remote_client_redacts_failures_and_bounds_responses():
    client = EnvironmentClient("https://example.test", "credential")

    class Opener:
        error = None

        def open(self, *_args, **_kwargs):
            raise self.error

    opener = Opener()
    client.opener = opener
    body = json.dumps(
        {
            "error": {
                "code": "forbidden",
                "message": "Environment unavailable",
                "status": 403,
                "request_id": "a" * 32,
                "timestamp": "2026-09-21T12:00:00.000Z",
                "details": [{"field": "environment", "message": "Unavailable", "type": "forbidden"}],
            }
        }
    ).encode()
    opener.error = urllib.error.HTTPError(
        "https://example.test",
        403,
        "forbidden",
        {"Content-Type": "application/json"},
        io.BytesIO(body),
    )
    with pytest.raises(ServiceError, match="HTTP 403: Environment unavailable") as structured:
        client.request("GET", "/")
    assert structured.value.code == "forbidden"
    assert structured.value.status == 403
    assert structured.value.request_id == "a" * 32
    assert structured.value.details == [
        {"field": "environment", "message": "Unavailable", "type": "forbidden"}
    ]

    opener.error = urllib.error.HTTPError("https://example.test", 403, "secret body", {}, None)
    with pytest.raises(HarnessError, match="HTTP 403") as denied:
        client.request("GET", "/")
    assert "credential" not in str(denied.value) and "secret body" not in str(denied.value)
    opener.error = urllib.error.URLError("private network detail")
    with pytest.raises(HarnessError, match="reconcile"):
        client.request("POST", "/")

    client.opener = type("Large", (), {"open": lambda *_args, **_kwargs: Response(b"x" * 16777217)})()
    with pytest.raises(HarnessError, match="size limit"):
        client.request("GET", "/")
    with pytest.raises(HarnessError, match="redirect refused"):
        NoRedirect().redirect_request(None, None, 302, "found", {}, "https://other.test")


def test_client_replay_stops_at_empty_page(monkeypatch):
    client = EnvironmentClient("https://example.test", "token")
    pages = iter(
        [
            {"events": [{"seq": 1}, {"seq": 2}], "cursor": 2},
            {"events": [], "cursor": 2},
        ]
    )
    monkeypatch.setattr(client, "events", lambda *_args: next(pages))
    assert list(client.replay("env")) == [{"seq": 1}, {"seq": 2}]


def test_plugin_discovery_loading_and_diagnostics(monkeypatch):
    distribution = types.SimpleNamespace(name="installed-package")
    entry = types.SimpleNamespace(
        name="custom", value="package:Environment", dist=distribution, load=lambda: SyntheticEnvironment
    )
    monkeypatch.setattr(plugins, "entry_points", lambda **kwargs: [entry])
    assert plugins.discover() == [
        {"name": "custom", "distribution": "installed-package", "target": "package:Environment"}
    ]
    assert isinstance(plugins.environment("custom", mode="event"), SyntheticEnvironment)
    assert isinstance(plugins.environment("synthetic-protocol"), SyntheticEnvironment)
    monkeypatch.setattr(plugins, "entry_points", lambda **kwargs: [])
    with pytest.raises(Unsupported, match="install one"):
        plugins.environment("missing")

    monkeypatch.setattr(
        plugins,
        "version",
        lambda package: (
            "1.2.3" if package == "pydantic" else (_ for _ in ()).throw(plugins.PackageNotFoundError)
        ),
    )
    monkeypatch.setattr(plugins, "discover", lambda: [{"name": "safe"}])
    report = plugins.doctor()
    assert report["dependencies"]["pydantic"] == "1.2.3"
    assert report["dependencies"]["modal"] is None
    assert report["plugins"] == [{"name": "safe"}]
    assert report["hosted_qualification"] == "not asserted by installation"


def test_receipts_are_signed_verified_and_fail_closed():
    private = Ed25519PrivateKey.generate()
    envelope = receipts.ReceiptSigner("supplier-1", private).sign({"result": "synthetic"})
    assert receipts.verify_receipt(envelope, {"supplier-1": private.public_key()}) == {"result": "synthetic"}
    for bad in (
        envelope | {"algorithm": "none"},
        envelope | {"key_id": "unknown"},
        envelope | {"signature": "not base64!"},
        envelope | {"signature": base64.b64encode(b"invalid").decode()},
    ):
        with pytest.raises(Conflict):
            receipts.verify_receipt(bad, {"supplier-1": private.public_key()})


def test_docker_backend_enforces_isolation_and_ownership(monkeypatch):
    with pytest.raises(ValueError, match="pinned"):
        backends.DockerBackend("image:latest")
    backend = backends.DockerBackend("image@sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="identity"):
        backends.identity("../escape")
    with pytest.raises(Forbidden, match="networked"):
        backend.start(["agent"], identity="safe", limits={"network": "bridge"})

    calls = []

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        if args[1] == "run":
            return types.SimpleNamespace(returncode=0, stdout="abc123def456\n")
        if args[1] == "inspect":
            return types.SimpleNamespace(
                stdout=json.dumps(
                    [
                        {
                            "Config": {"Labels": {"environment-harness": "true"}},
                            "State": {"Status": "running", "ExitCode": 0},
                        }
                    ]
                )
            )
        return types.SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(backends.subprocess, "run", execute)
    handle = backend.start(["agent", "--safe"], identity="worker_1", limits={})
    assert handle == "abc123def456"  # pragma: allowlist secret
    command = calls[0][0]
    assert "--network=none" in command and "--read-only" in command and "--cap-drop=ALL" in command
    assert backend.status(handle) == {"status": "running", "exit_code": 0}
    backend.stop(handle)
    assert calls[-1][0][:2] == ["docker", "stop"]
    with pytest.raises(Forbidden, match="handle"):
        backend.status("not-a-container")

    monkeypatch.setattr(
        backends.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(returncode=1, stdout=""),
    )
    with pytest.raises(Conflict, match="could not start"):
        backend.start(["agent"], identity="worker", limits={})


def test_docker_backend_rejects_unowned_container(monkeypatch):
    backend = backends.DockerBackend("image@sha256:" + "a" * 64)
    monkeypatch.setattr(
        backends.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(stdout='[{"Config":{"Labels":{}},"State":{}}]'),
    )
    with pytest.raises(Forbidden, match="not owned"):
        backend.status("abcdef123456")  # pragma: allowlist secret


def test_modal_backend_uses_network_blocking(monkeypatch):
    calls = []

    class Sandbox:
        @staticmethod
        def create(*command, **kwargs):
            calls.append((command, kwargs))
            return types.SimpleNamespace(object_id="modal-1")

        @staticmethod
        def from_id(handle):
            return types.SimpleNamespace(
                poll=lambda: None if handle == "running" else 9,
                terminate=lambda: calls.append(("terminate", handle)),
            )

    monkeypatch.setitem(sys.modules, "modal", types.SimpleNamespace(Sandbox=Sandbox))
    backend = backends.ModalBackend("app", "image")
    assert backend.start(["agent"], identity="worker", limits={}) == "modal-1"
    assert calls[0][1]["block_network"] is True
    assert backend.status("running") == {"status": "running", "exit_code": None}
    assert backend.status("done") == {"status": "exited", "exit_code": 9}
    backend.stop("done")


def test_hosted_adapters_and_object_storage(monkeypatch):
    row = hosted.Row(first=1, second=2)
    assert row[0] == 1 and row["second"] == 2

    raw = types.SimpleNamespace(
        fetchone=lambda: {"value": 1}, fetchall=lambda: [{"value": 1}], __iter__=lambda self: iter([])
    )
    cursor = hosted.Cursor(raw)
    assert cursor.fetchone()["value"] == 1
    assert cursor.fetchall()[0][0] == 1

    class Rows:
        def fetchone(self):
            return None

        def __iter__(self):
            return iter([{"value": 2}])

    empty = hosted.Cursor(Rows())
    assert empty.fetchone() is None
    assert list(empty)[0]["value"] == 2

    executed = []
    connection = hosted.Connection(
        types.SimpleNamespace(execute=lambda sql, params: executed.append((sql, params)) or raw)
    )
    connection.execute("SELECT ?", (1,))
    assert executed == [("SELECT %s", (1,))]
    with pytest.raises(ValueError, match="schema"):
        hosted.PostgresEvidenceStore("dsn", object(), schema="bad-name")

    class Body:
        closed = False

        def read(self, limit):
            assert limit == 16777217
            return b"artifact"

        def close(self):
            self.closed = True

    body = Body()
    client = types.SimpleNamespace(
        put_object=lambda **kwargs: executed.append(kwargs),
        get_object=lambda **kwargs: {"Body": body},
    )
    artifacts = hosted.S3Artifacts(client, "bucket", prefix="public")
    artifacts.put("env/file", b"data")
    assert executed[-1]["IfNoneMatch"] == "*" and executed[-1]["Key"] == "public/env/file"
    assert artifacts.get("env/file") == b"artifact" and body.closed


def test_postgres_connect_and_migrations_are_scoped_and_immutable(monkeypatch):
    operations = []

    class Database:
        applied = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params=()):
            operations.append((sql, params))
            if isinstance(sql, str) and sql.startswith("SELECT sha256"):
                return types.SimpleNamespace(fetchone=lambda: self.applied)
            return types.SimpleNamespace(fetchone=lambda: None)

    database = Database()

    class SQL(str):
        def format(self, *arguments):
            return SQL(super().format(*arguments))

    postgres_sql = types.SimpleNamespace(SQL=SQL, Identifier=lambda value: f'"{value}"')
    psycopg = types.ModuleType("psycopg")
    psycopg.connect = lambda dsn, row_factory: database
    psycopg.sql = postgres_sql
    rows = types.ModuleType("psycopg.rows")
    rows.DictRow = dict
    rows.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)

    store = hosted.PostgresEvidenceStore("postgresql://synthetic", object(), schema="public_harness")
    assert store.connect() is database
    assert operations[-1][0] == 'SET search_path TO "public_harness"'

    migration = types.SimpleNamespace(name="001_initial.sql", read_text=lambda: "CREATE TABLE safe(id int)")
    ignored = types.SimpleNamespace(name="README.md", read_text=lambda: "ignored")
    resources = types.SimpleNamespace(
        joinpath=lambda _name: types.SimpleNamespace(iterdir=lambda: [ignored, migration])
    )
    monkeypatch.setattr("importlib.resources.files", lambda _package: resources)
    store.initialize()
    assert any(sql == b"CREATE TABLE safe(id int)" for sql, _params in operations)
    assert any(
        isinstance(sql, str) and sql.startswith("INSERT INTO schema_migrations")
        for sql, _params in operations
    )

    import hashlib

    database.applied = {"sha256": hashlib.sha256(migration.read_text().encode()).hexdigest()}
    before = len(operations)
    store.initialize()
    assert not any(sql == b"CREATE TABLE safe(id int)" for sql, _params in operations[before:])
    database.applied = {"sha256": "changed"}
    with pytest.raises(ValueError, match="checksum changed"):
        store.initialize()


def test_postgres_store_queries_transactions_and_artifacts(monkeypatch):
    store = hosted.PostgresEvidenceStore("dsn", types.SimpleNamespace())
    db = types.SimpleNamespace(
        execute=lambda sql, params=(): types.SimpleNamespace(
            fetchone=lambda: {"id": params[0]}, fetchall=lambda: [{"seq": 1}]
        )
    )
    assert store._environment_row(db, "env")["id"] == "env"
    who = Principal(tenant="t", subject="a", role="agent", participant="a")
    assert store._event_page(db, "env", 0, who, 10) == [{"seq": 1}]

    class Context:
        def __enter__(self):
            return "raw"

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(store, "connect", Context)
    with store.transaction() as wrapped:
        assert isinstance(wrapped, hosted.Connection) and wrapped.connection == "raw"

    written = []
    store.object_store = types.SimpleNamespace(
        put=lambda key, data: written.append((key, data)), get=lambda key: b"read:" + key.encode()
    )
    store._write_artifact("env", "key", b"data")
    assert written == [("env/key", b"data")]
    assert store._read_artifact("env", "key") == b"read:env/key"


def test_http_model_and_mcp_adapters_record_boundaries(monkeypatch):
    agent = HTTPAgent("http://localhost:8000", "token", "impl", allow_loopback=True)
    monkeypatch.setattr(agent.client, "request", lambda *args: args)
    assert agent.act({"state": 1}) == ("POST", "/act", {"observation": {"state": 1}})
    with pytest.raises(Unsupported):
        agent.checkpoint()
    with pytest.raises(Unsupported):
        agent.restore({})

    appended = []

    class Store:
        class Transaction:
            def __enter__(self):
                return object()

            def __exit__(self, *_args):
                return None

        transaction = Transaction
        environment = staticmethod(lambda *_args: {"revision": 2})
        append = staticmethod(lambda *args: appended.append(args[3:]))

    principal = Principal(tenant="t", subject="a", role="agent", participant="a")
    model = InstrumentedModel(Store(), "env", principal, lambda request: {"text": request})
    assert model.call("hello")["text"] == "hello"
    assert [item[0] for item in appended] == ["model.request", "model.response"]

    def fail(_request):
        raise RuntimeError("provider failed")

    model.generate = fail
    with pytest.raises(RuntimeError):
        model.call("hello")
    assert appended[-1][0] == "model.failure"


def test_mcp_adapter_enforces_allowlist_and_records():
    async def scenario():
        records = []

        async def record(kind, value):
            records.append((kind, value))

        result = types.SimpleNamespace(model_dump=lambda **_kwargs: {"content": "synthetic"})

        class Session:
            async def call_tool(self, name, arguments):
                assert name == "lookup" and arguments == {"id": 1}
                return result

        tools = MCPTools(Session(), ["lookup"], record)
        assert await tools.call("lookup", {"id": 1}) == {"content": "synthetic"}
        assert [kind for kind, _value in records] == ["tool.intent", "tool.result"]
        with pytest.raises(Unsupported, match="allowlist"):
            await tools.call("shell", {})

    asyncio.run(scenario())


def test_worker_protocol_frames_methods_and_redacts_failures(monkeypatch, capsys):
    transition = types.SimpleNamespace(model_dump=lambda **_kwargs: {"state": {"turn": 1}})

    class Environment:
        spec = SyntheticEnvironment().spec

        def initialize(self, experiment):
            return {"participants": len(experiment.participants)}

        def observe(self, state, participant):
            return {"state": state, "participant": participant}

        def intervene(self, state, changes):
            return state | changes

        def resolve(self, state, actions, rng, events):
            assert rng.random() >= 0 and events == []
            return transition

    experiment = ExperimentSpec(
        environment=Environment.spec,
        participants=(AgentSpec(id="a", implementation="test", policy_version="1"),),
    )
    rng = random.Random(7)
    requests = [
        {"method": "spec", "arguments": {}},
        {"method": "initialize", "arguments": {"experiment": experiment.model_dump(mode="json")}},
        {"method": "observe", "arguments": {"state": {"turn": 0}, "participant": "a"}},
        {"method": "intervene", "arguments": {"state": {"turn": 0}, "changes": {"score": 1}}},
        {
            "method": "resolve",
            "arguments": {"state": {}, "actions": {}, "rng": rng.getstate(), "events": []},
        },
        {"method": "unknown", "arguments": {}},
    ]
    stream = b"".join((json.dumps(item) + "\n").encode() for item in requests)
    monkeypatch.setattr(worker, "environment", lambda _name: Environment())
    monkeypatch.setattr(worker.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(stream)))
    monkeypatch.setattr(worker.sys, "argv", ["worker", "synthetic"])
    worker.main()
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]["result"]["id"] == "synthetic-protocol"
    assert output[1]["result"] == {"participants": 1}
    assert output[2]["result"]["participant"] == "a"
    assert output[3]["result"]["score"] == 1
    assert output[4]["result"]["transition"]["state"] == {"turn": 1}
    assert output[5] == {"error": "worker_failure"}

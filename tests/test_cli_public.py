import json
import os
import sys
import types

from environment_harness import cli
from environment_harness.contracts import Principal
from environment_harness.store import EvidenceStore


def invoke(monkeypatch, capsys, store, *arguments):
    monkeypatch.setattr(sys, "argv", ["environment-harness", "--store", str(store), *arguments])
    cli.main()
    return capsys.readouterr().out


def test_cli_public_session_journey(tmp_path, monkeypatch, capsys):
    store_path = tmp_path / "evidence"
    result = json.loads(invoke(monkeypatch, capsys, store_path, "quickstart", "--turns", "1"))
    environment = result["id"]

    assert environment in invoke(monkeypatch, capsys, store_path, "list")
    assert json.loads(invoke(monkeypatch, capsys, store_path, "list", "--json"))[0]["id"] == environment
    assert environment in invoke(monkeypatch, capsys, store_path, "show", environment)
    assert (
        json.loads(invoke(monkeypatch, capsys, store_path, "show", environment, "--json"))["id"]
        == environment
    )
    assert "Revision" in invoke(monkeypatch, capsys, store_path, "timeline", environment)
    timeline = json.loads(
        invoke(
            monkeypatch,
            capsys,
            store_path,
            "timeline",
            environment,
            "--json",
            "--participant",
            "alice",
            "--kind",
            "action",
        )
    )
    assert isinstance(timeline, list)
    assert invoke(monkeypatch, capsys, store_path, "replay", environment).startswith("{")
    assert invoke(monkeypatch, capsys, store_path, "export", environment).startswith("{")
    assert len(invoke(monkeypatch, capsys, store_path, "token").strip()) > 20
    assert len(invoke(monkeypatch, capsys, store_path, "token", "--environment", environment).strip()) > 20
    assert (
        len(
            invoke(
                monkeypatch,
                capsys,
                store_path,
                "token",
                "--environment",
                environment,
                "--participant",
                "alice",
            ).strip()
        )
        > 20
    )
    attached = json.loads(invoke(monkeypatch, capsys, store_path, "attach", environment))
    assert attached["id"] == environment

    checkpoint = json.loads(invoke(monkeypatch, capsys, store_path, "checkpoint", environment))
    branch = json.loads(
        invoke(
            monkeypatch,
            capsys,
            store_path,
            "branch",
            environment,
            checkpoint["id"],
            "--interventions",
            '{"total":5}',
        )
    )
    assert branch["parent"] == environment
    assert "Compared" in invoke(monkeypatch, capsys, store_path, "compare", environment, branch["id"])
    compared = json.loads(
        invoke(monkeypatch, capsys, store_path, "compare", environment, branch["id"], "--json")
    )
    assert compared["environments"]
    assert json.loads(invoke(monkeypatch, capsys, store_path, "resume", environment))["status"] == "running"
    assert json.loads(invoke(monkeypatch, capsys, store_path, "cancel", environment))["status"] == "cancelled"


def test_cli_training_export_and_doctor(tmp_path, monkeypatch, capsys):
    store_path = tmp_path / "training"
    assert "protocol" in json.loads(invoke(monkeypatch, capsys, store_path, "doctor"))
    result = json.loads(invoke(monkeypatch, capsys, store_path, "quickstart", "--turns", "1", "--training"))
    exported = invoke(monkeypatch, capsys, store_path, "export", result["id"], "--training")
    assert '"schema":"environment-rollout.v1"' in exported


def test_cli_serve_uses_loopback_and_protected_credential(tmp_path, monkeypatch, capsys):
    store_path = tmp_path / "server"
    seen = {}

    class Server:
        def __init__(self, config):
            seen["config"] = config

        def run(self):
            seen["ran"] = True

    class Config:
        def __init__(self, app, **kwargs):
            self.app = app
            self.kwargs = kwargs

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(Server=Server, Config=Config))
    output = invoke(monkeypatch, capsys, store_path, "serve", "--port", "9876")
    assert "http://127.0.0.1:9876" in output and seen["ran"]
    assert seen["config"].kwargs["host"] == "127.0.0.1"
    token = store_path / "researcher-token"
    assert token.exists()
    if os.name != "nt":
        assert token.stat().st_mode & 0o777 == 0o600


def test_cli_serve_exits_cleanly_on_keyboard_interrupt(tmp_path, monkeypatch, capsys):
    class Server:
        def __init__(self, _config):
            pass

        def run(self):
            raise KeyboardInterrupt

    class Config:
        def __init__(self, app, **kwargs):
            self.app = app
            self.kwargs = kwargs

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(Server=Server, Config=Config))

    invoke(monkeypatch, capsys, tmp_path / "store", "serve")


def test_cli_inspect_forwards_limit(tmp_path, monkeypatch, capsys):
    store = EvidenceStore(tmp_path / "store")
    who = Principal(tenant="local", subject="local-researcher", role="researcher")
    assert who.role == "researcher"
    assert json.loads(invoke(monkeypatch, capsys, store.root, "list", "--json", "--limit", "1")) == []

import json
import sys
import threading
import types

from fastapi.testclient import TestClient

from environment_harness import cli, local_viewer
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
    assert result["status"] == "completed"
    assert result["revision"] == 1
    assert result["experiment"]["policy"]["max_turns"] == 1

    recorded = EvidenceStore(store_path)
    researcher = Principal(tenant="local", subject="local-researcher", role="researcher")
    events = list(recorded.replay(environment, researcher))
    assert {"checkpoint.committed", "artifact", "report"} <= {event["kind"] for event in events}
    assert any(
        event["kind"] == "action.attempted" and event["payload"]["receipt"]["status"] == "blocked"
        for event in events
    )
    reports = recorded.reports(environment, researcher)
    assert {"synthetic_total", "turn_reward", "executed_actions"} <= reports[0]["report"]["metrics"].keys()
    assert reports[0]["report"]["findings"][0]["category"] == "malformed"

    assert environment in invoke(monkeypatch, capsys, store_path, "list")
    assert environment in {
        item["id"] for item in json.loads(invoke(monkeypatch, capsys, store_path, "list", "--json"))
    }
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


def test_quickstart_builds_a_grouped_experiment_and_review_routes(tmp_path, monkeypatch, capsys):
    store_path = tmp_path / "review"

    result = json.loads(invoke(monkeypatch, capsys, store_path, "quickstart", "--turns", "2"))

    experiment = result["demo_experiment"]
    assert experiment["name"] == "Customer support workflow"
    assert experiment["status"] == "succeeded"
    assert experiment["completed"] == experiment["total"] == 6
    assert experiment["turns"] == 2
    assert {session["turns"] for session in experiment["sessions"]} == {2}
    assert {(session["scenario_id"], session["trial"]) for session in experiment["sessions"]} == {
        (scenario, trial)
        for scenario in ("refund-request", "damaged-delivery", "account-recovery")
        for trial in (1, 2)
    }
    assert [(item["name"], item["total"]) for item in result["demo_experiments"]] == [
        ("Customer support workflow", 6),
        ("Policy boundary checks", 4),
    ]
    assert [item["turns"] for item in result["demo_experiments"]] == [2, 1]
    assert [item["name"] for item in result["demo_environment_sessions"]] == [
        "Refund escalation review",
        "Account recovery review",
        "Team handoff review",
    ]
    assert result["review"] == {
        "home": "/home",
        "experiment": f"/experiment/{experiment['id']}",
        "environment_session": f"/session/{result['id']}/overview",
    }

    recorded = EvidenceStore(store_path)
    researcher = Principal(tenant="local", subject="local-researcher", role="researcher")
    snapshot = recorded.activity_snapshot(researcher)
    assert snapshot["experiments"][0]["id"] == experiment["id"]
    assert {
        scenario["metadata"]["name"] for item in snapshot["experiments"] for scenario in item["scenarios"]
    } == {
        "Refund request",
        "Damaged delivery",
        "Account recovery",
        "Low-confidence escalation",
        "High-confidence resolution",
    }
    assert {session["current_turn"] for item in snapshot["experiments"] for session in item["sessions"]} == {
        1,
        2,
    }
    assert {item["frozen"]["scenario_metadata"]["name"] for item in snapshot["standalone"]} == {
        "Refund escalation review",
        "Account recovery review",
        "Team handoff review",
    }
    experiment_session_ids = [session["id"] for session in experiment["sessions"]]
    reports = [recorded.reports(environment, researcher) for environment in experiment_session_ids]
    assert all(len(records) == 1 for records in reports)
    assert all(
        {"synthetic_total", "cumulative_reward", "executed_actions"} == set(records[0]["report"]["metrics"])
        for records in reports
    )
    comparison = json.loads(
        invoke(monkeypatch, capsys, store_path, "compare", *experiment_session_ids, "--json")
    )
    assert all(item["latest_report"] is not None for item in comparison["environments"])
    assert {group["metric"] for group in comparison["metric_groups"]} == {
        "synthetic_total",
        "cumulative_reward",
        "executed_actions",
    }
    assert all(group["reported_environments"] == 6 for group in comparison["metric_groups"])


def test_cli_training_export_and_doctor(tmp_path, monkeypatch, capsys):
    store_path = tmp_path / "training"
    assert "protocol" in json.loads(invoke(monkeypatch, capsys, store_path, "doctor"))
    result = json.loads(invoke(monkeypatch, capsys, store_path, "quickstart", "--turns", "1", "--training"))
    exported = invoke(monkeypatch, capsys, store_path, "export", result["id"], "--training")
    assert '"schema":"environment-rollout.v1"' in exported


def test_cli_serve_uses_loopback_local_access_without_writing_a_credential(tmp_path, monkeypatch, capsys):
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
    assert "Credential" not in output
    assert seen["config"].kwargs["host"] == "127.0.0.1"
    assert seen["config"].kwargs["proxy_headers"] is False
    token = store_path / "researcher-token"
    assert not token.exists()
    client = TestClient(
        seen["config"].app,
        base_url="http://127.0.0.1:9876",
        client=("127.0.0.1", 50000),
    )
    assert client.get("/viewer/config").json() == {"authentication": "local"}
    response = client.post("/local/connect", headers={"Origin": "http://127.0.0.1:9876"})
    assert response.status_code == 200
    assert EvidenceStore(store_path).authenticate(response.json()["token"]).role == "researcher"


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


def test_cli_open_flag_only_launches_the_plain_viewer_url(tmp_path, monkeypatch, capsys):
    opened = []

    class Server:
        started = True
        should_exit = False

        def __init__(self, _config):
            pass

        def run(self):
            pass

    class Config:
        def __init__(self, app, **kwargs):
            self.app = app
            self.kwargs = kwargs

    class Thread:
        def __init__(self, *, target, args, daemon):
            assert daemon is True
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(Server=Server, Config=Config))
    monkeypatch.setattr(threading, "Thread", Thread)
    monkeypatch.setattr(local_viewer, "open_browser", lambda url: opened.append(url) or True)

    invoke(monkeypatch, capsys, tmp_path / "store", "serve", "--port", "9876", "--open")

    assert opened == ["http://127.0.0.1:9876/home"]


def test_cli_inspect_forwards_limit(tmp_path, monkeypatch, capsys):
    store = EvidenceStore(tmp_path / "store")
    who = Principal(tenant="local", subject="local-researcher", role="researcher")
    assert who.role == "researcher"
    assert json.loads(invoke(monkeypatch, capsys, store.root, "list", "--json", "--limit", "1")) == []

"""Check the public example's advertised outcomes and observation boundary."""

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from environment_harness import EvidenceStore, Principal, showcase
from environment_harness.fixtures import SyntheticShowcaseAgent

ROOT = Path(__file__).resolve().parents[1]


def test_branch_example_outcomes_and_private_history(tmp_path):
    spec = importlib.util.spec_from_file_location("branch_demo", ROOT / "examples/branch_comparison.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.experiment(tmp_path)
    assert report["totals"] == {report["parent"]: 10, report["branch"]: 24}
    assert report["statuses"] == {report["parent"]: "completed", report["branch"]: "completed"}
    assert report["comparison"]["metrics"]["synthetic_total"]["independent_lineages"] == 1
    assert report["comparison"]["metrics"]["synthetic_total"]["standard_error"] is None
    assert report["comparison"]["environments"][0]["participants"] == ["alice", "bob"]
    store = EvidenceStore(tmp_path)
    for environment in (report["parent"], report["branch"]):
        alice = Principal(
            tenant="local", subject="alice", role="agent", environment=environment, participant="alice"
        )
        events = json.dumps(list(store.replay(environment, alice)))
        assert "synthetic-secret-alice" in events
        assert "synthetic-secret-bob" not in events
        assert (tmp_path / f"{environment}.jsonl").is_file()


def test_custom_command_example_runs_outside_checkout(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples/custom_agent.py"), "--store", str(tmp_path / "environment")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    output = json.loads(result.stdout)
    assert output["revision"] == 4
    assert output["status"] == "completed"


def test_typed_experiment_example_creates_reviewable_grouped_sessions(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples/typed_experiment.py"),
            "--store",
            str(tmp_path / "environment-sessions"),
            "--turns",
            "1",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    summary = json.loads(result.stdout)
    assert summary["status"] == "succeeded"
    assert summary["completed"] == summary["total"] == 4
    assert {session["scenario"] for session in summary["sessions"]} == {
        "negative-start",
        "positive-start",
    }
    assert summary["review"]["path"] == f"/experiment/{summary['experiment']}"


def test_custom_environment_experiment_records_operations_scores_and_findings(tmp_path):
    store_path = tmp_path / "custom-environment"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples/custom_environment_experiment.py"),
            "--store",
            str(store_path),
            "--turns",
            "2",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    summary = json.loads(result.stdout)
    assert summary["status"] == "succeeded"
    assert summary["completed"] == summary["total"] == 4
    assert summary["review"]["path"] == f"/experiment/{summary['experiment']}"
    assert {session["inspection"]["meets_threshold"] for session in summary["sessions"]} == {
        False,
        True,
    }
    assert {session["findings"] for session in summary["sessions"]} == {1}

    store = EvidenceStore(store_path)
    researcher = Principal(tenant="local", subject="example", role="researcher")
    for session in summary["sessions"]:
        record = store.reports(session["id"], researcher)
        assert record[-1]["report"]["metrics"]["operation_receipts"] == 1
        evidence = list(store.replay(session["id"], researcher))
        assert any(event["kind"] == "operation.receipt" for event in evidence)


def test_external_environment_experiment_connects_to_a_separate_simulator(tmp_path):
    store_path = tmp_path / "external-environment"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples/external_environment_experiment.py"),
            "--store",
            str(store_path),
            "--turns",
            "2",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    summary = json.loads(result.stdout)
    assert summary["status"] == "succeeded"
    assert summary["completed"] == summary["total"] == 4
    assert summary["connection"]["transport"] == "json-lines-subprocess"
    assert summary["connection"]["worker_pid"] != summary["driver_pid"]
    assert summary["review"]["path"] == f"/experiment/{summary['experiment']}"
    assert {session["receipt"]["status"] for session in summary["sessions"]} == {"moved"}

    store = EvidenceStore(store_path)
    researcher = Principal(tenant="local", subject="example", role="researcher")
    for session in summary["sessions"]:
        reports = store.reports(session["id"], researcher)
        assert reports[-1]["report"]["metrics"]["external_operations"] == 1
        evidence = list(store.replay(session["id"], researcher))
        receipt = next(event for event in evidence if event["kind"] == "operation.receipt")
        assert receipt["payload"]["receipt"]["worker_pid"] == summary["connection"]["worker_pid"]


def test_optional_pettingzoo_example_matches_its_frozen_agent_implementation(tmp_path):
    if importlib.util.find_spec("pettingzoo") is None:
        pytest.skip("PettingZoo extra is not installed")

    result = subprocess.run(
        [sys.executable, str(ROOT / "examples/pettingzoo_rps.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert ast.literal_eval(result.stdout) == {
        "public_adapter": "PettingZoo RPS",
        "revision": 3,
        "status": "completed",
    }


def test_synthetic_showcase_rejects_invalid_fixture_inputs(tmp_path, monkeypatch):
    researcher = Principal(tenant="local", subject="researcher", role="researcher")
    store = EvidenceStore(tmp_path)
    with pytest.raises(ValueError, match="turns must be at least one"):
        showcase.create_synthetic_experiment_showcase(store, researcher, turns=0)
    with pytest.raises(ValueError, match="turns must be at least one"):
        showcase.create_synthetic_showcase(store, researcher, turns=0)
    with pytest.raises(ValueError, match="unknown synthetic showcase state"):
        SyntheticShowcaseAgent().restore({"offset": 9})

    monkeypatch.setattr(
        showcase.EnvironmentSession,
        "submit",
        lambda *_args, **_kwargs: {"status": "completed"},
    )
    with pytest.raises(RuntimeError, match="expected its out-of-schema action to be blocked"):
        showcase.create_synthetic_showcase(store, researcher, turns=1)

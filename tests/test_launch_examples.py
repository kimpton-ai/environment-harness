"""Check the public example's advertised outcomes and observation boundary."""

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from environment_harness import EvidenceStore, Principal

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

"""Install built wheels in isolation and verify the optional package boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    core = next((ROOT / "dist").glob("environment_harness-*.whl"))
    companion = next((ROOT / "dist/decisions").glob("environment_harness_decisions-*.whl"))
    with tarfile.open(next((ROOT / "dist").glob("environment_harness-*.tar.gz"))) as archive:
        assert not any(
            "environment_harness_decisions" in name or "packages/decision-runtime" in name
            for name in archive.getnames()
        )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    with tempfile.TemporaryDirectory(prefix="decision-distribution-") as directory:
        root = Path(directory)
        subprocess.run([sys.executable, "-m", "venv", str(root / "venv")], check=True, env=env)
        python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(["uv", "pip", "install", "--python", str(python), str(core)], check=True, env=env)
        smoke = """
import importlib.util
import tempfile
from environment_harness import EnvironmentHarness, Scenario
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
assert importlib.util.find_spec('environment_harness_decisions') is None
assert importlib.util.find_spec('httpx') is None
with tempfile.TemporaryDirectory() as directory:
    harness = EnvironmentHarness(directory, environment=SyntheticEnvironment, agents={'alice': SyntheticAgent})
    session = harness.run(Scenario(id='distribution', input={'starting_total': 1}), turns=1)
    assert session.status == 'succeeded'
"""
        subprocess.run([str(python), "-I", "-c", smoke], check=True, cwd=root, env=env)
        subprocess.run(
            ["uv", "pip", "install", "--no-deps", "--python", str(python), str(companion)],
            check=True,
            env=env,
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import importlib.util; "
                "from environment_harness_decisions import DecisionOperation, FakeSelector; "
                "from environment_harness_decisions.jev import JevDecisionSelector; "
                "assert importlib.util.find_spec('httpx') is None",
            ],
            check=True,
            cwd=root,
            env=env,
        )
    print("Core and companion wheels operate without TypeSafe transport installed")


if __name__ == "__main__":
    main()

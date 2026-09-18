"""Prove changed regression tests fail against the base revision."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("REGRESSION_BASE_REF", "origin/main")
PROOF_TARGETS = ("tests/test_repository_policy.py::test_pep440_prerelease_maps_to_npm_semver",)
SAFE_ENV = {
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}


def git(*arguments: str, text: bool = False):
    return subprocess.run(["git", *arguments], cwd=ROOT, check=True, capture_output=True, text=text).stdout


def require_regression_test_failure(returncode: int) -> None:
    if returncode == 0:
        raise SystemExit("changed regression tests also pass on the base revision")
    if returncode != 1:
        raise SystemExit(f"regression proof runner failed with exit code {returncode}")


def main() -> None:
    changed = set(
        git(
            "diff",
            "--name-only",
            "--diff-filter=AM",
            BASE,
            "--",
            "tests/test_*.py",
            text=True,
        ).splitlines()
    )
    changed.update(
        git(
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            "tests/test_*.py",
            text=True,
        ).splitlines()
    )
    changed = sorted(changed)
    if not changed:
        print("regression proof: no added or changed test files")
        return
    proof_files = {target.split("::", 1)[0] for target in PROOF_TARGETS}
    if not proof_files.issubset(changed):
        raise SystemExit("regression proof targets must be added or changed relative to the base revision")
    archive = git("archive", "--format=tar", BASE)
    with tempfile.TemporaryDirectory(prefix="environment-harness-regression-") as directory:
        checkout = Path(directory) / "base"
        checkout.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(checkout, filter="data")
        for relative in changed:
            source = ROOT / relative
            destination = checkout / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        env = {key: value for key, value in os.environ.items() if key in SAFE_ENV}
        env["UV_CACHE_DIR"] = str(Path(directory) / "uv-cache")
        result = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(checkout),
                "--extra",
                "server",
                "pytest",
                "-q",
                *PROOF_TARGETS,
            ],
            cwd=checkout,
            env=env,
            check=False,
        )
        require_regression_test_failure(result.returncode)
    print("regression proof: changed tests fail on the base revision")


if __name__ == "__main__":
    main()

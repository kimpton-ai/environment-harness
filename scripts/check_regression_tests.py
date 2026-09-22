"""Prove changed regression tests fail against the base revision."""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("REGRESSION_BASE_REF", "origin/main")
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


def _module_path(root: Path, module: str) -> Path:
    relative = Path("src", *module.split("."))
    source = root / relative.with_suffix(".py")
    return source if source.exists() else root / relative / "__init__.py"


def _module_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    names: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def is_new_api_collection_failure(output: str, base_root: Path, head_root: Path = ROOT) -> bool:
    """Accept collection failure only when every error names an API added by the branch."""
    collection_errors = re.findall(r"^_+ ERROR collecting .+ _+$", output, re.MULTILINE)
    missing = re.findall(
        r"^(?:E\s+)?ImportError: cannot import name '([A-Za-z_]\w*)' from "
        r"'(environment_harness(?:\.[A-Za-z_]\w*)*)'",
        output,
        re.MULTILINE,
    )
    if not collection_errors or len(missing) != len(collection_errors):
        return False
    return all(
        name in _module_names(_module_path(head_root, module))
        and name not in _module_names(_module_path(base_root, module))
        for name, module in missing
    )


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
        setup = subprocess.run(
            ["uv", "sync", "--project", str(checkout), "--extra", "server"],
            cwd=checkout,
            env=env,
            check=False,
        )
        if setup.returncode != 0:
            raise SystemExit(f"regression proof environment failed with exit code {setup.returncode}")
        result = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(checkout),
                "--no-sync",
                "--extra",
                "server",
                "pytest",
                "-q",
                *changed,
            ],
            cwd=checkout,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if result.returncode == 2 and is_new_api_collection_failure(result.stdout + result.stderr, checkout):
            print("regression proof: changed tests require public APIs absent from the base revision")
            return
        require_regression_test_failure(result.returncode)
    print("regression proof: changed tests fail on the base revision")


if __name__ == "__main__":
    main()

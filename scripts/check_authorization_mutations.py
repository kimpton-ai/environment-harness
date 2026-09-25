"""Prove focused HTTP tests kill representative authorization-check mutations."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
MUTATIONS = {
    "tenant boundary": ('or row["tenant"] != access.tenant', "or False"),
    "session scope boundary": ("or not access.scoped_to(environment)", "or False"),
    "participant generation boundary": ('or p["generation"] != access.generation', "or False"),
    "policy check": ("access.require(action)", "None"),
}


def main() -> None:
    for name, (original, replacement) in MUTATIONS.items():
        with tempfile.TemporaryDirectory(prefix="environment-harness-mutation-") as directory:
            temporary = Path(directory)
            shutil.copytree(ROOT / "src", temporary / "src")
            store = temporary / "src/environment_harness/store.py"
            source = store.read_text()
            if source.count(original) != 1:
                raise SystemExit(f"authorization mutation target drifted: {name}")
            store.write_text(source.replace(original, replacement, 1))
            subprocess.run([sys.executable, "-m", "py_compile", str(store)], check=True)
            env = {key: value for key, value in os.environ.items() if key in SAFE_ENV}
            env["PYTHONPATH"] = str(temporary / "src")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_server_public_api.py::test_http_negative_credential_matrix",
                    "tests/test_authorization.py",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 1:
                raise SystemExit(
                    f"authorization mutation was not killed cleanly: {name}\n{result.stdout}{result.stderr}"
                )
            print(f"authorization mutation killed: {name}")


if __name__ == "__main__":
    main()

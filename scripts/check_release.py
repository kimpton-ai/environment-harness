"""Install the built wheel away from the checkout and verify the public journey."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SMOKE = '''
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import environment_harness
from environment_harness import EvidenceStore, Principal

assert str(Path(environment_harness.__file__).resolve()).startswith(str(Path(sys.prefix).resolve()))
from environment_harness.client import EnvironmentClient
from importlib.resources import files
assert any(
    item.name.endswith(".sql") for item in files("environment_harness").joinpath("migrations").iterdir()
)
doctor = json.loads(subprocess.check_output([str(Path(sys.executable).parent / "environment-harness"), "doctor"]))
assert doctor["dependencies"]["environment-harness"] == "0.1.0"
assert EnvironmentClient("http://127.0.0.1:8765", "synthetic", allow_loopback=True).endpoint == "http://127.0.0.1:8765"
report = json.loads(Path("environments/demo.json").read_text())
parent, child = report["parent"], report["branch"]
assert report["totals"] == {parent: 10, child: 24}
assert report["comparison"]["metrics"]["synthetic_total"]["independent_lineages"] == 1
assert report["comparison"]["metrics"]["synthetic_total"]["standard_error"] is None
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
server = subprocess.Popen(
    [sys.executable, "-m", "environment_harness.cli", "--store", "environments", "serve", "--port", str(port)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
origin = f"http://127.0.0.1:{port}"
def request(path, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    return urlopen(Request(origin + path, headers=headers), timeout=5)
try:
    for _ in range(100):
        if server.poll() is not None:
            raise RuntimeError("Installed viewer server exited before readiness")
        try:
            assert request("/").status == 200
            break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("Installed viewer did not become ready")
    for asset in ("app.js", "client.js", "types.js", "style.css"):
        assert request("/viewer/" + asset).status == 200
    try:
        request("/v1/environments")
        raise AssertionError("Unauthenticated environment list was accepted")
    except HTTPError as error:
        assert error.code == 401
    token = Path("environments/researcher-token").read_text().strip()
    environments = json.load(request("/v1/environments", token))
    assert {parent, child} <= {environment["id"] for environment in environments}
    for environment in (parent, child):
        exported = request(f"/v1/environments/{environment}/export", token).read()
        assert len(exported.splitlines()) > 10
        assert json.load(request(f"/v1/environments/{environment}/reports", token))
    store = EvidenceStore("environments")
    alice = Principal(tenant="local", subject="alice", role="agent", environment=parent, participant="alice")
    participant_token = store.issue(alice)
    try:
        request(f"/v1/environments/{parent}/observation?participant=bob", participant_token)
        raise AssertionError("Cross-participant observation was accepted")
    except HTTPError as error:
        assert error.code == 403
    events = request(f"/v1/environments/{parent}/events", participant_token).read().decode()
    assert "synthetic-secret-alice" in events
    assert "synthetic-secret-bob" not in events
finally:
    server.terminate()
    server.wait(timeout=10)
print(json.dumps({"wheel_import": True, "parent_total": 10, "branch_total": 24,
    "single_lineage": True, "viewer_assets": True, "authenticated_export": True,
    "unauthenticated_denied": True, "cross_participant_denied": True}))
'''


def main():
    wheels = list((ROOT / "dist").glob("environment_harness-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Build exactly one release wheel before running this check")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    with tempfile.TemporaryDirectory(prefix="environment-harness-release-") as directory:
        work = Path(directory)
        subprocess.run(["uv", "venv", "--python", sys.executable, str(work / "venv")], env=env, check=True)
        python = str(work / "venv/bin/python")
        subprocess.run(
            ["uv", "pip", "install", "--python", python, str(wheels[0]) + "[server]"],
            cwd=work, env=env, check=True,
        )
        for filename in ("branch_comparison.py", "custom_agent.py", "command_agent.py"):
            shutil.copyfile(ROOT / "examples" / filename, work / filename)
        for filename, store in (("branch_comparison.py", "environments"), ("custom_agent.py", "custom")):
            completed = subprocess.run(
                [python, filename, "--store", store], cwd=work, env=env, check=True,
                capture_output=True, text=True,
            )
            print(completed.stdout, end="")
            if filename == "custom_agent.py":
                assert json.loads(completed.stdout)["revision"] == 4
        result = subprocess.run(
            [python, "-c", SMOKE], cwd=work, env=env, capture_output=True, text=True, check=True,
        )
        checks = json.loads(result.stdout)
        checks["custom_command_four_turns"] = True
        checks["artifact"] = wheels[0].name
        destination = ROOT / ".local/release-check.json"
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(json.dumps(checks, indent=2) + "\n")
        print(json.dumps(checks, indent=2))


if __name__ == "__main__":
    main()

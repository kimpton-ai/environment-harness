"""Install the built wheel away from the checkout and verify the public journey."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
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

SMOKE = """
# The documentation-acceptance walkthrough, run against the installed wheel in a
# fresh environment: record a multi-segment native trajectory, import a
# historical source across a simulated restart, inspect the independent status
# dimensions the viewer reads, freeze and re-export a snapshot, and prove that
# evaluation export needs no training entitlement.
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import environment_harness
from environment_harness import AgentSpec, EnvironmentHarness, ExperimentSpec, Scenario
from importlib.metadata import version

assert str(Path(environment_harness.__file__).resolve()).startswith(str(Path(sys.prefix).resolve()))
from environment_harness.client import EnvironmentClient
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.trajectories import SourceRecord, SourceRegistration, SourceStatusUpdate
from importlib.resources import files
assert version("environment-harness") == environment_harness.__version__ == os.environ["EXPECTED_VERSION"]
assert files("environment_harness").joinpath("py.typed").is_file()
assert any(
    item.name.endswith(".sql") for item in files("environment_harness").joinpath("migrations").iterdir()
)
doctor = json.loads(subprocess.check_output([str(Path(sys.executable).parent / "environment-harness"), "doctor"]))
assert doctor["dependencies"]["environment-harness"] == os.environ["EXPECTED_VERSION"]
assert EnvironmentClient("http://127.0.0.1:8765", "synthetic", allow_loopback=True).endpoint == "http://127.0.0.1:8765"
report = json.loads(Path("environments/demo.json").read_text())
parent, child = report["parent"], report["branch"]
assert report["totals"] == {parent: 10, child: 24}
assert report["comparison"]["metrics"]["synthetic_total"]["independent_lineages"] == 1
assert report["comparison"]["metrics"]["synthetic_total"]["standard_error"] is None

# 1. Record a native session through the only public local-execution facade, using
#    a custom runner that stops inside its budget so the session stays resumable.
def first_turn_only(control, agents, *, turns):
    return control.advance(agents, turns=1)


harness = EnvironmentHarness(
    "environments",
    environment=SyntheticEnvironment,
    agents={"alice": SyntheticAgent, "bob": SyntheticAgent},
    session_runner=first_turn_only,
)
native = harness.run(Scenario(id="walkthrough", input={}), turns=4)
assert native.status == "succeeded"
assert native.record()["status"] == "running"

# 2. Import a historical source, then resume it from the acknowledged cursor in a
#    second process so the restart-safe boundary is exercised rather than asserted.
sources = harness.sources()
source = sources.register(
    SourceRegistration(
        namespace="com.example.simulator",
        run_id="release-walkthrough",
        schema_version="example.trace.v1",
        environment={"id": "example-simulator", "version": "1"},
        participants=("alice",),
        purpose="evaluation",
    )
)


def frame(index, previous_hash):
    return SourceRecord.create(
        id=f"frame-{index}",
        position=str(index),
        previous_hash=previous_hash,
        type="com.example.simulator.observation",
        segment="segment-1",
        participant="alice",
        revision=index - 1,
        time={
            "wallTime": f"2026-09-22T15:00:0{index}Z",
            "native": ({"clock": "simulator.frame", "value": index},),
        },
        data={"frame": index},
        audience=("*",),
    )


first = frame(1, "0" * 64)
acknowledged = sources.ingest(source.id, (first,))
assert acknowledged.position == "1" and acknowledged.hash == first.source_hash
# An identical retry is idempotent and does not advance the cursor.
assert sources.ingest(source.id, (first,)).hash == acknowledged.hash

RESUME = (
    "import json,sys\\n"
    "from environment_harness import EnvironmentHarness\\n"
    "from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment\\n"
    "from environment_harness.trajectories import SourceRecord, SourceStatusUpdate\\n"
    "harness = EnvironmentHarness('environments', environment=SyntheticEnvironment,"
    " agents={'alice': SyntheticAgent})\\n"
    "sources = harness.sources()\\n"
    "status = sources.status(sys.argv[1])\\n"
    "record = SourceRecord.create(id='frame-2', position='2',"
    " previous_hash=status.acknowledged_hash,"
    " type='com.example.simulator.observation', segment='segment-1', participant='alice',"
    " revision=1, time={'wallTime': '2026-09-22T15:00:02Z',"
    " 'native': ({'clock': 'simulator.frame', 'value': 2},)},"
    " data={'frame': 2}, audience=('*',))\\n"
    "ack = sources.ingest(sys.argv[1], (record,))\\n"
    "sources.update_status(sys.argv[1], SourceStatusUpdate(collection_state='complete',"
    " execution_state='completed',"
    " termination={'terminated': True, 'truncated': False, 'reason': 'goal'},"
    " verified_outcome={'state': 'success', 'evidence': (record.id,)},"
    " terminal_position=record.position, terminal_hash=record.source_hash, backlog=0))\\n"
    "print(json.dumps({'resumed_from': status.acknowledged_position, 'position': ack.position}))\\n"
)
resumed = json.loads(
    subprocess.check_output([sys.executable, "-c", RESUME, source.id], text=True)
)
assert resumed == {"resumed_from": "1", "position": "2"}

# 3. Freeze an immutable snapshot and prove repeated export is byte-identical.
snapshot = sources.freeze(source.id)
assert snapshot.status.complete and snapshot.status.record_count == 2
export = [json.dumps(row, sort_keys=True) for row in sources.export_snapshot(snapshot.metadata.id)]
assert export == [json.dumps(row, sort_keys=True) for row in sources.export_snapshot(snapshot.metadata.id)]

# 4. Evaluation export needs no training entitlement, but a dataset does.
try:
    sources.freeze_dataset("release-walkthrough", (source.id,))
    raise AssertionError("Evaluation-only evidence was accepted into a training dataset")
except Exception as error:
    assert "training-entitled" in str(error), error

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
server = subprocess.Popen(
    [sys.executable, "-m", "environment_harness.cli", "--store", "environments", "serve", "--port", str(port)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
origin = f"http://127.0.0.1:{port}"
token = subprocess.check_output(
    [sys.executable, "-m", "environment_harness.cli", "--store", "environments", "token"],
    text=True,
).strip()
def request(path, token=None, accept=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    if accept:
        headers["Accept"] = accept
    return urlopen(Request(origin + path, headers=headers), timeout=5)
try:
    for _ in range(100):
        if server.poll() is not None:
            raise RuntimeError("Installed viewer server exited before readiness")
        try:
            assert request("/overview").status == 200
            break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("Installed viewer did not become ready")
    for asset in ("app.js", "timeline.js", "client.js", "types.js", "style.css"):
        assert request("/viewer/" + asset).status == 200
    # The four global viewer destinations and one resource deep link each resolve.
    for route in ("/overview", "/experiments", "/sessions", "/trajectories", "/comparisons",
                  f"/sessions/{parent}", f"/trajectories/{source.id}/provenance"):
        assert request(route).status == 200, route
    # The removed 0.2 surface is gone, and an unauthenticated read is refused.
    for removed in ("/v1/environments", "/home", f"/session/{parent}"):
        try:
            request(removed, token)
            raise AssertionError(f"Removed surface was served: {removed}")
        except HTTPError as error:
            assert error.code == 404, (removed, error.code)
    try:
        request("/v1/sessions")
        raise AssertionError("Unauthenticated session list was accepted")
    except HTTPError as error:
        assert error.code == 401

    client = EnvironmentClient(origin, token, allow_loopback=True)
    assert {capability["name"] for capability in client.capabilities()["capabilities"]}
    listed = client.sessions()
    assert {parent, child, native.id} <= {item["metadata"]["id"] for item in listed["items"]}

    # 5. Pause and resume over HTTP so the trajectory gains a continuation segment.
    lease = client.command(native.id, "lease", owner="walkthrough")["result"]
    client.command(native.id, "control", lease=lease, command="pause")
    client.command(native.id, "resume", lease=lease)
    client.command(native.id, "release", lease=lease)
    native.advance(turns=2)
    trajectory = client.trajectory(native.id)
    segments = trajectory["status"]["segments"]
    assert [segment["kind"] for segment in segments] == ["execution", "continuation"]
    assert segments[1]["continues"] == segments[0]["id"]
    assert segments[1]["interruption"] == "session.paused"
    # Records are a paged stream, never materialized in status.
    assert "records" not in trajectory["status"]
    assert client.trajectory_records(native.id, limit=2)["records"]

    # 6. Collection, execution, termination and verified outcome are independent.
    imported = client.trajectory(source.id)
    assert imported["status"]["collection"]["state"] == "complete"
    assert imported["status"]["execution"]["state"] == "completed"
    assert imported["status"]["termination"]["terminated"] is True
    assert imported["status"]["verifiedOutcome"]["state"] == "success"
    assert imported["status"]["collection"]["acknowledgedPosition"] == "2"

    frozen = client.snapshot(snapshot.metadata.id)
    assert frozen["status"]["complete"] is True
    ndjson = request(
        f"/v1/snapshots/{snapshot.metadata.id}/records", token, accept="application/x-ndjson"
    ).read().decode()
    # Content negotiation streams the same rows the in-process export produced.
    assert [json.dumps(json.loads(line), sort_keys=True) for line in ndjson.splitlines() if line.strip()] == export

    for session in (parent, child):
        evidence = client.evidence(session)
        assert len(evidence["events"]) > 10
        assert client.scores(session)["items"]

    # A participant credential is bound to one session, participant and generation.
    participant_token = client.credentials(parent, "alice")["token"]
    try:
        EnvironmentClient(origin, participant_token, allow_loopback=True).observe(parent, "bob")
        raise AssertionError("Cross-participant observation was accepted")
    except Exception as error:
        assert "403" in str(error), error
    events = request(f"/v1/sessions/{parent}/evidence", participant_token).read().decode()
    assert "synthetic-secret-alice" in events
    assert "synthetic-secret-bob" not in events

    comparison = client.compare([parent, child])
    assert len(comparison["metric_groups"]) == 1
    assert comparison["metric_groups"][0]["definition"]["unit"] == "count"
    # The canonical creation route accepts a strict ExperimentSpec and returns a Location.
    created = client.create_experiment(
        ExperimentSpec(
            environment=SyntheticEnvironment().spec,
            participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
            scenario="cancel-target",
        ),
        operation_id="c" * 32,
    )
    cancel_id = created["id"]
    assert cancel_id == "c" * 32
    cancelled = client.cancel(cancel_id)["result"]
    assert cancelled["status"] == "cancelled" and not cancelled["unresolved_agent_work"]
    assert client.cancel(cancel_id)["result"] == cancelled
finally:
    server.terminate()
    server.wait(timeout=10)
print(json.dumps({"wheel_import": True, "parent_total": 10, "branch_total": 24,
    "single_lineage": True, "viewer_assets": True, "viewer_destinations": True,
    "removed_surface_absent": True, "unauthenticated_denied": True,
    "cross_participant_denied": True, "multi_segment_trajectory": True,
    "restart_safe_ingestion": True, "independent_status_dimensions": True,
    "repeatable_snapshot_export": True, "evaluation_export_without_entitlement": True,
    "defined_score_groups": True, "idempotent_cancellation": True}))
"""


def main():
    wheels = list((ROOT / "dist").glob("environment_harness-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Build exactly one release wheel before running this check")
    with tempfile.TemporaryDirectory(prefix="environment-harness-release-") as directory:
        work = Path(directory)
        env = {key: value for key, value in os.environ.items() if key in SAFE_ENV}
        env["UV_CACHE_DIR"] = str(work / "uv-cache")
        env["EXPECTED_VERSION"] = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        uv = shutil.which("uv", path=env.get("PATH"))
        if not uv:
            raise SystemExit("uv is required for the installed-artifact check")
        subprocess.run([uv, "venv", "--python", sys.executable, str(work / "venv")], env=env, check=True)
        python = str(work / "venv/bin/python")
        subprocess.run(
            [
                uv,
                "export",
                "--frozen",
                "--extra",
                "server",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(work / "requirements.txt"),
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                python,
                "--require-hashes",
                "-r",
                str(work / "requirements.txt"),
            ],
            cwd=work,
            env=env,
            check=True,
        )
        subprocess.run(
            [uv, "pip", "install", "--python", python, "--no-deps", str(wheels[0])],
            cwd=work,
            env=env,
            check=True,
        )
        for filename in ("branch_comparison.py", "custom_agent.py", "command_agent.py"):
            shutil.copyfile(ROOT / "examples" / filename, work / filename)
        for filename, store in (("branch_comparison.py", "environments"), ("custom_agent.py", "custom")):
            completed = subprocess.run(
                [python, filename, "--store", store],
                cwd=work,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            print(completed.stdout, end="")
            if filename == "custom_agent.py":
                assert json.loads(completed.stdout)["revision"] == 4
        result = subprocess.run(
            [python, "-c", SMOKE],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Surface the walkthrough's own failure instead of a truncated command echo.
            sys.stderr.write(result.stdout)
            sys.stderr.write(result.stderr)
            raise SystemExit(f"installed-wheel walkthrough failed with exit code {result.returncode}")
        checks = json.loads(result.stdout)
        checks["custom_command_four_turns"] = True
        checks["artifact"] = wheels[0].name
        destination = ROOT / ".local/release-check.json"
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(json.dumps(checks, indent=2) + "\n")
        print(json.dumps(checks, indent=2))


if __name__ == "__main__":
    main()

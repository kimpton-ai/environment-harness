"""Failure-path regression tests using only local synthetic programs and state."""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.conformance import check
from environment_harness.contracts import Capabilities, RunPolicy
from environment_harness.errors import Conflict, Forbidden, Unsupported
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.operations import Operations
from environment_harness.runner import _invoke, _prepare, run
from environment_harness.server import create_app


def setup(path, *, participants=("a",), policy=None, checkpoint=True):
    env = SyntheticEnvironment()
    store = EvidenceStore(path)
    session = EnvironmentSession(store, env)
    who = Principal(tenant="local", subject="local-researcher", role="researcher")
    spec = ExperimentSpec(
        environment=env.spec,
        participants=tuple(
            AgentSpec(
                id=p, implementation=SyntheticAgent.implementation, policy_version="1", checkpoint=checkpoint
            )
            for p in participants
        ),
        policy=policy or RunPolicy(),
    )
    environment = session.create(spec, who)["id"]
    agent = Principal(tenant="local", subject="a", role="agent", environment=environment, participant="a")
    return session, who, environment, agent, spec


@pytest.mark.parametrize("replacement", ["lease", "reconcile", "cancel", "transfer"])
@pytest.mark.parametrize("fail", [False, True])
def test_old_worker_cannot_replace_recovery_or_continuation(tmp_path, replacement, fail):
    session, who, environment, principal, _ = setup(tmp_path)
    entered, finish = threading.Event(), threading.Event()

    class Slow(SyntheticAgent):
        def act(self, observation):
            entered.set()
            assert finish.wait(5)
            if fail:
                raise TimeoutError("synthetic provider lost response")
            return {"value": 1}

        def checkpoint(self):
            return {"old_worker": True}

    observation = session.observe(environment, principal)
    old = session.lease(environment, who, "old")
    work = _prepare(session, environment, principal, observation, old)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(_invoke, session, environment, principal, observation, Slow(), work, old)
        try:
            assert entered.wait(3)
            session.release(environment, who, old)
            new = session.lease(environment, who, "recovery")
            if replacement == "reconcile":
                session.reconcile_agent(
                    environment,
                    who,
                    new,
                    operation_id=work["id"],
                    response={"value": -1},
                    agent_state={"recovered": True},
                    evidence={"receipt": "synthetic"},
                )
            elif replacement == "cancel":
                session.cancel(environment, who)
            elif replacement == "transfer":
                session.transfer(environment, who, new, "a", "replacement")
        finally:
            finish.set()
        with pytest.raises((Conflict, Forbidden, TimeoutError)):
            future.result(timeout=3)
    with session.store.transaction() as db:
        row = db.execute("SELECT * FROM agent_work WHERE environment=?", (environment,)).fetchone()
        state = json.loads(
            db.execute("SELECT participants FROM environments WHERE id=?", (environment,)).fetchone()[0]
        )
    if replacement == "reconcile":
        assert json.loads(row["response"]) == {"value": -1}
        assert state["a"]["agent_state"] == {"recovered": True}
        assert row["status"] == "responded"
    else:
        assert row["response"] is None and row["status"] == "unknown"
        assert state["a"]["agent_state"] == {}


def test_dispatch_set_validated_before_any_agent_call(tmp_path):
    session, who, environment, _, _ = setup(tmp_path, participants=("a", "b"))

    class Counting(SyntheticAgent):
        calls = 0

        def act(self, observation):
            self.calls += 1
            return super().act(observation)

    correct, wrong = Counting(), Counting()
    wrong.implementation = "different@1"
    with pytest.raises(Conflict, match="implementation"):
        run(session, environment, who, {"a": correct, "b": wrong}, turns=1)
    assert correct.calls == wrong.calls == 0
    assert not any(e["kind"] == "agent.dispatched" for e in session.store.replay(environment, who))
    run(session, environment, who, {"a": correct, "b": correct}, turns=1)
    dispatches = [e for e in session.store.replay(environment, who) if e["kind"] == "agent.dispatched"]
    assert len(dispatches) == 2
    assert all(e["payload"]["implementation"] == SyntheticAgent.implementation for e in dispatches)
    assert all(len(e["payload"]["config_hash"]) == 64 for e in dispatches)


@pytest.mark.parametrize("mode", ["sequential", "simultaneous", "event"])
@pytest.mark.parametrize("deadline", ["wall", "coordinator"])
@pytest.mark.parametrize("checkpoint", [False, True])
def test_conformance_modes_and_lease_cleanup(tmp_path, mode, deadline, checkpoint):
    env = SyntheticEnvironment(mode)
    env.spec = env.spec.model_copy(
        update={"phase_deadline": deadline, "capabilities": Capabilities(checkpoint=checkpoint)}
    )
    store = EvidenceStore(tmp_path)
    spec = ExperimentSpec(
        environment=env.spec, participants=(AgentSpec(id="a", implementation="test", policy_version="1"),)
    )
    events = [{"source": "synthetic", "cursor": 1, "event_time": 0, "payload": {}}] if mode == "event" else []
    result = check(store, env, spec, lambda _: {"value": 1}, events=events)
    assert bool(result["checkpoint"]) == checkpoint
    with store.transaction() as db:
        row = db.execute("SELECT * FROM environments WHERE id=?", (result["environment"],)).fetchone()
    assert row["revision"] == 1 and row["lease_until"] == 0


def test_conformance_failure_releases_lease_and_event_requires_input(tmp_path):
    session, _, _, _, spec = setup(tmp_path)
    with pytest.raises(AssertionError):
        check(session.store, session.environment, spec, lambda _: {"value": 999})
    with session.store.transaction() as db:
        assert all(r["lease_until"] == 0 for r in db.execute("SELECT lease_until FROM environments"))
    event_env = SyntheticEnvironment("event")
    with pytest.raises(Unsupported, match="explicit input"):
        check(session.store, event_env, spec.model_copy(update={"environment": event_env.spec}), lambda _: {})


def test_cancel_is_atomic_idempotent_and_preserves_unknown_reservations(tmp_path):
    policy = RunPolicy(allowed_endpoints=("synthetic",), allowed_operations=("test",), max_cost_micros=100)
    session, who, environment, agent, _ = setup(tmp_path, policy=policy)
    operations = Operations(session.store)
    for key in ("prepared", "unknown"):
        operations.prepare(
            environment,
            agent,
            key,
            endpoint="synthetic",
            operation="test",
            payload={},
            maximum_cost_micros=10,
        )
    with session.store.transaction() as db:
        db.execute("UPDATE operations SET status='unknown' WHERE id='unknown'")
    lease = session.lease(environment, who, "runner")
    observation = session.observe(environment, agent)
    _prepare(session, environment, agent, observation, lease)
    with pytest.raises(Forbidden):
        session.cancel(environment, agent)
    with pytest.raises(Forbidden):
        session.cancel(environment, who.model_copy(update={"tenant": "other"}))
    first = session.cancel(environment, who)
    assert first == session.control(environment, who, lease, "cancel")
    assert first["unresolved_operations"] == ["unknown"]
    assert session.get(environment, who)["reserved_micros"] == 10
    assert len([e for e in session.store.replay(environment, who) if e["kind"] == "session.cancelled"]) == 1
    with pytest.raises(Conflict):
        session.renew(environment, who, lease)
    with pytest.raises(Conflict):
        session.memory(environment, agent, {"late": True})
    with session.store.transaction() as db:
        assert db.execute("SELECT status FROM agent_work").fetchone()[0] == "failed"


@pytest.mark.parametrize("transport", ["cli", "http"])
@pytest.mark.skipif(os.name == "nt", reason="asserts POSIX signal and process-state semantics")
def test_cancel_active_command_stops_parent_and_child(tmp_path, transport):
    from environment_harness.adapters.programs import CommandAgent

    session, who, environment, _, _ = setup(tmp_path / "store", checkpoint=False)
    marker = tmp_path / "processes.json"
    child_code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    code = (
        "import json,os,signal,subprocess,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        'child=subprocess.Popen([sys.executable,"-c",sys.argv[2]]); '
        "Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid])); time.sleep(60)"
    )
    program = CommandAgent(
        [sys.executable, "-c", code, str(marker), child_code], SyntheticAgent.implementation
    )
    with ThreadPoolExecutor() as pool:
        future = pool.submit(run, session, environment, who, {"a": program}, turns=1)
        try:
            for _ in range(150):
                if marker.exists():
                    break
                time.sleep(0.02)
            assert marker.exists()
            if transport == "cli":
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "environment_harness.cli",
                        "--store",
                        str(tmp_path / "store"),
                        "cancel",
                        environment,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                assert result.returncode == 0, result.stderr
                result = json.loads(result.stdout)
            else:
                token = session.store.issue(who)
                client = TestClient(create_app(session))
                response = client.post(
                    f"/v1/environments/{environment}/commands",
                    json={"operation": "cancel", "arguments": {}},
                    headers={"Authorization": "Bearer " + token},
                )
                assert response.status_code == 200
                result = response.json()
            assert result["status"] == "cancelled"
            assert result["unresolved_agent_work"]
            with pytest.raises(Conflict):
                future.result(timeout=5)
            assert session.get(environment, who)["revision"] == 0
            for pid in json.loads(marker.read_text()):
                state = subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
                ).stdout.strip()
                assert not state or state.startswith("Z"), (pid, state)
        finally:
            session.cancel(environment, who)
            if marker.exists():
                for pid in json.loads(marker.read_text()):
                    try:
                        os.kill(pid, 9)
                    except ProcessLookupError:
                        pass


def test_cli_releases_checkpoint_and_resume_leases(tmp_path):
    session, who, environment, _, _ = setup(tmp_path)
    for command in ("checkpoint", "resume"):
        result = subprocess.run(
            [sys.executable, "-m", "environment_harness.cli", "--store", str(tmp_path), command, environment],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        lease = session.lease(environment, who, "different-worker")
        session.release(environment, who, lease)


def test_phase_timeout_does_not_commit_a_late_action(tmp_path):
    session, who, environment, _, _ = setup(tmp_path)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Slow(SyntheticAgent):
        def act(self, observation):
            started.set()
            assert release.wait(10)
            finished.set()
            return {"value": 1}

    with ThreadPoolExecutor() as pool:
        future = pool.submit(run, session, environment, who, {"a": Slow()}, turns=1, phase_timeout=0.05)
        assert started.wait(2)
        try:
            with pytest.raises(TimeoutError, match="agent phase deadline exceeded"):
                future.result(timeout=5)
        finally:
            release.set()
    assert finished.wait(5)
    assert session.get(environment, who)["revision"] == 0
    assert not any(e["kind"] == "action.executed" for e in session.store.replay(environment, who))


def test_command_can_cancel_after_closing_stdout(tmp_path):
    from environment_harness.adapters.programs import CommandAgent

    signal = threading.Event()
    marker = tmp_path / "ready"
    code = (
        "import os,sys,time; from pathlib import Path; os.close(1); Path(sys.argv[1]).touch(); time.sleep(60)"
    )
    agent = CommandAgent([sys.executable, "-c", code, str(marker)], "synthetic@1")
    with ThreadPoolExecutor() as pool:
        future = pool.submit(agent.act_cancellable, {}, signal)
        try:
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.02)
            assert marker.exists()
        finally:
            signal.set()
        with pytest.raises(Conflict, match="cancelled"):
            future.result(timeout=3)

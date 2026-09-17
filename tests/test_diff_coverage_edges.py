import base64
import hashlib
import json
import subprocess
import threading
import time
import types
from contextlib import contextmanager

import pytest

from environment_harness import (
    AgentSpec,
    EnvironmentSession,
    EvidenceStore,
    ExperimentSpec,
    Principal,
    runner,
)
from environment_harness.adapters.process import ProcessEnvironment
from environment_harness.adapters.programs import CommandAgent
from environment_harness.errors import Conflict, Unavailable
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.history import inherit, reconstruct_inherited


def inherited_chunk(*, encoding="base64-json-v1", environment="parent"):
    record = {
        "environment": environment,
        "seq": 1,
        "hash": "hash",
        "audience": '["a"]',
        "body": "{}",
    }
    raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    return {
        "environment": "child",
        "revision": 0,
        "kind": "history.inherited.chunk",
        "audience": ["a"],
        "payload": {
            "environment": "parent",
            "seq": 1,
            "hash": "hash",
            "record_sha256": hashlib.sha256(raw).hexdigest(),
            "encoding": encoding,
            "part": 0,
            "parts": 1,
            "data": base64.b64encode(raw).decode(),
        },
    }


def test_inherited_records_fail_closed_for_metadata_encoding_and_identity():
    original = {
        "environment": "parent",
        "seq": 1,
        "hash": "hash",
        "audience": '["a"]',
        "kind": "synthetic",
        "body": "{}",
        "event_time": None,
    }
    with pytest.raises(Conflict, match="cannot contain"):
        inherit(types.SimpleNamespace(append=lambda *_args: None), object(), "child", 0, original, 1)

    chunk = inherited_chunk()
    with pytest.raises(Conflict, match="chunk sequence"):
        list(reconstruct_inherited([chunk, chunk]))
    with pytest.raises(Conflict, match="integrity"):
        list(reconstruct_inherited([inherited_chunk(encoding="unsupported")]))
    with pytest.raises(Conflict, match="integrity"):
        list(reconstruct_inherited([inherited_chunk(environment="wrong")]))


def test_missing_subprocess_pipes_are_explicit_failures(monkeypatch):
    process = types.SimpleNamespace(stdin=None, stdout=None)
    environment = object.__new__(ProcessEnvironment)
    environment.lock = threading.Lock()
    environment.process = process
    environment.max_bytes = 1024
    environment.timeout = 1
    closed = []
    monkeypatch.setattr(environment, "close", lambda: closed.append(True))
    with pytest.raises(Unavailable, match="pipes"):
        environment._call("spec", {})
    assert closed

    agent = CommandAgent(["synthetic"], "synthetic")
    fake = types.SimpleNamespace(stdout=None)
    monkeypatch.setattr("environment_harness.adapters.programs.subprocess.Popen", lambda *_a, **_k: fake)
    terminated = []
    monkeypatch.setattr(agent, "_terminate_group", lambda process: terminated.append(process))
    with pytest.raises(Conflict, match="output pipe"):
        agent.act({})
    assert terminated == [fake]


def test_command_cleanup_falls_back_to_direct_process_kill(monkeypatch):
    calls = []

    class Process:
        pid = 123

        def poll(self):
            return None

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired("synthetic", timeout)

        def kill(self):
            calls.append(("kill", None))

    signals = []

    def killpg(_pid, signal):
        signals.append(signal)
        if signal == 0:
            raise PermissionError

    monkeypatch.setattr("environment_harness.adapters.programs.os.killpg", killpg)
    CommandAgent._terminate_group(Process())
    assert ("kill", None) in calls
    assert calls[-1] == ("wait", None)


def test_phase_guard_and_main_loop_deadlines_are_independent(tmp_path, monkeypatch):
    failures = []
    signal = threading.Event()
    with runner.phase_guard(object(), "environment", object(), {}, failures, {"a": signal}, 0):
        time.sleep(0.25)
    assert isinstance(failures[0], TimeoutError) and signal.is_set()

    implementation = SyntheticEnvironment()
    session = EnvironmentSession(EvidenceStore(tmp_path), implementation)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    spec = ExperimentSpec(
        environment=implementation.spec,
        participants=(AgentSpec(id="a", implementation="synthetic-agent@1", policy_version="1"),),
    )
    environment = session.create(spec, researcher)["id"]

    @contextmanager
    def no_monitor(*_args, **_kwargs):
        yield

    clock = iter((0, 2, 2, 2))
    monkeypatch.setattr(runner, "phase_guard", no_monitor)
    monkeypatch.setattr(runner, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))

    class WaitingAgent:
        implementation = "synthetic-agent@1"

        def act(self, _observation):
            threading.Event().wait(0.2)
            return {"value": 1}

    with pytest.raises(TimeoutError, match="phase deadline"):
        runner.run(session, environment, researcher, {"a": WaitingAgent()}, turns=1, phase_timeout=1)

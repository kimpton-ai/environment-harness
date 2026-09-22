from pathlib import Path

import pytest

from environment_harness_decisions.legacy_contracts import (
    GoalContext, MotorCandidate, MotorProfile, MotorRequest, MotorStep,
)
from environment_harness_decisions.legacy_operation import LegacyMotorOperation
from environment_harness_decisions.legacy_errors import MotorOutcomeUnknown


class Adapter:
    implementation = "fixture-adapter.v1"
    skills = ("move",)
    group_capabilities = ()
    supports_prepared_successors = False
    native_admits_prepared_successors = False

    def __init__(self):
        self.revision = 0
        self.submissions = []
        self.before_dispatches = []
        self.receipts = {}
        self.advance_on_plan = False

    def observe(self):
        return {"revision": str(self.revision), "position": self.revision}

    def plan(self, request, observation):
        if self.advance_on_plan:
            self.revision += 1
        return (MotorCandidate(id="step", description="advance", steps=(
            MotorStep(operation="move", target=request.target, arguments=request.arguments),
        )),)

    def selection_observation(self, request, observation):
        return {"revision": observation["revision"]}

    def revalidate(self, request, observation, selected, completed_index=0):
        return (selected,)

    def execute(self, step, *, operation_id, before_dispatch, cancel, deadline):
        before_dispatch()
        self.before_dispatches.append((operation_id, step.operation))
        self.submissions.append(operation_id)
        self.revision += 1
        result = {"operation_id": operation_id, "status": "completed", "revision": str(self.revision)}
        self.receipts[operation_id] = result
        return result

    def stop(self):
        pass

    def lookup(self, operation_id):
        return self.receipts.get(operation_id)


def request():
    return MotorRequest(
        skill="move", target={"x": 1}, expected={"revision": "1"}, observation_revision="0",
        goal_revision="session-7", goal_context=GoalContext(goal_id="g", revision="directive-3", milestone_id="m"),
    )


def make_operation(tmp_path, adapter=None):
    adapter = adapter or Adapter()
    profile = MotorProfile(mode="deterministic", adapter=adapter.implementation)
    operation = LegacyMotorOperation(adapter, profile, journal=tmp_path / "motor.sqlite")
    return operation, adapter


def test_legacy_facade_executes_one_native_step_and_projects_receipt(tmp_path):
    operation, adapter = make_operation(tmp_path)
    receipt = operation.execute(
        "motor:1", {"endpoint": "motor", "operation": "motor.execute", "payload": request(), "write": True},
        10, authority=lambda binding: None,
    )
    assert receipt["status"] == "completed"
    assert receipt["request"]["goal_revision"] == "session-7"
    assert receipt["selection"]["candidate_id"] == "step"
    assert "position" not in receipt["selection"].get("model_input", {}).get("observation", {})
    assert receipt["steps"][0]["receipt"]["status"] == "completed"
    assert len(adapter.submissions) == 1
    assert len(adapter.before_dispatches) == 1
    assert operation.lookup("motor:1") == receipt


def test_legacy_facade_supports_sequential_invocations(tmp_path):
    operation, adapter = make_operation(tmp_path)
    first = operation.execute("motor:1", {"endpoint": "motor", "operation": "motor.execute", "payload": request(), "write": True}, 10, authority=lambda _: None)
    second_request = request().model_copy(update={"observation_revision": "1", "expected": {"revision": "2"}})
    second = operation.execute("motor:2", {"endpoint": "motor", "operation": "motor.execute", "payload": second_request, "write": True}, 10, authority=lambda _: None)
    assert first["status"] == second["status"] == "completed"
    assert len(adapter.submissions) == 2


def test_stale_native_observation_projects_effect_free_reason(tmp_path):
    operation, adapter = make_operation(tmp_path)
    adapter.advance_on_plan = True
    receipt = operation.execute(
        "motor:stale", {"endpoint": "motor", "operation": "motor.execute", "payload": request(), "write": True},
        10, authority=lambda _: None,
    )
    assert receipt["status"] == "blocked"
    assert receipt["effect"] == "none"
    assert receipt["reason_code"] == "stale_observation_revision"
    assert adapter.submissions == []


def test_legacy_facade_requires_immediate_native_hook(tmp_path):
    adapter = Adapter()
    def execute_without_hook(step, *, operation_id, cancel, deadline):
        return {"operation_id": operation_id, "status": "completed"}
    adapter.execute = execute_without_hook
    operation, _ = make_operation(tmp_path, adapter)
    with pytest.raises(Exception):
        operation.execute(
            "motor:1", {"endpoint": "motor", "operation": "motor.execute", "payload": request(), "write": True},
            10, authority=lambda binding: None,
        )
    assert adapter.submissions == []


def test_legacy_facade_retains_unknown_native_effect_without_resubmitting(tmp_path):
    class UnknownAdapter(Adapter):
        def execute(self, step, *, operation_id, before_dispatch, cancel, deadline):
            before_dispatch()
            self.submissions.append(operation_id)
            self.revision += 1
            self.receipts[operation_id] = {"operation_id": operation_id, "status": "completed",
                                           "revision": str(self.revision)}
            raise RuntimeError("crash after native submission")

    operation, adapter = make_operation(tmp_path, UnknownAdapter())
    with pytest.raises(RuntimeError):
        operation.execute(
            "motor:1", {"endpoint": "motor", "operation": "motor.execute", "payload": request(), "write": True},
            10, authority=lambda binding: None,
        )
    with pytest.raises(Exception):
        operation.execute(
            "motor:1", {"endpoint": "motor", "operation": "motor.execute", "payload": request(), "write": True},
            10, authority=lambda binding: None,
        )
    assert len(adapter.submissions) == 1
    restarted = LegacyMotorOperation(adapter, MotorProfile(mode="deterministic", adapter=adapter.implementation), journal=tmp_path / "motor.sqlite")
    recovered = restarted.lookup("motor:1")
    assert recovered is not None and len(adapter.submissions) == 1


def test_legacy_stop_advances_legacy_epoch(tmp_path):
    operation, _ = make_operation(tmp_path)
    assert operation.stop_epoch == 0
    operation.stop()
    assert operation.stop_epoch == 1

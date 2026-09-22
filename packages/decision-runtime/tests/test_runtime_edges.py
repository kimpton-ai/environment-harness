"""Crash and recovery edges using the real shared contracts."""

import threading
import time

import pytest
from environment_harness.errors import Forbidden
from datetime import datetime, timedelta, timezone

from environment_harness_decisions import (
    Answer, CompiledCommand, DecisionSet, OutcomeUncertain, PreparedSuccessor, ProviderFailure,
)

from test_runtime import Browser, execute, setup as make_setup


def test_crash_after_native_submit_is_reconciled_without_resubmission(tmp_path):
    class CrashAfterSubmit(Browser):
        def execute(self, execution_id, command, *, before_dispatch, cancel, deadline):
            before_dispatch()
            self.submissions.append(execution_id)
            self.revision += 1
            receipt = __import__("environment_harness_decisions").NativeReceipt(
                execution_id=execution_id, outcome="applied", evidence={"page": "saved"}
            )
            self.receipts[execution_id] = receipt
            raise RuntimeError("process failed after native acknowledgement")

    op, invocation = make_setup(tmp_path, CrashAfterSubmit())
    with pytest.raises(RuntimeError):
        execute(op, invocation)
    assert len(op.control.submissions) == 1
    with pytest.raises(OutcomeUncertain):
        execute(op, invocation)
    receipt = op.lookup("session:op-1")
    assert receipt["effects_resolved"]
    assert len(op.control.submissions) == 1


def test_pre_submission_reservation_can_settle_zero_without_provider_call(tmp_path):
    class BlockedSelector:
        model = "fixture.v1"
        def maximum_charge_micros(self, *args):
            return 4
        def model_input(self, observation, decisions):
            raise RuntimeError("crash after reservation")

    op, invocation = make_setup(tmp_path, selector=BlockedSelector())
    with pytest.raises(RuntimeError):
        execute(op, invocation)
    receipt = op.lookup("session:op-1")
    assert receipt["cost_micros"] == 0
    with op.ledger.db() as db:
        attempt = db.execute("SELECT status,cost FROM attempts").fetchone()
        assert attempt[0] == "resolved" and attempt[1] == 0


def test_submitted_marker_crash_retains_unknown_charge(tmp_path):
    class CrashSelector:
        model = "fixture.v1"
        def maximum_charge_micros(self, *args):
            return 4
        def model_input(self, observation, decisions):
            return {"state": observation.model_input}
        def select(self, *args, **kwargs):
            raise RuntimeError("crash after submitted marker")

    op, invocation = make_setup(tmp_path, selector=CrashSelector())
    with pytest.raises(RuntimeError):
        execute(op, invocation)
    assert op.lookup("session:op-1") is None
    with op.ledger.db() as db:
        attempt = db.execute("SELECT status,cost,reservation FROM attempts").fetchone()
        assert attempt[0] == "unknown" and attempt[1] is None and attempt[2] == 4


def test_persisted_expiry_is_not_extended_after_restart(tmp_path):
    op, invocation = make_setup(tmp_path)
    expired = invocation.model_copy(update={
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    })
    receipt = execute(op, expired)
    assert receipt["status"] == "cancelled"
    assert op.lookup("session:op-1") == receipt


def test_prepared_successor_requires_live_predecessor(tmp_path):
    op, invocation = make_setup(tmp_path)
    op.policy = op.policy.model_copy(update={"prepared_successor": True})
    op.control.native_admits_prepared_successors = True
    assert execute(op, invocation)["status"] == "completed"
    successor = PreparedSuccessor(
        id="successor-1", operation_id="session:op-1", predecessor_execution_id="session:op-1:execution:0",
        execution_id="session:op-1:execution:1", expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        binding=invocation.binding, command=CompiledCommand(
            id="click", adapter_version=op.control.implementation, payload={"click": "save"}),
    )
    with pytest.raises(Forbidden):
        op.prepare_successor(successor)

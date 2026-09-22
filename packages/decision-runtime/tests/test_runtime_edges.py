"""Crash and recovery edges using the real shared contracts."""

import threading
import time

import pytest
from environment_harness_decisions import Answer, DecisionSet, OutcomeUncertain, ProviderFailure, Selection

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


def test_reserved_attempt_is_not_acknowledged_as_zero_on_lookup(tmp_path):
    class BlockedSelector:
        model = "fixture.v1"
        def maximum_charge_micros(self, *args):
            return 4
        def model_input(self, observation, decisions):
            raise RuntimeError("crash after reservation")

    op, invocation = make_setup(tmp_path, selector=BlockedSelector())
    with pytest.raises(RuntimeError):
        execute(op, invocation)
    assert op.lookup("session:op-1") is None
    # The provider response has not settled yet, so a restart/lookup cannot
    # invent a zero charge merely because a reservation was written.
    with op.ledger.db() as db:
        attempt = db.execute("SELECT status,cost FROM attempts").fetchone()
        assert attempt[0] != "resolved" or attempt[1] != 0

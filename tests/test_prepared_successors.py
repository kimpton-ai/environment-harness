from pathlib import Path

import pytest

from environment_harness.errors import Conflict
from environment_harness.motor import MotorExecutor, MotorOutcomeUnknown
from environment_harness.motor_adapters import BrowserMotor
from environment_harness.motor_contracts import (
    MotorExecutionMetadata,
    MotorProfile,
    MotorReceipt,
    MotorRequest,
    MotorSelection,
    MotorStep,
    PreparedSuccessorAdmission,
    PreparedSuccessorIntent,
    UnsupportedPreparation,
)


class Driver:
    def observe(self):
        return {"revision": "r1", "elements": [{"element_id": "button", "selector": "#button"}]}

    def execute(self, operation, payload, **kwargs):
        return {"operation_id": kwargs["operation_id"], "status": "completed"}

    def stop(self):
        pass

    def lookup(self, operation_id):
        return None


class PreparedBrowser(BrowserMotor):
    supports_prepared_successors = True

    def __init__(self, driver):
        super().__init__(driver)
        self.admissions = 0
        self.fail_prepare = False
        self.reconciled = False

    def prepare_successor(self, intent):
        if self.fail_prepare:
            raise TimeoutError("lost preparation acknowledgement")
        return intent

    def reconcile(self, operation_id):
        self.reconciled = True
        return {"operation_id": operation_id, "status": "completed"}

    def admit_successor(self, intent, *, predecessor):
        self.admissions += 1
        return PreparedSuccessorAdmission(
            intent_id=intent.intent_id,
            predecessor_operation_id=intent.predecessor_operation_id,
            operation_id=intent.operation_id,
            metadata=intent.metadata,
            status="admitted",
            admitted_at_ms=2,
        )


def successor_setup(tmp_path: Path):
    driver = Driver()
    adapter = PreparedBrowser(driver)
    executor = MotorExecutor(
        adapter, MotorProfile(adapter=adapter.implementation), journal=tmp_path / "motor.sqlite"
    )
    request = MotorRequest(
        skill="click",
        target={"element_id": "button", "selector": "#button"},
        expected={"ok": True},
        observation_revision="r1",
        goal_revision="g1",
        stop_epoch=executor.stop_epoch,
    )
    selection = MotorSelection(candidate_id="click-direct", model="test")
    metadata = MotorExecutionMetadata(
        owner="owner",
        epoch=1,
        goal_revision="g1",
        observation_revision="r1",
        stop_epoch=executor.stop_epoch,
    )
    intent = PreparedSuccessorIntent(
        intent_id="intent",
        operation_id="next",
        predecessor_operation_id="current",
        candidate_id="click-direct",
        metadata=metadata,
        step=MotorStep(operation="click", target=request.target),
        prepared_at_ms=1e13,
    )

    def authority(_):
        return {
            "owner": "owner",
            "goal_revision": "g1",
            "observation_revision": "r1",
            "stop_epoch": executor.stop_epoch,
        }

    predecessor = MotorReceipt(
        operation_id="current",
        status="completed",
        outcome="completed",
        effect="applied",
        profile=executor.profile,
        request=request,
        elapsed_ms=1,
    )
    return executor, adapter, request, selection, intent, authority, predecessor


def test_prepared_successor_contract_carries_fencing_metadata():
    metadata = MotorExecutionMetadata(
        owner="goal-owner",
        epoch=4,
        goal_revision="g7",
        observation_revision="o8",
        stop_epoch=2,
        ui_revision="screen-3",
    )
    intent = PreparedSuccessorIntent(
        intent_id="intent-1",
        operation_id="op-2",
        predecessor_operation_id="op-1",
        candidate_id="move-direct",
        metadata=metadata,
        step=MotorStep(operation="move", target={"x": 1}),
        prepared_at_ms=12.5,
    )
    assert intent.contract_version == "motor.prepared-successor.v1"
    assert intent.metadata.stop_epoch == 2


def test_sequential_adapters_explicitly_reject_preparation():
    adapter = BrowserMotor(Driver())
    metadata = MotorExecutionMetadata(owner="owner", goal_revision="g", observation_revision="r1")
    intent = PreparedSuccessorIntent(
        intent_id="intent",
        operation_id="next",
        predecessor_operation_id="current",
        candidate_id="click-direct",
        metadata=metadata,
        step=MotorStep(operation="click", target={"element_id": "x"}),
        prepared_at_ms=0,
    )
    with pytest.raises(UnsupportedPreparation):
        adapter.prepare_successor(intent)


def test_prepared_admission_is_one_shot_and_checks_predecessor_identity(tmp_path):
    executor, adapter, request, selection, intent, authority, predecessor = successor_setup(tmp_path)
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    with pytest.raises(Conflict):
        executor.admit_successor(
            intent.intent_id,
            request=request,
            selection=selection,
            authority=authority,
            predecessor={**predecessor.model_dump(mode="json"), "operation_id": "wrong"},
        )
    first = executor.admit_successor(
        intent.intent_id,
        request=request,
        selection=selection,
        authority=authority,
        predecessor=predecessor.model_dump(mode="json"),
    )
    second = executor.admit_successor(
        intent.intent_id,
        request=request,
        selection=selection,
        authority=authority,
        predecessor=predecessor.model_dump(mode="json"),
    )
    assert first.status == second.status == "admitted"
    assert adapter.admissions == 1


def test_restart_discards_uncommitted_successor_and_unknown_cannot_retry(tmp_path):
    executor, adapter, request, selection, intent, authority, _ = successor_setup(tmp_path)
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    restarted = MotorExecutor(
        PreparedBrowser(Driver()), executor.profile, journal=executor.journal, discard_prepared=True
    )
    with pytest.raises(MotorOutcomeUnknown):
        restarted.admit_successor(intent.intent_id, request=request, selection=selection, authority=authority)


def test_preparation_fault_is_quarantined_until_native_reconciliation(tmp_path):
    executor, adapter, request, selection, intent, authority, _ = successor_setup(tmp_path)
    adapter.fail_prepare = True
    with pytest.raises(MotorOutcomeUnknown):
        executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    with pytest.raises(MotorOutcomeUnknown):
        executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    admission = executor.reconcile_successor(intent.intent_id)
    assert admission.status == "admitted" and adapter.reconciled

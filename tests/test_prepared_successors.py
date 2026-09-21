import pytest

from environment_harness.motor_adapters import BrowserMotor
from environment_harness.motor_contracts import (
    MotorExecutionMetadata,
    MotorStep,
    PreparedSuccessorIntent,
    UnsupportedPreparation,
)


class Driver:
    def observe(self):
        return {"revision": "r1", "elements": []}

    def execute(self, operation, payload, **kwargs):
        return {"operation_id": kwargs["operation_id"], "status": "completed"}

    def stop(self):
        pass

    def lookup(self, operation_id):
        return None


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
        metadata=metadata,
        step=MotorStep(operation="click", target={"element_id": "x"}),
        prepared_at_ms=0,
    )
    with pytest.raises(UnsupportedPreparation):
        adapter.prepare_successor(intent)

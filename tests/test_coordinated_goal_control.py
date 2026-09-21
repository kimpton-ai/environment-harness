import pytest

import environment_harness

if not hasattr(environment_harness, "ControlClaim"):
    pytest.skip("coordinated contracts unavailable in base revision", allow_module_level=True)

from test_motor import request as motor_request
from test_motor import setup

from environment_harness import (
    ControlClaim,
    GoalContext,
    MotorGroup,
    MotorGroupReceipt,
    MotorProfile,
    MotorRequest,
    ProgressReceipt,
    ResourceOwnership,
)
from environment_harness.errors import Conflict, Forbidden
from environment_harness.motor import MotorExecutor
from environment_harness.motor_adapters import BrowserMotor


def context():
    return GoalContext(goal_id="goal-1", revision="7", milestone_id="place")


def request(*, skill="fill", stop_epoch=0):
    return MotorRequest(
        skill=skill,
        target={"slot": 1},
        expected={"revision": "8"},
        observation_revision="7",
        goal_revision="7",
        stop_epoch=stop_epoch,
        goal_context=context(),
        control_permissions=({"id": "p", "controls": {"channel": "a"}},),
    )


def claim(channel, owner="direct", resource="hand"):
    return ControlClaim(
        channel=channel,
        owner=owner,
        owner_namespace="minecraft",
        controls={"channel": channel},
        resources=(ResourceOwnership(resource=resource, owner=owner, namespace="minecraft"),),
    )


def test_group_rejects_conflicting_resources():
    with pytest.raises(ValueError, match="conflicting resource"):
        MotorGroup(
            group_id="g",
            goal_context=context(),
            claims=(claim("a"), claim("b")),
        )


def test_group_receipt_identity_and_goal_context_are_frozen():
    group = MotorGroup(
        group_id="g",
        goal_context=context(),
        claims=(claim("a"),),
    )
    receipt = MotorGroupReceipt(
        group_id=group.group_id,
        status="completed",
        goal_context=group.goal_context,
        stop_epoch=group.stop_epoch,
        operation_id="env:g",
        elapsed_ms=1,
    )
    assert receipt.group_id == "g"
    assert group.model_config["frozen"] is True
    with pytest.raises(ValueError, match="duplicate"):
        MotorGroupReceipt(
            group_id="g",
            status="completed",
            goal_context=context(),
            stop_epoch=0,
            operation_id="env:g",
            elapsed_ms=1,
            progress=(
                ProgressReceipt(
                    group_id="g", tick=0, channel="a", status="completed", operation_id="x", stop_epoch=0
                ),
                ProgressReceipt(
                    group_id="g", tick=0, channel="a", status="completed", operation_id="y", stop_epoch=0
                ),
            ),
        )


class LegacyAdapter:
    implementation = "legacy@1"
    skills = ("fill",)


class GroupBrowserMotor(BrowserMotor):
    group_capabilities = ("coordinated-control.v1",)


def dispatched_group(stop_epoch=0):
    group = MotorGroup(
        group_id="g",
        goal_context=context(),
        stop_epoch=stop_epoch,
        claims=(claim("a"),),
    )
    return motor_request(
        goal_context=context().model_copy(update={"revision": "7"}),
        stop_epoch=stop_epoch,
        control_permissions=({"id": "p", "controls": {"channel": "a"}},),
        group=group,
    )


def test_operations_dispatch_uses_group_validation(tmp_path):
    _, _, prepare, dispatch, *_ = setup(tmp_path, adapter_class=GroupBrowserMotor)
    prepare(dispatched_group())
    assert dispatch()["status"] == "completed"


def test_operations_dispatch_rejects_unsupported_or_stale_group(tmp_path):
    _, _, prepare, dispatch, *_ = setup(tmp_path / "unsupported")
    prepare(dispatched_group())
    with pytest.raises(Forbidden, match="does not advertise"):
        dispatch()

    _, motor, prepare, dispatch, *_ = setup(tmp_path / "stale", adapter_class=GroupBrowserMotor)
    prepare(dispatched_group(stop_epoch=1))
    with pytest.raises(Conflict, match="stop epoch"):
        dispatch()
    assert motor.stop_epoch == 0


def test_legacy_adapter_must_advertise_group_capability(tmp_path):
    executor = MotorExecutor(
        LegacyAdapter(), MotorProfile(adapter="legacy@1"), journal=tmp_path / "motor.sqlite"
    )
    group = MotorGroup(
        group_id="g",
        goal_context=context(),
        claims=(claim("a"),),
    )
    grouped = request().model_copy(update={"group": group})
    with pytest.raises(Forbidden, match="does not advertise"):
        executor.validate(
            {
                "endpoint": "motor",
                "operation": "motor.execute",
                "payload": grouped.model_dump(mode="json"),
                "write": True,
            },
            {
                "motor": executor.profile.model_dump(mode="json"),
                "environment": {"motor_skills": ["fill"]},
                "policy": {"allowed_endpoints": ["motor"], "allowed_operations": ["motor.execute"]},
            },
        )

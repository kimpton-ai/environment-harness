"""Public opt-in motor contracts and authority regression checks."""

import pytest
from pydantic import ValidationError

from environment_harness import AgentSpec, ExperimentSpec
from environment_harness.fixtures import SyntheticEnvironment


def test_experiment_freezes_declared_motor_profile():
    environment = SyntheticEnvironment().spec.model_dump()
    environment["motor_skills"] = ["fill"]
    spec = ExperimentSpec(
        environment=environment,
        participants=(AgentSpec(id="a", implementation="test", policy_version="1"),),
        motor={"adapter": "browser-motor@1"},
    )
    assert spec.motor.mode == "deterministic"
    assert spec.model_dump()["environment"]["motor_skills"] == ("fill",)
    with pytest.raises(ValidationError, match="frozen"):
        spec.motor.adapter = "different"
    with pytest.raises(ValidationError, match="declared environment skills"):
        ExperimentSpec.model_validate(
            {**spec.model_dump(), "environment": SyntheticEnvironment().spec.model_dump()}
        )


@pytest.mark.parametrize(
    "profile",
    [
        {"adapter": "browser-motor@1", "mode": "jev"},
        {"adapter": "browser-motor@1", "selector_model": "jev-1.13.0"},
    ],
)
def test_motor_profile_rejects_unpinned_or_unexpected_selector(profile):
    from environment_harness import MotorProfile

    with pytest.raises(ValidationError, match="pinned selector"):
        MotorProfile.model_validate(profile)

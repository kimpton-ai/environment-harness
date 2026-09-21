import time
from threading import Event

import pytest

from environment_harness.motor_adapters import BrowserMotor, DesktopMotor, MinecraftMotor, MotorError
from environment_harness.motor_contracts import MotorRequest


class Driver:
    def __init__(self, state):
        self.state, self.calls, self.stopped = state, [], False

    def observe(self):
        return self.state

    def execute(self, operation, payload, **kwargs):
        self.calls.append((operation, payload, kwargs))
        return {"status": "accepted"}

    def stop(self):
        self.stopped = True

    def lookup(self, operation_id):
        return None


def request(skill, target, arguments=None, expected=None, revision="r1"):
    return MotorRequest(
        skill=skill, target=target, arguments=arguments or {}, expected=expected or {"ok": True},
        observation_revision=revision, goal_revision="g1",
    )


def test_minecraft_plans_only_observed_explicit_targets():
    driver = Driver({"revision": "r1", "walkable_positions": [{"x": 2, "y": 64, "z": 1}],
                     "blocks": [{"x": 1, "y": 64, "z": 1, "name": "minecraft:oak_log"}]})
    motor = MinecraftMotor(driver)
    move = motor.plan(request("move", {"x": 2, "y": 64, "z": 1}), driver.observe())
    assert move[0].steps[0].target == {"x": 2, "y": 64, "z": 1}
    mine = motor.plan(request("mine", {"blocks": [{"x": 1, "y": 64, "z": 1, "name": "minecraft:oak_log"}],
                                      }, {"tool": "minecraft:axe"}), driver.observe())
    assert mine[0].steps[0].target["blocks"][0]["name"] == "minecraft:oak_log"
    with pytest.raises(MotorError, match="does not match"):
        motor.plan(request("mine", {"blocks": [{"x": 9, "y": 64, "z": 1, "name": "minecraft:oak_log"}]}), driver.observe())
    with pytest.raises(MotorError, match="missing"):
        motor.plan(request("mine", {"blocks": [{"x": 1, "y": 64, "z": 1, "name": "minecraft:oak_log"}]}), {"revision": "r1"})


def test_browser_requires_all_identifiers_and_can_offer_same_target_focus_alternative():
    state = {"revision": "r1", "elements": [{"element_id": "name", "selector": "#name",
                                                  "supported_operations": ["focus", "fill"]}]}
    motor = BrowserMotor(Driver(state))
    planned = motor.plan(request("fill", {"element_id": "name", "selector": "#name"}, {"text": "Ada"}), state)
    assert len(planned) == 2
    assert all(step.target == {"element_id": "name", "selector": "#name"}
               for candidate in planned for step in candidate.steps)
    with pytest.raises(MotorError, match="missing or ambiguous"):
        motor.plan(request("fill", {"element_id": "name", "selector": "#wrong"}, {"text": "Ada"}), state)
    replaced = {"revision": "r2", "elements": [{"element_id": "other", "selector": "#name"}]}
    with pytest.raises(MotorError, match="missing or ambiguous"):
        motor.plan(request("fill", {"element_id": "name", "selector": "#name"}, {"text": "Ada"}, revision="r2"), replaced)


def test_desktop_requires_explicit_stable_identity():
    state = {"revision": "r1", "windows": [{"app": "Editor", "window": "main",
                                                "elements": [{"element": "title"}]}]}
    motor = DesktopMotor(Driver(state))
    candidate = motor.plan(request("type", {"app": "Editor", "window": "main", "element": "title"}, {"text": "Hello"}), state)[0]
    assert candidate.steps[0].target == {"app": "Editor", "window": "main", "element": "title"}
    with pytest.raises(MotorError, match="requires app"):
        motor.plan(request("type", {"window": "main", "element": "title"}, {"text": "Hello"}), state)


def test_execute_passes_identity_cancel_deadline_and_stop():
    driver = Driver({"revision": "r1", "walkable_positions": [{"x": 1, "y": 64, "z": 1}]})
    motor = MinecraftMotor(driver)
    step = motor.plan(request("move", {"x": 1, "y": 64, "z": 1}), driver.observe())[0].steps[0]
    cancel = Event()
    motor.execute(step, operation_id="op-1", cancel=cancel, deadline=time.monotonic() + 1)
    assert driver.calls[0][2]["operation_id"] == "op-1"
    cancel.set()
    with pytest.raises(MotorError, match="cancelled"):
        motor.execute(step, operation_id="op-2", cancel=cancel, deadline=time.monotonic() + 1)
    motor.stop()
    assert driver.stopped

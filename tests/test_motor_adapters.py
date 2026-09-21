import time
from threading import Event

import pytest

from environment_harness.motor_adapters import (
    BrowserMotor,
    DesktopMotor,
    MinecraftMotor,
    MotorError,
)
from environment_harness.motor_contracts import MotorRequest, MotorStep


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
        skill=skill,
        target=target,
        arguments=arguments or {},
        expected=expected or {"ok": True},
        observation_revision=revision,
        goal_revision="g1",
    )


def test_minecraft_plans_only_observed_explicit_targets():
    driver = Driver(
        {
            "revision": "r1",
            "walkable_positions": [{"x": 2, "y": 64, "z": 1}],
            "blocks": [{"x": 1, "y": 64, "z": 1, "name": "minecraft:oak_log"}],
        }
    )
    motor = MinecraftMotor(driver)
    move = motor.plan(request("move", {"x": 2, "y": 64, "z": 1}), driver.observe())
    assert move[0].steps[0].target == {"x": 2, "y": 64, "z": 1}
    mine = motor.plan(
        request(
            "mine",
            {
                "blocks": [{"x": 1, "y": 64, "z": 1, "name": "minecraft:oak_log"}],
            },
            {"tool": "minecraft:axe"},
        ),
        driver.observe(),
    )
    assert mine[0].steps[0].target["blocks"][0]["name"] == "minecraft:oak_log"
    with pytest.raises(MotorError, match="does not match"):
        motor.plan(
            request("mine", {"blocks": [{"x": 9, "y": 64, "z": 1, "name": "minecraft:oak_log"}]}),
            driver.observe(),
        )
    with pytest.raises(MotorError, match="missing"):
        motor.plan(
            request("mine", {"blocks": [{"x": 1, "y": 64, "z": 1, "name": "minecraft:oak_log"}]}),
            {"revision": "r1"},
        )


def test_browser_requires_all_identifiers_and_can_offer_same_target_focus_alternative():
    state = {
        "revision": "r1",
        "elements": [{"element_id": "name", "selector": "#name", "supported_operations": ["focus", "fill"]}],
    }
    motor = BrowserMotor(Driver(state))
    planned = motor.plan(request("fill", {"element_id": "name", "selector": "#name"}, {"text": "Ada"}), state)
    assert len(planned) == 2
    assert all(
        step.target == {"element_id": "name", "selector": "#name"}
        for candidate in planned
        for step in candidate.steps
    )
    with pytest.raises(MotorError, match="missing or ambiguous"):
        motor.plan(request("fill", {"element_id": "name", "selector": "#wrong"}, {"text": "Ada"}), state)
    replaced = {"revision": "r2", "elements": [{"element_id": "other", "selector": "#name"}]}
    with pytest.raises(MotorError, match="missing or ambiguous"):
        motor.plan(
            request("fill", {"element_id": "name", "selector": "#name"}, {"text": "Ada"}, revision="r2"),
            replaced,
        )


def test_desktop_requires_explicit_stable_identity():
    state = {
        "revision": "r1",
        "windows": [{"app": "Editor", "window": "main", "elements": [{"element": "title"}]}],
    }
    motor = DesktopMotor(Driver(state))
    candidate = motor.plan(
        request("type", {"app": "Editor", "window": "main", "element": "title"}, {"text": "Hello"}), state
    )[0]
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


def test_adapter_validation_observation_and_execution_safety():
    with pytest.raises(TypeError, match="driver must provide"):
        MinecraftMotor(object())
    with pytest.raises(ValueError, match="between 1 and 10000"):
        MinecraftMotor(Driver({}), max_steps=0)
    with pytest.raises(ValueError, match="between 1 and 10000"):
        MinecraftMotor(Driver({}), max_steps=10001)
    motor = MinecraftMotor(Driver({"revision": "r1"}))
    assert motor.observe() == {"revision": "r1"}
    bad_driver = Driver([])
    bad_motor = MinecraftMotor(bad_driver)
    with pytest.raises(MotorError, match="observation must be an object"):
        bad_motor.observe()
    with pytest.raises(MotorError, match="revision is required"):
        motor._revision({})
    with pytest.raises(MotorError, match="revision is required"):
        motor._revision({"revision": 1})
    with pytest.raises(TypeError, match="requires MotorRequest"):
        motor.plan(object(), {"revision": "r1"})
    step = MotorStep(operation="move", target={"x": 1})
    with pytest.raises(MotorError, match="invalid motor step"):
        motor.execute(object(), operation_id="op", cancel=Event(), deadline=None)
    with pytest.raises(MotorError, match="invalid motor step"):
        motor.execute(step, operation_id="", cancel=Event(), deadline=None)
    with pytest.raises(MotorError, match="deadline expired"):
        motor.execute(step, operation_id="op", cancel=Event(), deadline=time.monotonic() - 1)
    bad_driver.execute = lambda *args, **kwargs: []
    with pytest.raises(MotorError, match="invalid receipt"):
        bad_motor.execute(step, operation_id="op", cancel=Event(), deadline=None)
    assert bad_motor.lookup("missing") is None


def test_revalidate_preserves_frozen_plan_and_rejects_changes():
    state = {"revision": "r1", "walkable_positions": [{"x": 1, "y": 2, "z": 3}]}
    motor = MinecraftMotor(Driver(state))
    req = request("move", {"x": 1, "y": 2, "z": 3})
    selected = motor.plan(req, state)[0]
    assert motor.revalidate(req, state, selected) == (selected,)
    with pytest.raises(MotorError, match="no longer available"):
        motor.revalidate(req, state, selected.model_copy(update={"id": "other"}))
    with pytest.raises(MotorError, match="plan length changed"):
        motor.revalidate(req, state, selected.model_copy(update={"steps": ()}))
    changed_step = selected.steps[0].model_copy(update={"operation": "mine"})
    with pytest.raises(MotorError, match="plan steps changed"):
        motor.revalidate(req, state, selected.model_copy(update={"steps": (changed_step,)}))


def test_minecraft_rejects_stale_invalid_and_unsafe_requests():
    state = {
        "revision": "r1",
        "walkable_positions": [{"x": 1, "y": 2, "z": 3}],
        "blocks": [{"x": 1, "y": 2, "z": 3, "name": "stone"}],
        "recipes": ["torch"],
    }
    motor = MinecraftMotor(Driver(state))
    with pytest.raises(MotorError, match="stale"):
        motor.plan(request("move", {"x": 1, "y": 2, "z": 3}, revision="r2"), state)
    with pytest.raises(MotorError, match="unsupported"):
        motor.plan(request("fly", {}), state)
    with pytest.raises(MotorError, match="walkable"):
        motor.plan(request("move", {"x": 8, "y": 2, "z": 3}), state)
    with pytest.raises(MotorError, match="integer"):
        motor.plan(request("move", {"x": True, "y": 2, "z": 3}), state)
    with pytest.raises(MotorError, match="explicit blocks"):
        motor.plan(request("mine", {}), state)
    with pytest.raises(MotorError, match="block name"):
        motor.plan(request("mine", {"blocks": [{"x": 1, "y": 2, "z": 3}]}), state)
    duplicate = {"x": 1, "y": 2, "z": 3, "name": "stone"}
    with pytest.raises(MotorError, match="duplicates"):
        motor.plan(request("mine", {"blocks": [duplicate, duplicate]}), state)
    with pytest.raises(MotorError, match="recipe"):
        motor.plan(request("craft", {"recipe": "sword"}, {"count": 1}), state)
    with pytest.raises(MotorError, match="integer"):
        motor.plan(request("craft", {"recipe": "torch"}, {"count": True}), state)
    with pytest.raises(MotorError, match="between 1 and 64"):
        motor.plan(request("craft", {"recipe": "torch"}, {"count": 65}), state)
    assert (
        motor.plan(request("craft", {"recipe": "torch"}, {"count": 2}), state)[0].steps[0].operation
        == "craft"
    )


def test_browser_and_desktop_validate_operations_targets_and_text():
    browser_state = {
        "revision": "r1",
        "elements": [{"element_id": "a", "selector": "#a", "supported_operations": []}, "noise"],
    }
    browser = BrowserMotor(Driver(browser_state))
    with pytest.raises(MotorError, match="stale"):
        browser.plan(request("read", {"element_id": "a"}, revision="r2"), browser_state)
    with pytest.raises(MotorError, match="unsupported"):
        browser.plan(request("submit", {"element_id": "a"}), browser_state)
    with pytest.raises(MotorError, match="requires element_id"):
        browser.plan(request("read", {}), browser_state)
    with pytest.raises(MotorError, match="no elements"):
        browser.plan(request("read", {"element_id": "a"}), {"revision": "r1"})
    with pytest.raises(MotorError, match="invalid"):
        browser.plan(request("fill", {"element_id": "a"}, {"text": 1}), browser_state)
    assert browser.plan(request("read", {"element_id": "a"}), browser_state)[0].steps[0].arguments == {}
    selector_state = {"revision": "r1", "elements": [{"selector": "#other"}, {"selector": "#only"}]}
    assert browser.plan(request("click", {"selector": "#only"}), selector_state)[0].steps[0].target == {
        "selector": "#only"
    }
    with pytest.raises(MotorError, match="missing or ambiguous"):
        browser.plan(request("read", {"selector": "#missing"}), selector_state)
    desktop_state = {
        "revision": "r1",
        "windows": [
            "noise",
            {"app": "Wrong", "window": "Main"},
            {"app": "App", "window": "Other", "elements": []},
            {"app": "App", "window": "Main", "elements": {}},
            {
                "app": "App",
                "window": "Main",
                "elements": ["noise", {"element": "other"}, {"element": "field"}],
            },
        ],
    }
    desktop = DesktopMotor(Driver(desktop_state))
    with pytest.raises(MotorError, match="stale"):
        desktop.plan(
            request("read", {"app": "App", "window": "Main", "element": "field"}, revision="r2"),
            desktop_state,
        )
    with pytest.raises(MotorError, match="unsupported"):
        desktop.plan(request("submit", {"app": "App", "window": "Main", "element": "field"}), desktop_state)
    with pytest.raises(MotorError, match="invalid"):
        desktop.plan(
            request("type", {"app": "App", "window": "Main", "element": "field"}, {"text": 2}), desktop_state
        )
    with pytest.raises(MotorError, match="no windows"):
        desktop.plan(
            request("read", {"app": "App", "window": "Main", "element": "field"}), {"revision": "r1"}
        )
    with pytest.raises(MotorError, match="missing or ambiguous"):
        desktop.plan(request("read", {"app": "Other", "window": "Main", "element": "field"}), desktop_state)
    assert (
        desktop.plan(request("read", {"app": "App", "window": "Main", "element": "field"}), desktop_state)[0]
        .steps[0]
        .target["element"]
        == "field"
    )

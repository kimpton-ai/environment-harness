import pytest

from environment_harness.motor_adapters import BrowserMotor, DesktopMotor, MinecraftMotor, MotorError


class Driver:
    def __init__(self, state):
        self.state, self.calls, self.stopped = state, [], False

    def observe(self):
        return self.state

    def execute(self, skill, payload):
        self.calls.append((skill, payload))
        if skill == "move":
            self.state["position"] = dict(payload["target"])
        elif skill == "mine":
            selected = {(b["x"], b["y"], b["z"]) for b in payload["blocks"]}
            self.state["blocks"] = [b for b in self.state.get("blocks", []) if (b["x"], b["y"], b["z"]) not in selected]
        elif skill == "craft":
            self.state.setdefault("inventory", {})[payload["recipe"]] = payload["count"]
        elif skill in {"fill", "type"}:
            target = payload["target"]
            if "element_id" in target:
                for element in self.state.get("elements", []):
                    if element.get("element_id") == target["element_id"]:
                        element["value"] = payload["text"]
            else:
                for window in self.state.get("windows", []):
                    for element in window.get("elements", []):
                        if element.get("element") == target["element"].get("element"):
                            element["value"] = payload["text"]
        elif skill == "click":
            target = payload["target"]
            for element in self.state.get("elements", []):
                if element.get("element_id") == target.get("element_id"):
                    element["clicked"] = True
        return {"driver_status": "ok"}

    def stop(self):
        self.stopped = True

    def lookup(self, operation_id):
        return None


def test_minecraft_move_readback_and_idempotency():
    driver = Driver({"position": {"x": 0, "y": 64, "z": 0}})
    motor = MinecraftMotor(driver)
    request = {"skill": "move", "target": {"x": 2, "y": 64, "z": 1}, "expected": {"position": {"x": 2, "y": 64, "z": 1}}}
    first = motor.run("move-1", request)
    second = motor.run("move-1", request)
    assert first == second and len(driver.calls) == 1
    with pytest.raises(MotorError, match="reused"):
        motor.run("move-1", {**request, "target": {"x": 3, "y": 64, "z": 1}})


def test_minecraft_requires_explicit_blocks_and_observed_effect():
    driver = Driver({"blocks": [{"x": 1, "y": 64, "z": 1, "name": "minecraft:oak_log"}]})
    motor = MinecraftMotor(driver)
    with pytest.raises(MotorError):
        motor.run("mine-1", {"skill": "mine", "blocks": [], "expected": {"blocks": []}})
    receipt = motor.run("mine-2", {
        "skill": "mine", "blocks": [{"x": 1, "y": 64, "z": 1, "name": "minecraft:oak_log"}],
        "tool": "minecraft:axe", "expected": {"blocks": []},
    })
    assert receipt["status"] == "completed"


def test_minecraft_changed_state_fails_and_stop_blocks_new_work():
    driver = Driver({"position": {"x": 0, "y": 64, "z": 0}})
    motor = MinecraftMotor(driver)
    with pytest.raises(MotorError):
        motor.run("move-1", {"skill": "move", "target": {"x": 1, "y": 64, "z": 0}, "expected": {"position": {"x": 9, "y": 64, "z": 0}}})
    motor.stop()
    assert driver.stopped
    with pytest.raises(MotorError, match="stopped"):
        motor.run("move-2", {"skill": "move", "target": {"x": 1, "y": 64, "z": 0}, "expected": {"position": {"x": 1, "y": 64, "z": 0}}})


def test_browser_requires_unique_target_and_confirms_fill():
    driver = Driver({"elements": [{"element_id": "name", "selector": "#name", "value": ""}]})
    motor = BrowserMotor(driver)
    receipt = motor.run("fill-1", {"skill": "fill", "target": {"element_id": "name"}, "text": "Ada", "expected": {"elements": [{"element_id": "name", "selector": "#name", "value": "Ada"}]}})
    assert receipt["after"]["elements"][0]["value"] == "Ada"
    ambiguous = Driver({"elements": [{"selector": ".save"}, {"selector": ".save"}]})
    with pytest.raises(MotorError, match="ambiguous"):
        BrowserMotor(ambiguous).run("click-1", {"skill": "click", "target": {"selector": ".save"}, "expected": {}})


def test_desktop_requires_unique_app_window_element_and_readback():
    state = {"windows": [{"app": "Editor", "window": "main", "elements": [{"element": "title", "value": ""}]}]}
    driver = Driver(state)
    receipt = DesktopMotor(driver).run("type-1", {"skill": "type", "target": {"app": "Editor", "window": "main", "element": "title"}, "text": "Hello", "expected": {"windows": [{"app": "Editor", "window": "main", "elements": [{"element": "title", "value": "Hello"}]}]}})
    assert receipt["status"] == "completed"
    missing = Driver({"windows": []})
    with pytest.raises(MotorError, match="missing"):
        DesktopMotor(missing).run("type-2", {"skill": "type", "target": {"app": "Editor", "window": "main", "element": "title"}, "text": "x", "expected": {}})

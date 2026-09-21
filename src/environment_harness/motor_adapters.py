"""Optional deterministic motor adapters for environments with imperative UIs.

The adapters consume an already-authorized, concrete request. They never infer a
target or choose a replacement. Drivers are injected so the public SDK has no
Minecraft, browser, or desktop dependency.
"""

from __future__ import annotations

from copy import deepcopy


class MotorError(Exception):
    """The requested motor operation could not be completed safely."""


def _dict(value, label):
    if not isinstance(value, dict):
        raise MotorError(f"{label} must be an object")
    return value


def _integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise MotorError(f"{label} must be an integer")
    return value


def _target_point(value, label="target"):
    value = _dict(value, label)
    return {axis: _integer(value.get(axis), f"{label}.{axis}") for axis in ("x", "y", "z")}


def _subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _subset(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return expected == actual
    return expected == actual


class _Adapter:
    def __init__(self, driver, *, max_steps=128):
        if not all(callable(getattr(driver, name, None)) for name in ("observe", "execute", "stop", "lookup")):
            raise TypeError("driver must provide observe, execute, stop, and lookup")
        if not isinstance(max_steps, int) or not 1 <= max_steps <= 10000:
            raise ValueError("max_steps must be between 1 and 10000")
        self.driver, self.max_steps = driver, max_steps
        self._requests, self._receipts = {}, {}
        self._stopped = False

    def stop(self):
        self._stopped = True
        self.driver.stop()

    def lookup(self, operation_id):
        return self._receipts.get(operation_id) or self.driver.lookup(operation_id)

    def _start(self, operation_id, request):
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
            raise MotorError("invalid operation_id")
        request = deepcopy(_dict(request, "request"))
        prior = self._requests.get(operation_id)
        if prior is not None and prior != request:
            raise MotorError("operation_id was reused with a different request")
        if operation_id in self._receipts:
            return request, self._receipts[operation_id]
        self._requests[operation_id] = request
        if self._stopped:
            raise MotorError("motor is stopped")
        return request, None

    def _finish(self, operation_id, request, before, after, result):
        if not isinstance(result, dict):
            raise MotorError("driver returned an invalid receipt")
        receipt = {
            "operation_id": operation_id,
            "status": "completed",
            "skill": request["skill"],
            "before": before,
            "after": after,
            "driver": deepcopy(result),
        }
        self._receipts[operation_id] = receipt
        return deepcopy(receipt)

    def _readback(self, request, after):
        expected = request.get("expected")
        if expected is None or not _subset(expected, after):
            raise MotorError("postcondition was not confirmed by observation")


class MinecraftMotor(_Adapter):
    """Bounded movement, mining, and crafting over a native Minecraft driver."""

    def run(self, operation_id, request):
        request, cached = self._start(operation_id, request)
        if cached is not None:
            return cached
        skill = request.get("skill")
        before = deepcopy(_dict(self.driver.observe(), "observation"))
        if skill == "move":
            target = _target_point(request.get("target"))
            if request.get("max_steps", self.max_steps) > self.max_steps:
                raise MotorError("movement exceeds motor step bound")
            payload = {"target": target, "max_steps": _integer(request.get("max_steps", self.max_steps), "max_steps")}
        elif skill == "mine":
            blocks = request.get("blocks")
            if not isinstance(blocks, list) or not 1 <= len(blocks) <= 128:
                raise MotorError("mine requires 1..128 explicit blocks")
            normalized, seen = [], set()
            for index, block in enumerate(blocks):
                point = _target_point(block, f"blocks[{index}]")
                name = block.get("name")
                if not isinstance(name, str) or not name.startswith(("minecraft:", "")) or not name:
                    raise MotorError("mine block name is required")
                key = tuple(point.values())
                if key in seen:
                    raise MotorError("mine block set contains duplicates")
                seen.add(key)
                normalized.append({**point, "name": name})
            payload = {"blocks": normalized, "tool": request.get("tool")}
        elif skill == "craft":
            recipe = request.get("recipe")
            count = _integer(request.get("count"), "count")
            if not isinstance(recipe, str) or not recipe or not 1 <= count <= 64:
                raise MotorError("craft requires a recipe and count between 1 and 64")
            payload = {"recipe": recipe, "count": count}
        else:
            raise MotorError("unsupported Minecraft motor skill")
        result = self.driver.execute(skill, payload)
        after = deepcopy(_dict(self.driver.observe(), "observation"))
        self._readback(request, after)
        return self._finish(operation_id, request, before, after, result)


class BrowserMotor(_Adapter):
    """Deterministic browser focus, fill, click, and readback routines."""

    def _resolve(self, observation, target):
        target = _dict(target, "target")
        elements = observation.get("elements")
        if not isinstance(elements, list):
            raise MotorError("browser observation has no elements")
        matches = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            if target.get("element_id") is not None and element.get("element_id") == target["element_id"]:
                matches.append(element)
            elif target.get("selector") is not None and element.get("selector") == target["selector"]:
                matches.append(element)
        if len(matches) != 1:
            raise MotorError("browser target is missing or ambiguous")
        return deepcopy(matches[0])

    def run(self, operation_id, request):
        request, cached = self._start(operation_id, request)
        if cached is not None:
            return cached
        skill = request.get("skill")
        before = deepcopy(_dict(self.driver.observe(), "observation"))
        target = self._resolve(before, request.get("target"))
        payload = {"target": target}
        if skill == "fill":
            text = request.get("text")
            if not isinstance(text, str) or len(text) > 4096:
                raise MotorError("fill text is invalid")
            payload["text"] = text
        elif skill not in {"focus", "click", "read"}:
            raise MotorError("unsupported browser motor skill")
        result = self.driver.execute(skill, payload)
        after = deepcopy(_dict(self.driver.observe(), "observation"))
        self._readback(request, after)
        return self._finish(operation_id, request, before, after, result)


class DesktopMotor(_Adapter):
    """Deterministic desktop app/window/element focus and typing routines."""

    def _resolve(self, observation, target):
        target = _dict(target, "target")
        windows = observation.get("windows")
        if not isinstance(windows, list):
            raise MotorError("desktop observation has no windows")
        matches = []
        for window in windows:
            if not isinstance(window, dict):
                continue
            if target.get("app") is not None and window.get("app") != target["app"]:
                continue
            if target.get("window") is not None and window.get("window") != target["window"]:
                continue
            elements = window.get("elements", [])
            for element in elements if isinstance(elements, list) else []:
                if not isinstance(element, dict):
                    continue
                if target.get("element") is None or element.get("element") == target["element"]:
                    matches.append({"window": window, "element": element})
        if len(matches) != 1:
            raise MotorError("desktop target is missing or ambiguous")
        return deepcopy(matches[0])

    def run(self, operation_id, request):
        request, cached = self._start(operation_id, request)
        if cached is not None:
            return cached
        skill = request.get("skill")
        before = deepcopy(_dict(self.driver.observe(), "observation"))
        target = self._resolve(before, request.get("target"))
        payload = {"target": target}
        if skill == "type":
            text = request.get("text")
            if not isinstance(text, str) or len(text) > 4096:
                raise MotorError("type text is invalid")
            payload["text"] = text
        elif skill not in {"focus", "activate", "click", "read"}:
            raise MotorError("unsupported desktop motor skill")
        result = self.driver.execute(skill, payload)
        after = deepcopy(_dict(self.driver.observe(), "observation"))
        self._readback(request, after)
        return self._finish(operation_id, request, before, after, result)

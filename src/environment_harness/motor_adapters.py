"""Optional deterministic motor adapters for environments with imperative UIs.

The adapters consume an already-authorized, concrete request. They never infer a
target or choose a replacement. Drivers are injected so the public SDK has no
Minecraft, browser, or desktop dependency.
"""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field


class MotorError(Exception):
    """The requested motor operation could not be completed safely."""


@dataclass(frozen=True)
class MotorRequest:
    skill: str
    target: dict
    arguments: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    observation_revision: str = ""
    goal_revision: str = ""
    stop_epoch: int = 0
    max_steps: int = 32
    timeout_ms: int = 10000

    def __post_init__(self):
        if not isinstance(self.skill, str) or not self.skill:
            raise MotorError("skill is required")
        _dict(self.target, "target")
        _dict(self.arguments, "arguments")
        if not isinstance(self.expected, dict) or not self.expected:
            raise MotorError("expected postconditions are required")
        if not isinstance(self.observation_revision, str) or not self.observation_revision:
            raise MotorError("observation_revision is required")
        if not isinstance(self.goal_revision, str) or not self.goal_revision:
            raise MotorError("goal_revision is required")
        if isinstance(self.stop_epoch, bool) or not isinstance(self.stop_epoch, int) or self.stop_epoch < 0:
            raise MotorError("stop_epoch must be a nonnegative integer")
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int) or not 1 <= self.max_steps <= 10000:
            raise MotorError("max_steps must be between 1 and 10000")
        if isinstance(self.timeout_ms, bool) or not isinstance(self.timeout_ms, int) or not 1 <= self.timeout_ms <= 600000:
            raise MotorError("timeout_ms must be between 1 and 600000")


@dataclass(frozen=True)
class MotorStep:
    operation: str
    target: dict
    arguments: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MotorCandidate:
    id: str
    description: str
    steps: tuple[MotorStep, ...]


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

    def observe(self):
        return deepcopy(_dict(self.driver.observe(), "observation"))

    def _revision(self, observation):
        revision = observation.get("revision")
        if not isinstance(revision, str) or not revision:
            raise MotorError("observation revision is required")
        return revision

    def _request(self, request):
        if not isinstance(request, MotorRequest):
            raise TypeError("plan requires MotorRequest")
        return request

    def execute(self, step, *, operation_id, cancel=None, deadline=None):
        if not isinstance(step, MotorStep) or not isinstance(operation_id, str) or not operation_id:
            raise MotorError("invalid motor step or operation_id")
        if cancel is not None and cancel.is_set():
            raise MotorError("motor execution cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise MotorError("motor execution deadline expired")
        payload = {"target": deepcopy(step.target), **deepcopy(step.arguments)}
        result = self.driver.execute(step.operation, payload, operation_id=operation_id, cancel=cancel, deadline=deadline)
        if not isinstance(result, dict):
            raise MotorError("driver returned an invalid receipt")
        return deepcopy(result)

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

    implementation = "minecraft-motor@1"
    skills = ("move", "mine", "craft")

    def plan(self, request, observation):
        request = self._request(request)
        observation = _dict(observation, "observation")
        if self._revision(observation) != request.observation_revision:
            raise MotorError("observation revision is stale")
        if request.skill not in self.skills:
            raise MotorError("unsupported Minecraft motor skill")
        target = deepcopy(request.target)
        if request.skill == "move":
            point = _target_point(target)
            walkable = observation.get("walkable_positions")
            if walkable is not None and point not in walkable:
                raise MotorError("move target is not an observed walkable position")
            step = MotorStep("move", point, {"max_steps": request.max_steps})
        elif request.skill == "mine":
            blocks = target.get("blocks")
            if not isinstance(blocks, list) or not 1 <= len(blocks) <= 128:
                raise MotorError("mine requires 1..128 explicit blocks")
            observed = {(b.get("x"), b.get("y"), b.get("z"), b.get("name")) for b in observation.get("blocks", []) if isinstance(b, dict)}
            normalized = []
            for index, block in enumerate(blocks):
                point = _target_point(block, f"blocks[{index}]")
                name = block.get("name")
                if not isinstance(name, str) or not name:
                    raise MotorError("mine block name is required")
                if observed and (*point.values(), name) not in observed:
                    raise MotorError("mine target does not match current observation")
                normalized.append({**point, "name": name})
            step = MotorStep("mine", {"blocks": normalized}, {"tool": request.arguments.get("tool")})
        else:
            recipe = target.get("recipe")
            count = _integer(request.arguments.get("count"), "count")
            if not isinstance(recipe, str) or not recipe or not 1 <= count <= 64:
                raise MotorError("craft requires recipe and count between 1 and 64")
            recipes = observation.get("recipes")
            if recipes is not None and recipe not in recipes:
                raise MotorError("craft recipe is not currently available")
            step = MotorStep("craft", {"recipe": recipe}, {"count": count})
        return (MotorCandidate(f"{request.skill}-direct", f"Execute bounded Minecraft {request.skill}", (step,)),)

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

    implementation = "browser-motor@1"
    skills = ("focus", "fill", "click", "read")

    def plan(self, request, observation):
        request = self._request(request)
        observation = _dict(observation, "observation")
        if self._revision(observation) != request.observation_revision:
            raise MotorError("observation revision is stale")
        if request.skill not in self.skills:
            raise MotorError("unsupported browser motor skill")
        target = self._resolve(observation, request.target)
        arguments = {}
        if request.skill == "fill":
            text = request.arguments.get("text")
            if not isinstance(text, str) or len(text) > 4096:
                raise MotorError("fill text is invalid")
            arguments["text"] = text
        return (MotorCandidate(f"{request.skill}-direct", f"Execute bounded browser {request.skill}",
                               (MotorStep(request.skill, target, arguments),)),)

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

    implementation = "desktop-motor@1"
    skills = ("focus", "activate", "click", "type", "read")

    def plan(self, request, observation):
        request = self._request(request)
        observation = _dict(observation, "observation")
        if self._revision(observation) != request.observation_revision:
            raise MotorError("observation revision is stale")
        if request.skill not in self.skills:
            raise MotorError("unsupported desktop motor skill")
        target = self._resolve(observation, request.target)
        arguments = {}
        if request.skill == "type":
            text = request.arguments.get("text")
            if not isinstance(text, str) or len(text) > 4096:
                raise MotorError("type text is invalid")
            arguments["text"] = text
        return (MotorCandidate(f"{request.skill}-direct", f"Execute bounded desktop {request.skill}",
                               (MotorStep(request.skill, target, arguments),)),)

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

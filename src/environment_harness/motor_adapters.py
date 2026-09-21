"""Optional deterministic motor adapters for environments with imperative UIs.

The adapters consume an already-authorized, concrete request. They never infer a
target or choose a replacement. Drivers are injected so the public SDK has no
Minecraft, browser, or desktop dependency.
"""

from __future__ import annotations

import time
from copy import deepcopy

from .motor_contracts import MotorCandidate, MotorRequest, MotorStep


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

    def execute(self, step, *, operation_id, cancel, deadline):
        if not isinstance(step, MotorStep) or not isinstance(operation_id, str) or not operation_id:
            raise MotorError("invalid motor step or operation_id")
        if cancel.is_set():
            raise MotorError("motor execution cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise MotorError("motor execution deadline expired")
        payload = {"target": deepcopy(step.target), **deepcopy(step.arguments)}
        result = self.driver.execute(step.operation, payload, operation_id=operation_id, cancel=cancel, deadline=deadline)
        if not isinstance(result, dict):
            raise MotorError("driver returned an invalid receipt")
        return deepcopy(result)

    def stop(self):
        self.driver.stop()

    def lookup(self, operation_id):
        return self.driver.lookup(operation_id)

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
            step = MotorStep(operation="move", target=point, arguments={"max_steps": request.max_steps})
        elif request.skill == "mine":
            blocks = target.get("blocks")
            if not isinstance(blocks, list) or not 1 <= len(blocks) <= 128:
                raise MotorError("mine requires 1..128 explicit blocks")
            current_blocks = observation.get("blocks")
            if not isinstance(current_blocks, list) or not current_blocks:
                raise MotorError("mine target observation is missing")
            observed = {(b.get("x"), b.get("y"), b.get("z"), b.get("name")) for b in current_blocks if isinstance(b, dict)}
            normalized = []
            seen = set()
            for index, block in enumerate(blocks):
                point = _target_point(block, f"blocks[{index}]")
                name = block.get("name")
                if not isinstance(name, str) or not name:
                    raise MotorError("mine block name is required")
                key = (*point.values(), name)
                if key in seen:
                    raise MotorError("mine block set contains duplicates")
                seen.add(key)
                if key not in observed:
                    raise MotorError("mine target does not match current observation")
                normalized.append({**point, "name": name})
            step = MotorStep(operation="mine", target={"blocks": normalized}, arguments={"tool": request.arguments.get("tool")})
        else:
            recipe = target.get("recipe")
            count = _integer(request.arguments.get("count"), "count")
            if not isinstance(recipe, str) or not recipe or not 1 <= count <= 64:
                raise MotorError("craft requires recipe and count between 1 and 64")
            recipes = observation.get("recipes")
            if recipes is not None and recipe not in recipes:
                raise MotorError("craft recipe is not currently available")
            step = MotorStep(operation="craft", target={"recipe": recipe}, arguments={"count": count})
        return (MotorCandidate(id=f"{request.skill}-direct", description=f"Execute bounded Minecraft {request.skill}", steps=(step,)),)

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
        direct = MotorCandidate(id=f"{request.skill}-direct", description=f"Execute bounded browser {request.skill}",
                                steps=(MotorStep(operation=request.skill, target=target, arguments=arguments),))
        supports = any(
            element.get("element_id") == target.get("element_id")
            and element.get("selector") == target.get("selector")
            and {"focus", "fill"}.issubset(set(element.get("supported_operations", [])))
            for element in observation["elements"] if isinstance(element, dict)
        )
        if request.skill == "fill" and supports:
            focused = MotorCandidate(id="fill-focus-then-fill", description="Focus then fill the same browser element",
                                     steps=(MotorStep(operation="focus", target=target), MotorStep(operation="fill", target=target, arguments=arguments)))
            return direct, focused
        return (direct,)

    def _resolve(self, observation, target):
        target = _dict(target, "target")
        if target.get("element_id") is None and target.get("selector") is None:
            raise MotorError("browser target requires element_id or selector")
        elements = observation.get("elements")
        if not isinstance(elements, list):
            raise MotorError("browser observation has no elements")
        matches = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            if target.get("element_id") is not None and element.get("element_id") != target["element_id"]:
                continue
            if target.get("selector") is not None and element.get("selector") != target["selector"]:
                continue
            if target.get("element_id") is not None or target.get("selector") is not None:
                matches.append(element)
        if len(matches) != 1:
            raise MotorError("browser target is missing or ambiguous")
        return {key: matches[0][key] for key in ("element_id", "selector") if key in matches[0]}

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
        return (MotorCandidate(id=f"{request.skill}-direct", description=f"Execute bounded desktop {request.skill}",
                               steps=(MotorStep(operation=request.skill, target=target, arguments=arguments),)),)

    def _resolve(self, observation, target):
        target = _dict(target, "target")
        if any(not isinstance(target.get(key), str) or not target[key] for key in ("app", "window", "element")):
            raise MotorError("desktop target requires app, window, and element")
        windows = observation.get("windows")
        if not isinstance(windows, list):
            raise MotorError("desktop observation has no windows")
        matches = []
        for window in windows:
            if not isinstance(window, dict):
                continue
            if window.get("app") != target["app"]:
                continue
            if window.get("window") != target["window"]:
                continue
            elements = window.get("elements", [])
            for element in elements if isinstance(elements, list) else []:
                if not isinstance(element, dict):
                    continue
                if element.get("element") == target["element"]:
                    matches.append({"window": window, "element": element})
        if len(matches) != 1:
            raise MotorError("desktop target is missing or ambiguous")
        match = matches[0]
        return {"app": match["window"]["app"], "window": match["window"]["window"], "element": match["element"]["element"]}

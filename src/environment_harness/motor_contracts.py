"""Frozen, bounded contracts for optional application motor assistance."""
from __future__ import annotations

from threading import Event
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MotorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MotorProfile(MotorRecord):
    mode: Literal["deterministic", "jev"] = "deterministic"
    executor: Literal["motor.v1"] = "motor.v1"
    adapter: str = Field(min_length=1)
    selector_model: str | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.mode == "jev") != bool(self.selector_model):
            raise ValueError("Jev mode requires a pinned selector model; deterministic mode has none")
        return self


class MotorControlPermission(MotorRecord):
    """One exact, environment-issued permission for bounded control inputs."""

    id: str = Field(min_length=1, max_length=80)
    controls: dict[str, Any] = Field(default_factory=dict)
    max_steps: int = Field(default=1, ge=1, le=128, strict=True)
    max_travel: float | None = Field(default=None, ge=0)
    protected_region_revision: str | None = None


class MotorRequest(MotorRecord):
    skill: str = Field(min_length=1, max_length=80)
    target: dict[str, Any]
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(min_length=1)
    observation_revision: str = Field(min_length=1)
    goal_revision: str = Field(min_length=1)
    stop_epoch: int = Field(default=0, ge=0, strict=True)
    max_steps: int = Field(default=32, ge=1, le=128, strict=True)
    timeout_ms: int = Field(default=10000, ge=1, le=120000, strict=True)
    control_permissions: tuple[MotorControlPermission, ...] = Field(default_factory=tuple, max_length=128)
    max_recovery_attempts: int = Field(default=0, ge=0, le=128, strict=True)


class MotorStep(MotorRecord):
    operation: str = Field(min_length=1)
    target: dict[str, Any]
    arguments: dict[str, Any] = Field(default_factory=dict)
    controls: dict[str, Any] = Field(default_factory=dict)


class MotorCandidate(MotorRecord):
    id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=2000)
    steps: tuple[MotorStep, ...] = Field(min_length=1, max_length=128)


class MotorSelection(MotorRecord):
    candidate_id: str | None = None
    model: str
    cost_micros: int = Field(default=0, ge=0, strict=True)
    usage: dict[str, Any] = Field(default_factory=dict)
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0, le=1)


class MotorReceipt(MotorRecord):
    operation_id: str
    status: Literal["completed", "blocked", "cancelled"]
    cost_micros: int = Field(default=0, ge=0, strict=True)
    profile: MotorProfile
    request: MotorRequest
    reason: str | None = None
    reason_code: str | None = Field(default=None, min_length=1, max_length=80)
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    selection: MotorSelection | None = None
    steps: tuple[dict[str, Any], ...] = ()
    elapsed_ms: float = Field(ge=0)


class MotorAdapter(Protocol):
    implementation: str
    skills: tuple[str, ...]

    def observe(self) -> dict[str, Any]: ...
    def plan(self, request: MotorRequest, observation: dict[str, Any]) -> tuple[MotorCandidate, ...]: ...
    def execute(self, step: MotorStep, *, operation_id: str, cancel: Event, deadline: float) -> dict[str, Any]: ...
    def stop(self) -> None: ...
    def lookup(self, operation_id: str) -> dict[str, Any] | None: ...

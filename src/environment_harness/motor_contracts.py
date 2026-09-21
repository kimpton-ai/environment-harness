"""Frozen, bounded contracts for optional application motor assistance."""

from __future__ import annotations

from threading import Event
from typing import Any, Literal, Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class MotorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MilestoneIdentity(MotorRecord):
    """Stable identity for a milestone inside a goal revision."""

    goal_id: str = Field(min_length=1, max_length=200)
    milestone_id: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=200)


class GoalContext(MotorRecord):
    """Versioned, immutable authority context shared by coordinated controls."""

    goal_id: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=200)
    milestone_id: str = Field(min_length=1, max_length=200)

    @property
    def milestone(self) -> MilestoneIdentity:
        return MilestoneIdentity(goal_id=self.goal_id, milestone_id=self.milestone_id, revision=self.revision)


class ResourceOwnership(MotorRecord):
    """A resource lease claimed by one named channel owner."""

    resource: str = Field(min_length=1, max_length=200)
    owner: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("owner", "owner_id"),
    )
    namespace: str = Field(min_length=1, max_length=200)


class ControlClaim(MotorRecord):
    """A channel and its resources, owned by a direct or adapter-backed controller."""

    channel: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=200)
    owner_namespace: str = Field(min_length=1, max_length=200)
    controls: dict[str, Any] = Field(min_length=1)
    resources: tuple[ResourceOwnership, ...] = ()

    @model_validator(mode="after")
    def ownership_matches(self):
        for resource in self.resources:
            if resource.owner != self.owner or resource.namespace != self.owner_namespace:
                raise ValueError("resource ownership must match its channel claim")
        return self


class ProgressReceipt(MotorRecord):
    """Durable progress for one coordinated group tick."""

    group_id: str = Field(min_length=1, max_length=200)
    tick: int = Field(ge=0, strict=True)
    channel: str = Field(min_length=1, max_length=200)
    status: Literal["completed", "blocked", "cancelled"]
    operation_id: str = Field(min_length=1, max_length=300)
    stop_epoch: int = Field(ge=0, strict=True)


class MotorExecutionMetadata(MotorRecord):
    """The fencing values that make a motor operation admissible."""

    contract_version: Literal["motor.execution.v1"] = "motor.execution.v1"
    owner: str = Field(min_length=1, max_length=200)
    epoch: int = Field(default=0, ge=0, strict=True)
    goal_revision: str = Field(min_length=1)
    observation_revision: str = Field(min_length=1)
    stop_epoch: int = Field(default=0, ge=0, strict=True)
    interface_revision: str | None = Field(default=None, min_length=1)
    ui_revision: str | None = Field(default=None, min_length=1)
    observation_frame_id: str | None = Field(default=None, min_length=1, max_length=200)
    camera_revision: str | None = Field(default=None, min_length=1, max_length=200)


class PreparedSuccessorIntent(MotorRecord):
    """A successor that may be admitted at a native boundary.

    Preparation records intent only. It never proves that the successor ran.
    """

    contract_version: Literal["motor.prepared-successor.v1"] = "motor.prepared-successor.v1"
    intent_id: str = Field(min_length=1, max_length=160)
    operation_id: str = Field(min_length=1, max_length=160)
    predecessor_operation_id: str = Field(min_length=1, max_length=160)
    candidate_id: str = Field(min_length=1, max_length=80)
    metadata: MotorExecutionMetadata
    step: "MotorStep"
    prepared_at_ms: float = Field(ge=0)
    freshness_ms: int = Field(default=1000, ge=1, le=120000, strict=True)
    expires_tick: int | None = Field(default=None, ge=0, strict=True)
    phase: Literal["prepared"] = "prepared"


class PreparedSuccessorAdmission(MotorRecord):
    """The native boundary decision for a prepared successor."""

    contract_version: Literal["motor.prepared-successor-admission.v1"] = (
        "motor.prepared-successor-admission.v1"
    )
    intent_id: str = Field(min_length=1, max_length=160)
    predecessor_operation_id: str = Field(min_length=1, max_length=160)
    operation_id: str = Field(min_length=1, max_length=160)
    metadata: MotorExecutionMetadata
    status: Literal["admitted", "rejected", "unknown"]
    reason_code: str | None = Field(default=None, min_length=1, max_length=100)
    admitted_at_ms: float | None = Field(default=None, ge=0)


class UnsupportedPreparation(RuntimeError):
    """Raised when a sequential adapter is asked to prepare a successor."""


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
    goal_context: GoalContext | None = None
    group: "MotorGroup | None" = None


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
    # ``status`` is retained for wire compatibility. ``outcome`` distinguishes
    # a proven rejection from an effect whose result is still unknown.
    outcome: Literal["rejected", "accepted", "started", "completed", "cancelled", "unknown"] = "completed"
    effect: Literal["none", "possible", "applied"] = "none"
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


class MotorGroup(MotorRecord):
    """Bounded coordinated control contract; execution is owned by the runtime."""

    group_id: str = Field(min_length=1, max_length=200)
    goal_context: GoalContext
    claims: tuple[ControlClaim, ...] = Field(min_length=1, max_length=64)
    max_ticks: int = Field(default=32, ge=1, le=128, strict=True)
    deadline_ms: int = Field(default=10000, ge=1, le=120000, strict=True)
    stop_epoch: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_compatible_channel_claims(self):
        channels = [claim.channel for claim in self.claims]
        if len(channels) != len(set(channels)):
            raise ValueError("each channel may have only one owner")
        resources: dict[str, ControlClaim] = {}
        for claim in self.claims:
            for ownership in claim.resources:
                key = ownership.resource
                if key in resources:
                    raise ValueError("conflicting resource ownership")
                resources[key] = claim
        return self


class MotorGroupReceipt(MotorRecord):
    group_id: str = Field(min_length=1, max_length=200)
    status: Literal["completed", "blocked", "cancelled"]
    goal_context: GoalContext
    stop_epoch: int = Field(ge=0, strict=True)
    progress: tuple[ProgressReceipt, ...] = ()
    operation_id: str = Field(min_length=1, max_length=300)
    cost_micros: int = Field(default=0, ge=0, strict=True)
    elapsed_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def unique_progress_effects(self):
        identities = [(item.tick, item.channel) for item in self.progress]
        operation_ids = [item.operation_id for item in self.progress]
        if len(identities) != len(set(identities)) or len(operation_ids) != len(set(operation_ids)):
            raise ValueError("duplicate group effect receipt identity")
        if any(
            item.group_id != self.group_id or item.stop_epoch != self.stop_epoch for item in self.progress
        ):
            raise ValueError("progress receipt does not belong to this group")
        return self


MotorRequest.model_rebuild()


class MotorAdapter(Protocol):
    implementation: str
    skills: tuple[str, ...]
    group_capabilities: tuple[str, ...]

    def observe(self) -> dict[str, Any]: ...
    def plan(self, request: MotorRequest, observation: dict[str, Any]) -> tuple[MotorCandidate, ...]: ...
    def execute(
        self, step: MotorStep, *, operation_id: str, cancel: Event, deadline: float
    ) -> dict[str, Any]: ...
    def stop(self) -> None: ...
    def lookup(self, operation_id: str) -> dict[str, Any] | None: ...

    supports_prepared_successors: bool

    def prepare_successor(self, intent: PreparedSuccessorIntent) -> PreparedSuccessorIntent: ...
    def admit_successor(
        self, intent: PreparedSuccessorIntent, *, predecessor: MotorReceipt
    ) -> PreparedSuccessorAdmission: ...
    def reconcile(self, operation_id: str) -> dict[str, Any] | None: ...


PreparedSuccessorIntent.model_rebuild()

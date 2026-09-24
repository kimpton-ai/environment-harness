from __future__ import annotations

import json
from threading import Event
from typing import TYPE_CHECKING, Annotated, Any, Generic, Literal, Mapping, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

Json = dict[str, Any]
Mode = Literal["sequential", "simultaneous", "event"]
InputT = TypeVar("InputT")
MAX_OPERATION_PLAN_BYTES = 1_048_576
MAX_OPERATION_RECEIPT_BYTES = 131_072
MAX_OPERATION_RECEIPTS_BYTES = 8_388_608
MAX_COST_MICROS = 9_223_372_036_854_775_807

if TYPE_CHECKING:
    from .operations import EnvironmentOperation


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Scenario(Record, Generic[InputT]):
    """A stable, serializable input snapshot for an environment session."""

    id: str = Field(min_length=1, max_length=200)
    input: InputT
    reference: Any | None = None
    metadata: Json = Field(default_factory=dict)

    @model_validator(mode="after")
    def serializable(self):
        json.dumps(self.model_dump(mode="json"), allow_nan=False)
        return self


class Capabilities(Record):
    replay: bool = True
    checkpoint: bool = False
    resume: bool = False
    branch: bool = False
    agent_checkpoint: bool = False
    rendered_requests: bool = False
    token_ids: bool = False
    logprobs: bool = False
    live_reads: bool = False
    external_writes: bool = False

    @model_validator(mode="after")
    def consistency(self):
        if (self.resume or self.branch) and not self.checkpoint:
            raise ValueError("resume and branch require checkpoint support")
        return self


class OperationSpec(Record):
    """Frozen identity and public configuration for environment-supplied work."""

    name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,160}$")
    version: str = Field(min_length=1, max_length=200)
    config: Json = Field(default_factory=dict)

    @model_validator(mode="after")
    def serializable(self):
        try:
            json.dumps(self.config, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("operation config must be JSON serializable") from error
        return self


class EnvironmentSpec(Record):
    protocol: Literal["environment-session.v1"] = "environment-session.v1"
    id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1)
    implementation: str = Field(min_length=1)
    observation_schema: Json = Field(default_factory=lambda: {"type": "object"})
    action_schema: Json = Field(default_factory=lambda: {"type": "object"})
    scenario_schema: Json = Field(default_factory=lambda: {"type": "object"})
    scheduling: Mode
    modalities: tuple[str, ...] = ("text", "json")
    operations: tuple[OperationSpec, ...] = ()
    capabilities: Capabilities = Field(default_factory=Capabilities)
    purposes: tuple[Literal["evaluation", "training"], ...] = ("evaluation",)
    stale_action: Literal["reject"] = "reject"
    missing_action: Literal["reject", "noop"] = "reject"
    phase_seconds: float = Field(default=60, gt=0, le=86400)
    phase_deadline: Literal["wall", "coordinator"] = "wall"

    @model_validator(mode="after")
    def unique_operations(self):
        names = [operation.name for operation in self.operations]
        if len(names) != len(set(names)):
            raise ValueError("duplicate environment operation")
        return self


class OperationSpecV2(OperationSpec):
    """A frozen v2 operation declaration, including its maximum effect class."""

    access: Literal["read", "write"]


class EnvironmentSpecV2(EnvironmentSpec):
    """The additive environment-session.v2 contract."""

    # Pydantic enforces frozen=True for these models; pyright does not model that field immutability.
    protocol: Literal["environment-session.v2"] = "environment-session.v2"  # pyright: ignore[reportIncompatibleVariableOverride]
    operations: tuple[OperationSpecV2, ...] = ()  # pyright: ignore[reportIncompatibleVariableOverride]
    max_transition_operations: int = Field(default=64, ge=0, le=64, strict=True)


EnvironmentSpecUnion = Annotated[EnvironmentSpec | EnvironmentSpecV2, Field(discriminator="protocol")]


class OperationRequest(Record):
    """One host-authorized external operation requested by a v2 transition."""

    key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    operation: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,160}$")
    version: str = Field(min_length=1, max_length=200)
    payload: Json = Field(default_factory=dict)
    max_cost_micros: int = Field(default=0, ge=0, le=MAX_COST_MICROS, strict=True)
    depends_on: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def serializable(self):
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("operation dependencies must be unique")
        try:
            json.dumps(self.payload, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("operation payload must be JSON serializable") from error
        return self


class OperationPlan(Record):
    """A bounded, immutable DAG of effects to journal before dispatch."""

    model_config = ConfigDict(
        json_schema_extra={
            "description": "At most 64 requests and at most 1 MiB of canonical JSON. Dependencies must form a DAG."
        }
    )

    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    operations: tuple[OperationRequest, ...] = Field(
        default=(), max_length=64, description="At most 64 typed requests in the operation DAG."
    )
    continuation: Json = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_acyclic_plan(self):
        keys = [operation.key for operation in self.operations]
        if len(set(keys)) != len(keys):
            raise ValueError("operation plan keys must be unique")
        by_key = {operation.key: operation for operation in self.operations}
        for operation in self.operations:
            if operation.key in operation.depends_on or any(
                key not in by_key for key in operation.depends_on
            ):
                raise ValueError("operation plan dependency is missing or self-referential")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key):
            if key in visiting:
                raise ValueError("operation plan dependencies must be acyclic")
            if key in visited:
                return
            visiting.add(key)
            for dependency in by_key[key].depends_on:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in keys:
            visit(key)
        if sum(operation.max_cost_micros for operation in self.operations) > MAX_COST_MICROS:
            raise ValueError("operation plan reservation exceeds the signed 64-bit budget limit")
        try:
            encoded = json.dumps(self.model_dump(mode="json"), allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValueError("operation plan must be JSON serializable") from error
        if len(encoded.encode("utf-8")) > MAX_OPERATION_PLAN_BYTES:
            raise ValueError("operation plan exceeds 1 MiB")
        return self


class OperationReceipt(Record):
    """A settled provider receipt bound to its plan request and journal ID."""

    key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    operation_id: str = Field(min_length=1, max_length=128)
    operation: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,160}$")
    version: str = Field(min_length=1, max_length=200)
    receipt: Json
    cost_micros: int = Field(ge=0, le=MAX_COST_MICROS, strict=True)

    @model_validator(mode="after")
    def serializable(self):
        if (
            self.receipt.get("operation_id") != self.operation_id
            or type(self.receipt.get("cost_micros")) is not int
            or self.receipt.get("cost_micros") != self.cost_micros
        ):
            raise ValueError("operation receipt identity or cost does not match its provider receipt")
        try:
            encoded = json.dumps(self.model_dump(mode="json"), allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValueError("operation receipt must be JSON serializable") from error
        if len(encoded.encode("utf-8")) > MAX_OPERATION_RECEIPT_BYTES:
            raise ValueError("operation receipt exceeds 128 KiB")
        return self


class AgentSpec(Record):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    implementation: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    config: Json = Field(default_factory=dict)
    checkpoint: bool = False


class RunPolicy(Record):
    max_turns: int = Field(default=10000, ge=1)
    max_cost_micros: int = Field(default=0, ge=0)
    max_event_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    max_artifact_bytes: int = Field(default=16777216, ge=1)
    max_checkpoint_bytes: int = Field(default=67108864, ge=1, le=1073741824)
    max_state_bytes: int = Field(default=4194304, ge=1024)
    allowed_endpoints: tuple[str, ...] = ()
    allowed_operations: tuple[str, ...] = ()
    external_writes: bool = False


class ExperimentSpec(Record):
    environment: EnvironmentSpecUnion
    participants: tuple[AgentSpec, ...] = Field(min_length=1)
    seed: int = 0
    scenario: str = "synthetic"
    scenario_input: Any = Field(default_factory=dict)
    scenario_reference: Any | None = None
    scenario_metadata: Json = Field(default_factory=dict)
    split: Literal["training", "heldout"] = "heldout"
    purpose: Literal["evaluation", "training"] = "evaluation"
    time_boundary: str = "unspecified"
    interventions: Json = Field(default_factory=dict)
    scoring_versions: tuple[str, ...] = ()
    policy: RunPolicy = Field(default_factory=RunPolicy)
    operations: tuple[OperationSpec | OperationSpecV2, ...] = ()

    @model_validator(mode="after")
    def check(self):
        ids = [p.id for p in self.participants]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate participant")
        if self.purpose not in self.environment.purposes:
            raise ValueError("environment entitlement denies purpose")
        if self.purpose == "training" and self.split != "training":
            raise ValueError("heldout environments cannot be used for training")
        if (
            self.environment.protocol == "environment-session.v2"
            and self.policy.max_cost_micros > MAX_COST_MICROS
        ):
            raise ValueError("v2 operation budget exceeds the signed 64-bit journal limit")
        if self.policy.external_writes and not self.environment.capabilities.external_writes:
            raise ValueError("external writes unsupported")
        available = {operation.name: operation for operation in self.environment.operations}
        selected = [operation.name for operation in self.operations]
        if len(selected) != len(set(selected)):
            raise ValueError("duplicate operation")
        if any(
            operation.name not in available or available[operation.name].version != operation.version
            for operation in self.operations
        ):
            raise ValueError("operation is not supplied by the environment")
        if isinstance(self.environment, EnvironmentSpecV2) and any(
            not isinstance(operation, OperationSpecV2) or available[operation.name] != operation
            for operation in self.operations
        ):
            raise ValueError("v2 selected operation must exactly match its frozen environment declaration")
        return self


class ActivityEvent(Record):
    """One durable item from the tenant activity outbox."""

    id: int = Field(ge=1)
    topic: str
    experiment: str | None = None
    environment: str | None = None
    kind: str
    payload: Json = Field(default_factory=dict)
    created: float


class ActivityPage(Record):
    """A resumable JSON page from an activity feed."""

    events: tuple[ActivityEvent, ...] = ()
    cursor: int = Field(ge=0)


class ActivitySession(Record):
    """Current activity projection for one environment session."""

    kind: Literal["session"] = "session"
    id: str
    scenario_id: str
    trial: int = Field(ge=1)
    status: str
    current_turn: int = Field(ge=0)
    target_turns: int | None = Field(default=None, ge=1)
    participants: tuple[str, ...] = ()
    latest_activity: str | None = None
    failure: str | None = None
    environment: Json = Field(default_factory=dict)
    frozen: Json = Field(default_factory=dict)
    updated: float


class ActivityScenario(Record):
    """Frozen scenario and aggregate state within an experiment."""

    kind: Literal["scenario"] = "scenario"
    id: str
    input: Any
    reference: Any | None = None
    metadata: Json = Field(default_factory=dict)
    status: str
    completed: int = Field(ge=0)
    total: int = Field(ge=0)
    running: int = Field(ge=0)
    queued: int = Field(ge=0)
    failed: int = Field(ge=0)
    latest_activity: str | None = None
    sessions: tuple[ActivitySession, ...] = ()
    updated: float


class ActivityProgress(Record):
    completed: int = Field(ge=0)
    total: int = Field(ge=0)


class ActivityExperiment(Record):
    """Current aggregate and children for one experiment."""

    kind: Literal["experiment"] = "experiment"
    id: str
    name: str
    status: str
    progress: ActivityProgress
    running: int = Field(ge=0)
    queued: int = Field(ge=0)
    failed: int = Field(ge=0)
    latest_activity: str | None = None
    score_summary: dict[str, float] = Field(default_factory=dict)
    frozen: Json = Field(default_factory=dict)
    scenarios: tuple[ActivityScenario, ...] = ()
    sessions: tuple[ActivitySession, ...] = ()
    updated: float


class ActivitySummary(Record):
    running: int = Field(ge=0)
    queued: int = Field(ge=0)
    failed: int = Field(ge=0)


class ActivitySnapshot(Record):
    """Authoritative recovery snapshot for the live activity hierarchy."""

    summary: ActivitySummary
    experiments: tuple[ActivityExperiment, ...] = ()
    standalone: tuple[ActivitySession, ...] = ()
    cursor: int = Field(ge=0)


class Principal(Record):
    tenant: str
    subject: str
    role: Literal["researcher", "agent", "scorer", "worker"]
    environment: str | None = None
    participant: str | None = None
    generation: int = 0


class Action(Record):
    operation_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    participant: str
    observation_id: str
    revision: int = Field(ge=0)
    payload: Json


class EventInput(Record):
    kind: str
    payload: Json
    audience: tuple[str, ...] = ()
    event_time: float | None = None


class Transition(Record):
    state: Json
    outcomes: dict[str, Json] = Field(default_factory=dict)
    events: tuple[EventInput, ...] = ()
    rewards: dict[str, float] = Field(default_factory=dict)
    terminated: bool = False
    truncated: bool = False
    reason: str | None = None
    pending_outcomes: bool = False
    next_actor: str | None = None


class Finding(Record):
    rule: str
    participant: str
    observation_id: str
    action_id: str | None
    action_item: str | None = None
    opportunity_event: int | None = None
    outcome_event: int
    consequence_events: tuple[int, ...] = ()
    category: Literal["competence", "compliance", "harm", "infrastructure", "malformed"]
    status: Literal["attempted", "blocked", "executed", "consequential", "omitted", "inconclusive"]
    judgment: str
    uncertainty: str


class MetricDefinition(Record):
    id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=200)
    unit: str = Field(min_length=1, max_length=200)


class ScoreReport(Record):
    scorer: str
    version: str
    kind: Literal["deterministic", "model", "human"]
    evidence_cursor: int = Field(ge=0)
    metrics: Json
    metric_definitions: dict[str, MetricDefinition] = Field(default_factory=dict)
    findings: tuple[Finding, ...] = ()
    rewards: dict[str, float] = Field(default_factory=dict)
    uncertainty: str
    provenance: Json


class Environment(Protocol):
    spec: EnvironmentSpec
    operations: Mapping[str, EnvironmentOperation]

    def initialize(self, experiment: ExperimentSpec) -> Json: ...
    def observe(self, state: Json, participant: str) -> Json: ...
    def resolve(
        self, state: Json, actions: dict[str, Json | None], random, events: list[Json]
    ) -> Transition: ...
    def intervene(self, state: Json, changes: Json) -> Json: ...


class EnvironmentV2(Protocol):
    """A v2 environment plans bounded effects before committing a transition."""

    spec: EnvironmentSpecV2
    operations: Mapping[str, EnvironmentOperation]

    def initialize(self, experiment: ExperimentSpec) -> Json: ...
    def observe(self, state: Json, participant: str) -> Json: ...
    def intervene(self, state: Json, changes: Json) -> Json: ...
    def plan_transition(
        self, state: Json, actions: dict[str, Json | None], random, events: list[Json]
    ) -> OperationPlan: ...
    def resolve_transition(
        self,
        state: Json,
        actions: dict[str, Json | None],
        random,
        events: list[Json],
        plan: OperationPlan,
        receipts: Mapping[str, OperationReceipt],
    ) -> Transition: ...


class AgentProgram(Protocol):
    implementation: str

    def act(self, observation: Json) -> Json: ...
    def checkpoint(self) -> Json: ...
    def restore(self, state: Json) -> None: ...


class CancellableAgentProgram(AgentProgram, Protocol):
    def act_cancellable(self, observation: Json, cancel_event: Event) -> Json: ...


class Scorer(Protocol):
    def score(self, evidence: list[Json]) -> ScoreReport: ...


class RuntimeBackend(Protocol):
    def start(self, command: list[str], *, identity: str, limits: Json) -> str: ...
    def status(self, handle: str) -> Json: ...
    def stop(self, handle: str) -> None: ...

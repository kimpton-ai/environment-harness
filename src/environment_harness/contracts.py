from __future__ import annotations

from threading import Event
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

Json = dict[str, Any]
Mode = Literal["sequential", "simultaneous", "event"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


class EnvironmentSpec(Record):
    protocol: Literal["environment-session.v1"] = "environment-session.v1"
    id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1)
    implementation: str = Field(min_length=1)
    observation_schema: Json = Field(default_factory=lambda: {"type": "object"})
    action_schema: Json = Field(default_factory=lambda: {"type": "object"})
    scheduling: Mode
    modalities: tuple[str, ...] = ("text", "json")
    capabilities: Capabilities = Field(default_factory=Capabilities)
    purposes: tuple[Literal["evaluation", "training"], ...] = ("evaluation",)
    stale_action: Literal["reject"] = "reject"
    missing_action: Literal["reject", "noop"] = "reject"
    phase_seconds: float = Field(default=60, gt=0, le=86400)
    phase_deadline: Literal["wall", "coordinator"] = "wall"


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
    max_state_bytes: int = Field(default=4194304, ge=1024)
    allowed_endpoints: tuple[str, ...] = ()
    allowed_operations: tuple[str, ...] = ()
    external_writes: bool = False


class ExperimentSpec(Record):
    environment: EnvironmentSpec
    participants: tuple[AgentSpec, ...] = Field(min_length=1)
    seed: int = 0
    scenario: str = "synthetic"
    split: Literal["training", "heldout"] = "heldout"
    purpose: Literal["evaluation", "training"] = "evaluation"
    time_boundary: str = "unspecified"
    interventions: Json = Field(default_factory=dict)
    scoring_versions: tuple[str, ...] = ()
    policy: RunPolicy = Field(default_factory=RunPolicy)

    @model_validator(mode="after")
    def check(self):
        ids = [p.id for p in self.participants]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate participant")
        if self.purpose not in self.environment.purposes:
            raise ValueError("environment entitlement denies purpose")
        if self.purpose == "training" and self.split != "training":
            raise ValueError("heldout environments cannot be used for training")
        if self.policy.external_writes and not self.environment.capabilities.external_writes:
            raise ValueError("external writes unsupported")
        return self


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

    def initialize(self, experiment: ExperimentSpec) -> Json: ...
    def observe(self, state: Json, participant: str) -> Json: ...
    def resolve(
        self, state: Json, actions: dict[str, Json | None], random, events: list[Json]
    ) -> Transition: ...
    def intervene(self, state: Json, changes: Json) -> Json: ...


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

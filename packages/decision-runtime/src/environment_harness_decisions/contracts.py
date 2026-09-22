"""Versioned immutable contracts. Executable commands never enter selector input."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from threading import Event
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenDict(dict):
    def _immutable(self, *args, **kwargs):
        raise TypeError("decision records are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable


def freeze(value):
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(item) for item in value)
    return value


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    contract_version: Literal["decision.v1"] = "decision.v1"

    def model_copy(self, *, update=None, deep=False):
        values = self.model_dump(mode="python")
        values.update(update or {})
        return type(self).model_validate(values)

    @model_validator(mode="after")
    def immutable(self):
        for name in type(self).model_fields:
            object.__setattr__(self, name, freeze(getattr(self, name)))
        return self


class InvocationLimits(Record):
    max_steps: int = Field(ge=1, le=128, strict=True)
    timeout_ms: int = Field(ge=1, le=3600000, strict=True)
    max_cost_micros: int = Field(ge=0, strict=True)
    max_corrections: int = Field(default=0, ge=0, le=128, strict=True)


class Objective(Record):
    id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    authorized_scope: tuple[str, ...] = Field(min_length=1)
    completion_conditions: tuple[str, ...] = Field(min_length=1)
    handoff_conditions: tuple[str, ...] = ()
    limits: InvocationLimits
    parameters: dict[str, Any] = Field(default_factory=dict)


class AuthorityBinding(Record):
    environment_id: str = Field(min_length=1)
    session_revision: str = Field(min_length=1)
    directive_revision: str = Field(min_length=1)
    observation_revision: str = Field(min_length=1)
    recovery_generation: int = Field(ge=0, strict=True)
    owner: str = Field(min_length=1)
    stop_epoch: int = Field(ge=0, strict=True)

    @property
    def goal_revision(self):
        """Compatibility with the SDK authority callback; this is the session revision."""
        return self.session_revision


class BoundedInvocation(Record):
    objective: Objective
    binding: AuthorityBinding
    expires_at: datetime

    @model_validator(mode="after")
    def valid_authority(self):
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone aware")
        object.__setattr__(self, "expires_at", self.expires_at.astimezone(timezone.utc))
        if self.objective.revision != self.binding.directive_revision:
            raise ValueError("directive revision mismatch")
        return self

    @property
    def goal_revision(self):
        return self.binding.session_revision


class Observation(Record):
    revision: str = Field(min_length=1)
    model_input: dict[str, Any]


class ChoiceOption(Record):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)


class DecisionQuestion(Record):
    id: str = Field(min_length=1)
    kind: Literal["choice", "noul", "score"]
    prompt: str = Field(min_length=1)
    options: tuple[ChoiceOption, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    version: str = "question.v1"

    @model_validator(mode="after")
    def bounded(self):
        if self.kind == "choice":
            ids = [option.id for option in self.options]
            if not ids or len(ids) != len(set(ids)):
                raise ValueError("choice options must have unique nonempty IDs")
        elif self.kind == "score":
            if self.minimum is None or self.maximum is None or self.minimum > self.maximum:
                raise ValueError("score requires ordered finite bounds")
        return self


class DecisionSet(Record):
    id: str = Field(min_length=1)
    observation_revision: str = Field(min_length=1)
    questions: tuple[DecisionQuestion, ...] = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_questions(self):
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError("duplicate question ID")
        return self


class Answer(Record):
    question_id: str = Field(min_length=1)
    value: str | float | bool | None
    probabilities: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def probabilities_bounded(self):
        if any(not math.isfinite(p) or p < 0 or p > 1 for p in self.probabilities.values()):
            raise ValueError("invalid per-question probability")
        return self


class Selection(Record):
    answers: tuple[Answer, ...] = ()
    model: str = Field(min_length=1)
    cost_micros: int | None = Field(default=None, ge=0, strict=True)
    raw_response: dict[str, Any] = Field(default_factory=dict)
    model_input: dict[str, Any] = Field(default_factory=dict)
    abstention: str | None = None
    versions: dict[str, str] = Field(default_factory=dict)

    def validate_answers(self, decisions: DecisionSet):
        if self.abstention:
            return
        answers = {answer.question_id: answer for answer in self.answers}
        if len(answers) != len(self.answers) or set(answers) != {q.id for q in decisions.questions}:
            raise ValueError("answers must cover each question exactly once")
        for question in decisions.questions:
            value = answers[question.id].value
            if question.kind == "choice" and value not in {option.id for option in question.options}:
                raise ValueError("unknown candidate ID")
            if question.kind == "noul" and not isinstance(value, bool):
                raise ValueError("Noul answer must be boolean")
            if question.kind == "score" and (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not question.minimum <= value <= question.maximum
            ):
                raise ValueError("score answer outside bounds")


class CompiledCommand(Record):
    id: str = Field(min_length=1)
    payload: dict[str, Any]
    adapter_version: str = Field(min_length=1)


class Admission(Record):
    execution_id: str = Field(min_length=1)
    binding: AuthorityBinding
    accepted: bool
    reason: str | None = None


class NativeReceipt(Record):
    execution_id: str = Field(min_length=1)
    outcome: Literal["applied", "rejected", "cancelled", "unknown"]
    reason: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class Verification(Record):
    status: Literal["continue", "completed", "handoff", "failed"]
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class DecisionPolicy(Record):
    version: str = "decision-policy.v1"
    profile: str
    selector_model: str
    endpoint: str
    adapter_version: str
    question_version: str = "question.v1"
    hard_budget: bool = True
    retry_before_submission: bool = True
    prepared_successor: bool = False


class DecisionReceipt(Record):
    operation_id: str
    status: Literal["completed", "handoff", "abstained", "rejected", "cancelled", "uncertain"]
    reason: str
    cost_micros: int = Field(ge=0, strict=True)
    reserved_micros: int = Field(default=0, ge=0, strict=True)
    charge_resolved: bool
    effects_resolved: bool
    execution_ids: tuple[str, ...] = ()
    artifact_hashes: tuple[str, ...] = ()
    timings_ms: dict[str, float] = Field(default_factory=dict)
    accounting_id: str


class PreparedSuccessor(Record):
    id: str
    operation_id: str
    predecessor_execution_id: str
    execution_id: str
    expires_at: datetime
    binding: AuthorityBinding
    command: CompiledCommand

    @model_validator(mode="after")
    def aware(self):
        if self.expires_at.tzinfo is None:
            raise ValueError("successor expiry must be timezone aware")
        return self


class ProviderFailure(RuntimeError):
    def __init__(self, reason: str, *, submitted: bool = True, uncharged: bool = False):
        super().__init__(reason)
        self.submitted, self.uncharged = submitted, uncharged


class OutcomeUncertain(RuntimeError):
    """The SDK must retain its operation reservation and reconcile using lookup."""


class EnvironmentControl(Protocol):
    identity: str
    implementation: str

    def observe(self, objective: Objective) -> Observation: ...
    def decisions(self, objective: Objective, observation: Observation) -> DecisionSet: ...
    def compile(
        self, objective: Objective, observation: Observation, decisions: DecisionSet, selection: Selection
    ) -> CompiledCommand: ...
    def admit(self, execution_id: str, command: CompiledCommand, binding: AuthorityBinding) -> Admission: ...
    def execute(
        self, execution_id: str, command: CompiledCommand, *, before_dispatch, cancel: Event, deadline: float
    ) -> NativeReceipt: ...
    def verify(
        self, objective: Objective, before: Observation, after: Observation, receipt: NativeReceipt
    ) -> Verification: ...
    def stop(self) -> None: ...
    def lookup(self, execution_id: str) -> NativeReceipt | None: ...


class DecisionSelector(Protocol):
    model: str

    def maximum_charge_micros(self, objective, observation, decisions) -> int | None: ...
    def select(self, selection_id, objective, observation, decisions, *, cancel, deadline) -> Selection: ...
    def lookup(self, attempt_id) -> Selection | None: ...

"""TypeSafe Jev provider adapter.

The adapter owns provider transport, request encoding, and response parsing.  It
does not authorize or execute an environment command.  The caller supplies the
already-authorized :class:`DecisionSet` and receives typed answers which the
environment must validate before dispatch.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .contracts import ProviderFailure as _SharedProviderFailure

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_PRICE_MICROS_PER_MILLION = 42_000
PROVIDER_VERSION = "typesafe-systemone.v1"
QUESTION_ADAPTER_VERSION = "typesafe-questions.v1"
POLICY_VERSION = "typesafe-policy.v1"
ABSTENTION_CANCELLED = "cancelled"


class JevError(RuntimeError):
    """Base class for provider adapter failures."""


class JevResponseError(JevError):
    """The provider response cannot be safely interpreted."""


class JevBudgetError(JevError):
    """The verified bound or reported usage exceeds the operation budget."""


class JevProviderFailure(_SharedProviderFailure):
    """A transport failure with explicit charge/submission classification."""

    def __init__(self, message: str, *, submitted: bool, uncharged: bool) -> None:
        super().__init__(message, submitted=submitted, uncharged=uncharged)

    @property
    def retryable(self) -> bool:
        # A retry is safe only when a request was proven not to have been sent
        # and the provider proved it was not charged.
        return not self.submitted and self.uncharged


class Transport(Protocol):
    def __call__(
        self, endpoint: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> bytes | Mapping[str, Any]: ...


class TokenBound(Protocol):
    def __call__(self, encoded_request: bytes) -> int: ...


@dataclass(frozen=True)
class ProviderEvidence:
    """Evidence that freezes provider details in an operation record."""

    endpoint: str
    model: str
    provider_version: str = PROVIDER_VERSION
    question_adapter_version: str = QUESTION_ADAPTER_VERSION
    policy_version: str = POLICY_VERSION
    token_bound_source: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        values: dict[str, str | None] = {
            "endpoint": self.endpoint,
            "model": self.model,
            "provider_version": self.provider_version,
            "question_adapter_version": self.question_adapter_version,
            "policy_version": self.policy_version,
            "token_bound_source": self.token_bound_source,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class _FallbackSelection:
    answers: tuple[Any, ...]
    model: str
    cost_micros: int | None
    raw_response: dict[str, Any]
    model_input: dict[str, Any]
    abstention: str | None
    versions: dict[str, str | None]


def _selection(**values: Any) -> Any:
    """Construct the shared contract without making the core SDK a dependency."""
    try:
        from .contracts import Selection  # type: ignore
    except ImportError:
        return _FallbackSelection(**values)
    return Selection(**values)


def _answer(**values: Any) -> Any:
    try:
        from .contracts import Answer  # type: ignore
    except ImportError:
        return values
    return Answer(**values)


class JevDecisionSelector:
    """Select answers for one bounded decision set using TypeSafe SystemOne.

    The selector sends exactly one request.  It never retries a request on its
    own, because a timeout does not prove that the provider was uncharged.  A
    runtime may retry only after catching :class:`JevProviderFailure` with
    ``retryable`` true.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        transport: Transport | None = None,
        max_input_bytes: int = 262_144,
        price_micros_per_million: int = DEFAULT_PRICE_MICROS_PER_MILLION,
        token_bound: TokenBound | None = None,
        token_bound_source: str | None = None,
    ) -> None:
        if not model:
            raise ValueError("model is required")
        if not endpoint.startswith("https://"):
            raise ValueError("endpoint must use HTTPS")
        if max_input_bytes < 1024:
            raise ValueError("max_input_bytes must be at least 1024")
        if price_micros_per_million < 0:
            raise ValueError("price_micros_per_million must be nonnegative")
        if (token_bound is None) != (token_bound_source is None):
            raise ValueError("token_bound and token_bound_source must be supplied together")
        self.api_key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise ValueError("JevDecisionSelector requires TYPESAFE_API_KEY or an explicit api_key")
        self.model = model
        self.endpoint = endpoint
        self.max_input_bytes = max_input_bytes
        self.price_micros_per_million = price_micros_per_million
        self.token_bound = token_bound
        self.evidence = ProviderEvidence(endpoint, model, token_bound_source=token_bound_source)
        self.transport = transport or self._urllib_transport

    def _question_payload(self, question: Any) -> dict[str, Any]:
        kind = question.kind
        payload: dict[str, Any] = {"type": kind}
        if getattr(question, "prompt", None) is not None:
            payload["instructions"] = question.prompt
        options = tuple(getattr(question, "options", ()) or ())
        if kind == "choice":
            if not options:
                raise ValueError(f"choice question {question.id!r} has no options")
            payload["criteria"] = {option.id: option.label for option in options}
        elif kind == "noul":
            criteria = {"true": "yes or true", "false": "no or false"}
            for option in options:
                if option.id in criteria:
                    criteria[option.id] = option.label
            payload["criteria"] = criteria
        elif kind == "score":
            if options:
                payload["criteria"] = [option.label for option in options]
            else:
                minimum = getattr(question, "minimum", None)
                maximum = getattr(question, "maximum", None)
                if minimum is None or maximum is None or maximum < minimum:
                    raise ValueError(f"score question {question.id!r} has no valid range")
                payload["criteria"] = [str(value) for value in range(int(minimum), int(maximum) + 1)]
        else:
            raise ValueError(f"unsupported Jev question type: {kind!r}")
        return payload

    def model_input(self, observation: Any, decisions: Any) -> dict[str, Any]:
        state = getattr(observation, "model_input", None)
        if not isinstance(state, dict):
            raise ValueError("observation.model_input must be an object")
        questions = tuple(getattr(decisions, "questions", ()) or ())
        if not questions:
            raise ValueError("decision set must contain at least one question")
        encoded_questions = {}
        for question in questions:
            if question.id in encoded_questions:
                raise ValueError(f"duplicate question id: {question.id!r}")
            encoded_questions[question.id] = self._question_payload(question)
        request = {"state": state, "model": self.model, "questions": encoded_questions}
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.max_input_bytes:
            raise ValueError("Jev input exceeds max_input_bytes")
        return request

    def maximum_charge_micros(self, objective: Any, observation: Any, decisions: Any) -> int | None:
        """Return a provider charge bound only when a verified bound is configured."""
        del objective
        request = self.model_input(observation, decisions)
        if self.token_bound is None:
            return None
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        bound = self.token_bound(encoded)
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
            raise ValueError("token_bound must return a nonnegative integer")
        return math.ceil(bound * self.price_micros_per_million / 1_000_000)

    def _verified_token_bound(self, request: dict[str, Any]) -> int | None:
        if self.token_bound is None:
            return None
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        bound = self.token_bound(encoded)
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
            raise ValueError("token_bound must return a nonnegative integer")
        return bound

    def select(
        self,
        selection_id: str,
        objective: Any,
        observation: Any,
        decisions: Any,
        *,
        cancel: threading.Event,
        deadline: float,
    ) -> Any:
        del selection_id, objective
        request = self.model_input(observation, decisions)
        if cancel.is_set() or time.monotonic() >= deadline:
            return _selection(
                answers=(), model=self.model, cost_micros=0, raw_response={},
                model_input=request, abstention=ABSTENTION_CANCELLED, versions=self.evidence.as_dict()
            )
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            raw = self.transport(
                self.endpoint,
                {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                body,
                max(0.001, deadline - time.monotonic()),
            )
        except JevProviderFailure:
            raise
        except Exception as exc:
            raise JevProviderFailure("Jev request outcome is unknown", submitted=True, uncharged=False) from exc
        try:
            response = json.loads(raw) if isinstance(raw, (bytes, bytearray, str)) else dict(raw)
        except Exception as exc:
            raise JevResponseError("Jev response was not valid JSON") from exc
        result = self._parse(response, request, decisions)
        if cancel.is_set() or time.monotonic() >= deadline:
            return _selection(
                answers=result.answers, model=result.model, cost_micros=result.cost_micros,
                raw_response=result.raw_response, model_input=result.model_input,
                abstention=ABSTENTION_CANCELLED, versions=result.versions
            )
        return result

    def _parse(self, response: Any, request: dict[str, Any], decisions: Any) -> Any:
        if not isinstance(response, dict) or response.get("model") != self.model:
            raise JevResponseError("Jev response omitted or changed the pinned model")
        usage = response.get("usage")
        if not isinstance(usage, dict) or not all(
            isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool) and usage[key] >= 0
            for key in ("input_tokens", "output_tokens")
        ):
            raise JevResponseError("Jev response omitted valid usage")
        token_bound = self._verified_token_bound(request)
        if token_bound is not None and usage["input_tokens"] > token_bound:
            raise JevBudgetError("Jev usage exceeded the verified token bound")
        cost = math.ceil(usage["input_tokens"] * self.price_micros_per_million / 1_000_000)
        maximum = self.maximum_charge_micros(None, type("Observation", (), {"model_input": request["state"]})(), decisions)
        if maximum is not None and cost > maximum:
            raise JevBudgetError("Jev usage exceeded the verified charge bound")
        answers_raw = response.get("answers")
        if not isinstance(answers_raw, dict):
            raise JevResponseError("Jev response omitted answers")
        answers = []
        for question in tuple(getattr(decisions, "questions", ()) or ()):
            answer = answers_raw.get(question.id)
            if not isinstance(answer, dict) or answer.get("type") != question.kind:
                raise JevResponseError(f"Jev response answer type mismatch for {question.id!r}")
            values: dict[str, Any] = {"question_id": question.id, "probabilities": {}}
            if question.kind == "choice":
                choice = answer.get("choice")
                valid = {option.id for option in tuple(getattr(question, "options", ()) or ())}
                probabilities = answer.get("probabilities")
                confidence = answer.get("confidence")
                if choice not in valid or not self._probabilities(probabilities, valid) or not self._unit(confidence):
                    raise JevResponseError(f"invalid choice answer for {question.id!r}")
                values.update(value=choice, probabilities=probabilities)
            elif question.kind == "noul":
                value = answer.get("noul")
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
                    raise JevResponseError(f"invalid noul answer for {question.id!r}")
                values["value"] = bool(value >= 0.5)
                values["probabilities"] = {"true": float(value), "false": float(1 - value)}
            else:
                value = answer.get("score")
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                    raise JevResponseError(f"invalid score answer for {question.id!r}")
                minimum = getattr(question, "minimum", None)
                maximum_value = getattr(question, "maximum", None)
                if minimum is not None and maximum_value is not None and not minimum <= value <= maximum_value:
                    raise JevResponseError(f"score answer outside bounds for {question.id!r}")
                values["value"] = float(value)
                probabilities = answer.get("probabilities")
                if isinstance(probabilities, dict):
                    values["probabilities"] = probabilities
            answers.append(_answer(**values))
        return _selection(
            answers=tuple(answers), model=self.model, cost_micros=cost,
            raw_response=response, model_input=request, abstention=None, versions=self.evidence.as_dict()
        )

    @staticmethod
    def _unit(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1

    @classmethod
    def _probabilities(cls, value: Any, keys: set[str]) -> bool:
        return isinstance(value, dict) and set(value) == keys and all(cls._unit(item) for item in value.values()) and abs(sum(value.values()) - 1) <= 1e-5

    @staticmethod
    def _urllib_transport(endpoint: str, headers: Mapping[str, str], body: bytes, timeout: float) -> bytes:
        request = urllib.request.Request(endpoint, data=body, headers=dict(headers), method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(1_048_576)


# A concise alias retained for consumers that used the old motor adapter name.
JevSelector = JevDecisionSelector

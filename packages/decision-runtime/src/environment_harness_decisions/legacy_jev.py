"""Compatibility TypeSafe selector for legacy motor consumers."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from math import ceil
from typing import Any, Callable

from .contracts import Answer, ChoiceOption, DecisionQuestion, DecisionSet, Observation
from .jev import DEFAULT_ENDPOINT

# Keep historical constants importable without importing the old motor package.
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_PRICE_MICROS_PER_MILLION = 42_000
DEFAULT_MAX_INPUT_BYTES = 262_144
DEFAULT_TOKEN_BOUND_MULTIPLIER = 4
ABSTAIN_ID = "__abstain__"


class JevOutcomeUnknown(RuntimeError):
    """The provider outcome or charge cannot be proven."""


class JevBudgetExceeded(JevOutcomeUnknown):
    """The provider usage exceeded a bounded reservation."""


class JevSelector:
    def __init__(self, api_key: str | None = None, *, model: str = DEFAULT_MODEL,
                 endpoint: str = DEFAULT_ENDPOINT, transport: Callable | None = None,
                 max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
                 price_micros_per_million: int = DEFAULT_PRICE_MICROS_PER_MILLION,
                 token_bound_multiplier: int = DEFAULT_TOKEN_BOUND_MULTIPLIER,
                 verified_token_bound: Callable[[bytes], int] | None = None,
                 verified_token_bound_source: str | None = None):
        if not model or not endpoint.startswith("https://"):
            raise ValueError("model is required and endpoint must use HTTPS")
        if (verified_token_bound is None) != (verified_token_bound_source is None):
            raise ValueError("verified token bound and source must be supplied together")
        self.api_key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise ValueError("JevSelector requires TYPESAFE_API_KEY or an explicit api_key")
        self.model, self.endpoint = model, endpoint
        self.max_input_bytes = max_input_bytes
        self.price_micros_per_million = price_micros_per_million
        self.token_bound_multiplier = token_bound_multiplier
        self._transport = transport
        self.verified_token_bound = verified_token_bound
        self.verified_token_bound_source = verified_token_bound_source
        self.last_model_input: dict[str, Any] | None = None
        self.last_raw_response: dict[str, Any] | None = None
        self._inner = self._make_inner()

    @property
    def transport(self):
        return self._transport

    @transport.setter
    def transport(self, value):
        self._transport = value

    def _make_inner(self):
        from .jev import JevDecisionSelector
        def call(endpoint, headers, body, timeout):
            if self._transport is None:
                return JevDecisionSelector._httpx_transport(endpoint, headers, body, timeout)
            return self._transport(endpoint, headers, body, timeout)
        return JevDecisionSelector(api_key=self.api_key, model=self.model, endpoint=self.endpoint,
                                   transport=call, max_input_bytes=self.max_input_bytes,
                                   price_micros_per_million=self.price_micros_per_million,
                                   token_bound=self.verified_token_bound,
                                   token_bound_source=self.verified_token_bound_source)

    @staticmethod
    def _candidate_values(candidates):
        if not isinstance(candidates, tuple) or not candidates:
            raise ValueError("candidates must be a nonempty tuple")
        seen = set()
        for candidate in candidates:
            if not hasattr(candidate, "id") or candidate.id in seen or not candidate.id:
                raise ValueError("candidate IDs must be unique and nonempty")
            if candidate.id == ABSTAIN_ID:
                raise ValueError(f"candidate ID {ABSTAIN_ID!r} is reserved")
            seen.add(candidate.id)
        return candidates

    def _body(self, state, candidates):
        self._candidate_values(candidates)
        if not isinstance(state, dict):
            raise ValueError("state must be an object")
        body = {"state": state, "model": self.model, "questions": {"motor": {
            "type": "choice",
            "instructions": "Which supplied motor candidate should execute next? Choose a candidate ID, or abstain if none should execute.",
            "criteria": {**{candidate.id: candidate.description for candidate in candidates},
                         ABSTAIN_ID: "Do not execute a motor candidate; defer to the planner."},
        }}}
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > self.max_input_bytes:
            raise ValueError("Jev input exceeds max_input_bytes")
        return encoded

    def maximum_cost(self, state, candidates):
        """Legacy byte based estimate. It is never used as a hard bound."""
        encoded = self._body(state, candidates)
        return ceil(len(encoded) * self.token_bound_multiplier * self.price_micros_per_million / 1_000_000)

    def verified_maximum_cost(self, state, candidates):
        self._candidate_values(candidates)
        body = self._body(state, candidates)
        if self.verified_token_bound is None:
            return None
        bound = self.verified_token_bound(body)
        if type(bound) is not int or bound < 0:
            raise ValueError("verified token bound must return a nonnegative integer")
        return ceil(bound * self.price_micros_per_million / 1_000_000)

    def _decisions(self, candidates):
        options = tuple(ChoiceOption(id=candidate.id, label=candidate.description) for candidate in candidates)
        options += (ChoiceOption(id=ABSTAIN_ID, label="Do not execute a motor candidate; defer to the planner."),)
        return DecisionSet(id="motor", observation_revision="legacy", questions=(DecisionQuestion(
            id="motor", kind="choice",
            prompt="Which supplied motor candidate should execute next? Choose a candidate ID, or abstain if none should execute.",
            options=options),))

    def select(self, state, candidates, *, maximum_cost_micros, cancel, deadline):
        try:
            candidates = self._candidate_values(candidates)
            hard_bound = self.verified_maximum_cost(state, candidates)
            if hard_bound is None:
                raise JevBudgetExceeded("verified Jev charge bound is required")
            if hard_bound > maximum_cost_micros:
                raise JevBudgetExceeded("Jev reservation exceeds maximum_cost_micros")
            if cancel.is_set() or time.monotonic() >= deadline:
                return self._abstain()
            observation = Observation(revision="legacy", model_input=state)
            decisions = self._decisions(candidates)
            result = self._inner.select("legacy-selection", None, observation, decisions,
                                        cancel=cancel, deadline=deadline)
            self.last_model_input = result.model_input
            self.last_raw_response = result.raw_response
            answers = {answer.question_id: answer for answer in result.answers}
            value = answers.get("motor").value if answers.get("motor") else None
            probabilities = answers.get("motor").probabilities if answers.get("motor") else {}
            usage = result.raw_response.get("usage", {}) if isinstance(result.raw_response, dict) else {}
            if result.abstention:
                return self._abstain(cost=result.cost_micros, probabilities=probabilities, usage=usage)
            return self._motor_selection(value, result.cost_micros, probabilities, usage)
        except JevBudgetExceeded:
            raise
        except Exception as exc:
            error = JevOutcomeUnknown("Jev request outcome is unknown")
            for name in ("model_input", "raw_response", "provider_response", "cost_micros"):
                if hasattr(exc, name):
                    setattr(error, name, getattr(exc, name))
            raise error from exc

    def _motor_selection(self, candidate_id, cost, probabilities, usage):
        from .legacy_contracts import MotorSelection
        if candidate_id == ABSTAIN_ID or candidate_id is None:
            return self._abstain(cost=cost, probabilities=probabilities, usage=usage)
        return MotorSelection(candidate_id=candidate_id, model=self.model, cost_micros=cost,
                              usage=usage, probabilities=probabilities)

    def _abstain(self, *, cost=0, probabilities=None, usage=None):
        from .legacy_contracts import MotorSelection
        return MotorSelection(candidate_id=None, model=self.model, cost_micros=cost,
                              usage=dict(usage or {}), probabilities=dict(probabilities or {}))


JevCommandSelector = JevSelector

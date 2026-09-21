"""Optional TypeSafe Jev selector for already-authorized motor candidates."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from math import ceil
from typing import Any, Callable

from .motor_contracts import MotorCandidate, MotorSelection

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_PRICE_MICROS_PER_MILLION = 42_000
DEFAULT_MAX_INPUT_BYTES = 262_144

Transport = Callable[[str, dict[str, str], bytes, float], bytes | dict[str, Any]]


def _abstain(model: str, *, usage=None, probabilities=None, confidence=None, cost=0) -> MotorSelection:
    return MotorSelection(
        candidate_id=None,
        model=model,
        cost_micros=cost,
        usage=dict(usage or {}),
        probabilities=dict(probabilities or {}),
        confidence=confidence,
    )


class JevSelector:
    """Select one supplied candidate, or abstain safely.

    Jev receives descriptions of existing candidates. It never receives or
    returns permission to invent motor steps; the caller executes the selected
    candidate from its own validated set.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        transport: Transport | None = None,
        max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
        price_micros_per_million: int = DEFAULT_PRICE_MICROS_PER_MILLION,
    ) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("model is required")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ValueError("endpoint must use HTTPS")
        if not isinstance(max_input_bytes, int) or max_input_bytes < 1024:
            raise ValueError("max_input_bytes must be at least 1024")
        if not isinstance(price_micros_per_million, int) or price_micros_per_million < 0:
            raise ValueError("price_micros_per_million must be nonnegative")
        self.api_key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY")
        self.model = model
        self.endpoint = endpoint
        self.max_input_bytes = max_input_bytes
        self.price_micros_per_million = price_micros_per_million
        self.transport = transport or self._urllib_transport

    @staticmethod
    def _candidate_values(candidates: tuple[MotorCandidate, ...]):
        if not isinstance(candidates, tuple) or not candidates:
            raise ValueError("candidates must be a nonempty tuple")
        values = []
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, MotorCandidate):
                raise ValueError("candidates must contain MotorCandidate values")
            if candidate.id in seen or not candidate.id:
                raise ValueError("candidate IDs must be unique and nonempty")
            if not isinstance(candidate.description, str) or not candidate.description:
                raise ValueError("candidate descriptions must be nonempty")
            seen.add(candidate.id)
            values.append(candidate)
        return values

    def _body(self, state: dict[str, Any], candidates: tuple[MotorCandidate, ...]) -> bytes:
        if not isinstance(state, dict):
            raise ValueError("state must be an object")
        self._candidate_values(candidates)
        body = {
            "state": state,
            "model": self.model,
            "questions": {
                "motor": {
                    "type": "choice",
                    "instructions": "Which supplied motor candidate should execute next? Choose only a candidate ID.",
                    "criteria": {candidate.id: candidate.description for candidate in candidates},
                }
            },
        }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.max_input_bytes:
            raise ValueError("Jev input exceeds max_input_bytes")
        return encoded

    def maximum_cost(self, state: dict[str, Any], candidates: tuple[MotorCandidate, ...]) -> int:
        """Return a conservative preflight reservation in micro-USD."""
        encoded = self._body(state, candidates)
        token_upper_bound = len(encoded)
        return ceil(token_upper_bound * self.price_micros_per_million / 1_000_000)

    def select(
        self,
        state: dict[str, Any],
        candidates: tuple[MotorCandidate, ...],
        *,
        maximum_cost_micros: int,
        cancel: threading.Event,
        deadline: float,
    ) -> MotorSelection:
        try:
            body = self._body(state, candidates)
            reservation = ceil(len(body) * self.price_micros_per_million / 1_000_000)
        except (TypeError, ValueError):
            return _abstain(self.model)
        if reservation > maximum_cost_micros or not self.api_key:
            return _abstain(self.model)
        if cancel.is_set() or time.monotonic() >= deadline:
            return _abstain(self.model)
        try:
            timeout = max(0.001, deadline - time.monotonic())
            raw = self.transport(
                self.endpoint,
                {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                body,
                timeout,
            )
            if cancel.is_set() or time.monotonic() >= deadline:
                return _abstain(self.model)
            response = json.loads(raw) if isinstance(raw, (bytes, bytearray, str)) else raw
            return self._parse(response, candidates)
        except Exception:
            return _abstain(self.model)

    def _parse(self, response: Any, candidates: tuple[MotorCandidate, ...]) -> MotorSelection:
        if not isinstance(response, dict) or not isinstance(response.get("model"), str):
            return _abstain(self.model)
        usage = response.get("usage")
        if not isinstance(usage, dict) or any(
            isinstance(usage.get(key), bool) or not isinstance(usage.get(key), int) or usage.get(key) < 0
            for key in ("input_tokens", "output_tokens")
        ):
            return _abstain(response["model"])
        answers = response.get("answers")
        answer = answers.get("motor") if isinstance(answers, dict) else None
        ids = {candidate.id for candidate in candidates}
        probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
        choice = answer.get("choice") if isinstance(answer, dict) else None
        confidence = answer.get("confidence") if isinstance(answer, dict) else None
        valid_probs = (
            isinstance(probabilities, dict)
            and set(probabilities) == ids
            and all(isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1
                    for value in probabilities.values())
            and abs(sum(probabilities.values()) - 1.0) <= 1e-5
        )
        valid_confidence = isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and 0 <= confidence <= 1
        cost = ceil(usage["input_tokens"] * self.price_micros_per_million / 1_000_000)
        if not isinstance(choice, str) or choice not in ids or not valid_probs or not valid_confidence:
            return _abstain(response["model"], usage=usage, probabilities=probabilities if isinstance(probabilities, dict) else {}, confidence=confidence if valid_confidence else None, cost=cost)
        return MotorSelection(
            candidate_id=choice,
            model=response["model"],
            cost_micros=cost,
            usage=dict(usage),
            probabilities=dict(probabilities),
            confidence=float(confidence),
        )

    @staticmethod
    def _urllib_transport(endpoint: str, headers: dict[str, str], body: bytes, timeout: float):
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(1_048_576)

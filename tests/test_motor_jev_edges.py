import json
import math
import threading
import time

import pytest

from environment_harness import motor_jev
from environment_harness.motor_contracts import MotorCandidate, MotorStep
from environment_harness.motor_jev import ABSTAIN_ID, JevOutcomeUnknown, JevSelector


def candidates():
    step = MotorStep(operation="noop", target={}, arguments={})
    return (
        MotorCandidate(id="move", description="Move to the supplied target", steps=(step,)),
        MotorCandidate(id="mine", description="Mine the supplied block", steps=(step,)),
    )


def response(**answer):
    return {
        "model": "jev-1.13.0",
        "answers": {"motor": answer},
        "usage": {"input_tokens": 100, "output_tokens": 10, "request_id": "edge"},
    }


def select(selector, payload, *, deadline=None):
    return selector.select(
        {},
        candidates(),
        maximum_cost_micros=100,
        cancel=threading.Event(),
        deadline=time.monotonic() + 1 if deadline is None else deadline,
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"model": ""}, "model is required"),
        ({"endpoint": "http://localhost"}, "endpoint must use HTTPS"),
        ({"max_input_bytes": 1023}, "max_input_bytes must be at least 1024"),
        ({"price_micros_per_million": -1}, "price_micros_per_million must be nonnegative"),
        ({"token_bound_multiplier": 0}, "token_bound_multiplier must be positive"),
    ],
)
def test_constructor_rejects_invalid_configuration(kwargs, message):
    with pytest.raises(ValueError, match=message):
        JevSelector("secret", **kwargs)


def test_invalid_candidates_and_state_fail_closed_or_raise():
    selector = JevSelector("secret", transport=lambda *_: response())
    assert select(selector, {}) is not None
    assert (
        selector.select(
            [], candidates(), maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1
        ).candidate_id
        is None
    )
    with pytest.raises(ValueError, match="nonempty tuple"):
        selector.maximum_cost({}, ())
    with pytest.raises(ValueError, match="MotorCandidate"):
        selector.maximum_cost({}, (object(),))
    invalid_id = MotorCandidate.model_construct(id="", description="ok", steps=candidates()[0].steps)
    with pytest.raises(ValueError, match="unique and nonempty"):
        selector.maximum_cost({}, (invalid_id,))
    invalid_description = MotorCandidate.model_construct(
        id="bad", description="", steps=candidates()[0].steps
    )
    with pytest.raises(ValueError, match="descriptions"):
        selector.maximum_cost({}, (invalid_description,))


def test_expired_deadline_does_not_dispatch():
    calls = []
    selector = JevSelector("secret", transport=lambda *args: calls.append(args))
    result = select(selector, {}, deadline=time.monotonic() - 1)
    assert result.candidate_id is None
    assert not calls


def test_bytes_json_response_and_urllib_transport(monkeypatch):
    selector = JevSelector(
        "secret",
        transport=lambda *_: json.dumps(
            response(
                choice="mine",
                confidence=0.8,
                probabilities={"move": 0.1, "mine": 0.8, ABSTAIN_ID: 0.1},
            )
        ).encode(),
    )
    assert select(selector, {}).candidate_id == "mine"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit == 1_048_576
            return b"ok"

    calls = []

    def urlopen(request, timeout):
        calls.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(motor_jev.urllib.request, "urlopen", urlopen)
    result = JevSelector._urllib_transport("https://example.test", {"X-Test": "yes"}, b"{}", 0.25)
    assert result == b"ok"
    assert calls[0][0].full_url == "https://example.test"
    assert calls[0][1] == 0.25


def test_invalid_json_and_missing_model_are_unknown():
    invalid_json = JevSelector("secret", transport=lambda *_: b"not-json")
    with pytest.raises(JevOutcomeUnknown, match="valid JSON"):
        select(invalid_json, {})
    missing_model = JevSelector("secret", transport=lambda *_: {"usage": {}})
    with pytest.raises(JevOutcomeUnknown, match="omitted its model"):
        select(missing_model, {})


@pytest.mark.parametrize(
    "usage",
    [{}, {"input_tokens": -1, "output_tokens": 1}, {"input_tokens": True, "output_tokens": 1}],
)
def test_invalid_usage_is_unknown(usage):
    selector = JevSelector("secret", transport=lambda *_: {**response(), "usage": usage})
    with pytest.raises(JevOutcomeUnknown, match="valid usage"):
        select(selector, {})


@pytest.mark.parametrize(
    ("answer", "expected_probabilities", "expected_confidence"),
    [
        ({"choice": "mine", "confidence": 0.8, "probabilities": {"move": math.nan}}, {}, 0.8),
        (
            {
                "choice": "mine",
                "confidence": math.inf,
                "probabilities": {"move": 0.1, "mine": 0.8, ABSTAIN_ID: 0.1},
            },
            {"move": 0.1, "mine": 0.8, ABSTAIN_ID: 0.1},
            None,
        ),
        ({"choice": "mine", "confidence": 0.8, "probabilities": {1: 1.0}}, {}, 0.8),
    ],
)
def test_nonfinite_or_malformed_scores_abstain_with_safe_types(
    answer, expected_probabilities, expected_confidence
):
    selector = JevSelector("secret", transport=lambda *_: response(**answer))
    result = select(selector, {})
    assert result.candidate_id is None
    assert result.cost_micros == 5
    assert result.probabilities == expected_probabilities
    assert result.confidence == expected_confidence

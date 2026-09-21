import json
import threading
import time

import pytest

from environment_harness.motor_contracts import MotorCandidate, MotorStep
from environment_harness.motor_jev import ABSTAIN_ID, JevBudgetExceeded, JevOutcomeUnknown, JevSelector


def candidates():
    step = MotorStep(operation="noop", target={}, arguments={})
    return (
        MotorCandidate(id="move", description="Move to the supplied target", steps=(step,)),
        MotorCandidate(id="mine", description="Mine the supplied block", steps=(step,)),
    )


def response(choice="mine", model="jev-1.13.0"):
    return {
        "model": model,
        "answers": {"motor": {"type": "choice", "choice": choice, "confidence": 0.8,
                                "probabilities": {"move": 0.1, "mine": 0.8, ABSTAIN_ID: 0.1}}},
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


def test_selector_sends_choice_and_returns_existing_candidate():
    calls = []

    def transport(endpoint, headers, body, timeout):
        calls.append((endpoint, headers, json.loads(body), timeout))
        return response()

    selector = JevSelector("secret", transport=transport)
    result = selector.select({"position": {"x": 1}}, candidates(), maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1)
    assert result.candidate_id == "mine"
    assert calls[0][2]["questions"]["motor"]["criteria"][ABSTAIN_ID]
    assert calls[0][1]["Authorization"] == "Bearer secret"


def test_invalid_or_generated_choice_abstains_and_accounts_usage():
    selector = JevSelector("secret", transport=lambda *_: response("invented"))
    result = selector.select({}, candidates(), maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1)
    assert result.candidate_id is None and result.usage["input_tokens"] == 100 and result.cost_micros == 5


def test_budget_cancellation_deadline_and_size_fail_closed_without_transport():
    calls = []
    selector = JevSelector("secret", max_input_bytes=1024, transport=lambda *args: calls.append(args))
    assert selector.select({"x": "a" * 2000}, candidates(), maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1).candidate_id is None
    event = threading.Event()
    event.set()
    assert selector.select({}, candidates(), maximum_cost_micros=100, cancel=event, deadline=time.monotonic() + 1).candidate_id is None
    with pytest.raises(JevBudgetExceeded):
        selector.select({}, candidates(), maximum_cost_micros=0, cancel=threading.Event(), deadline=time.monotonic() + 1)
    assert not calls


def test_maximum_cost_is_conservative_and_duplicate_candidates_are_rejected():
    selector = JevSelector("secret")
    assert selector.maximum_cost({}, candidates()) >= 1
    duplicate = (candidates()[0], candidates()[0])
    assert selector.select({}, duplicate, maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1).candidate_id is None


def test_reserved_abstain_is_valid_and_candidate_collision_is_rejected():
    selector = JevSelector("secret", transport=lambda *_: response(ABSTAIN_ID))
    result = selector.select({}, candidates(), maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1)
    assert result.candidate_id is None
    assert set(result.probabilities) == {"move", "mine", ABSTAIN_ID}
    colliding = (MotorCandidate(id=ABSTAIN_ID, description="bad", steps=candidates()[0].steps),)
    assert selector.select({}, colliding, maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1).candidate_id is None


def test_missing_credentials_fail_at_construction(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        JevSelector(None)


def test_post_dispatch_failure_is_unknown_and_never_zero_cost():
    selector = JevSelector("secret", transport=lambda *_: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(JevOutcomeUnknown):
        selector.select({}, candidates(), maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1)

    malformed = JevSelector("secret", transport=lambda *_: {"model": "jev-1.13.0", "answers": {}})
    with pytest.raises(JevOutcomeUnknown):
        malformed.select({}, candidates(), maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1)


def test_model_mismatch_and_usage_over_reservation_are_unknown():
    mismatch = JevSelector("secret", transport=lambda *_: response(model="jev-preview"))
    with pytest.raises(JevOutcomeUnknown):
        mismatch.select({}, candidates(), maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1)

    over = JevSelector("secret", transport=lambda *_: {**response(), "usage": {"input_tokens": 10_000, "output_tokens": 10}})
    with pytest.raises(JevBudgetExceeded):
        over.select({}, candidates(), maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1,
                    )


def test_cancellation_after_dispatch_preserves_valid_usage_cost():
    cancel = threading.Event()

    def transport(*_):
        cancel.set()
        return response()

    selector = JevSelector("secret", transport=transport)
    result = selector.select({}, candidates(), maximum_cost_micros=100, cancel=cancel, deadline=time.monotonic() + 1)
    assert result.candidate_id is None
    assert result.usage["input_tokens"] == 100
    assert result.cost_micros == 5

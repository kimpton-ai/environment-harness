import json
import threading
import time

from environment_harness.motor_contracts import MotorCandidate
from environment_harness.motor_jev import JevSelector


def candidates():
    return (
        MotorCandidate(id="move", description="Move to the supplied target", steps=()),
        MotorCandidate(id="mine", description="Mine the supplied block", steps=()),
    )


def response(choice="mine", model="jev-1.13.0"):
    return {
        "model": model,
        "answers": {"motor": {"type": "choice", "choice": choice, "confidence": 0.8,
                                "probabilities": {"move": 0.2, "mine": 0.8}}},
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
    assert calls[0][2]["questions"]["motor"]["criteria"] == {"move": "Move to the supplied target", "mine": "Mine the supplied block"}
    assert calls[0][1]["Authorization"] == "Bearer secret"


def test_invalid_or_generated_choice_abstains_and_accounts_usage():
    selector = JevSelector("secret", transport=lambda *_: response("invented"))
    result = selector.select({}, candidates(), maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1)
    assert result.candidate_id is None and result.usage["input_tokens"] == 100 and result.cost_micros == 5


def test_budget_cancellation_deadline_and_size_fail_closed_without_transport():
    calls = []
    selector = JevSelector("secret", max_input_bytes=1024, transport=lambda *args: calls.append(args))
    assert selector.select({"x": "a" * 2000}, candidates(), maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1).candidate_id is None
    event = threading.Event(); event.set()
    assert selector.select({}, candidates(), maximum_cost_micros=100, cancel=event, deadline=time.monotonic() + 1).candidate_id is None
    assert selector.select({}, candidates(), maximum_cost_micros=0, cancel=threading.Event(), deadline=time.monotonic() + 1).candidate_id is None
    assert not calls


def test_maximum_cost_is_conservative_and_duplicate_candidates_are_rejected():
    selector = JevSelector("secret")
    assert selector.maximum_cost({}, candidates()) >= 1
    duplicate = (candidates()[0], candidates()[0])
    assert selector.select({}, duplicate, maximum_cost_micros=100, cancel=threading.Event(), deadline=time.monotonic() + 1).candidate_id is None

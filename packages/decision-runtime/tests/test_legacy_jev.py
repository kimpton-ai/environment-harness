import json
import threading
import time

import pytest

from environment_harness_decisions.legacy_contracts import MotorCandidate, MotorStep
from environment_harness_decisions.legacy_jev import JevBudgetExceeded, JevSelector


def candidates():
    return (MotorCandidate(id="left", description="turn left", steps=(MotorStep(operation="turn", target={}),)),)


def response():
    return {"model": "jev-1.13.0", "answers": {"motor": {
        "type": "choice", "choice": "left", "confidence": 0.9,
        "probabilities": {"left": 0.8, "__abstain__": 0.2},
    }}, "usage": {"input_tokens": 10, "output_tokens": 2}}


def test_legacy_surface_uses_mutable_transport_and_retains_evidence():
    calls = []
    selector = JevSelector(api_key="test", verified_token_bound=lambda body: 100,
                           verified_token_bound_source="fixture", transport=lambda *args: calls.append(args) or response())
    result = selector.select({"observation": "fixture"}, candidates(), maximum_cost_micros=10,
                             cancel=threading.Event(), deadline=time.monotonic() + 2)
    assert result.candidate_id == "left"
    assert selector.last_model_input["state"] == {"observation": "fixture"}
    assert selector.last_raw_response == response()
    assert len(calls) == 1
    assert calls[0][2] == selector._body({"observation": "fixture"}, candidates())
    selector.transport = lambda *args: {**response(), "answers": {"motor": {**response()["answers"]["motor"], "choice": "__abstain__"}}}
    assert selector.select({}, candidates(), maximum_cost_micros=10, cancel=threading.Event(), deadline=time.monotonic() + 2).candidate_id is None


def test_unverified_legacy_estimate_cannot_authorize_network_call():
    calls = []
    selector = JevSelector(api_key="test", transport=lambda *args: calls.append(args))
    assert selector.maximum_cost({}, candidates()) >= 0
    assert selector.verified_maximum_cost({}, candidates()) is None
    with pytest.raises(JevBudgetExceeded):
        selector.select({}, candidates(), maximum_cost_micros=10, cancel=threading.Event(), deadline=time.monotonic() + 2)
    assert calls == []


def test_provider_parse_failure_preserves_known_evidence():
    bad = response()
    bad["answers"]["motor"]["choice"] = "invented"
    selector = JevSelector(api_key="test", verified_token_bound=lambda body: 100,
                           verified_token_bound_source="fixture", transport=lambda *args: bad)
    with pytest.raises(Exception) as caught:
        selector.select({}, candidates(), maximum_cost_micros=10, cancel=threading.Event(), deadline=time.monotonic() + 2)
    assert getattr(caught.value, "model_input", None)
    assert getattr(caught.value, "raw_response", None) == bad

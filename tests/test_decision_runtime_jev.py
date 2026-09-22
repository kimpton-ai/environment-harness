import threading
import time

import pytest

from environment_harness_decisions.contracts import ChoiceOption, DecisionQuestion, DecisionSet, Observation, ProviderFailure
from environment_harness_decisions.jev import JevBudgetError, JevDecisionSelector, JevResponseError


def _decisions():
    return DecisionSet(
        id="controls",
        observation_revision="obs-1",
        questions=(
            DecisionQuestion(
                id="direction",
                kind="choice",
                prompt="Choose a direction",
                options=(ChoiceOption(id="left", label="left"), ChoiceOption(id="right", label="right")),
            ),
            DecisionQuestion(id="safe", kind="noul", prompt="Is this safe?"),
            DecisionQuestion(
                id="throttle", kind="score", prompt="Throttle", minimum=0, maximum=4,
                options=tuple(ChoiceOption(id=str(i), label=label) for i, label in enumerate(("none", "low", "medium", "high", "max"))),
            ),
        ),
    )


def _response():
    return {
        "model": "jev-1.13.0",
        "answers": {
            "direction": {
                "type": "choice",
                "choice": "right",
                "confidence": 0.8,
                "probabilities": {"left": 0.2, "right": 0.8},
            },
            "safe": {"type": "noul", "noul": 0.75},
            "throttle": {
                "type": "score",
                "score": 3,
                "confidence": 0.7,
                "legend": {"0": "none", "1": "low", "2": "medium", "3": "high", "4": "max"},
                "probabilities": {"0": 0.1, "1": 0.1, "2": 0.1, "3": 0.6, "4": 0.1},
            },
        },
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }


def test_selector_composes_all_supported_questions_and_retains_evidence():
    calls = []

    def transport(endpoint, headers, body, timeout):
        calls.append((endpoint, headers, body, timeout))
        return _response()

    selector = JevDecisionSelector(
        api_key="runtime-only",
        transport=transport,
        token_bound=lambda body: 100,
        token_bound_source="fixture-tokenizer.v1",
    )
    result = selector.select(
        "selection-1", None, Observation(revision="obs-1", model_input={"state": "fixture"}), _decisions(),
        cancel=threading.Event(), deadline=time.monotonic() + 2,
    )

    assert len(calls) == 1
    assert calls[0][1]["Authorization"] == "Bearer runtime-only"
    assert {answer.question_id for answer in result.answers} == {"direction", "safe", "throttle"}
    assert result.answers[1].value is True
    assert result.raw_response["answers"]["safe"]["noul"] == 0.75
    assert result.versions["model"] == "jev-1.13.0"
    assert result.versions["token_bound_source"] == "fixture-tokenizer.v1"
    assert "runtime-only" not in str(result.model_input)


def test_hard_budget_fails_closed_without_verified_token_bound():
    selector = JevDecisionSelector(api_key="x", transport=lambda *_: _response())
    assert selector.maximum_charge_micros(None, Observation(revision="o", model_input={}), _decisions()) is None


def test_reported_usage_above_verified_bound_is_rejected():
    response = _response()
    response["usage"]["input_tokens"] = 101
    selector = JevDecisionSelector(
        api_key="x", transport=lambda *_: response, token_bound=lambda body: 100, token_bound_source="fixture"
    )
    with pytest.raises(JevBudgetError):
        selector.select(
            "s", None, Observation(revision="obs-1", model_input={}), _decisions(),
            cancel=threading.Event(), deadline=time.monotonic() + 2,
        )


def test_invalid_combined_response_is_rejected_without_partial_answer():
    response = _response()
    response["answers"]["direction"]["choice"] = "invented"
    selector = JevDecisionSelector(api_key="x", transport=lambda *_: response, token_bound=lambda body: 100, token_bound_source="fixture")
    with pytest.raises(JevResponseError):
        selector.select(
            "s", None, Observation(revision="obs-1", model_input={}), _decisions(),
            cancel=threading.Event(), deadline=time.monotonic() + 2,
        )


def test_cancel_before_transport_does_not_call_provider():
    calls = []
    selector = JevDecisionSelector(api_key="x", transport=lambda *_: calls.append(1))
    cancel = threading.Event()
    cancel.set()
    result = selector.select(
        "s", None, Observation(revision="obs-1", model_input={}), _decisions(),
        cancel=cancel, deadline=time.monotonic() + 2,
    )
    assert result.abstention == "cancelled"
    assert calls == []


def test_parse_failure_retains_request_response_and_charge_evidence():
    response = _response()
    response["answers"]["direction"]["choice"] = "not-authorized"
    selector = JevDecisionSelector(
        api_key="x", transport=lambda *_: response, token_bound=lambda body: 100, token_bound_source="fixture"
    )
    with pytest.raises(JevResponseError) as caught:
        selector.select(
            "s", None, Observation(revision="obs-1", model_input={"private": "state"}), _decisions(),
            cancel=threading.Event(), deadline=time.monotonic() + 2,
        )
    error = caught.value
    assert error.raw_response == response
    assert error.model_input["state"] == {"private": "state"}
    assert error.cost_micros == 1


def test_fractional_score_requires_explicit_discrete_levels():
    decisions = DecisionSet(
        id="d", observation_revision="obs-1",
        questions=(DecisionQuestion(id="score", kind="score", prompt="rate", minimum=0.5, maximum=1.5),),
    )
    selector = JevDecisionSelector(api_key="x", transport=lambda *_: _response())
    with pytest.raises(ValueError, match="fractional bounds"):
        selector.model_input(Observation(revision="obs-1", model_input={}), decisions)


def test_proven_pre_submit_provider_failure_is_not_reclassified():
    failure = ProviderFailure("connection failed before submit", submitted=False, uncharged=True)
    selector = JevDecisionSelector(api_key="x", transport=lambda *_: (_ for _ in ()).throw(failure))
    with pytest.raises(ProviderFailure) as caught:
        selector.select(
            "s", None, Observation(revision="obs-1", model_input={}), _decisions(),
            cancel=threading.Event(), deadline=time.monotonic() + 2,
        )
    assert caught.value is failure

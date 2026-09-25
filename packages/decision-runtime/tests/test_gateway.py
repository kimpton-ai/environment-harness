import hashlib
import json
import threading
import time

import pytest
from test_jev import _decisions, _response

from environment_harness_decisions import (
    DecisionGatewayRequest,
    EvalRouterGatewaySelector,
    EvalRouterGenerationClient,
    Observation,
    ProviderFailure,
    QualifiedImageInput,
    Selection,
)
from environment_harness_decisions.jev import JevProviderFailure


def test_gateway_envelope_uses_durable_id_and_gateway_charge_without_provider_credentials():
    calls = []
    image = QualifiedImageInput(
        uri="https://assets.example.test/frame.png",
        media_type="image/png",
        sha256=hashlib.sha256(b"frame").hexdigest(),
        byte_size=5,
    )

    def transport(endpoint, headers, body, timeout):
        calls.append((endpoint, headers, json.loads(body)))
        return {
            "protocol_version": "evalrouter.decision.v1",
            "operation_id": "selection-7",
            "status": "completed",
            "charged_micros": 4,
            "result": _response(),
        }

    selector = EvalRouterGatewaySelector(
        endpoint="https://gateway.example.test/v1/decisions",
        run_id="run-1",
        episode_id="episode-1",
        workspace_id="workspace-1",
        unit_id="unit-1",
        generation=1,
        gateway_token="gateway-secret",
        transport=transport,
        token_bound=lambda body: 100,
        token_bound_source="fixture-tokenizer.v1",
        images=(image,),
    )
    result = selector.select(
        "selection-7",
        None,
        Observation(revision="obs-1", model_input={"state": "fixture"}),
        _decisions(),
        cancel=threading.Event(),
        deadline=time.monotonic() + 2,
    )

    assert result.cost_micros == 4
    assert result.versions["provider"] == "evalrouter"
    assert calls[0][1]["Authorization"] == "Bearer gateway-secret"
    envelope = DecisionGatewayRequest.model_validate(calls[0][2])
    assert envelope.operation_id == "selection-7"
    assert envelope.model == "jev-1.13.0"
    assert envelope.maximum_charge_micros == 5
    assert envelope.images[0] == image
    assert "Authorization" not in json.dumps(envelope.model_dump())


def test_image_qualification_rejects_unbounded_or_non_image_inputs():
    with pytest.raises(ValueError):
        QualifiedImageInput(
            uri="http://assets.example.test/frame.png",
            media_type="image/png",
            sha256="0" * 64,
            byte_size=5,
        )
    with pytest.raises(ValueError):
        QualifiedImageInput(
            uri="data:image/png;base64,ZmFrZQ==",
            media_type="image/png",
            sha256="0" * 64,
            byte_size=5,
        )
    with pytest.raises(ValueError):
        QualifiedImageInput(
            uri="https://assets.example.test/frame.png",
            media_type="text/plain",
            sha256="0" * 64,
            byte_size=5,
        )


def test_gateway_does_not_retry_and_preserves_explicit_pre_submit_classification():
    calls = []

    def transport(endpoint, headers, body, timeout):
        calls.append(1)
        return {
            "protocol_version": "evalrouter.decision.v1",
            "operation_id": "selection-8",
            "status": "rejected",
            "charged_micros": 0,
            "error": {
                "code": "unsubmitted",
                "message": "gateway did not submit",
                "submitted": False,
                "charged": False,
            },
        }

    selector = EvalRouterGatewaySelector(
        endpoint="https://gateway.example.test/v1/decisions",
        run_id="run-1",
        episode_id="episode-1",
        workspace_id="workspace-1",
        unit_id="unit-1",
        generation=1,
        transport=transport,
        token_bound=lambda body: 100,
        token_bound_source="fixture-tokenizer.v1",
    )
    with pytest.raises(JevProviderFailure) as caught:
        selector.select(
            "selection-8",
            None,
            Observation(revision="obs-1", model_input={}),
            _decisions(),
            cancel=threading.Event(),
            deadline=time.monotonic() + 2,
        )
    assert caught.value.retryable
    assert calls == [1]


def test_lookup_returns_only_completed_identity_bound_selection():
    selection = Selection(model="jev-1.13.0", cost_micros=4)

    def lookup(endpoint, headers, operation_id, scope, timeout):
        return {
            "protocol_version": "evalrouter.decision.v1",
            "operation_id": operation_id,
            "status": "completed",
            "charged_micros": 4,
            "result": selection.model_dump(mode="json"),
        }

    selector = EvalRouterGatewaySelector(
        endpoint="https://gateway.example.test/v1/decisions",
        run_id="run-1",
        episode_id="episode-1",
        workspace_id="workspace-1",
        unit_id="unit-1",
        generation=1,
        transport=lambda *_: {},
        lookup_transport=lookup,
        token_bound=lambda body: 100,
        token_bound_source="fixture",
    )
    assert selector.lookup("selection-9").cost_micros == 4

    def uncertain(endpoint, headers, operation_id, scope, timeout):
        return {
            "protocol_version": "evalrouter.decision.v1",
            "operation_id": operation_id,
            "status": "uncertain",
            "charged_micros": None,
        }

    selector.lookup_transport = uncertain
    assert selector.lookup("selection-9") is None


def test_generation_client_binds_output_bound_and_lookup():
    calls = []

    def transport(endpoint, headers, body, timeout):
        calls.append(json.loads(body))
        return {
            "protocol_version": "evalrouter.generation.v1",
            "operation_id": "gen-1",
            "status": "completed",
            "charged_micros": 3,
            "result": {"content": "{}"},
        }

    client = EvalRouterGenerationClient(
        endpoint="https://gateway.example.test/v1/generate",
        run_id="run-1",
        episode_id="episode-1",
        model="astra-v1",
        workspace_id="workspace-1",
        unit_id="unit-1",
        generation=1,
        transport=transport,
        max_output_tokens=128,
    )
    assert client({"messages": []}, operation_id="gen-1", maximum_charge_micros=5) == "{}"
    assert calls[0]["request"]["max_output_tokens"] == 128


def test_generation_rejected_before_submission_is_retryable_once_and_unknown_is_not():
    calls = []

    def rejected(*_args):
        calls.append(1)
        return {
            "protocol_version": "evalrouter.generation.v1",
            "operation_id": "gen-2",
            "status": "rejected",
            "charged_micros": 0,
            "error": {"code": "unsubmitted", "message": "not sent", "submitted": False, "charged": False},
        }

    client = EvalRouterGenerationClient(
        endpoint="https://gateway.example.test/v1/generate",
        run_id="run-1",
        episode_id="episode-1",
        model="astra-v1",
        workspace_id="workspace-1",
        unit_id="unit-1",
        generation=1,
        transport=rejected,
        max_output_tokens=128,
    )
    with pytest.raises(JevProviderFailure) as caught:
        client({"messages": []}, operation_id="gen-2", maximum_charge_micros=5)
    assert caught.value.retryable and calls == [1]

    def unknown(*_args):
        return {
            "protocol_version": "evalrouter.generation.v1",
            "operation_id": "gen-3",
            "status": "uncertain",
            "charged_micros": None,
        }

    client.transport = unknown
    with pytest.raises(ProviderFailure) as caught:
        client({"messages": []}, operation_id="gen-3", maximum_charge_micros=5)
    assert caught.value.submitted and not caught.value.uncharged


def test_generation_cancellation_happens_before_transport():
    calls = []
    client = EvalRouterGenerationClient(
        endpoint="https://gateway.example.test/v1/generate",
        run_id="run-1",
        episode_id="episode-1",
        model="astra-v1",
        workspace_id="workspace-1",
        unit_id="unit-1",
        generation=1,
        transport=lambda *_: calls.append(1),
        max_output_tokens=128,
    )
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProviderFailure) as caught:
        client({"messages": []}, operation_id="gen-4", maximum_charge_micros=5, cancel=cancel)
    assert not caught.value.submitted and caught.value.uncharged and calls == []


def _generation_client(transport):
    return EvalRouterGenerationClient(
        endpoint="https://gateway.example.test/v1/generate",
        run_id="run-1",
        episode_id="episode-1",
        model="astra-v1",
        workspace_id="workspace-1",
        unit_id="unit-1",
        generation=1,
        gateway_token="gateway-secret",
        transport=transport,
    )


def test_unbilled_generation_rejection_stays_retryable_instead_of_reading_as_an_overcharge():
    """A proven-unsubmitted, unbilled rejection must keep its retry classification.

    The charge bound was checked before the status branch, so a rejection with
    no charge at all surfaced as a non-retryable "charge exceeded bound".
    """

    def transport(endpoint, headers, body, timeout):
        return {
            "protocol_version": "evalrouter.generation.v1",
            "operation_id": "generation-1",
            "status": "rejected",
            "error": {
                "code": "upstream_unavailable",
                "message": "provider refused before submission",
                "submitted": False,
                "charged": False,
            },
        }

    with pytest.raises(JevProviderFailure) as raised:
        _generation_client(transport)(
            {"messages": [{"role": "user", "content": "hello"}]},
            operation_id="generation-1",
            maximum_charge_micros=10,
        )

    assert str(raised.value) == "provider refused before submission"
    assert raised.value.submitted is False
    assert raised.value.uncharged is True
    assert raised.value.retryable is True


def test_generation_overcharge_is_still_refused_before_the_result_is_used():
    def transport(endpoint, headers, body, timeout):
        return {
            "protocol_version": "evalrouter.generation.v1",
            "operation_id": "generation-2",
            "status": "completed",
            "charged_micros": 11,
            "result": {"model": "astra-v1", "content": "hi"},
        }

    with pytest.raises(ProviderFailure) as raised:
        _generation_client(transport)(
            {"messages": [{"role": "user", "content": "hello"}]},
            operation_id="generation-2",
            maximum_charge_micros=10,
        )

    assert "charge exceeded bound" in str(raised.value)


def test_completed_generation_without_a_charge_fails_closed():
    def transport(endpoint, headers, body, timeout):
        return {
            "protocol_version": "evalrouter.generation.v1",
            "operation_id": "generation-3",
            "status": "completed",
            "result": {"model": "astra-v1", "content": "hi"},
        }

    with pytest.raises(ProviderFailure) as raised:
        _generation_client(transport)(
            {"messages": [{"role": "user", "content": "hello"}]},
            operation_id="generation-3",
            maximum_charge_micros=10,
        )

    assert "omitted charge" in str(raised.value)


def test_abstention_records_the_gateway_charge_rather_than_the_local_estimate():
    """An abstention after submission was billed, so the charge is authoritative.

    The abstention path returned early and kept the locally computed token-price
    estimate, so the ledger recorded a cost the gateway never charged.
    """
    cancel = threading.Event()

    def transport(endpoint, headers, body, timeout):
        # Cancelling after submission is what drives the post-request abstention.
        cancel.set()
        return {
            "protocol_version": "evalrouter.decision.v1",
            "operation_id": "selection-abstain",
            "status": "completed",
            "charged_micros": 4,
            "result": _response(),
        }

    selector = EvalRouterGatewaySelector(
        endpoint="https://gateway.example.test/v1/decisions",
        run_id="run-1",
        episode_id="episode-1",
        workspace_id="workspace-1",
        unit_id="unit-1",
        generation=1,
        gateway_token="gateway-secret",
        transport=transport,
        token_bound=lambda body: 100,
        token_bound_source="fixture-tokenizer.v1",
    )
    result = selector.select(
        "selection-abstain",
        None,
        Observation(revision="obs-1", model_input={"state": "fixture"}),
        _decisions(),
        cancel=cancel,
        deadline=time.monotonic() + 2,
    )

    assert result.abstention == "cancelled"
    # The local token-price estimate for this fixture is 1 micro, so a passing
    # assertion here means the authoritative gateway charge was adopted.
    assert result.cost_micros == 4

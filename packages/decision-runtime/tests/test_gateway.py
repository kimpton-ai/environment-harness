import hashlib
import json
import threading
import time

import pytest

from environment_harness_decisions import (
    DecisionGatewayRequest,
    EvalRouterGatewaySelector,
    Observation,
    QualifiedImageInput,
)
from environment_harness_decisions.jev import JevProviderFailure
from test_jev import _decisions, _response


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
            "charged_micros": 7,
            "result": _response(),
        }

    selector = EvalRouterGatewaySelector(
        endpoint="https://gateway.example.test/v1/decisions",
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

    assert result.cost_micros == 7
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
            "error": {"code": "unsubmitted", "message": "gateway did not submit", "submitted": False, "charged": False},
        }

    selector = EvalRouterGatewaySelector(
        endpoint="https://gateway.example.test/v1/decisions",
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

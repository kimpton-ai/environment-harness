"""EvalRouter decision gateway boundary.

The gateway owns provider credentials and billing.  This module owns the
versioned wire envelope and delegates TypeSafe request encoding and response
interpretation to :mod:`jev`; it never retries a gateway request.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import ProviderFailure, Selection
from .jev import DEFAULT_MODEL, JevDecisionSelector, JevProviderFailure

GATEWAY_PROTOCOL_VERSION = "evalrouter.decision.v1"
GATEWAY_PROVIDER = "evalrouter"


class GatewayTransport(Protocol):
    def __call__(self, endpoint: str, headers: Mapping[str, str], body: bytes, timeout: float) -> Any: ...


class QualifiedImageInput(BaseModel):
    """A bounded, content-addressed image reference accepted by the gateway."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    uri: str = Field(min_length=1, max_length=2048)
    media_type: str = Field(pattern=r"^image/[a-z0-9.+-]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=1, le=16 * 1024 * 1024, strict=True)

    @field_validator("uri")
    @classmethod
    def safe_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "data"}:
            raise ValueError("image uri must use https or data")
        if parsed.scheme == "https" and not parsed.netloc:
            raise ValueError("https image uri requires a host")
        return value


class DecisionGatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: str = GATEWAY_PROTOCOL_VERSION
    operation_id: str = Field(min_length=1, max_length=256)
    model: str = DEFAULT_MODEL
    maximum_charge_micros: int = Field(ge=0, strict=True)
    request: dict[str, Any]
    images: tuple[QualifiedImageInput, ...] = ()


class GatewayError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    submitted: bool = True
    charged: bool = True


class DecisionGatewayResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: str = GATEWAY_PROTOCOL_VERSION
    operation_id: str = Field(min_length=1)
    status: str
    result: dict[str, Any] | None = None
    charged_micros: int | None = Field(default=None, ge=0, strict=True)
    error: GatewayError | None = None


@dataclass(frozen=True)
class GatewayEvidence:
    endpoint: str
    protocol_version: str = GATEWAY_PROTOCOL_VERSION
    provider: str = GATEWAY_PROVIDER
    model: str = DEFAULT_MODEL

    def as_dict(self) -> dict[str, str]:
        return {
            "endpoint": self.endpoint,
            "protocol_version": self.protocol_version,
            "provider": self.provider,
            "model": self.model,
        }


class EvalRouterGatewaySelector:
    """DecisionSelector that sends one bounded request through EvalRouter.

    ``gateway_token`` authenticates the gateway only.  It is never included in
    the request model input or durable evidence.  The gateway response's
    charge is authoritative and replaces the provider-local estimate.
    """

    model = DEFAULT_MODEL

    def __init__(
        self,
        *,
        endpoint: str,
        gateway_token: str | None = None,
        transport: GatewayTransport | None = None,
        token_bound,
        token_bound_source: str,
        max_input_bytes: int = 262_144,
        images: tuple[QualifiedImageInput, ...] = (),
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("gateway endpoint must be HTTPS without query or fragment")
        if not token_bound_source:
            raise ValueError("token_bound_source is required")
        self.endpoint = endpoint
        self.gateway_token = gateway_token if gateway_token is not None else os.environ.get("EVALROUTER_TOKEN")
        self.transport = transport or self._httpx_transport
        self.images = tuple(images)
        self.evidence = GatewayEvidence(endpoint=endpoint)
        self._jev = _GatewayJev(
            owner=self,
            endpoint=endpoint,
            transport=self._call_gateway,
            api_key="gateway-owned",
            token_bound=token_bound,
            token_bound_source=token_bound_source,
            max_input_bytes=max_input_bytes,
        )
        self._jev.gateway_images = self.images

    def model_input(self, observation, decisions):
        return self._jev.model_input(observation, decisions)

    def maximum_charge_micros(self, objective, observation, decisions):
        # This is a conservative local reservation.  The gateway remains the
        # sole authority for actual provider charge and billing.
        return self._jev.maximum_charge_micros(objective, observation, decisions)

    def select(self, selection_id, objective, observation, decisions, *, cancel, deadline):
        self._operation_id = selection_id
        self._charged_micros = None
        result = self._jev.select(
            selection_id, objective, observation, decisions, cancel=cancel, deadline=deadline
        )
        if result.abstention:
            return result.model_copy(update={"versions": {**result.versions, **self.evidence.as_dict()}})
        if self._charged_micros is None:
            raise ProviderFailure("gateway response omitted authoritative charge", submitted=True, uncharged=False)
        return result.model_copy(
            update={"cost_micros": self._charged_micros, "versions": {**result.versions, **self.evidence.as_dict()}}
        )

    def lookup(self, attempt_id):
        return None

    def _call_gateway(self, endpoint, headers, body, timeout):
        request = json.loads(body)
        envelope = DecisionGatewayRequest(
            operation_id=self._operation_id,
            model=self.model,
            maximum_charge_micros=self._maximum_charge_micros,
            request=request,
            images=self.images,
        )
        outbound_headers = {"Content-Type": "application/json", "X-EvalRouter-Protocol": GATEWAY_PROTOCOL_VERSION}
        if self.gateway_token:
            outbound_headers["Authorization"] = f"Bearer {self.gateway_token}"
        raw = self.transport(endpoint, outbound_headers, envelope.model_dump_json().encode(), timeout)
        try:
            response = DecisionGatewayResponse.model_validate_json(raw) if isinstance(raw, (bytes, str)) else DecisionGatewayResponse.model_validate(raw)
        except Exception as exc:
            raise ProviderFailure("gateway response was not valid", submitted=True, uncharged=False) from exc
        if response.protocol_version != GATEWAY_PROTOCOL_VERSION or response.operation_id != self._operation_id:
            raise ProviderFailure("gateway response identity mismatch", submitted=True, uncharged=False)
        self._charged_micros = response.charged_micros
        if response.status != "completed" or response.result is None:
            error = response.error
            submitted = True if error is None else error.submitted
            uncharged = False if error is None else not error.charged
            raise JevProviderFailure(error.message if error else "gateway request failed", submitted=submitted, uncharged=uncharged)
        if response.charged_micros is None:
            raise ProviderFailure("gateway response omitted charge", submitted=True, uncharged=False)
        return response.result

    @staticmethod
    def _httpx_transport(endpoint, headers, body, timeout):
        import httpx

        response = httpx.post(endpoint, headers=headers, content=body, timeout=timeout)
        response.raise_for_status()
        return response.content


class _GatewayJev(JevDecisionSelector):
    def __init__(self, *, owner, **kwargs):
        self.owner = owner
        super().__init__(**kwargs)

    def model_input(self, observation, decisions):
        request = super().model_input(observation, decisions)
        if self._gateway_images:
            request = {**request, "images": [image.model_dump(mode="json") for image in self._gateway_images]}
        return request

    @property
    def _gateway_images(self):
        return getattr(self, "gateway_images", ())

    def select(self, selection_id, objective, observation, decisions, *, cancel, deadline):
        self.owner._maximum_charge_micros = self.maximum_charge_micros(objective, observation, decisions)
        return super().select(selection_id, objective, observation, decisions, cancel=cancel, deadline=deadline)

"""EvalRouter decision gateway boundary.

The gateway owns provider credentials and billing.  This module owns the
versioned wire envelope and delegates TypeSafe request encoding and response
interpretation to :mod:`jev`; it never retries a gateway request.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol
from urllib.parse import urlsplit
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import ProviderFailure, Selection
from .jev import DEFAULT_MODEL, JevDecisionSelector, JevProviderFailure

GATEWAY_PROTOCOL_VERSION = "evalrouter.decision.v1"
GATEWAY_PROVIDER = "evalrouter"


def _validate_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("gateway endpoint must be HTTPS or loopback HTTP without credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("HTTP gateway endpoints are restricted to loopback")


class GatewayTransport(Protocol):
    def __call__(self, endpoint: str, headers: Mapping[str, str], body: bytes, timeout: float) -> Any: ...


class GatewayLookupTransport(Protocol):
    def __call__(self, endpoint: str, headers: Mapping[str, str], operation_id: str,
                 scope: Mapping[str, Any], timeout: float) -> Any: ...


class QualifiedImageInput(BaseModel):
    """A bounded, content-addressed image reference accepted by the gateway."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    uri: str = Field(min_length=1, max_length=22_400_000)
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
        if parsed.scheme == "https" and (parsed.username or parsed.password):
            raise ValueError("image uri cannot contain credentials")
        if parsed.scheme == "data":
            try:
                header, encoded = value.split(",", 1)
                if ";base64" not in header:
                    raise ValueError
                decoded = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise ValueError("data image uri must contain valid base64") from exc
            if len(decoded) > 16 * 1024 * 1024:
                raise ValueError("image exceeds maximum size")
        return value

    @field_validator("sha256")
    @classmethod
    def lowercase_hash(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def qualify_content(self):
        if self.uri.startswith("data:"):
            content = base64.b64decode(self.uri.split(",", 1)[1], validate=True)
            if len(content) != self.byte_size or hashlib.sha256(content).hexdigest() != self.sha256:
                raise ValueError("data image uri does not match byte_size or sha256")
        return self


class DecisionGatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal["evalrouter.decision.v1"] = GATEWAY_PROTOCOL_VERSION
    run_id: str = Field(min_length=1, max_length=256)
    episode_id: str = Field(min_length=1, max_length=256)
    workspace_id: str = Field(min_length=1, max_length=256)
    unit_id: str = Field(min_length=1, max_length=256)
    generation: int = Field(ge=1, strict=True)
    operation_id: str = Field(min_length=1, max_length=256)
    model: str = DEFAULT_MODEL
    maximum_charge_micros: int = Field(ge=0, strict=True)
    pricing: Literal["gateway_authoritative"] = "gateway_authoritative"
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
    protocol_version: Literal["evalrouter.decision.v1"] = GATEWAY_PROTOCOL_VERSION
    operation_id: str = Field(min_length=1)
    status: str
    result: dict[str, Any] | None = None
    charged_micros: int | None = Field(default=None, ge=0, strict=True)
    error: GatewayError | None = None


class GenerationGatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal["evalrouter.generation.v1"] = "evalrouter.generation.v1"
    run_id: str = Field(min_length=1, max_length=256)
    episode_id: str = Field(min_length=1, max_length=256)
    workspace_id: str = Field(min_length=1, max_length=256)
    unit_id: str = Field(min_length=1, max_length=256)
    generation: int = Field(ge=1, strict=True)
    operation_id: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=128)
    maximum_charge_micros: int = Field(ge=0, strict=True)
    pricing: Literal["gateway_authoritative"] = "gateway_authoritative"
    request: dict[str, Any]

    @model_validator(mode="after")
    def fixed_billing_request(self):
        if self.request.get("service_tier") != "default" or self.request.get("prompt_cache_options") != {"mode": "explicit"}:
            raise ValueError("gateway generation billing settings are fixed")
        if any(key in self.request for key in ("breakpoints", "tools", "n")):
            raise ValueError("gateway generation request cannot use breakpoints, tools, or n")
        return self


class GenerationGatewayResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal["evalrouter.generation.v1"] = "evalrouter.generation.v1"
    operation_id: str = Field(min_length=1)
    status: Literal["completed", "rejected", "uncertain"]
    result: dict[str, Any] | None = None
    charged_micros: int | None = Field(default=None, ge=0, strict=True)
    error: GatewayError | None = None


class EvalRouterGenerationClient:
    """Stateless generation callable for planners using chat messages.

    The request is submitted once.  Provider keys and billing stay at
    EvalRouter; an ambiguous response is deliberately surfaced as a failure.
    """

    def __init__(self, *, endpoint: str, run_id: str, episode_id: str, model: str,
                 transport: GatewayTransport | None = None, lookup_transport: GatewayLookupTransport | None = None,
                 workspace_id: str, unit_id: str, generation: int,
                 gateway_token: str | None = None, max_output_tokens: int = 4096):
        _validate_endpoint(endpoint)
        self.endpoint, self.run_id, self.episode_id, self.model = endpoint, run_id, episode_id, model
        self.workspace_id, self.unit_id, self.generation = workspace_id, unit_id, generation
        self.transport = transport or EvalRouterGatewaySelector._httpx_transport
        self.lookup_transport = lookup_transport or self._httpx_lookup_transport
        if max_output_tokens < 1 or max_output_tokens > 131072:
            raise ValueError("max_output_tokens must be between 1 and 131072")
        self.max_output_tokens = max_output_tokens
        self._local = threading.local()
        self.gateway_token = gateway_token if gateway_token is not None else os.environ.get("EVALROUTER_TOKEN")

    def __call__(self, request: Mapping[str, Any], *, operation_id: str,
                 maximum_charge_micros: int, cancel: threading.Event | None = None,
                 deadline: float | None = None) -> str:
        if cancel is not None and cancel.is_set():
            raise ProviderFailure("cancelled before gateway submission", submitted=False, uncharged=True)
        envelope = GenerationGatewayRequest(
            run_id=self.run_id, episode_id=self.episode_id, workspace_id=self.workspace_id,
            unit_id=self.unit_id, generation=self.generation, operation_id=operation_id,
            model=self.model, maximum_charge_micros=maximum_charge_micros,
            request={
                **dict(request),
                "max_output_tokens": self.max_output_tokens,
                "service_tier": "default",
                "prompt_cache_options": {"mode": "explicit"},
            },
        )
        headers = {"Content-Type": "application/json", "X-EvalRouter-Protocol": "evalrouter.generation.v1"}
        if self.gateway_token:
            headers["Authorization"] = f"Bearer {self.gateway_token}"
        raw = self.transport(self.endpoint, headers, envelope.model_dump_json().encode(),
                             max(0.001, (deadline - time.monotonic()) if deadline else 30.0))
        try:
            response = GenerationGatewayResponse.model_validate_json(raw) if isinstance(raw, (bytes, str)) else GenerationGatewayResponse.model_validate(raw)
        except Exception as exc:
            raise ProviderFailure("gateway generation response was not valid", submitted=True, uncharged=False) from exc
        if response.protocol_version != "evalrouter.generation.v1" or response.operation_id != operation_id:
            raise ProviderFailure("gateway generation response identity mismatch", submitted=True, uncharged=False)
        if response.charged_micros is None or response.charged_micros > maximum_charge_micros:
            raise ProviderFailure("gateway generation charge exceeded bound", submitted=True, uncharged=False)
        self._local.last_charge_micros = response.charged_micros
        self._local.last_response = response.model_dump(mode="json")
        if response.status != "completed" or not response.result:
            error = response.error
            raise JevProviderFailure(error.message if error else "gateway generation failed",
                                     submitted=True if error is None else error.submitted,
                                     uncharged=False if error is None else not error.charged)
        response_model = response.result.get("model")
        if response_model is not None and response_model != self.model:
            raise ProviderFailure("gateway generation model mismatch", submitted=True, uncharged=False)
        content = response.result.get("content")
        if not isinstance(content, str):
            raise ProviderFailure("gateway generation omitted content", submitted=True, uncharged=False)
        return content

    @property
    def last_charge_micros(self):
        return getattr(self._local, "last_charge_micros", None)

    @property
    def last_response(self):
        return getattr(self._local, "last_response", None)

    def lookup(self, operation_id: str) -> GenerationGatewayResponse | None:
        if self.lookup_transport is None:
            return None
        headers = {"X-EvalRouter-Protocol": "evalrouter.generation.v1"}
        if self.gateway_token:
            headers["Authorization"] = f"Bearer {self.gateway_token}"
        try:
            raw = self.lookup_transport(self.endpoint, headers, operation_id, {
                "run_id": self.run_id, "episode_id": self.episode_id,
                "workspace_id": self.workspace_id, "unit_id": self.unit_id,
                "generation": self.generation,
            }, 30.0)
            response = (GenerationGatewayResponse.model_validate_json(raw)
                        if isinstance(raw, (bytes, str)) else GenerationGatewayResponse.model_validate(raw))
        except Exception:
            return None
        if response.protocol_version != "evalrouter.generation.v1" or response.operation_id != operation_id:
            return None
        return response

    @staticmethod
    def _httpx_lookup_transport(endpoint, headers, operation_id, scope, timeout):
        import httpx

        url = endpoint.rstrip("/") + "/" + quote(operation_id, safe="")
        response = httpx.get(url, headers=headers, params=scope, timeout=timeout)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content


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
        run_id: str,
        episode_id: str,
        workspace_id: str,
        unit_id: str,
        generation: int,
        gateway_token: str | None = None,
        transport: GatewayTransport | None = None,
        lookup_transport: GatewayLookupTransport | None = None,
        token_bound,
        token_bound_source: str,
        max_input_bytes: int = 262_144,
        images: tuple[QualifiedImageInput, ...] = (),
    ) -> None:
        _validate_endpoint(endpoint)
        if not token_bound_source:
            raise ValueError("token_bound_source is required")
        self.endpoint = endpoint
        self.run_id, self.episode_id = run_id, episode_id
        self.workspace_id, self.unit_id, self.generation = workspace_id, unit_id, generation
        self.gateway_token = gateway_token if gateway_token is not None else os.environ.get("EVALROUTER_TOKEN")
        self.transport = transport or self._httpx_transport
        self.lookup_transport = lookup_transport or self._httpx_lookup_transport
        self.images = tuple(images)
        self.max_input_bytes = max_input_bytes
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
        self._local = threading.local()

    def model_input(self, observation, decisions):
        return self._jev.model_input(observation, decisions)

    def maximum_charge_micros(self, objective, observation, decisions):
        # This is a conservative local reservation.  The gateway remains the
        # sole authority for actual provider charge and billing.
        return self._jev.maximum_charge_micros(objective, observation, decisions)

    def request_encoded(self, body: bytes, *, maximum_charge_micros: int,
                        cancel: threading.Event, deadline: float, operation_id: str | None = None):
        """Compatibility boundary for the Minecraft Jev command interpreter."""
        self._local.operation_id = operation_id or f"jev:{__import__('uuid').uuid4().hex}"
        self._local.maximum_charge_micros = maximum_charge_micros
        response, _estimated, evidence = self._jev.request_encoded(
            body, maximum_charge_micros=maximum_charge_micros, cancel=cancel, deadline=deadline
        )
        if self._local.charged_micros is None:
            raise ProviderFailure("gateway response omitted authoritative charge", submitted=True, uncharged=False)
        return response, self._local.charged_micros, {**evidence, **self.evidence.as_dict()}

    def select(self, selection_id, objective, observation, decisions, *, cancel, deadline):
        self._local.operation_id = selection_id
        self._local.charged_micros = None
        result = self._jev.select(
            selection_id, objective, observation, decisions, cancel=cancel, deadline=deadline
        )
        if result.abstention:
            return result.model_copy(update={"versions": {**result.versions, **self.evidence.as_dict()}})
        if self._local.charged_micros is None:
            raise ProviderFailure("gateway response omitted authoritative charge", submitted=True, uncharged=False)
        return result.model_copy(
            update={"cost_micros": self._local.charged_micros, "versions": {**result.versions, **self.evidence.as_dict()}}
        )

    def lookup(self, attempt_id):
        if self.lookup_transport is None:
            return None
        headers = {"X-EvalRouter-Protocol": GATEWAY_PROTOCOL_VERSION}
        if self.gateway_token:
            headers["Authorization"] = f"Bearer {self.gateway_token}"
        try:
            raw = self.lookup_transport(self.endpoint, headers, attempt_id, {
                "run_id": self.run_id, "episode_id": self.episode_id,
                "workspace_id": self.workspace_id, "unit_id": self.unit_id,
                "generation": self.generation,
            }, 30.0)
            response = (DecisionGatewayResponse.model_validate_json(raw)
                        if isinstance(raw, (bytes, str)) else DecisionGatewayResponse.model_validate(raw))
        except Exception:
            return None
        if (response.protocol_version != GATEWAY_PROTOCOL_VERSION
                or response.operation_id != attempt_id
                or response.status != "completed"
                or response.result is None
                or response.charged_micros is None):
            return None
        try:
            selection = Selection.model_validate(response.result)
        except Exception:
            return None
        if selection.cost_micros is not None and selection.cost_micros != response.charged_micros:
            return None
        return selection.model_copy(update={"cost_micros": response.charged_micros})

    def _call_gateway(self, endpoint, headers, body, timeout):
        request = json.loads(body)
        envelope = DecisionGatewayRequest(
            operation_id=self._local.operation_id,
            run_id=self.run_id,
            episode_id=self.episode_id,
            workspace_id=self.workspace_id,
            unit_id=self.unit_id,
            generation=self.generation,
            model=self.model,
            maximum_charge_micros=self._local.maximum_charge_micros,
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
        if response.protocol_version != GATEWAY_PROTOCOL_VERSION or response.operation_id != self._local.operation_id:
            raise ProviderFailure("gateway response identity mismatch", submitted=True, uncharged=False)
        self._local.charged_micros = response.charged_micros
        self._local.last_response = response.model_dump(mode="json")
        if response.charged_micros is not None and response.charged_micros > self._local.maximum_charge_micros:
            raise ProviderFailure("gateway charge exceeded reserved bound", submitted=True, uncharged=False)
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

    @staticmethod
    def _httpx_lookup_transport(endpoint, headers, operation_id, scope, timeout):
        import httpx

        url = endpoint.rstrip("/") + "/" + quote(operation_id, safe="")
        response = httpx.get(url, headers=headers, params=scope, timeout=timeout)
        if response.status_code == 404:
            return None
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
        self.owner._local.maximum_charge_micros = self.maximum_charge_micros(objective, observation, decisions)
        return super().select(selection_id, objective, observation, decisions, cancel=cancel, deadline=deadline)

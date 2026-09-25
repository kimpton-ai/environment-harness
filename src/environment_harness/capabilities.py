"""Deployment capability discovery for conditionally available operations.

`GET /v1/capabilities` is available to every valid bearer credential and returns
only non-secret stable capability names with `enabled` and an optional public
reason. Every conditionally available OpenAPI operation declares its capability
through `x-capability`; calling a disabled one returns
`501 capability_unavailable` with the same name, while transient failure of an
enabled capability returns `503 service_unavailable`.
"""

from __future__ import annotations

from pydantic import Field

from .contracts import Record

#: Stable capability names. Adding one requires an OpenAPI `x-capability`
#: annotation on every operation it gates, or the contract test fails closed.
HISTORICAL_INGESTION = "historical-ingestion"
PARTICIPANT_CREDENTIALS = "participant-credentials"
LOCAL_VIEWER = "local-viewer"

CAPABILITY_NAMES = (HISTORICAL_INGESTION, PARTICIPANT_CREDENTIALS, LOCAL_VIEWER)


class Capability(Record):
    """One non-secret deployment capability."""

    name: str = Field(min_length=1, max_length=100)
    enabled: bool
    #: A public explanation. It never names a host, credential, or internal path.
    reason: str | None = Field(default=None, max_length=300)


class CapabilityDocument(Record):
    """The capability singleton returned by `GET /v1/capabilities`."""

    protocol: str = Field(default="environment-session.v1", min_length=1, max_length=100)
    capabilities: tuple[Capability, ...] = Field(min_length=1)


def document(*, trajectory_ingestion: bool, local_access: bool) -> CapabilityDocument:
    return CapabilityDocument(
        capabilities=(
            Capability(
                name=HISTORICAL_INGESTION,
                enabled=trajectory_ingestion,
                reason=(
                    None
                    if trajectory_ingestion
                    else "historical ingestion is disabled on this read-only deployment"
                ),
            ),
            Capability(name=PARTICIPANT_CREDENTIALS, enabled=True),
            Capability(
                name=LOCAL_VIEWER,
                enabled=local_access,
                reason=None if local_access else "this deployment requires an issued credential",
            ),
        )
    )

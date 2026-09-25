"""Server-owned credential policies and private access contexts.

EnvironmentHarness has no users, groups, organizations, OAuth, or RBAC. A remote
caller sends only an opaque bearer credential; the server resolves it to an
identity plus one of the fixed policies below. Permissions are never accepted in
a request, a query parameter, or an SDK domain method, so a caller cannot mint
broader access by asserting a claim.

Nothing in this module is part of the public API. ``docs/AUTHENTICATION.md``
documents the boundary these policies implement.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from .errors import Forbidden
from .store import digest

#: Every internal action an authenticated HTTP operation or private runtime
#: mutation may require. Adding an action without registering it in
#: :data:`POLICY_ACTIONS` fails closed because no policy allows it.
ACTIONS = (
    "capabilities.read",
    "session.read",
    "session.create",
    "session.write",
    "session.control",
    "observation.read",
    "participant.act",
    "participant.memory.write",
    "operation.prepare",
    "evidence.read.full",
    "evidence.read.scoped",
    "activity.read",
    "artifact.read",
    "artifact.write",
    "score.read",
    "score.write",
    "trajectory.read",
    "snapshot.create",
    "source.register",
    "source.ingest",
    "source.status.write",
    "dataset.read",
    "dataset.create",
    "training.read",
    "training.execute",
    "credential.participant.issue",
    "comparison.read",
)

_READ_ACTIONS = frozenset(
    {
        "capabilities.read",
        "session.read",
        "observation.read",
        "evidence.read.full",
        "activity.read",
        "artifact.read",
        "score.read",
        "trajectory.read",
        "dataset.read",
        "training.read",
        "comparison.read",
    }
)

_PARTICIPANT_ACTIONS = frozenset(
    {
        "capabilities.read",
        "session.read",
        "observation.read",
        "participant.act",
        "participant.memory.write",
        "operation.prepare",
        "evidence.read.scoped",
        "artifact.read",
        "artifact.write",
    }
)

PolicyName = Literal["trusted-local", "admin", "viewer", "participant"]

#: Fixed server-owned credential policies. These names describe internal
#: credential behavior, not users, organization membership, or public roles.
POLICY_ACTIONS: dict[str, frozenset[str]] = {
    # The direct in-process Python SDK is a trusted local interface.
    "trusted-local": frozenset(ACTIONS),
    # Issued through the trusted ``environment-harness token`` command or the
    # embedding API. Training integrations are never executed over HTTP, and the
    # participant surface is reachable only through a participant credential.
    "admin": frozenset(ACTIONS)
    - {
        "training.execute",
        "participant.act",
        "participant.memory.write",
        "operation.prepare",
        "evidence.read.scoped",
    },
    # The automatic loopback viewer credential stays in page memory.
    "viewer": _READ_ACTIONS,
    # Issued only through the participant-credential operation and bound to one
    # session, participant, and generation.
    "participant": _PARTICIPANT_ACTIONS,
}

#: Policies a credential may carry. ``trusted-local`` is never issuable: it
#: exists only for the in-process facade.
ISSUABLE_POLICIES = ("admin", "viewer", "participant")

#: Policies that are constrained to a single session, participant, generation.
PARTICIPANT_SCOPED = ("participant",)


def fingerprint(value) -> str:
    """Render one canonical digest as a grouped, human-comparable fingerprint.

    The grouping is cosmetic but deliberate: a fingerprint is published in
    generated documentation and manifests, and an unbroken 64-character hex run
    there is indistinguishable from a leaked secret to a scanner.
    """

    raw = digest(value)
    return "sha256:" + "-".join(raw[index : index + 8] for index in range(0, len(raw), 8))


def registry_fingerprint() -> str:
    """Fingerprint the access registry so the HTTP migration gate detects drift."""

    return fingerprint(
        {
            "actions": list(ACTIONS),
            "policies": {name: sorted(actions) for name, actions in sorted(POLICY_ACTIONS.items())},
            "issuable": list(ISSUABLE_POLICIES),
            "participant_scoped": list(PARTICIPANT_SCOPED),
        }
    )


@dataclass(frozen=True)
class _AccessContext:
    """One resolved caller identity and its server-assigned policy.

    ``session``, ``participant``, and ``generation`` are object-level
    constraints. A context with ``session`` set cannot observe or mutate any
    other session even when its policy would otherwise allow the action.
    """

    tenant: str
    subject: str
    policy: str
    session: str | None = None
    participant: str | None = None
    generation: int = 0

    def __post_init__(self):
        if self.policy not in POLICY_ACTIONS:
            raise ValueError("unknown access policy")
        if self.policy in PARTICIPANT_SCOPED and (self.session is None or self.participant is None):
            raise ValueError("participant policies require a session and participant constraint")

    @property
    def full_evidence(self) -> bool:
        """True when this context reads unfiltered evidence rather than one audience."""

        return "evidence.read.full" in POLICY_ACTIONS[self.policy]

    def allows(self, action: str) -> bool:
        return action in POLICY_ACTIONS[self.policy]

    def require(self, action: str) -> None:
        if action not in ACTIONS:
            raise Forbidden("unregistered access action")
        if not self.allows(action):
            raise Forbidden("credential policy denies this operation")

    def scoped_to(self, session: str) -> bool:
        return self.session is None or self.session == session

    def replace(self, **changes) -> _AccessContext:
        """Derive a related context. Server-owned policies stay authoritative."""

        return replace(self, **changes)


def trusted_local(tenant: str, subject: str = "environment-harness") -> _AccessContext:
    """Create the unauthenticated in-process context used by the local facade."""

    return _AccessContext(tenant=tenant, subject=subject, policy="trusted-local")

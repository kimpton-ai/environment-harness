class HarnessError(Exception):
    """One stable error code shared by Python, HTTP, OpenAPI, and TypeScript.

    Every subclass carries a stable ``code`` so no caller parses message text.
    See ``docs/API-REFERENCE.md`` for the full taxonomy.
    """

    code = "harness_error"


class Unauthenticated(HarnessError):
    """A credential is missing, malformed, expired, revoked, or otherwise invalid."""

    code = "unauthorized"


class Forbidden(HarnessError):
    code = "forbidden"


class Conflict(HarnessError):
    code = "conflict"


class Unsupported(HarnessError):
    """An unknown required feature or an unsupported contract kind."""

    code = "unsupported_contract"


class EvidenceIncomplete(HarnessError):
    """Incomplete evidence, an unresolved reward, or a nonterminal input."""

    code = "evidence_incomplete"


class PayloadTooLarge(HarnessError):
    """An oversized request, event, or artifact."""

    code = "payload_too_large"


class CapabilityUnavailable(HarnessError):
    """A configured capability is disabled on this deployment."""

    code = "capability_unavailable"

    def __init__(self, capability: str, reason: str | None = None):
        super().__init__(reason or f"capability {capability!r} is not enabled on this deployment")
        self.capability = capability


class Unavailable(HarnessError):
    """A transient dependency or store unavailability."""

    code = "service_unavailable"


class BudgetExceeded(HarnessError):
    code = "budget_exhausted"


class ServiceError(HarnessError):
    """A validated error envelope returned by a remote EnvironmentHarness service."""

    def __init__(self, message, *, code, status, request_id, timestamp, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id
        self.timestamp = timestamp
        self.details = details

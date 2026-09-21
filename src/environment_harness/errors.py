class HarnessError(Exception):
    code = "harness_error"


class Forbidden(HarnessError):
    code = "forbidden"


class Conflict(HarnessError):
    code = "conflict"


class Unsupported(HarnessError):
    code = "unsupported"


class Unavailable(HarnessError):
    code = "unavailable"


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

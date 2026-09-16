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

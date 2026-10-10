"""Stable exception identities for version-one motor callers."""

from .contracts import OutcomeUncertain


class MotorError(ValueError):
    pass


MotorOutcomeUnknown = OutcomeUncertain

"""Explicit experiment-specification workflow for advanced local callers.

The authorization-aware session runtime is deliberately not exported. Local
execution goes through :class:`environment_harness.EnvironmentHarness`, the only
public local-execution facade.
"""

from .contracts import ExperimentSpec
from .runner import run

__all__ = ["ExperimentSpec", "run"]

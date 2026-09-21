"""EnvironmentHarness. Supplier implementations are independently owned."""

from .contracts import AgentSpec, EnvironmentSpec, ExperimentSpec, Principal
from .motor import MotorExecutor
from .motor_contracts import (
    ControlClaim,
    GoalContext,
    MilestoneIdentity,
    MotorAdapter,
    MotorGroup,
    MotorGroupReceipt,
    MotorProfile,
    MotorRequest,
    ProgressReceipt,
    ResourceOwnership,
)
from .runtime import EnvironmentSession
from .store import EvidenceStore

__all__ = [
    "AgentSpec",
    "EnvironmentSpec",
    "ExperimentSpec",
    "Principal",
    "EnvironmentSession",
    "EvidenceStore",
    "MotorExecutor",
    "MotorAdapter",
    "MotorProfile",
    "MotorRequest",
    "GoalContext",
    "MilestoneIdentity",
    "ResourceOwnership",
    "ControlClaim",
    "ProgressReceipt",
    "MotorGroup",
    "MotorGroupReceipt",
]
__version__ = "0.2.3rc1"

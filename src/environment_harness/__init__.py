"""EnvironmentHarness. Supplier implementations are independently owned."""

from .contracts import AgentSpec, EnvironmentSpec, ExperimentSpec, Principal, Scenario
from .harness import EnvironmentHarness, EnvironmentSession, Experiment, ExperimentResult
from .motor import MotorExecutor
from .motor_contracts import (
    ControlClaim,
    GoalContext,
    MilestoneIdentity,
    MotorAdapter,
    MotorExecutionMetadata,
    MotorGroup,
    MotorGroupReceipt,
    MotorProfile,
    MotorRequest,
    PreparedSuccessorAdmission,
    PreparedSuccessorIntent,
    ProgressReceipt,
    ResourceOwnership,
    UnsupportedPreparation,
)
from .store import EvidenceStore

__all__ = [
    "AgentSpec",
    "EnvironmentSpec",
    "ExperimentSpec",
    "Principal",
    "Scenario",
    "EnvironmentSession",
    "EnvironmentHarness",
    "Experiment",
    "ExperimentResult",
    "EvidenceStore",
    "MotorExecutor",
    "MotorAdapter",
    "MotorProfile",
    "MotorRequest",
    "MotorExecutionMetadata",
    "PreparedSuccessorAdmission",
    "PreparedSuccessorIntent",
    "UnsupportedPreparation",
    "GoalContext",
    "MilestoneIdentity",
    "ResourceOwnership",
    "ControlClaim",
    "ProgressReceipt",
    "MotorGroup",
    "MotorGroupReceipt",
]
__version__ = "0.2.4rc1"

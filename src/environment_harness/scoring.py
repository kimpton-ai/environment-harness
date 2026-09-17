"""Generic raw measurements and isolated scorer programs. No supplier grading rules."""

from collections import Counter

from .adapters.programs import CommandAgent
from .contracts import ScoreReport


class EventMeasurements:
    """Raw event counts. Inactivity is not inferred to be compliant or competent."""

    def __init__(self, version="1"):
        self.version = version

    def score(self, evidence):
        counts = Counter()
        cursor = 0
        for event in evidence:
            cursor = max(cursor, event["seq"])
            counts[event["kind"]] += 1
            if event["kind"] == "action.attempted":
                counts["blocked"] += event["payload"]["receipt"]["status"] == "blocked"
                counts["malformed"] += event["payload"].get("category") == "malformed"
        return ScoreReport(
            scorer="event-measurements",
            version=self.version,
            kind="deterministic",
            evidence_cursor=cursor,
            metrics={
                "attempted_actions": counts["action.attempted"],
                "executed_actions": counts["action.executed"],
                "blocked_attempts": counts["blocked"],
                "malformed_outputs": counts["malformed"] + counts["agent.malformed"],
                "infrastructure_failures": counts["agent.failure"] + counts["model.failure"],
                "competence": None,
                "compliance": None,
                "harm": None,
                "opportunities": None,
            },
            metric_definitions={
                key: {"id": "environment-harness." + key, "version": self.version, "unit": "count"}
                for key in ("attempted_actions", "executed_actions", "blocked_attempts", "malformed_outputs", "infrastructure_failures")
            },
            uncertainty="Event counts are observed. Domain competence, compliance, harm and opportunities require a supplier scorer.",
            provenance={"measurement": "raw-event-counts", "domain_judgments": False},
        )


class CommandScorer:
    """Run a deterministic or model-backed scorer through a separate JSON process.

    The caller supplies only evidence authorized for this scorer. The scorer must
    produce a complete ScoreReport, including its kind, provenance and uncertainty.
    """

    def __init__(self, command, implementation, *, timeout=30):
        self.program = CommandAgent(command, implementation, timeout=timeout)

    def score(self, evidence):
        return ScoreReport.model_validate(self.program.act({"evidence": evidence}))

"""A rich, deterministic EnvironmentHarness example with synthetic evidence only."""

from __future__ import annotations

from .access import _AccessContext
from .contracts import (
    Action,
    AgentSpec,
    ExperimentSpec,
    Finding,
    MetricDefinition,
    RunPolicy,
    Scenario,
    ScoreReport,
)
from .fixtures import SyntheticEnvironment, SyntheticScenarioInput, SyntheticShowcaseAgent
from .harness import EnvironmentHarness
from .runner import run
from .runtime import _SessionRuntime


def _record_synthetic_comparison_report(store, researcher, environment: str) -> None:
    """Record deterministic fixture metrics that exercise cross-session comparison."""
    evidence = list(store.replay(environment, researcher))
    executed = [event for event in evidence if event["kind"] == "action.executed"]
    rewards = [
        event["payload"]["reward"]
        for event in executed
        if isinstance(event["payload"].get("reward"), (int, float))
    ]
    total = next(
        event["payload"]["total"] for event in reversed(evidence) if event["kind"] == "synthetic.total"
    )
    store.report(
        environment,
        researcher,
        ScoreReport(
            scorer="synthetic-showcase",
            version="1",
            kind="deterministic",
            evidence_cursor=max(event["seq"] for event in evidence),
            metrics={
                "synthetic_total": total,
                "cumulative_reward": sum(rewards),
                "executed_actions": len(executed),
            },
            metric_definitions={
                "synthetic_total": MetricDefinition(id="synthetic.total", version="1", unit="count"),
                "cumulative_reward": MetricDefinition(
                    id="synthetic.cumulative-reward", version="1", unit="reward"
                ),
                "executed_actions": MetricDefinition(
                    id="environment-harness.executed-actions", version="1", unit="count"
                ),
            },
            uncertainty=(
                "Synthetic comparison fixture only; these values demonstrate SDK aggregation "
                "and do not measure model quality or safety."
            ),
            provenance={"synthetic": True, "source": "environment_harness.showcase"},
        ),
    )


def create_synthetic_experiment_showcase(
    store,
    researcher,
    *,
    turns: int = 3,
    name: str = "Customer support workflow",
    scenarios=None,
):
    """Run a typed scenario × trial experiment for the grouped viewer surfaces."""
    if turns < 1:
        raise ValueError("showcase turns must be at least one")
    scenarios = scenarios or (
        Scenario(
            id="refund-request",
            input=SyntheticScenarioInput(starting_total=-3),
            reference={"starting_total": -3},
            metadata={"name": "Refund request", "description": "Review a refund request."},
        ),
        Scenario(
            id="damaged-delivery",
            input=SyntheticScenarioInput(starting_total=0),
            reference={"starting_total": 0},
            metadata={"name": "Damaged delivery", "description": "Review a damaged delivery."},
        ),
        Scenario(
            id="account-recovery",
            input=SyntheticScenarioInput(starting_total=3),
            reference={"starting_total": 3},
            metadata={"name": "Account recovery", "description": "Review an account recovery."},
        ),
    )
    harness = EnvironmentHarness(
        store,
        environment_factory=SyntheticEnvironment,
        agent_factories={
            "alice": lambda: SyntheticShowcaseAgent(0),
            "bob": lambda: SyntheticShowcaseAgent(1),
        },
        scoring_versions=("synthetic-showcase@1",),
        max_concurrency=2,
        tenant=researcher.tenant,
    )
    result = harness.experiment(
        name,
        scenarios,
        trials=2,
        seed=42,
        turns=turns,
    ).run()
    for session in result.sessions:
        _record_synthetic_comparison_report(store, researcher, session.id)
    return {
        "id": result.id,
        "name": result.name,
        "status": result.status,
        "total": result.total,
        "completed": result.completed,
        "running": result.running,
        "queued": result.queued,
        "failed": result.failed,
        "turns": turns,
        "sessions": [
            {
                "id": session.id,
                "scenario_id": session.scenario_id,
                "trial": session.trial + 1,
                "seed": session.seed,
                "status": session.status,
                "turns": turns,
            }
            for session in result.sessions
        ],
    }


def create_synthetic_review_demo(
    store,
    researcher,
    *,
    turns: int = 10,
    training: bool = False,
):
    """Create grouped and rich standalone data that can be reviewed in the local viewer."""
    boundary_turns = max(1, turns // 2)
    boundary_experiment = create_synthetic_experiment_showcase(
        store,
        researcher,
        turns=boundary_turns,
        name="Policy boundary checks",
        scenarios=(
            Scenario(
                id="low-confidence-escalation",
                input=SyntheticScenarioInput(starting_total=-5),
                reference={"starting_total": -5},
                metadata={
                    "name": "Low-confidence escalation",
                    "description": "Exercise an escalation below the synthetic threshold.",
                },
            ),
            Scenario(
                id="high-confidence-resolution",
                input=SyntheticScenarioInput(starting_total=5),
                reference={"starting_total": 5},
                metadata={
                    "name": "High-confidence resolution",
                    "description": "Exercise a resolution above the synthetic threshold.",
                },
            ),
        ),
    )
    experiment = create_synthetic_experiment_showcase(store, researcher, turns=turns)
    showcase = create_synthetic_showcase(
        store,
        researcher,
        turns=turns,
        training=training,
        name="Refund escalation review",
        scenario="refund-escalation-review",
    )
    account_recovery = create_synthetic_showcase(
        store,
        researcher,
        turns=min(turns, 4),
        participants=("maya", "leo"),
        training=training,
        name="Account recovery review",
        scenario="account-recovery-review",
    )
    team_handoff = create_synthetic_showcase(
        store,
        researcher,
        turns=min(turns, 4),
        participants=("nora", "omar", "priya", "quinn"),
        training=training,
        name="Team handoff review",
        scenario="team-handoff-review",
    )
    named_sessions = (
        ("Refund escalation review", showcase),
        ("Account recovery review", account_recovery),
        ("Team handoff review", team_handoff),
    )
    return showcase | {
        "demo_experiment": experiment,
        "demo_experiments": [experiment, boundary_experiment],
        "demo_environment_sessions": [
            {
                "id": item["id"],
                "name": name,
                "scenario": item["experiment"]["scenario"],
                "participants": item["participants"],
                "turns": item["revision"],
                "status": item["status"],
            }
            for name, item in named_sessions
        ],
        "review": {
            "home": "/home",
            "experiment": f"/experiment/{experiment['id']}",
            "environment_session": f"/session/{showcase['id']}/overview",
        },
    }


def create_synthetic_showcase(
    store,
    researcher,
    *,
    turns: int = 10,
    participants: tuple[str, ...] = ("alice", "bob"),
    training: bool = False,
    name: str | None = None,
    scenario: str = "viewer-showcase",
):
    """Create one environment session that exercises the viewer's recorded-evidence surfaces."""
    if turns < 1:
        raise ValueError("showcase turns must be at least one")
    implementation = SyntheticEnvironment()
    session = _SessionRuntime(store, implementation)
    spec = ExperimentSpec(
        environment=implementation.spec,
        participants=tuple(
            AgentSpec(
                id=name,
                implementation="synthetic-showcase-agent@1",
                policy_version="1",
                config={"sequence_offset": index},
                checkpoint=True,
            )
            for index, name in enumerate(participants)
        ),
        scenario=scenario,
        scenario_metadata={
            "description": "Synthetic environment-session evidence for viewer demonstration only.",
        }
        | ({"name": name} if name else {}),
        purpose="training" if training else "evaluation",
        split="training" if training else "heldout",
        policy=RunPolicy(max_turns=turns),
        scoring_versions=("synthetic-showcase@1",),
    )
    environment = session.create(spec, researcher)["id"]
    agents = {name: SyntheticShowcaseAgent(index) for index, name in enumerate(participants)}
    checkpoint_turn = max(1, turns // 2)
    blocked_turn = min(2, turns)

    for turn_number in range(1, turns + 1):
        blocked = None
        if turn_number == blocked_turn:
            participant = participants[0]
            principal = _AccessContext(
                tenant=researcher.tenant,
                subject=participant,
                policy="participant",
                session=environment,
                participant=participant,
            )
            observation = session.observe(environment, principal)
            operation_id = f"showcase-blocked-{turn_number}-{participant}"
            receipt = session.submit(
                environment,
                principal,
                Action(
                    operation_id=operation_id,
                    participant=participant,
                    observation_id=observation["id"],
                    revision=observation["revision"],
                    payload={"value": 2},
                ),
            )
            if receipt["status"] != "blocked":
                raise RuntimeError("synthetic showcase expected its out-of-schema action to be blocked")
            blocked = (operation_id, observation["id"], participant)

        result = run(session, environment, researcher, agents, turns=1)
        if turn_number == checkpoint_turn:
            lease = session.lease(environment, researcher, "synthetic-showcase-checkpoint")
            try:
                session.checkpoint(environment, researcher, lease, exact_agents=True)
            finally:
                session.release(environment, researcher, lease)
            store.artifact(
                environment,
                researcher,
                b'{"synthetic":true,"purpose":"viewer-showcase"}\n',
                audience=("*",),
                media_type="application/json",
            )

        evidence = list(store.replay(environment, researcher))
        executed = [
            event
            for event in evidence
            if event["kind"] == "action.executed" and event["revision"] == result["revision"]
        ]
        total = next(
            event["payload"]["total"] for event in reversed(evidence) if event["kind"] == "synthetic.total"
        )
        findings = ()
        if blocked is not None:
            operation_id, observation_id, participant = blocked
            attempted = next(
                event
                for event in evidence
                if event["kind"] == "action.attempted"
                and event["payload"]["action"]["operation_id"] == operation_id
            )
            findings = (
                Finding(
                    rule="synthetic.out-of-schema-action",
                    participant=participant,
                    observation_id=observation_id,
                    action_id=operation_id,
                    outcome_event=attempted["seq"],
                    category="malformed",
                    status="blocked",
                    judgment="The showcase intentionally submitted a value outside the synthetic action schema.",
                    uncertainty="Synthetic protocol demonstration only.",
                ),
            )
        store.report(
            environment,
            researcher,
            ScoreReport(
                scorer="synthetic-showcase",
                version="1",
                kind="deterministic",
                evidence_cursor=max(event["seq"] for event in evidence),
                metrics={
                    "synthetic_total": total,
                    "turn_reward": sum(
                        event["payload"]["reward"]
                        for event in executed
                        if isinstance(event["payload"].get("reward"), (int, float))
                    ),
                    "executed_actions": len(executed),
                },
                metric_definitions={
                    "synthetic_total": MetricDefinition(id="synthetic.total", version="1", unit="count"),
                    "turn_reward": MetricDefinition(id="synthetic.turn-reward", version="1", unit="reward"),
                    "executed_actions": MetricDefinition(
                        id="environment-harness.executed-actions", version="1", unit="count"
                    ),
                },
                findings=findings,
                rewards={
                    event["payload"]["participant"]: event["payload"]["reward"]
                    for event in executed
                    if isinstance(event["payload"].get("reward"), (int, float))
                },
                uncertainty="Synthetic protocol demonstration only; these values do not measure model quality or safety.",
                provenance={"synthetic": True, "source": "environment_harness.showcase"},
            ),
        )
    return session.get(environment, researcher)

import pytest

from environment_harness import AgentSpec, EvidenceStore, ExperimentSpec
from environment_harness.access import _AccessContext
from environment_harness.contracts import Action, Finding, ScoreReport
from environment_harness.errors import Conflict
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.runtime import _SessionRuntime
from environment_harness.store import uid


def setup(tmp_path):
    store = EvidenceStore(tmp_path)
    implementation = SyntheticEnvironment()
    session = _SessionRuntime(store, implementation)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    spec = ExperimentSpec(
        environment=implementation.spec,
        participants=(
            AgentSpec(id="a", implementation="synthetic", policy_version="1"),
            AgentSpec(id="b", implementation="synthetic", policy_version="1"),
        ),
        scoring_versions=("control@1",),
    )
    environment = session.create(spec, researcher)["id"]
    agents = {
        name: _AccessContext(
            tenant="tenant", subject=name, policy="participant", session=environment, participant=name
        )
        for name in ("a", "b")
    }
    observations = {name: session.observe(environment, principal) for name, principal in agents.items()}
    action = Action(
        operation_id=uid(),
        participant="a",
        observation_id=observations["a"]["id"],
        revision=0,
        payload={"value": 1},
    )
    session.submit(environment, agents["a"], action)
    other_action = Action(
        operation_id=uid(),
        participant="b",
        observation_id=observations["b"]["id"],
        revision=0,
        payload={"value": 1},
    )
    session.submit(environment, agents["b"], other_action)
    lease = session.lease(environment, researcher, "worker")
    session.resolve(environment, researcher, lease)
    session.release(environment, researcher, lease)
    events = list(store.replay(environment, researcher))
    outcome = next(
        event
        for event in events
        if event["kind"] == "action.executed" and event["payload"]["participant"] == "a"
    )
    consequence = next(event for event in events if event["kind"] == "transition.committed")
    return store, researcher, environment, observations, action, outcome, consequence


def report(store, researcher, environment, finding):
    return ScoreReport(
        scorer="control",
        version="1",
        kind="deterministic",
        evidence_cursor=store.verify(environment, researcher)["events"],
        metrics={"score": 1},
        findings=(finding,),
        uncertainty="synthetic test",
        provenance={"synthetic": True},
    )


def action_finding(observation, action, outcome, consequence, **changes):
    values = {
        "rule": "synthetic-action",
        "participant": "a",
        "observation_id": observation["id"],
        "action_id": action.operation_id,
        "outcome_event": outcome["seq"],
        "consequence_events": (consequence["seq"],),
        "category": "competence",
        "status": "executed",
        "judgment": "synthetic",
        "uncertainty": "none",
    }
    values.update(changes)
    return Finding(**values)


def test_action_findings_require_same_participant_observation_and_outcome(tmp_path):
    store, researcher, environment, observations, action, outcome, consequence = setup(tmp_path)
    valid = action_finding(observations["a"], action, outcome, consequence)
    assert (
        store.report(environment, researcher, report(store, researcher, environment, valid))["revision"] == 1
    )
    with pytest.raises(Conflict, match="scorer version"):
        store.report(
            environment,
            researcher,
            report(store, researcher, environment, valid).model_copy(update={"version": "2"}),
        )

    invalid = [
        action_finding(observations["a"], action, outcome, consequence, observation_id=uid()),
        action_finding(observations["a"], action, outcome, consequence, action_id=uid()),
        action_finding(observations["a"], action, outcome, consequence, participant="b"),
        action_finding(
            observations["a"], action, outcome, consequence, observation_id=observations["b"]["id"]
        ),
        action_finding(observations["a"], action, outcome, consequence, outcome_event=999999),
        action_finding(observations["a"], action, outcome, consequence, outcome_event=consequence["seq"]),
        action_finding(observations["a"], action, outcome, consequence, consequence_events=(999999,)),
    ]
    for finding in invalid:
        with pytest.raises(Conflict):
            store.report(environment, researcher, report(store, researcher, environment, finding))


def test_omission_findings_require_ordered_participant_evidence(tmp_path):
    store, researcher, environment, observations, action, outcome, consequence = setup(tmp_path)
    with store.transaction() as db:
        row = store.environment(db, environment, researcher)
        opportunity = store.append(
            db, environment, row["revision"], "opportunity", {"participant": "a"}, ("a",)
        )
        omitted = store.append(db, environment, row["revision"], "outcome", {"actor": "a"}, ("a",))
        effect = store.append(db, environment, row["revision"], "consequence", {"participant": "a"}, ("a",))
    finding = Finding(
        rule="synthetic-omission",
        participant="a",
        observation_id=observations["a"]["id"],
        action_id=None,
        action_item="expected action",
        opportunity_event=opportunity["seq"],
        outcome_event=omitted["seq"],
        consequence_events=(effect["seq"],),
        category="competence",
        status="omitted",
        judgment="synthetic",
        uncertainty="none",
    )
    assert (
        store.report(environment, researcher, report(store, researcher, environment, finding))["revision"]
        == 1
    )

    for changes in (
        {"observation_id": uid()},
        {"participant": "b"},
        {"opportunity_event": 999999},
        {"opportunity_event": omitted["seq"], "outcome_event": opportunity["seq"]},
        {"consequence_events": (999999,)},
    ):
        with pytest.raises(Conflict):
            store.report(
                environment,
                researcher,
                report(store, researcher, environment, finding.model_copy(update=changes)),
            )

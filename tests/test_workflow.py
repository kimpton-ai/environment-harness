import json

import pytest
from fastapi.testclient import TestClient

from environment_harness.contracts import Action, AgentSpec, ExperimentSpec, Principal, RunPolicy, ScoreReport
from environment_harness.errors import Conflict, Forbidden, Unsupported
from environment_harness.evaluation import rollouts
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.operations import Operations
from environment_harness.runner import run
from environment_harness.runtime import EnvironmentSession
from environment_harness.server import create_app
from environment_harness.store import EvidenceStore, uid


@pytest.fixture
def setup(tmp_path):
    store = EvidenceStore(tmp_path)
    env = SyntheticEnvironment()
    session = EnvironmentSession(store, env)
    researcher = Principal(tenant="test", subject="researcher", role="researcher")
    spec = ExperimentSpec(
        environment=env.spec,
        participants=tuple(
            AgentSpec(id=p, implementation="synthetic@1", policy_version="1", checkpoint=True)
            for p in ("alice", "bob")
        ),
        scoring_versions=("control@1",),
        policy=RunPolicy(max_turns=20),
    )
    environment = session.create(spec, researcher)["id"]
    agents = {
        p: Principal(tenant="test", subject=p, role="agent", environment=environment, participant=p)
        for p in ("alice", "bob")
    }
    return store, session, researcher, spec, environment, agents


def decide(session, environment, who, value=1):
    observation = session.observe(environment, who)
    action = Action(
        operation_id=uid(),
        participant=who.participant,
        observation_id=observation["id"],
        revision=observation["revision"],
        payload={"value": value},
    )
    return action, session.submit(environment, who, action)


def test_complete_local_flow_and_branch(setup):
    store, session, who, spec, environment, agents = setup
    run(session, environment, who, {p: SyntheticAgent() for p in agents}, turns=3)
    assert session.observe(environment, agents["alice"])["payload"]["total"] == 6
    lease = session.lease(environment, who, "test")
    checkpoint = session.checkpoint(environment, who, lease, exact_agents=True)
    child = session.branch(environment, who, checkpoint["id"], {"total": 50})
    assert session.observe(child["id"], who, "alice")["payload"]["total"] == 50
    assert session.observe(environment, agents["alice"])["payload"]["total"] == 6
    assert child["lineage"] == environment
    with pytest.raises(Forbidden):
        session.get(child["id"], agents["alice"])
    assert store.verify(environment, who)["events"] > 10
    with pytest.raises(Forbidden):
        list(rollouts(store, environment, who))


def test_simultaneous_cutoff_and_idempotency(setup):
    store, session, who, spec, environment, agents = setup
    a, first = decide(session, environment, agents["bob"])
    assert session.observe(environment, agents["alice"])["payload"]["total"] == 0
    assert session.submit(environment, agents["bob"], a) == first
    decide(session, environment, agents["alice"])
    lease = session.lease(environment, who, "test")
    session.resolve(environment, who, lease)
    assert session.submit(environment, agents["bob"], a)["status"] == "committed"
    assert session.observe(environment, agents["alice"])["payload"]["total"] == 2
    with pytest.raises(Conflict):
        session.submit(environment, agents["bob"], a.model_copy(update={"payload": {"value": -1}}))


def test_isolation_artifacts_and_authority(setup):
    store, session, who, spec, environment, agents = setup
    with pytest.raises(Forbidden):
        session.observe(environment, agents["alice"], "bob")
    with pytest.raises(Forbidden):
        session.get(environment, who.model_copy(update={"tenant": "other"}))
    artifact = store.artifact(environment, agents["alice"], b"private")
    with pytest.raises(Forbidden):
        store.read_artifact(environment, agents["bob"], artifact["id"])
    assert store.read_artifact(environment, agents["alice"], artifact["id"])[0] == b"private"
    lease = session.lease(environment, who, "test")
    replacement = session.transfer(environment, who, lease, "alice", "replacement")
    with pytest.raises(Forbidden):
        session.observe(environment, agents["alice"])
    assert session.observe(environment, replacement)["generation"] == 1
    private = json.dumps(list(store.replay(environment, agents["bob"])))
    assert "synthetic-secret-alice" not in private


def test_fencing_and_resume_preserve_state(setup):
    store, session, who, spec, environment, agents = setup
    old = session.lease(environment, who, "old")
    with pytest.raises(Conflict):
        session.lease(environment, who, "new")
    session.release(environment, who, old)
    new = session.lease(environment, who, "new")
    with pytest.raises(Conflict):
        session.checkpoint(environment, who, old)
    for principal in agents.values():
        decide(session, environment, principal)
    restored = EnvironmentSession(EvidenceStore(store.root), SyntheticEnvironment())
    restored.resume(environment, who, new)
    restored.resolve(environment, who, new)
    assert restored.observe(environment, agents["alice"])["payload"]["total"] == 2


def test_ambiguous_external_write_is_not_repeated(setup):
    store, session, who, spec, _, _ = setup
    policy = RunPolicy(
        max_cost_micros=10, allowed_endpoints=("https://example.invalid",), allowed_operations=("read",)
    )
    environment = session.create(spec.model_copy(update={"policy": policy}), who)["id"]
    agent = Principal(tenant=who.tenant, subject="alice", role="agent", environment=environment, participant="alice")
    ops = Operations(store)
    ops.prepare(
        environment,
        agent,
        "request",
        endpoint="https://example.invalid",
        operation="read",
        payload={},
        maximum_cost_micros=10,
    )
    lease = session.lease(environment, who, "test")

    class Provider:
        endpoint = "https://example.invalid"
        calls = 0

        def execute(self, operation, request, maximum):
            self.calls += 1
            self.receipt = {"operation_id": operation, "cost_micros": 3, "result": "synthetic"}
            raise TimeoutError()

        def lookup(self, operation):
            return self.receipt

    provider = Provider()
    with pytest.raises(TimeoutError):
        ops.dispatch(session, environment, who, lease, "request", provider)
    with pytest.raises(Conflict):
        ops.dispatch(session, environment, who, lease, "request", provider)
    with pytest.raises(Conflict):
        session.resume(environment, who, lease)
    receipt = ops.reconcile(environment, who, "request", provider)
    assert ops.settle(environment, who, "request", receipt) == receipt
    assert provider.calls == 1
    assert session.get(environment, who)["spent_micros"] == 3
    assert session.get(environment, who)["reserved_micros"] == 0


def test_api_and_viewer(setup):
    store, session, who, spec, environment, agents = setup
    token = store.issue(who)
    client = TestClient(create_app(session))
    headers = {"Authorization": "Bearer " + token}
    assert client.get("/").status_code == 200
    assert client.get("/viewer/app.js").status_code == 200
    assert client.get("/v1/environments").status_code == 401
    assert client.get("/v1/environments", headers=headers).json()[0]["id"] == environment
    response = client.get(f"/v1/environments/{environment}/events", headers=headers | {"Accept": "text/event-stream"})
    assert "event: evidence" in response.text
    agent_token = store.issue(agents["alice"])
    agent_headers = {"Authorization": "Bearer " + agent_token}
    assert (
        client.get(f"/v1/environments/{environment}/observation?participant=bob", headers=agent_headers).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/environments/{environment}/commands",
            headers=agent_headers,
            json={"operation": "lease", "arguments": {"owner": "bad"}},
        ).status_code
        == 403
    )


def test_training_and_report_revision(setup):
    store, session, who, spec, environment, agents = setup
    training = spec.model_copy(
        update={"scenario": "synthetic-training", "purpose": "training", "split": "training"}
    )
    environment = session.create(training, who)["id"]
    run(session, environment, who, {p: SyntheticAgent() for p in agents}, turns=2)
    report = ScoreReport(
        scorer="control",
        version="1",
        kind="deterministic",
        evidence_cursor=store.verify(environment, who)["events"],
        metrics={"synthetic": 1},
        uncertainty="Synthetic control only",
        provenance={"synthetic": True},
    )
    first = store.report(environment, who, report)
    second = store.report(environment, who, report.model_copy(update={"metrics": {"synthetic": 2}}))
    assert first["revision"] == 1 and second["revision"] == 2
    rows = list(rollouts(store, environment, who))
    assert len(rows) == 4 and rows[0]["policy_version"] == "1" and rows[0]["token_ids"] is None
    with pytest.raises(Unsupported):
        list(rollouts(store, environment, who, require_token_ids=True))


def test_separate_environment_process(tmp_path):
    import sys

    from environment_harness.adapters.process import ProcessEnvironment

    env = ProcessEnvironment([sys.executable, "-m", "environment_harness.worker"])
    try:
        session = EnvironmentSession(EvidenceStore(tmp_path), env)
        who = Principal(tenant="process", subject="researcher", role="researcher")
        spec = ExperimentSpec(
            environment=env.spec,
            participants=(AgentSpec(id="alice", implementation="synthetic@1", policy_version="1"),),
        )
        environment = session.create(spec, who)["id"]
        result = run(session, environment, who, {"alice": SyntheticAgent()}, turns=1)
        assert result["revision"] == 1
    finally:
        env.close()


def test_delayed_outcomes_require_a_report(tmp_path):
    from environment_harness.contracts import Transition
    from environment_harness.scoring import EventMeasurements

    class Delayed(SyntheticEnvironment):
        def resolve(self, state, actions, random, events):
            return Transition(state=state, terminated=True, pending_outcomes=True)

    env = Delayed()
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, env)
    who = Principal(tenant="delayed", subject="researcher", role="researcher")
    spec = ExperimentSpec(
        environment=env.spec,
        participants=(AgentSpec(id="alice", implementation="synthetic@1", policy_version="1"),),
        scoring_versions=("event-measurements@1",),
    )
    environment = session.create(spec, who)["id"]
    run(session, environment, who, {"alice": SyntheticAgent()}, turns=1)
    lease = session.lease(environment, who, "finalizer")
    with pytest.raises(Conflict):
        session.finalize_outcomes(environment, who, lease, 1)
    report = EventMeasurements().score(store.replay(environment, who))
    assert report.metrics["compliance"] is None
    saved = store.report(environment, who, report)
    assert session.finalize_outcomes(environment, who, lease, saved["revision"])["status"] == "completed"

import pytest

from environment_harness.access import _AccessContext
from environment_harness.agent_state import AgentJournal
from environment_harness.contracts import Action, AgentSpec, ExperimentSpec
from environment_harness.errors import Conflict, Forbidden
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.runtime import _SessionRuntime
from environment_harness.store import EvidenceStore


def test_coordinator_journal_and_inherited_artifact_isolation(tmp_path):
    env = SyntheticEnvironment()
    env.spec = env.spec.model_copy(update={"phase_deadline": "coordinator"})
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, env)
    who = _AccessContext(tenant="t", subject="r", policy="trusted-local")
    spec = ExperimentSpec(
        environment=env.spec,
        participants=tuple(
            AgentSpec(id=p, implementation="synthetic-agent@1", policy_version="1", checkpoint=True)
            for p in ("a", "b")
        ),
    )
    environment = session.create(spec, who)["id"]
    agent = _AccessContext(
        tenant="t", subject="a", policy="participant", participant="a", session=environment
    )
    journal = AgentJournal(session, environment, agent, 0)
    artifact = store.artifact(environment, agent, b"private context")
    journal.save({"revision": 0, "tool_response": "durable"}, {"artifact": artifact["id"]})
    assert journal.load()["tool_response"] == "durable"
    lease = session.lease(environment, who, "worker")
    for p in ("a", "b"):
        principal = agent.replace(participant=p, subject=p)
        obs = session.observe(environment, principal)
        session.submit(
            environment,
            principal,
            Action(operation_id=p, participant=p, observation_id=obs["id"], revision=0, payload={"value": 1}),
        )
    with pytest.raises(Conflict):
        session.resolve(environment, who, lease)
    session.close_phase(environment, who, lease, revision=0)
    session.resolve(environment, who, lease)
    with pytest.raises(Conflict):
        journal.save({"revision": 0}, {})
    checkpoint = session.checkpoint(environment, who, lease)
    child = session.branch(environment, who, checkpoint["id"])["id"]
    child_agent = agent.replace(session=child)
    assert store.read_artifact(child, child_agent, artifact["id"])[0] == b"private context"
    with pytest.raises(Forbidden):
        store.read_artifact(child, child_agent.replace(participant="b", subject="b"), artifact["id"])
    assert store.verify(child, who)["events"] > 0


def setup_environment(tmp_path, env=None):
    env = env or SyntheticEnvironment()
    session = _SessionRuntime(EvidenceStore(tmp_path), env)
    who = _AccessContext(tenant="t", subject="r", policy="trusted-local")
    spec = ExperimentSpec(
        environment=env.spec,
        participants=(
            AgentSpec(id="a", implementation="synthetic-agent@1", policy_version="1", checkpoint=True),
        ),
    )
    environment = session.create(spec, who)["id"]
    return session, who, environment


def test_transition_compute_releases_database_and_cancel_fences_commit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from environment_harness.fixtures import SyntheticAgent
    from environment_harness.runner import run

    entered, finish = Event(), Event()

    class SlowEnvironment(SyntheticEnvironment):
        def resolve(self, *args):
            entered.set()
            assert finish.wait(5)
            return super().resolve(*args)

    session, who, environment = setup_environment(tmp_path, SlowEnvironment())
    with ThreadPoolExecutor() as pool:
        future = pool.submit(run, session, environment, who, {"a": SyntheticAgent()}, turns=1, owner="writer")
        try:
            assert entered.wait(5)
            # This read and cancellation would block behind the old write transaction.
            assert session.get(environment, who)["revision"] == 0
            lease = session.lease(environment, who, "writer")
            session.control(environment, who, lease, "cancel")
        finally:
            finish.set()
        with pytest.raises(Conflict):
            future.result(timeout=5)
    assert session.get(environment, who)["revision"] == 0
    assert session.get(environment, who)["status"] == "cancelled"


def test_durable_response_reused_after_submission_crash(tmp_path, monkeypatch):
    from environment_harness.fixtures import SyntheticAgent
    from environment_harness.runner import run

    class CountingAgent(SyntheticAgent):
        calls = 0

        def act(self, observation):
            self.calls += 1
            return super().act(observation)

    env = SyntheticEnvironment()
    env.spec = env.spec.model_copy(update={"phase_deadline": "coordinator"})
    session, who, environment = setup_environment(tmp_path, env)
    agent = CountingAgent()
    original = session.submit
    monkeypatch.setattr(session, "submit", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        run(session, environment, who, {"a": agent}, turns=1)
    monkeypatch.setattr(session, "submit", original)
    run(session, environment, who, {"a": agent}, turns=1)
    assert agent.calls == 1
    assert session.get(environment, who)["revision"] == 1


def test_ambiguous_agent_dispatch_is_never_repeated(tmp_path):
    from environment_harness.fixtures import SyntheticAgent
    from environment_harness.runner import run

    class AmbiguousAgent(SyntheticAgent):
        calls = 0

        def act(self, observation):
            self.calls += 1
            raise TimeoutError("provider response lost")

    session, who, environment = setup_environment(tmp_path)
    agent = AmbiguousAgent()
    with pytest.raises(TimeoutError):
        run(session, environment, who, {"a": agent}, turns=1)
    with pytest.raises(Conflict, match="reconciliation"):
        run(session, environment, who, {"a": agent}, turns=1)
    assert agent.calls == 1

    lease = session.lease(environment, who, "reconcile")
    with pytest.raises(Conflict, match="reconciled"):
        session.checkpoint(environment, who, lease)
    with session.store.transaction() as db:
        work = db.execute("SELECT id FROM agent_work WHERE environment=?", (environment,)).fetchone()
    session.reconcile_agent(
        environment,
        who,
        lease,
        operation_id=work["id"],
        response={"value": 1},
        agent_state={},
        evidence={"lookup_receipt": "synthetic-receipt"},
    )
    session.release(environment, who, lease)
    run(session, environment, who, {"a": agent}, turns=1)
    assert agent.calls == 1
    assert session.get(environment, who)["revision"] == 1

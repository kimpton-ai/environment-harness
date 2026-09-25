"""Restart-safe local scheduling: the database is the durable queue."""

from __future__ import annotations

import threading

import pytest
from pydantic import BaseModel

from environment_harness import EnvironmentHarness, EvidenceStore, Scenario
from environment_harness.contracts import Capabilities, EnvironmentSpec, Transition
from environment_harness.errors import Conflict
from environment_harness.harness import EnvironmentReference, _EnvironmentRegistry


class Input(BaseModel):
    model_config = {"extra": "forbid"}

    difficulty: int = 1


class Counter:
    """A minimal environment whose identity is explicit and stable."""

    scenario_type = Input
    identity = "counter"
    version = "1"
    configuration: dict = {}

    def __init__(self):
        self.operations = {}
        self.spec = EnvironmentSpec(
            id=self.identity,
            version=self.version,
            implementation=f"{self.identity}@{self.version}",
            scheduling="simultaneous",
            capabilities=Capabilities(checkpoint=True, resume=True, branch=True),
            missing_action="noop",
            phase_seconds=3600,
            scenario_schema=Input.model_json_schema(),
            action_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            **self.configuration,
        )

    def initialize(self, experiment):
        return {"total": 0}

    def observe(self, state, participant):
        return dict(state)

    def resolve(self, state, actions, random, events):
        return Transition(state={"total": state["total"] + 1})

    def intervene(self, state, changes):
        return dict(state, **changes)


class Other(Counter):
    identity = "other"


class Renamed(Counter):
    identity = "counter"
    version = "2"


class Reconfigured(Counter):
    identity = "counter"
    version = "1"
    configuration = {"modalities": ("text",)}


class Agent:
    implementation = "counter-agent@1"
    supports_checkpoint = False

    def act(self, observation):
        return {"value": 1}


def harness(store, environment=(Counter,), **options):
    return EnvironmentHarness(
        store,
        environment=environment,
        agents={"agent": Agent},
        **options,
    )


def test_registry_requires_supplied_typed_factories(tmp_path):
    with pytest.raises(ValueError, match="at least one environment is required"):
        _EnvironmentRegistry([])
    with pytest.raises(TypeError, match="not an instance"):
        _EnvironmentRegistry(["environment_harness.fixtures:SyntheticEnvironment"])
    with pytest.raises(ValueError, match="duplicate environment factory"):
        _EnvironmentRegistry([Counter, Counter])

    registry = _EnvironmentRegistry([Counter, Other])
    assert registry.reference(Counter).id == "counter"
    assert registry.reference(Other).id == "other"
    with pytest.raises(ValueError, match="was not supplied"):
        registry.reference(Renamed)
    with pytest.raises(ValueError, match="explicitly"):
        _ = registry.default
    assert registry.resolve(registry.reference(Counter)) is Counter
    assert registry.resolve(EnvironmentReference(id="counter", version="9", spec_digest="0" * 64)) is None


def test_experiments_select_one_supplied_implementation(tmp_path):
    local = harness(tmp_path, environment=(Counter, Other))
    with pytest.raises(ValueError, match="explicitly"):
        local.experiment("ambiguous", [Scenario(id="a", input={})], turns=1)
    with pytest.raises(ValueError, match="was not supplied"):
        local.experiment("absent", [Scenario(id="a", input={})], turns=1, environment=Renamed)

    first = local.experiment("counter", [Scenario(id="a", input={})], turns=1, environment=Counter).run()
    second = local.experiment("other", [Scenario(id="a", input={})], turns=1, environment=Other).run()
    assert first.status == second.status == "succeeded"

    with local.store.transaction() as db:
        references = {
            row["environment_id"] for row in db.execute("SELECT environment_id FROM session_runs").fetchall()
        }
    assert references == {"counter", "other"}


def test_one_spec_supports_many_experiments_and_only_references_are_serialized(tmp_path):
    local = harness(tmp_path)
    first = local.experiment("first", [Scenario(id="a", input={})], turns=1).run()
    second = local.experiment("second", [Scenario(id="a", input={})], turns=1).run()
    assert first.id != second.id
    with local.store.transaction() as db:
        rows = db.execute(
            "SELECT environment_id,environment_version,spec_digest FROM session_runs"
        ).fetchall()
    assert len({row["spec_digest"] for row in rows}) == 1
    # Only the portable reference is persisted: no import path or class object.
    for row in rows:
        assert row["environment_id"] == "counter" and row["environment_version"] == "1"
        assert len(row["spec_digest"]) == 64


def test_experiment_and_sessions_commit_before_any_submission(tmp_path):
    """A crash after the Experiment transaction leaves durable queued work."""

    store = EvidenceStore(tmp_path)
    local = harness(store, max_concurrency=1)
    submissions = []

    def refuse(job):
        submissions.append(job["id"])
        raise RuntimeError("executor rejected delivery")

    local._executor.submit = lambda _target, job: refuse(job)  # type: ignore[assignment]
    experiment = local.experiment(
        "durable", [Scenario(id="a", input={}), Scenario(id="b", input={})], trials=2, turns=1
    )
    experiment.start()
    assert submissions

    with store.transaction() as db:
        statuses = [
            row["status"]
            for row in db.execute(
                "SELECT status FROM session_runs WHERE experiment=?", (experiment.id,)
            ).fetchall()
        ]
        activity = [row["kind"] for row in db.execute("SELECT kind FROM event_outbox ORDER BY id").fetchall()]
    # Every session stays durably queued and the failure is recorded.
    assert statuses == ["queued"] * 4
    assert "scheduler.submission_failed" in activity

    # A fresh process reconstructs the queue and runs each session exactly once.
    recovered = harness(store, max_concurrency=2)
    result = recovered.experiment("durable", [Scenario(id="a", input={})], turns=1)
    result.id = experiment.id
    result._started = True
    for session in result.sessions:
        session.wait(15)
    assert {session.status for session in result.sessions} == {"succeeded"}
    with store.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM session_runs WHERE experiment=?", (experiment.id,)).fetchone()[0]
            == 4
        )


def test_startup_reconciliation_interrupts_orphans_and_preserves_terminal_rows(tmp_path):
    store = EvidenceStore(tmp_path)
    first = harness(store)
    finished = first.run(Scenario(id="done", input={}), turns=1)
    assert finished.status == "succeeded"

    orphan = first.start(Scenario(id="orphan", input={}), turns=1)
    orphan.wait(15)
    with store.transaction() as db:
        db.execute("UPDATE session_runs SET status='running' WHERE environment=?", (orphan.id,))
        terminal_before = dict(
            db.execute("SELECT * FROM session_runs WHERE environment=?", (finished.id,)).fetchone()
        )

    second = harness(store)
    with store.transaction() as db:
        terminal_after = dict(
            db.execute("SELECT * FROM session_runs WHERE environment=?", (finished.id,)).fetchone()
        )
    assert terminal_after == terminal_before
    assert second.session(orphan.id).status == "interrupted"
    with store.transaction() as db:
        reason = db.execute("SELECT error FROM session_runs WHERE environment=?", (orphan.id,)).fetchone()[
            "error"
        ]
        corrections = [
            row["kind"]
            for row in db.execute(
                "SELECT kind FROM event_outbox WHERE environment=? ORDER BY id", (orphan.id,)
            ).fetchall()
        ]
    assert reason == "process_loss"
    assert corrections[-1] == "environment_session.interrupted"

    # Interrupted work stays behind explicit resume and is never replayed.
    with pytest.raises(Conflict, match="explicit resume"):
        second.session(orphan.id).wait()
    second.session(orphan.id).resume().wait(15)
    assert second.session(orphan.id).status == "succeeded"

    # Repeated reconciliation is idempotent.
    before = second.reconcile()
    after = second.reconcile()
    assert before["interrupted"] == after["interrupted"] == []


def test_reconciliation_requeues_persisted_work_exactly_once(tmp_path):
    store = EvidenceStore(tmp_path)
    first = harness(store, reconcile=False)
    experiment = first.experiment("queued", [Scenario(id="a", input={})], trials=3, turns=1)
    # Commit the durable queue without letting this process deliver it.
    first._executor.shutdown(wait=False)
    with pytest.raises(RuntimeError):
        first._executor.submit(lambda: None)
    experiment.start()
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM session_runs WHERE status='queued'").fetchone()[0] == 3

    second = harness(store, max_concurrency=2, reconcile=False)
    identities = second.reconcile()["requeued"]
    assert len(identities) == 3
    # A second reconciliation reports nothing further and requeues nothing.
    assert second.reconcile()["requeued"] == []
    for identity in identities:
        second.session(identity).wait(15)
    assert {second.session(identity).status for identity in identities} == {"succeeded"}
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM session_runs").fetchone()[0] == 3
        assert db.execute("SELECT count(DISTINCT id) FROM environments").fetchone()[0] == 3


def test_two_processes_reconciling_concurrently_converge(tmp_path):
    store = EvidenceStore(tmp_path)
    seed = harness(store, reconcile=False)
    session = seed.start(Scenario(id="contended", input={}), turns=1)
    session.wait(15)
    with store.transaction() as db:
        db.execute("UPDATE session_runs SET status='running' WHERE environment=?", (session.id,))

    results = []
    barrier = threading.Barrier(2)

    def reconcile():
        barrier.wait()
        results.append(harness(store, reconcile=False).reconcile())

    threads = [threading.Thread(target=reconcile) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    # Exactly one process corrects the row; both converge on the same state.
    assert sum(len(result["interrupted"]) for result in results) == 1
    assert seed.session(session.id).status == "interrupted"
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM session_runs").fetchone()[0] == 1


def test_missing_or_mismatched_factories_block_before_any_code_runs(tmp_path):
    store = EvidenceStore(tmp_path)
    first = harness(store, reconcile=False)
    experiment = first.experiment("blocked", [Scenario(id="a", input={})], turns=1)
    first._executor.shutdown(wait=False)
    experiment.start()

    # A new default that differs from the frozen reference never substitutes.
    absent = harness(store, environment=(Other,))
    identity = absent.sessions()[0].id
    absent.session(identity).wait(15)
    assert absent.session(identity).status == "blocked"
    with store.transaction() as db:
        row = db.execute("SELECT * FROM session_runs WHERE environment=?", (identity,)).fetchone()
        assert row["error"] == "environment_not_configured"
        assert "counter@1" in row["blocked_reason"]
        # Nothing executed: no environment row and no evidence.
        assert db.execute("SELECT count(*) FROM environments").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0

    # Changed versions and changed schemas are both mismatches.
    for substitute in (Renamed, Reconfigured):
        mismatch = harness(store, environment=(substitute,))
        mismatch.session(identity).resume().wait(15)
        assert mismatch.session(identity).status == "blocked"

    # Supplying the correct factory makes the same frozen Session schedulable.
    repaired = harness(store)
    repaired.session(identity).resume().wait(15)
    assert repaired.session(identity).status == "succeeded"
    with store.transaction() as db:
        row = db.execute("SELECT * FROM session_runs WHERE environment=?", (identity,)).fetchone()
        assert row["blocked_reason"] is None
        # The frozen Experiment and Session references were never rewritten.
        assert row["environment_id"] == "counter" and row["environment_version"] == "1"


def test_a_factory_that_lies_about_its_identity_is_rejected(tmp_path):
    store = EvidenceStore(tmp_path)
    calls = {"count": 0}

    class Drifting(Counter):
        def __init__(self):
            super().__init__()
            calls["count"] += 1
            if calls["count"] > 1:
                self.spec = self.spec.model_copy(update={"modalities": ("json",)})

    local = harness(store, environment=(Drifting,))
    session = local.start(Scenario(id="drift", input={}), turns=1)
    session.wait(15)
    assert session.status == "blocked"
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM environments").fetchone()[0] == 0


def test_public_python_calls_never_select_code_by_import_path(tmp_path):
    with pytest.raises(TypeError, match="not an instance"):
        EnvironmentHarness(
            tmp_path,
            environment=("environment_harness.fixtures:SyntheticEnvironment",),
            agents={"agent": Agent},
        )
    local = harness(tmp_path)
    with pytest.raises(ValueError, match="was not supplied"):
        local.start(Scenario(id="a", input={}), environment="counter")  # type: ignore[arg-type]


def test_repeated_reconciliation_skips_work_it_already_tracks(tmp_path):
    """The second call must not requeue a job this process is already running.

    The first reconciliation schedules the durable rows; until they finish, the
    rows are still `queued` in the database while their jobs are tracked in
    memory. A second call has to recognise that and do nothing.
    """

    import threading

    release = threading.Event()

    class Blocking(Agent):
        def act(self, observation):
            assert release.wait(15)
            return super().act(observation)

    store = EvidenceStore(tmp_path)
    seed = harness(store, reconcile=False)
    seed._executor.shutdown(wait=False)
    seed.experiment("blocked", [Scenario(id="a", input={})], trials=2, turns=1).start()
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM session_runs WHERE status='queued'").fetchone()[0] == 2

    second = EnvironmentHarness(
        store, environment=(Counter,), agents={"agent": Blocking}, max_concurrency=2, reconcile=False
    )
    requeued = second.reconcile()["requeued"]
    assert len(requeued) == 2

    # The rows are still queued but their jobs are tracked, so nothing is added.
    assert second.reconcile()["requeued"] == []
    release.set()
    for identity in requeued:
        second.session(identity).wait(15)
    assert {second.session(identity).status for identity in requeued} == {"succeeded"}
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM session_runs").fetchone()[0] == 2

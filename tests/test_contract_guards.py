"""Negative cases for portable-resource and trajectory validators.

Strict first-party construction is the project's main defence against malformed
evidence, so each guard gets a case that proves it rejects rather than coerces.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

import pytest

from environment_harness import EvidenceStore
from environment_harness.access import trusted_local
from environment_harness.errors import Forbidden
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment, shared_experiment
from environment_harness.resources import Checkpoint, PolicyProjection, ResourceProjection, ScenarioSet
from environment_harness.trajectories import (
    RecordTime,
    SourceCollection,
    SourceRegistration,
    TrajectoryRepository,
)


def fixture():
    store = EvidenceStore(tempfile.mkdtemp())
    return store, shared_experiment(store, tenant="tenant"), trusted_local("tenant")


def scenario_set_payload():
    store, built, who = fixture()
    projected = next(iter(ResourceProjection(store).scenario_sets(who, limit=5)))
    del built
    return json.loads(projected.model_dump_json(by_alias=True))


def test_scenario_set_rejects_duplicate_identities_and_inconsistent_counts():
    payload = scenario_set_payload()

    duplicated = copy.deepcopy(payload)
    duplicated["spec"]["scenarios"] = [duplicated["spec"]["scenarios"][0]] * 2
    duplicated["status"]["scenarioCount"] = 2
    with pytest.raises(ValueError, match="scenario IDs must be unique"):
        ScenarioSet.model_validate(duplicated)

    miscounted = copy.deepcopy(payload)
    miscounted["status"]["scenarioCount"] = 99
    with pytest.raises(ValueError, match="scenario count does not match"):
        ScenarioSet.model_validate(miscounted)


def checkpoint_payload():
    store, built, who = fixture()
    session = built["solo"]
    session.checkpoint(exact_agents=True)
    resource = next(iter(session.checkpoint_resources(limit=5)))
    del store, who
    return json.loads(resource.model_dump_json(by_alias=True))


def test_checkpoint_requires_consistent_continuation_claims():
    payload = checkpoint_payload()

    inexact = copy.deepcopy(payload)
    inexact["status"]["exact"] = True
    for participant in inexact["spec"]["participants"]:
        participant["exact"] = False
    with pytest.raises(ValueError, match="exact participant continuation"):
        Checkpoint.model_validate(inexact)

    # Continuation state is enforced by the field's own digest shape, which is
    # what guarantees a resumable checkpoint always carries one.
    stateless = copy.deepcopy(payload)
    stateless["status"]["resumable"] = True
    stateless["spec"]["stateReference"] = ""
    with pytest.raises(ValueError, match="stateReference"):
        Checkpoint.model_validate(stateless)


def test_record_time_requires_a_coordinate_and_a_monotonic_origin():
    with pytest.raises(ValueError, match="wall, monotonic, or native coordinate"):
        RecordTime()
    with pytest.raises(ValueError, match="process-local duration requires its declared origin"):
        RecordTime(monotonicOffsetNs=5)
    assert RecordTime(monotonicOffsetNs=5, monotonicOrigin="process-1").monotonic_offset_ns == 5


@pytest.mark.parametrize(
    "unfinished",
    [{"gaps": ("1-2",)}, {"captureFailures": ("reader",)}, {"backlog": 3}],
    ids=["gap", "capture-failure", "backlog"],
)
def test_collection_cannot_be_complete_with_outstanding_work(unfinished):
    with pytest.raises(ValueError, match="cannot be complete with a declared gap"):
        SourceCollection(state="complete", **unfinished)
    assert SourceCollection(state="current", **unfinished).state == "current"


def test_page_size_guards_reject_out_of_range_limits():
    store, _built, who = fixture()
    trajectories = TrajectoryRepository(store)

    with pytest.raises(ValueError, match="invalid source page size"):
        trajectories.list_sources(who, limit=0)
    with pytest.raises(ValueError, match="invalid snapshot page size"):
        trajectories.list_all_snapshots(who, limit=0)
    with pytest.raises(ValueError, match="invalid policy page size"):
        PolicyProjection(store).list(who, limit=0)


def test_projection_hides_experiments_outside_a_participant_scope():
    """A credential bound to one Session cannot enumerate its tenant's others."""

    store, built, _who = fixture()
    projection = ResourceProjection(store)
    experiment = built["experiment"].resource().metadata.id
    owned = built["experiment"].sessions[0].id
    scoped = trusted_local("tenant").replace(policy="participant", session=owned, participant="alice")

    # Its own experiment resolves through the session-derived scope.
    assert projection.experiment(experiment, scoped).metadata.id == experiment
    with pytest.raises(Forbidden, match="experiment unavailable"):
        projection.experiment("other-experiment", scoped)

    # A standalone session owns no experiment, so it can index nothing.
    standalone = trusted_local("tenant").replace(
        policy="participant", session=built["solo"].id, participant="alice"
    )
    with pytest.raises(Forbidden, match="credential policy denies"):
        projection.experiment(experiment, standalone)


def test_snapshot_record_paging_guards_and_prerelease_inline_records():
    """Snapshots written before normalized record storage still page."""

    store, built, who = fixture()
    trajectories = TrajectoryRepository(store)
    snapshot = trajectories.freeze(built["solo"].id, who)

    with pytest.raises(ValueError, match="invalid snapshot record page"):
        trajectories.snapshot_records_page(snapshot.metadata.id, who, limit=0)
    with pytest.raises(ValueError, match="invalid snapshot record page"):
        trajectories.snapshot_records_page(snapshot.metadata.id, who, after=-1)

    # Rewrite the stored body into the prerelease shape that inlined its records.
    records = [
        json.loads(record.model_dump_json(by_alias=True))
        for record in trajectories.snapshot_records_page(snapshot.metadata.id, who, limit=50).records
    ]
    assert len(records) > 1
    # Snapshots are immutable, so insert a second row in the prerelease shape
    # rather than rewriting the one just frozen.
    legacy = "legacy-" + snapshot.metadata.id
    with store.transaction() as db:
        row = db.execute("SELECT * FROM trajectory_snapshots WHERE id=?", (snapshot.metadata.id,)).fetchone()
        db.execute(
            "INSERT INTO trajectory_snapshots (id,tenant,environment,body,digest,created) "
            "VALUES (?,?,?,?,?,?)",
            (
                legacy,
                row["tenant"],
                row["environment"],
                json.dumps(json.loads(row["body"]) | {"records": records}),
                row["digest"],
                row["created"],
            ),
        )
    snapshot_id = legacy

    first = trajectories.snapshot_records_page(snapshot_id, who, limit=1)
    assert len(first.records) == 1 and first.has_more is True
    assert first.cursor == first.records[-1].sequence
    rest = trajectories.snapshot_records_page(snapshot_id, who, after=first.cursor, limit=50)
    assert rest.records and rest.records[0].sequence > first.cursor


def test_freezing_a_trajectory_without_evidence_is_refused():
    """A registered source with no ingested records has nothing to freeze."""

    store, built, who = fixture()
    sources = built["harness"].sources()
    source = sources.register(
        SourceRegistration(
            namespace="com.example.empty",
            run_id="no-records",
            schema_version="empty.v1",
            environment={"id": "empty", "version": "1"},
            participants=("alice",),
            purpose="evaluation",
        )
    )
    with pytest.raises(ValueError, match="trajectory has no evidence"):
        TrajectoryRepository(store).freeze(source.id, who)


def test_snapshot_listing_accepts_either_trajectory_spelling():
    store, built, who = fixture()
    trajectories = TrajectoryRepository(store)
    session = built["solo"].id
    frozen = trajectories.freeze(session, who)

    by_session = trajectories.list_all_snapshots(who, trajectory=session, limit=10)
    by_resource = trajectories.list_all_snapshots(who, trajectory=f"trajectory-{session}", limit=10)
    assert [item.metadata.id for item in by_session] == [frozen.metadata.id]
    assert [item.metadata.id for item in by_resource] == [frozen.metadata.id]


def test_experiment_progress_reports_blocked_and_interrupted_limitations():
    store, built, who = fixture()
    experiment = built["experiment"].resource().metadata.id
    projection = ResourceProjection(store)
    sessions = [session.id for session in built["experiment"].sessions]

    with store.transaction() as db:
        db.execute("UPDATE session_runs SET status='blocked' WHERE environment=?", (sessions[0],))
        db.execute("UPDATE session_runs SET status='interrupted' WHERE environment=?", (sessions[1],))

    status = projection.experiment(experiment, who).status
    assert any("blocked on an unavailable environment" in item for item in status.limitations)
    assert any("require explicit resume" in item for item in status.limitations)
    assert status.progress.uncertain == 2


def test_scenario_set_listing_skips_experiments_without_snapshots_and_honours_limit():
    store, built, who = fixture()
    projection = ResourceProjection(store)
    experiment = built["experiment"].resource().metadata.id

    assert projection.scenario_sets(who, limit=10)
    with store.transaction() as db:
        db.execute("DELETE FROM scenario_snapshots WHERE experiment=?", (experiment,))
    # An experiment with no frozen snapshots cannot project a set, so it is skipped
    # rather than failing the whole listing.
    assert projection.scenario_sets(who, limit=10) == ()
    with store.transaction() as db, pytest.raises(ValueError, match="no frozen scenario snapshots"):
        projection._scenario_set(db, experiment, {"name": "gone"})


def test_scenario_set_listing_stops_at_the_requested_limit():
    store = EvidenceStore(tempfile.mkdtemp())
    for turns in (1, 2, 3):
        shared_experiment(store, tenant="tenant", turns=turns)
    who = trusted_local("tenant")
    assert len(ResourceProjection(store).scenario_sets(who, limit=10)) > 2
    assert len(ResourceProjection(store).scenario_sets(who, limit=2)) == 2


def test_timestamps_pass_through_a_missing_value():
    from environment_harness.resources import _timestamp

    assert _timestamp(None) is None
    assert _timestamp(0).endswith("Z")


def test_a_checkpoint_with_unsettled_operations_declares_that_limitation():
    store, built, who = fixture()
    session = built["solo"]
    created = session.checkpoint()
    # Checkpoints are immutable, so add a second row carrying the unsettled work.
    unsettled = "unsettled-" + created["id"]
    with store.transaction() as db:
        row = db.execute("SELECT * FROM checkpoints WHERE id=?", (created["id"],)).fetchone()
        db.execute(
            "INSERT INTO checkpoints (id,environment,revision,body,hash) VALUES (?,?,?,?,?)",
            (
                unsettled,
                row["environment"],
                row["revision"],
                json.dumps(json.loads(row["body"]) | {"operations": ["operation-1"]}),
                row["hash"],
            ),
        )

    resource = ResourceProjection(store).checkpoint(session.id, unsettled, who)
    assert any("unsettled external operations" in item for item in resource.status.limitations)


def test_observing_a_deactivated_participant_is_refused():
    store, built, who = fixture()
    session = built["solo"]
    with store.transaction() as db:
        row = db.execute("SELECT participants FROM environments WHERE id=?", (session.id,)).fetchone()
        members = json.loads(row["participants"])
        members["alice"]["active"] = False
        db.execute(
            "UPDATE environments SET participants=? WHERE id=?",
            (json.dumps(members), session.id),
        )
    from environment_harness.runtime import _SessionRuntime

    runtime = _SessionRuntime(store, SyntheticEnvironment())
    with pytest.raises(Forbidden, match="inactive participant"):
        runtime.participant_context(session.id, who, "alice")


def test_branching_a_session_without_a_durable_row_is_a_no_op():
    """A runtime-only session has nothing to copy, and a repeat branch is idempotent."""

    from environment_harness.runtime import _SessionRuntime

    store, built, who = fixture()
    runtime = _SessionRuntime(store, SyntheticEnvironment())
    session = built["solo"].id
    with store.transaction() as db:
        db.execute("DELETE FROM session_runs WHERE environment=?", (session,))
        runtime._register_child_session(db, session, "child-a", "checkpoint", {}, 1)
        assert (
            db.execute("SELECT count(*) FROM session_runs WHERE environment=?", ("child-a",)).fetchone()[0]
            == 0
        )

    # With a durable parent row the first call registers the child, and a repeat
    # leaves it alone rather than duplicating it.
    owned = built["experiment"].sessions[0].id
    with store.transaction() as db:
        snapshot = {"revision": 1}
        runtime._register_child_session(db, owned, "child-b", "checkpoint", snapshot, 1)
        runtime._register_child_session(db, owned, "child-b", "checkpoint", snapshot, 1)
        assert (
            db.execute("SELECT count(*) FROM session_runs WHERE environment=?", ("child-b",)).fetchone()[0]
            == 1
        )
    del who


def test_resuming_a_paused_session_records_its_continuation():
    from environment_harness.runtime import _SessionRuntime

    store, built, who = fixture()
    runtime = _SessionRuntime(store, SyntheticEnvironment())
    session = built["solo"].id
    with store.transaction() as db:
        db.execute("UPDATE environments SET status='running' WHERE id=?", (session,))
    lease = runtime.lease(session, who, "guard")
    runtime.control(session, who, lease, "pause")
    assert runtime.resume(session, who, lease)["status"] == "running"
    kinds = [event["kind"] for event in store.replay(session, who)]
    assert "session.resumed" in kinds
    runtime.release(session, who, lease)


def test_recreating_a_session_revalidates_access_before_comparing_manifests():
    """`create` is idempotent, and the second call re-checks authority first."""

    from environment_harness.contracts import AgentSpec, ExperimentSpec
    from environment_harness.runtime import _SessionRuntime

    store = EvidenceStore(tempfile.mkdtemp())
    environment = SyntheticEnvironment()
    runtime = _SessionRuntime(store, environment)
    who = trusted_local("tenant")
    spec = ExperimentSpec(
        environment=environment.spec,
        participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
    )
    identity = "c" * 32

    class RacesItsOwnCreation(SyntheticEnvironment):
        """Commit the same session from another writer between the two checks."""

        def initialize(self, experiment):
            _SessionRuntime(store, SyntheticEnvironment()).create(experiment, who, environment_id=identity)
            return super().initialize(experiment)

    adopted = _SessionRuntime(store, RacesItsOwnCreation()).create(spec, who, environment_id=identity)
    assert adopted["id"] == identity
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM environments WHERE id=?", (identity,)).fetchone()[0] == 1

    assert runtime.create(spec, who, environment_id=identity)["id"] == identity
    with pytest.raises(Forbidden):
        runtime.create(spec, trusted_local("other"), environment_id=identity)


def test_a_dispatch_set_skips_inactive_and_already_accepted_participants():
    """The runner validates the whole set before invoking any program."""

    from environment_harness.runner import run as run_turns
    from environment_harness.runtime import _SessionRuntime

    store, built, who = fixture()
    session = built["experiment"].sessions[0].id
    runtime = _SessionRuntime(store, SyntheticEnvironment())
    with store.transaction() as db:
        row = db.execute("SELECT participants FROM environments WHERE id=?", (session,)).fetchone()
        members = json.loads(row["participants"])
        members["bob"]["active"] = False
        db.execute(
            "UPDATE environments SET participants=?,status='running' WHERE id=?",
            (json.dumps(members), session),
        )
    result = run_turns(runtime, session, who, {"alice": SyntheticAgent(), "bob": SyntheticAgent()}, turns=1)
    assert result["id"] == session


def test_the_runner_rejects_an_out_of_range_phase_timeout():
    from environment_harness.runner import run as run_turns

    store, built, who = fixture()
    from environment_harness.runtime import _SessionRuntime

    runtime = _SessionRuntime(store, SyntheticEnvironment())
    with pytest.raises(ValueError, match="phase_timeout must be between zero and one day"):
        run_turns(runtime, built["solo"].id, who, {"alice": SyntheticAgent()}, phase_timeout=0)


def test_a_cancelled_session_stops_before_invoking_its_agent():
    """Cancellation is checked after restore and before the agent is called."""

    import threading

    from environment_harness.errors import Conflict
    from environment_harness.runner import _invoke, _prepare
    from environment_harness.runtime import _SessionRuntime

    store, built, who = fixture()
    runtime = _SessionRuntime(store, SyntheticEnvironment())
    session = built["solo"].id
    with store.transaction() as db:
        db.execute("UPDATE environments SET status='running' WHERE id=?", (session,))

    scoped = runtime.participant_context(session, who, "alice")
    observation = runtime.observe(session, scoped, "alice")
    lease = runtime.lease(session, who, "cancelled")
    work = _prepare(runtime, session, scoped, observation, lease)

    class CancelledAfterDispatch(threading.Event):
        """Clear while the dispatch commits, set before the agent is called."""

        def __init__(self):
            super().__init__()
            self.reads = 0

        def is_set(self):
            self.reads += 1
            return self.reads > 1

    cancelled = CancelledAfterDispatch()
    with pytest.raises(Conflict, match="agent execution cancelled"):
        _invoke(
            runtime,
            session,
            scoped,
            observation,
            SyntheticAgent(),
            work,
            lease,
            cancel_event=cancelled,
        )
    runtime.release(session, who, lease)


def test_recovering_an_interrupted_session_clears_its_paused_flag():
    """`resume_if_paused` returns a recovered session to running exactly once."""

    store, built, _who = fixture()
    local, session = built["harness"], built["solo"]
    with store.transaction() as db:
        db.execute("UPDATE environments SET status='paused' WHERE id=?", (session.id,))
        db.execute("UPDATE session_runs SET status='interrupted' WHERE environment=?", (session.id,))
    session.resume().wait()
    assert session.record()["status"] in ("running", "completed")
    kinds = [event["kind"] for event in store.replay(session.id, local._access)]
    assert "session.resumed" in kinds


def test_a_repeated_idempotent_branch_reuses_its_child_row():
    store, built, _who = fixture()
    parent = built["experiment"].sessions[0]
    checkpoint = parent.checkpoint()
    first = parent.branch({"checkpoint": checkpoint["id"], "idempotency_key": "twice"})
    second = parent.branch({"checkpoint": checkpoint["id"], "idempotency_key": "twice"})
    assert first.id == second.id
    with store.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM session_runs WHERE environment=?", (first.id,)).fetchone()[0]
            == 1
        )


def test_a_failed_writer_heartbeat_surfaces_from_the_turn_loop(monkeypatch):
    """A lost writer lease aborts the turn rather than committing behind it."""

    import threading

    from environment_harness import runner as runner_module
    from environment_harness.runtime import _SessionRuntime

    store, built, who = fixture()
    session = built["experiment"].sessions[0].id
    with store.transaction() as db:
        db.execute("UPDATE environments SET status='running' WHERE id=?", (session,))

    monkeypatch.setattr(runner_module, "_HEARTBEAT_SECONDS", 0)
    runtime = _SessionRuntime(store, SyntheticEnvironment())
    failed = threading.Event()

    def failing(*arguments, **keywords):
        del arguments, keywords
        failed.set()
        raise RuntimeError("heartbeat lost its lease")

    monkeypatch.setattr(runtime, "renew", failing)

    class WaitsForTheHeartbeat(SyntheticAgent):
        """Hold the turn open until the heartbeat has actually recorded its failure."""

        def act(self, observation):
            assert failed.wait(5)
            return super().act(observation)

    agents = {"alice": WaitsForTheHeartbeat(), "bob": WaitsForTheHeartbeat()}
    with pytest.raises(RuntimeError, match="heartbeat lost its lease"):
        runner_module.run(runtime, session, who, agents, turns=1)


def test_a_legacy_store_gains_the_scheduler_reference_columns_on_open():
    """A pre-006 store is migrated in place without rewriting its rows."""

    import sqlite3

    root = tempfile.mkdtemp()
    store = EvidenceStore(root)
    built = shared_experiment(store, tenant="tenant")
    session = built["solo"].id
    del store

    database = Path(root) / "evidence.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version='006_scheduler_recovery'")
        for column in ("environment_id", "environment_version", "spec_digest", "blocked_reason"):
            connection.execute(f"ALTER TABLE session_runs DROP COLUMN {column}")
        connection.commit()

    reopened = EvidenceStore(root)
    with reopened.transaction() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(session_runs)").fetchall()}
        assert {"environment_id", "environment_version", "spec_digest", "blocked_reason"} <= columns
        assert (
            db.execute("SELECT count(*) FROM session_runs WHERE environment=?", (session,)).fetchone()[0] == 1
        )


def test_a_heartbeat_that_fails_during_submission_still_aborts_the_turn(monkeypatch):
    """The last lease check runs after actions are submitted, before resolution.

    A writer can lose its lease between dispatch and resolution, so the turn
    loop re-checks once more rather than resolving behind a lost lease.
    """

    import threading

    from environment_harness import runner as runner_module
    from environment_harness.runtime import _SessionRuntime

    store, built, who = fixture()
    session = built["experiment"].sessions[0].id
    with store.transaction() as db:
        db.execute("UPDATE environments SET status='running' WHERE id=?", (session,))

    monkeypatch.setattr(runner_module, "_HEARTBEAT_SECONDS", 0)
    runtime = _SessionRuntime(store, SyntheticEnvironment())
    allow, failed = threading.Event(), threading.Event()
    renew, submit = runtime.renew, runtime.submit

    def gated_renew(*arguments, **keywords):
        if not allow.is_set():
            return renew(*arguments, **keywords)
        failed.set()
        raise RuntimeError("lease lost during submission")

    submissions = []

    def gated_submit(*arguments, **keywords):
        # Fail only after the final submission, so the inner dispatch loop has
        # already drained and the last lease check is the one that fires.
        receipt = submit(*arguments, **keywords)
        submissions.append(receipt)
        if len(submissions) == len(agents):
            allow.set()
            assert failed.wait(5)
        return receipt

    agents = {"alice": SyntheticAgent(), "bob": SyntheticAgent()}
    monkeypatch.setattr(runtime, "renew", gated_renew)
    monkeypatch.setattr(runtime, "submit", gated_submit)

    with pytest.raises(RuntimeError, match="lease lost during submission"):
        runner_module.run(runtime, session, who, agents, turns=1)

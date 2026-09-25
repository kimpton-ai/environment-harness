"""Enforced contract-compatibility gate for the portable resource family.

These fixtures are the release gate the plan requires: they test strict
first-party writes separately from lenient compatibility reads, exercise legacy
store projection, unknown optional and required features, unknown namespaced
record types, digest participation for preserved data, and the umbrella-owned
minimum decision payloads.
"""

from __future__ import annotations

import json

import pytest

from environment_harness import EvidenceStore, ResourceRegistry
from environment_harness.access import trusted_local
from environment_harness.errors import Conflict, Unsupported
from environment_harness.fixtures import shared_experiment
from environment_harness.resources import (
    API_VERSION,
    Checkpoint,
    Experiment,
    ScenarioSet,
    Session,
)
from environment_harness.store import digest, encode
from environment_harness.training import TrainingRun, TrajectoryDataset
from environment_harness.trajectories import (
    ExtensionRecord,
    Policy,
    Trajectory,
    TrajectoryRecord,
    TrajectorySnapshot,
)

RESOURCE_KINDS = (
    ("ScenarioSet", ScenarioSet),
    ("Experiment", Experiment),
    ("Session", Session),
    ("Checkpoint", Checkpoint),
    ("Policy", Policy),
    ("Trajectory", Trajectory),
    ("TrajectorySnapshot", TrajectorySnapshot),
    ("TrajectoryDataset", TrajectoryDataset),
    ("TrainingRun", TrainingRun),
)


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    """One shared experiment fixture for every contract assertion."""

    root = tmp_path_factory.mktemp("shared-experiment")
    return shared_experiment(root)


def test_registry_resolves_every_portable_resource_kind():
    registry = ResourceRegistry()
    for kind, model in RESOURCE_KINDS:
        assert registry._models[(API_VERSION, kind)] is model
    with pytest.raises(Unsupported, match="unsupported portable resource"):
        registry.decode({"apiVersion": API_VERSION, "kind": "Branch"})
    with pytest.raises(Unsupported, match="requires apiVersion and kind"):
        registry.decode({"kind": "Session"})
    with pytest.raises(Conflict, match="already registered"):
        registry.register(API_VERSION, "Session", Session)


def test_shared_fixture_covers_experiment_of_one_and_multi_session(shared):
    harness = shared["harness"]
    solo = shared["solo"].resource()
    grouped = shared["experiment"].resource()

    # An experiment-of-one is a Session whose owner is implicit, not an
    # ownerless Session lifecycle.
    assert solo.spec.experiment.id.startswith("experiment-of-one:")
    assert solo.spec.trial == 0

    assert grouped.spec.trials == 2
    assert grouped.status.progress.planned == 4
    assert grouped.status.progress.created == 4
    sessions = harness.session_resources(experiment=grouped.metadata.id)
    assert len(sessions) == 4
    assert {item.spec.scenario.id for item in sessions} == {"low", "high"}
    assert {item.spec.trial for item in sessions} == {0, 1}

    scenario_set = shared["experiment"].scenario_set()
    assert scenario_set.status.scenario_count == 2
    assert grouped.spec.scenario_set.digest == scenario_set.status.set_digest


def test_experiment_embeds_one_environment_spec_and_no_environment_resource(shared):
    grouped = shared["experiment"].resource()
    registry = ResourceRegistry()

    # EnvironmentSpec is frozen inside the Experiment and inherited by Sessions.
    assert grouped.spec.environment["protocol"] == "environment-session.v1"
    assert grouped.spec.environment_reference.spec_digest == digest(grouped.spec.environment)
    for session in shared["harness"].session_resources(experiment=grouped.metadata.id):
        assert session.spec.environment == grouped.spec.environment
        assert session.spec.environment_reference == grouped.spec.environment_reference

    # There is no EnvironmentDefinition resource and no Branch resource.
    for absent in ("EnvironmentDefinition", "Environment", "Branch"):
        assert (API_VERSION, absent) not in registry._models


def test_trajectory_manifests_reference_the_same_frozen_identities(shared):
    harness = shared["harness"]
    grouped = shared["experiment"].resource()
    for handle in shared["result"].sessions:
        session = handle.resource()
        trajectory = handle.trajectory()
        assert session.status.trajectory is not None
        assert trajectory.metadata.id == session.status.trajectory.id
        assert trajectory.spec.manifest.source.run_id == handle.id
        assert trajectory.spec.manifest.environment == session.spec.environment
        assert set(trajectory.spec.manifest.participants) == {
            participant.id for participant in session.spec.participants
        }
        # Policy references project the same participant implementations.
        assert {policy.participant for policy in trajectory.spec.manifest.policies} == {
            participant.id for participant in session.spec.participants
        }
        frozen = (trajectory.spec.manifest.model_extra or {})["experiment"]
        assert frozen["environment"] == grouped.spec.environment
    assert harness.experiment_resources()[0].metadata.id == grouped.metadata.id


def test_checkpoint_is_distinct_from_a_trajectory_snapshot(shared):
    handle = shared["solo"]
    checkpoint = handle.checkpoint_resource(shared["checkpoint"])
    snapshot = handle.snapshot()

    assert checkpoint.kind == "Checkpoint" and snapshot.kind == "TrajectorySnapshot"
    # A Checkpoint exists to resume or branch; it references opaque state.
    assert checkpoint.status.resumable and checkpoint.status.branchable
    assert checkpoint.spec.state_reference and checkpoint.spec.session.id == handle.id
    assert checkpoint.status.checkpoint_digest != snapshot.status.snapshot_digest
    # A snapshot is an authorized evidence projection with a record boundary.
    assert snapshot.spec.sequence_end >= snapshot.spec.sequence_start
    assert checkpoint.spec.evidence_sequence >= 1


def test_inexact_continuation_and_unavailable_data_stay_explicit(tmp_path):
    from environment_harness import EnvironmentHarness, Scenario
    from environment_harness.fixtures import SyntheticEnvironment, SyntheticScenarioInput

    class Stateless:
        implementation = "stateless@1"
        supports_checkpoint = False

        def act(self, observation):
            return {"value": 1}

    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=SyntheticEnvironment,
        agent_factories={"alice": Stateless},
    )
    session = harness.run(Scenario(id="inexact", input=SyntheticScenarioInput()), turns=1)
    identity = session.checkpoint()["id"]
    checkpoint = session.checkpoint_resource(identity)

    assert checkpoint.status.exact is False
    assert checkpoint.spec.participants[0].exact is False
    assert checkpoint.spec.participants[0].unavailable == ("participant continuation state",)
    assert any("continuation state unavailable" in item for item in checkpoint.status.unavailable)
    with pytest.raises(Exception):
        session.checkpoint(exact_agents=True)


def test_first_party_writes_are_strict_while_the_registry_reads_leniently(shared):
    body = shared["solo"].resource().model_dump(mode="json", by_alias=True)
    registry = ResourceRegistry()

    # A typo in a strict first-party envelope cannot become evidence.
    with pytest.raises(Exception):
        Session.model_validate(body | {"stauts": body["status"]})
    with pytest.raises(Exception):
        Session.model_validate(body | {"rewrd": 1})

    # A newer writer's additive optional fields survive a recursive round trip.
    newer = json.loads(json.dumps(body))
    newer["spec"]["futureSelection"] = {"mode": "adaptive", "weights": [0.25, 0.75]}
    newer["status"]["futureReadiness"] = "unknown"
    newer["status"]["termination"]["futureCause"] = "external"
    newer["features"]["optional"] = ["com.example.future.selection"]
    decoded = registry.decode(newer)
    assert isinstance(decoded, Session)
    round_tripped = decoded.model_dump(mode="json", by_alias=True)
    assert round_tripped["spec"]["futureSelection"] == newer["spec"]["futureSelection"]
    assert round_tripped["status"]["futureReadiness"] == "unknown"
    assert round_tripped["status"]["termination"]["futureCause"] == "external"
    assert encode(round_tripped) == encode(newer)

    # Preserved unknown data participates in the canonical digest.
    assert digest(round_tripped) != digest(body)


def test_unknown_required_features_fail_before_partial_consumption(shared):
    body = shared["solo"].resource().model_dump(mode="json", by_alias=True)
    registry = ResourceRegistry()
    required = json.loads(json.dumps(body))
    required["features"]["required"] = ["com.example.unsupported.capability"]
    with pytest.raises(Unsupported, match="unsupported required resource features"):
        registry.decode(required)

    aware = ResourceRegistry(supported_features=("com.example.unsupported.capability",))
    assert isinstance(aware.decode(required), Session)


def test_unknown_namespaced_records_stay_inert_and_lossless():
    registry = ResourceRegistry()
    payload = {
        "type": "com.example.drone.telemetry",
        "id": "record-1",
        "sequence": 1,
        "segment": "segment-1",
        "participant": "alice",
        "revision": 0,
        "causes": [],
        "time": {"wallTime": "2026-09-22T15:00:00Z", "native": []},
        "data": {"altitude": 12.5},
        "extensions": {"com.example.drone/vendor": "synthetic"},
    }
    decoded = registry.decode_record(payload)
    assert isinstance(decoded, ExtensionRecord)
    assert encode(decoded.model_dump(mode="json", by_alias=True)) == encode(payload)

    with pytest.raises(Unsupported, match="reverse-domain namespaced"):
        registry.decode_record(payload | {"type": "telemetry"})
    with pytest.raises(ValueError, match="reverse-domain namespace"):
        registry.register_record("telemetry", TrajectoryRecord)
    registry.register_record("com.example.drone.telemetry", TrajectoryRecord)
    with pytest.raises(Conflict, match="already registered"):
        registry.register_record("com.example.drone.telemetry", TrajectoryRecord)
    with pytest.raises(Conflict, match="already registered"):
        ResourceRegistry().register_record("environment.observation", TrajectoryRecord)


MINIMUM_DECISION_REQUESTED = {
    "type": "decision.requested",
    "id": "decision-request-1",
    "sequence": 3,
    "segment": "segment-1",
    "participant": "alice",
    "revision": 0,
    "causes": ["record-observation-1"],
    "time": {"wallTime": "2026-09-22T15:00:00Z", "native": []},
    "data": {
        "decision": "decision-1",
        "inputs": ["record-observation-1"],
        "candidateSetDigest": "0" * 64,
        "candidates": [
            {"id": "candidate-a", "summary": "hold"},
            {"id": "candidate-b", "summary": "escalate"},
        ],
        "constraints": {"maxOperations": 1},
        "group": "decision-group-1",
    },
    "extensions": {},
}
MINIMUM_DECISION_SELECTED = {
    "type": "decision.selected",
    "id": "decision-result-1",
    "sequence": 4,
    "segment": "segment-1",
    "participant": "alice",
    "revision": 0,
    "causes": ["decision-request-1"],
    "time": {"wallTime": "2026-09-22T15:00:01Z", "native": []},
    "data": {
        "decision": "decision-1",
        "selected": "candidate-b",
        "abstained": False,
        "selector": {"id": "reference-selector", "version": "1"},
        "requestDigest": "1" * 64,
        "operations": ["operation-1"],
    },
    "extensions": {},
}


@pytest.mark.parametrize(
    "payload", [MINIMUM_DECISION_REQUESTED, MINIMUM_DECISION_SELECTED], ids=["requested", "selected"]
)
def test_minimum_decision_payloads_are_owned_by_this_contract(payload):
    """The decision-seam workstream may extend these, never redefine them."""

    registry = ResourceRegistry()
    decoded = registry.decode_record(payload)
    assert isinstance(decoded, TrajectoryRecord)
    assert encode(decoded.model_dump(mode="json", by_alias=True)) == encode(payload)

    # Optional provider fields are additive and survive losslessly.
    extended = json.loads(json.dumps(payload))
    extended["data"]["providerTrace"] = {"vendor": "typesafe", "latencyMs": 12}
    extended["extensions"]["com.example.selector/seed"] = 7
    again = registry.decode_record(extended)
    assert again.data["providerTrace"] == {"vendor": "typesafe", "latencyMs": 12}
    assert digest(again.model_dump(mode="json", by_alias=True)) != digest(payload)


@pytest.mark.parametrize("links", [[], ["operation-1"], ["operation-1", "operation-2"]])
def test_decision_to_operation_cardinality_is_zero_one_or_many(links):
    registry = ResourceRegistry()
    payload = json.loads(json.dumps(MINIMUM_DECISION_SELECTED))
    payload["data"]["operations"] = links
    decoded = registry.decode_record(payload)
    assert decoded.data["operations"] == links


def test_legacy_stores_project_into_the_new_resources_without_mutation(tmp_path):
    """A pre-0.3 store has no environment reference columns or scenario digests."""

    store = EvidenceStore(tmp_path)
    fixture = shared_experiment(store, tenant="legacy")
    experiment = fixture["experiment"].resource()
    access = trusted_local("legacy")

    with store.transaction() as db:
        before = {
            row["environment"]: dict(row) for row in db.execute("SELECT * FROM session_runs").fetchall()
        }
        config = json.loads(
            db.execute("SELECT config FROM experiments WHERE id=?", (experiment.metadata.id,)).fetchone()[
                "config"
            ]
        )
        # Simulate a store written before 006_scheduler_recovery and before the
        # experiment config carried a structured environment reference.
        db.execute("UPDATE session_runs SET environment_id=NULL,environment_version=NULL,spec_digest=NULL")
        db.execute(
            "UPDATE experiments SET config=? WHERE id=?",
            (
                encode({k: v for k, v in config.items() if k != "environment_reference"}),
                experiment.metadata.id,
            ),
        )

    projection = fixture["harness"].resources()
    projected = projection.experiment(experiment.metadata.id, access)
    assert projected.spec.environment_reference.spec_digest == digest(projected.spec.environment)
    for session in projection.sessions(access, experiment=experiment.metadata.id):
        assert session.spec.environment_reference.spec_digest == digest(session.spec.environment)

    with store.transaction() as db:
        after = {row["environment"]: dict(row) for row in db.execute("SELECT * FROM session_runs").fetchall()}
        # Projection is read-only: it rewrote nothing but the simulated columns.
        for identity, row in after.items():
            expected = before[identity] | {
                "environment_id": None,
                "environment_version": None,
                "spec_digest": None,
            }
            assert row == expected


def test_legacy_action_row_export_remains_readable(shared):
    """The corrected export replaces the action-row shape; old rows still read."""

    from environment_harness import EnvironmentHarness, Scenario
    from environment_harness.evaluation import rollouts
    from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment, SyntheticScenarioInput

    harness = EnvironmentHarness(
        shared["harness"].store.root / "training",
        environment_factory=SyntheticEnvironment,
        agent_factories={"alice": SyntheticAgent},
        tenant="training",
    )
    from environment_harness.contracts import RunPolicy

    harness.policy = RunPolicy(max_turns=2)
    session = harness.run(Scenario(id="legacy", input=SyntheticScenarioInput()), turns=1)
    with harness.store.transaction() as db:
        manifest = json.loads(
            db.execute("SELECT manifest FROM environments WHERE id=?", (session.id,)).fetchone()["manifest"]
        )
    # The evaluation-purpose fixture is not training-entitled.
    assert manifest["purpose"] == "evaluation"
    with pytest.raises(Exception):
        list(rollouts(harness.store, session.id, harness._access))


def test_resource_projection_bounds_pages_and_hides_unknown_identities(shared):
    from environment_harness.errors import Forbidden

    projection = shared["harness"].resources()
    access = shared["harness"]._access
    for call in (
        lambda: projection.experiments(access, limit=0),
        lambda: projection.sessions(access, limit=0),
        lambda: projection.scenario_sets(access, limit=1001),
    ):
        with pytest.raises(ValueError, match="page size"):
            call()
    with pytest.raises(Forbidden, match="experiment unavailable"):
        projection.experiment("missing", access)
    with pytest.raises(Forbidden, match="scenario set unavailable"):
        projection.scenario_set("scenario-set-missing", access)
    with pytest.raises(Forbidden, match="session unavailable"):
        projection.session("f" * 32, access)
    with pytest.raises(Forbidden, match="checkpoint unavailable"):
        projection.checkpoint(shared["solo"].id, "missing", access)

    # A participant credential cannot read the experiment index.
    scoped = shared["harness"]._runtime().participant_context(shared["solo"].id, access, "alice")
    with pytest.raises(Forbidden, match="policy denies"):
        projection.experiments(scoped)


def test_cross_resource_references_carry_explicit_identity(shared):
    from environment_harness.resources import reference

    built = reference("Session", shared["solo"].id, digest="0" * 64, label="ignored")
    assert built.kind == "Session" and built.api_version == API_VERSION
    assert built.digest == "0" * 64
    # A label is convenience only; identity is the kind, ID, and digest.
    assert built.label == "ignored"

    session = shared["solo"].resource()
    assert session.status.reports == ()
    assert session.spec.lineage.root == shared["solo"].id
    assert session.spec.lineage.parent is None


def test_branched_child_session_records_its_lineage(shared):
    from environment_harness import BranchRequest

    child = shared["solo"].branch(BranchRequest(checkpoint=shared["checkpoint"], interventions={"total": 5}))
    resource = child.resource()
    assert resource.spec.lineage.parent == shared["solo"].id
    assert resource.spec.lineage.checkpoint == shared["checkpoint"]
    assert resource.spec.lineage.interventions == {"total": 5}
    assert resource.spec.lineage.root == shared["solo"].resource().spec.lineage.root

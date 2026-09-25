import json

import pytest
from pydantic import ValidationError

from environment_harness import AgentSpec, EvidenceStore, ExperimentSpec
from environment_harness.access import _AccessContext
from environment_harness.contracts import Action, Capabilities, OperationSpec, RunPolicy
from environment_harness.errors import Conflict, Forbidden, Unsupported
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.operations import Operations
from environment_harness.runtime import _SessionRuntime
from environment_harness.store import uid


def specification(environment, **changes):
    values = {
        "environment": environment.spec,
        "participants": (AgentSpec(id="a", implementation="synthetic", policy_version="1"),),
    }
    values.update(changes)
    return ExperimentSpec(**values)


def test_contract_invariants_reject_inconsistent_authority_and_purpose():
    with pytest.raises(ValidationError, match="checkpoint"):
        Capabilities(resume=True)
    environment = SyntheticEnvironment()
    with pytest.raises(ValidationError, match="duplicate participant"):
        specification(
            environment,
            participants=(
                AgentSpec(id="a", implementation="synthetic", policy_version="1"),
                AgentSpec(id="a", implementation="synthetic", policy_version="1"),
            ),
        )
    evaluation_only = SyntheticEnvironment()
    evaluation_only.spec = evaluation_only.spec.model_copy(update={"purposes": ("evaluation",)})
    with pytest.raises(ValidationError, match="entitlement denies purpose"):
        specification(evaluation_only, purpose="training", split="training")

    expanded = SyntheticEnvironment()
    expanded.spec = expanded.spec.model_copy(update={"purposes": ("evaluation", "training")})
    with pytest.raises(ValidationError, match="heldout"):
        specification(expanded, purpose="training", split="heldout")
    with pytest.raises(ValidationError, match="external writes unsupported"):
        specification(expanded, policy=RunPolicy(external_writes=True))


def test_operation_contracts_reject_ambiguous_or_unserializable_configuration():
    operation = OperationSpec(name="world.inspect", version="1")
    environment = SyntheticEnvironment()

    with pytest.raises(ValidationError, match="JSON serializable"):
        OperationSpec(name="world.inspect", version="1", config={"threshold": float("nan")})
    with pytest.raises(ValidationError, match="duplicate environment operation"):
        environment.spec.model_copy(update={"operations": (operation, operation)}).model_validate(
            environment.spec.model_copy(update={"operations": (operation, operation)}).model_dump()
        )

    environment.spec = environment.spec.model_copy(update={"operations": (operation,)})
    with pytest.raises(ValidationError, match="duplicate operation"):
        specification(environment, operations=(operation, operation))


def test_session_creation_is_scoped_idempotent_and_split_safe(tmp_path):
    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    agent = _AccessContext(
        tenant="tenant",
        subject="a",
        policy="participant",
        session="0" * 32,
        participant="a",
    )
    spec = specification(environment)

    with pytest.raises(Forbidden, match="policy denies|cannot create sessions"):
        session.create(spec, agent)
    with pytest.raises(Forbidden, match="policy denies|cannot create sessions"):
        session.create(spec, researcher.replace(session="a" * 32))

    changed_environment = SyntheticEnvironment()
    changed_environment.spec = changed_environment.spec.model_copy(update={"version": "2"})
    with pytest.raises(Conflict, match="contract mismatch"):
        session.create(specification(changed_environment), researcher)
    for identifier in ("short", "G" * 32):
        with pytest.raises(ValueError, match="32 lowercase hex"):
            session.create(spec, researcher, environment_id=identifier)

    identifier = "a" * 32
    first = session.create(spec, researcher, environment_id=identifier)
    assert session.create(spec, researcher, environment_id=identifier) == first
    with pytest.raises(Conflict, match="reused with different experiment"):
        session.create(spec.model_copy(update={"seed": 2}), researcher, environment_id=identifier)
    # A participant credential sees only the session it is bound to.
    assert session.list(agent) == []
    assert [item["id"] for item in session.list(agent.replace(session=identifier))] == [identifier]
    for invalid_limit in (0, 1001):
        with pytest.raises(ValueError, match="page size"):
            session.list_page(researcher, invalid_limit)
    for invalid_cursor in ("short", "g" * 32):
        with pytest.raises(ValueError, match="cursor"):
            session.list_page(researcher, cursor=invalid_cursor)
    session.create(spec, researcher, environment_id="b" * 32)
    session.create(spec, researcher, environment_id="c" * 32)
    page, cursor = session.list_page(researcher, limit=2)
    assert [row["id"] for row in page] == ["c" * 32, "b" * 32]
    assert cursor == "b" * 32
    final_page, final_cursor = session.list_page(researcher, limit=2, cursor=cursor)
    assert [row["id"] for row in final_page] == [identifier]
    assert final_cursor is None
    scoped_agent = agent.replace(session=identifier)
    assert "experiment" not in session.get(identifier, scoped_agent)

    incompatible = SyntheticEnvironment()
    incompatible.spec = incompatible.spec.model_copy(update={"version": "2"})
    with pytest.raises(Conflict, match="version changed"):
        _SessionRuntime(store, incompatible).observe(identifier, researcher, "a")


def test_state_size_split_lease_and_participant_edges(tmp_path):
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")

    class Large(SyntheticEnvironment):
        def initialize(self, experiment):
            return {"state": "x" * 2000}

    large = Large()
    session = _SessionRuntime(EvidenceStore(tmp_path / "large"), large)
    with pytest.raises(Conflict, match="state limit"):
        session.create(specification(large, policy=RunPolicy(max_state_bytes=1024)), researcher)

    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path / "normal")
    session = _SessionRuntime(store, environment)
    spec = specification(environment)
    identifier = session.create(spec, researcher)["id"]
    with pytest.raises(ValueError, match="invalid lease"):
        session.lease(identifier, researcher, "")
    with pytest.raises(ValueError, match="invalid lease"):
        session.lease(identifier, researcher, "owner", ttl=301)
    lease = session.lease(identifier, researcher, "owner")
    lease = session.renew(identifier, researcher, lease, ttl=10)
    assert lease["expires"] > 0
    with pytest.raises(ValueError, match="invalid lease"):
        session.renew(identifier, researcher, lease, ttl=0)
    session.control(identifier, researcher, lease, "pause")
    with pytest.raises(Conflict, match="not running"):
        session.renew(identifier, researcher, lease)
    with pytest.raises(ValueError, match="unknown control"):
        session.control(identifier, researcher, lease, "destroy")

    session.control(identifier, researcher, lease, "pause")
    session.resume(identifier, researcher, lease)
    with store.transaction() as db:
        row = db.execute("SELECT participants FROM environments WHERE id=?", (identifier,)).fetchone()
        participants = json.loads(row["participants"])
        participants["a"]["active"] = False
        db.execute(
            "UPDATE environments SET participants=? WHERE id=?",
            (json.dumps(participants), identifier),
        )
    with pytest.raises(Forbidden, match="inactive participant"):
        session.observe(identifier, researcher, "a")


def test_action_submission_distinguishes_authorization_and_phase_failures(tmp_path):
    environment_impl = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, environment_impl)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    spec = specification(environment_impl)
    environment = session.create(spec, researcher)["id"]
    agent = _AccessContext(
        tenant="tenant", subject="a", policy="participant", session=environment, participant="a"
    )
    observation = session.observe(environment, agent)

    def action(**changes):
        values = {
            "operation_id": uid(),
            "participant": "a",
            "observation_id": observation["id"],
            "revision": observation["revision"],
            "payload": {"value": 1},
        }
        values.update(changes)
        return Action(**values)

    with pytest.raises(Forbidden, match="another participant"):
        session.submit(environment, agent, action(participant="b"))
    with pytest.raises(Forbidden, match="delivered observation"):
        session.submit(environment, agent, action(observation_id=uid()))
    assert session.submit(environment, agent, action())["status"] == "accepted"
    assert session.submit(environment, agent, action())["reason"] == "decision_already_submitted"

    lease = session.lease(environment, researcher, "owner")
    session.control(environment, researcher, lease, "pause")
    blocked = session.submit(environment, agent, action())
    assert blocked["reason"] == "decision_already_submitted"


def test_memory_transfer_and_external_event_limits(tmp_path):
    implementation = SyntheticEnvironment("event")
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, implementation)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    spec = specification(
        implementation,
        participants=(
            AgentSpec(id="a", implementation="synthetic", policy_version="1"),
            AgentSpec(id="b", implementation="synthetic", policy_version="1"),
        ),
        policy=RunPolicy(max_state_bytes=1024),
    )
    environment = session.create(spec, researcher)["id"]
    agent = _AccessContext(
        tenant="tenant", subject="a", policy="participant", session=environment, participant="a"
    )
    with pytest.raises(Unsupported, match="checkpoint hook"):
        session.memory(environment, agent, {}, agent_state={})
    with pytest.raises(Conflict, match="context limit"):
        session.memory(environment, agent, {"large": "x" * 2000})

    lease = session.lease(environment, researcher, "worker")
    with pytest.raises(Unsupported, match="joining"):
        session.transfer(environment, researcher, lease, "missing", "controller")
    session.external_event(environment, researcher, lease, source="feed", cursor=1, event_time=1, payload={})
    with pytest.raises(Conflict, match="cursor must increase"):
        session.external_event(
            environment, researcher, lease, source="feed", cursor=1, event_time=2, payload={}
        )
    with pytest.raises(Conflict, match="queue limit"):
        session.external_event(
            environment,
            researcher,
            lease,
            source="large",
            cursor=1,
            event_time=2,
            payload={"large": "x" * 2000},
        )

    observation = session.observe(environment, agent)
    submitted = Action(
        operation_id=uid(),
        participant="a",
        observation_id=observation["id"],
        revision=0,
        payload={"value": 1},
    )
    session.submit(environment, agent, submitted)
    with pytest.raises(Conflict, match="decision boundary"):
        session.transfer(environment, researcher, lease, "a", "replacement")

    with store.transaction() as db:
        db.execute("UPDATE actions SET status='failed' WHERE environment=?", (environment,))
    replacement = session.transfer(environment, researcher, lease, "a", "replacement", active=False)
    assert replacement["generation"] == 1


def test_checkpoint_resume_branch_and_terminal_guards(tmp_path):
    implementation = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, implementation)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    spec = specification(implementation)
    environment = session.create(spec, researcher)["id"]
    lease = session.lease(environment, researcher, "worker")

    with pytest.raises(Unsupported, match="continue exactly"):
        session.checkpoint(environment, researcher, lease, exact_agents=True)
    checkpoint = session.checkpoint(environment, researcher, lease)
    with pytest.raises(Forbidden, match="checkpoint unavailable"):
        session.branch(environment, researcher, uid())
    with pytest.raises(ValueError, match="invalid branch ID"):
        session.branch(environment, researcher, checkpoint["id"], new_environment="not-an-environment-id")
    with pytest.raises(Conflict, match="versions changed"):
        session.resume(environment, researcher, lease, implementations={"a": "changed"})

    session.cancel(environment, researcher)
    terminal_lease = session.lease(environment, researcher, "terminal")
    with pytest.raises(Conflict, match="cannot be resumed"):
        session.resume(environment, researcher, terminal_lease)
    with pytest.raises(Conflict, match="already terminal"):
        session.control(environment, researcher, terminal_lease, "pause")
    with store.transaction() as db:
        db.execute("UPDATE environments SET status='completed' WHERE id=?", (environment,))
    with pytest.raises(Conflict, match="already terminal"):
        session.cancel(environment, researcher)


def test_checkpoint_and_phase_guards_cover_unsupported_modes(tmp_path):
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")

    unsupported = SyntheticEnvironment()
    unsupported.spec = unsupported.spec.model_copy(update={"capabilities": Capabilities()})
    session = _SessionRuntime(EvidenceStore(tmp_path / "unsupported"), unsupported)
    environment = session.create(specification(unsupported), researcher)["id"]
    lease = session.lease(environment, researcher, "worker")
    with pytest.raises(Unsupported, match="checkpoints"):
        session.checkpoint(environment, researcher, lease)
    with pytest.raises(Unsupported, match="cannot resume"):
        session.resume(environment, researcher, lease)
    with pytest.raises(Unsupported, match="counterfactual"):
        session.branch(environment, researcher, uid())
    with pytest.raises(Unsupported, match="wall-clock"):
        session.close_phase(environment, researcher, lease, revision=0)

    coordinator = SyntheticEnvironment()
    coordinator.spec = coordinator.spec.model_copy(update={"phase_deadline": "coordinator"})
    coordinated = _SessionRuntime(EvidenceStore(tmp_path / "coordinator"), coordinator)
    identifier = coordinated.create(specification(coordinator), researcher)["id"]
    coordinated_lease = coordinated.lease(identifier, researcher, "worker")
    with pytest.raises(Conflict, match="phase no longer active"):
        coordinated.close_phase(identifier, researcher, coordinated_lease, revision=1)

    operations = Operations(coordinated.store)
    agent = _AccessContext(
        tenant="tenant", subject="a", policy="participant", session=identifier, participant="a"
    )
    with coordinated.store.transaction() as db:
        row = coordinated.store.environment(db, identifier, agent)
        manifest = json.loads(row["manifest"])
        manifest["policy"]["allowed_endpoints"] = ["https://provider.invalid"]
        manifest["policy"]["allowed_operations"] = ["lookup"]
        db.execute("UPDATE environments SET manifest=? WHERE id=?", (json.dumps(manifest), identifier))
    operations.prepare(
        identifier,
        agent,
        "pending",
        endpoint="https://provider.invalid",
        operation="lookup",
        payload={},
    )
    with coordinated.store.transaction() as db:
        db.execute("UPDATE operations SET status='unknown' WHERE id='pending'")
    with pytest.raises(Conflict, match="ambiguous external operations"):
        coordinated.checkpoint(identifier, researcher, coordinated_lease)


def test_split_and_resolve_phase_guards(tmp_path):
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    implementation = SyntheticEnvironment()
    session = _SessionRuntime(EvidenceStore(tmp_path / "split"), implementation)
    session.create(specification(implementation), researcher)
    training = specification(
        implementation,
        purpose="training",
        split="training",
    )
    with pytest.raises(Conflict, match="another split"):
        session.create(training, researcher)

    identifier = session.list(researcher)[0]["id"]
    lease = session.lease(identifier, researcher, "worker")
    with pytest.raises(Conflict, match="required decisions"):
        session.resolve(identifier, researcher, lease)
    session.control(identifier, researcher, lease, "pause")
    with pytest.raises(Conflict, match="not running"):
        session.resolve(identifier, researcher, lease)

    event_impl = SyntheticEnvironment("event")
    event_session = _SessionRuntime(EvidenceStore(tmp_path / "event"), event_impl)
    event_id = event_session.create(specification(event_impl), researcher)["id"]
    event_lease = event_session.lease(event_id, researcher, "worker")
    with pytest.raises(Conflict, match="awaits an event"):
        event_session.resolve(event_id, researcher, event_lease)

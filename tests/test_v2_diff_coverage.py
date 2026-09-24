"""Adversarial regression cases for the v2 PR's durable and typed boundaries."""

import json
import random

import pytest
from pydantic import ValidationError
from test_v2_operations import ENDPOINT, SampleOperation, V2Environment

from environment_harness import (
    AgentSpec,
    EnvironmentSession,
    EnvironmentSpec,
    EnvironmentSpecV2,
    EvidenceStore,
    ExperimentSpec,
    OperationPlan,
    OperationReceipt,
    OperationRequest,
    OperationSpecV2,
    Principal,
)
from environment_harness.adapters.remote import PROTOCOL, PROTOCOL_V2, RemoteEnvironment
from environment_harness.contracts import (
    MAX_COST_MICROS,
    Action,
    Capabilities,
    OperationSpec,
    RunPolicy,
    Transition,
)
from environment_harness.errors import BudgetExceeded, Conflict, Forbidden, Unavailable
from environment_harness.operations import EnvironmentOperation, Operations
from environment_harness.runtime import tuples
from environment_harness.store import digest, encode
from environment_harness.worker import dispatch


def test_v2_model_bounds_cover_unreachable_serialization_and_budget_edges():
    operation = OperationRequest(key="a", operation="read", version="1")
    with pytest.raises(ValidationError, match="unique"):
        OperationRequest(key="duplicate-dependencies", operation="read", version="1", depends_on=("a", "a"))
    with pytest.raises(ValidationError, match="unique"):
        OperationPlan(plan_id="duplicate", operations=(operation, operation))
    with pytest.raises(ValidationError, match="signed 64-bit"):
        OperationPlan(
            plan_id="sum-overflow",
            operations=(
                operation.model_copy(update={"key": "a", "max_cost_micros": MAX_COST_MICROS}),
                operation.model_copy(update={"key": "b", "max_cost_micros": MAX_COST_MICROS}),
            ),
        )

    # The normal Pydantic boundary rejects these before constructing a Record.
    # Call validators on deliberately corrupted records to cover their fail-closed
    # defensive paths used when loading old or malformed durable rows.
    corrupt_plan = OperationPlan.model_construct(plan_id="bad", operations=(), continuation={"x": object()})
    with pytest.raises(ValueError, match="JSON serializable"):
        corrupt_plan.bounded_acyclic_plan()
    corrupt_request = OperationRequest.model_construct(
        key="bad", operation="read", version="1", payload={"x": object()}, max_cost_micros=0, depends_on=()
    )
    with pytest.raises(ValueError, match="JSON serializable"):
        corrupt_request.serializable()

    with pytest.raises(ValidationError, match="identity or cost"):
        OperationReceipt(
            key="a",
            operation_id="receipt-1",
            operation="read",
            version="1",
            receipt={"operation_id": "receipt-2", "cost_micros": 0},
            cost_micros=0,
        )
    corrupt_receipt = OperationReceipt.model_construct(
        key="a",
        operation_id="r",
        operation="read",
        version="1",
        receipt={"operation_id": "r", "cost_micros": 0, "x": object()},
        cost_micros=0,
    )
    with pytest.raises(ValueError, match="JSON serializable"):
        corrupt_receipt.serializable()
    with pytest.raises(ValidationError, match="128 KiB"):
        OperationReceipt(
            key="a",
            operation_id="r",
            operation="read",
            version="1",
            receipt={"operation_id": "r", "cost_micros": 0, "payload": "x" * 131_000},
            cost_micros=0,
        )

    env = V2Environment()
    policy = RunPolicy().model_copy(update={"max_cost_micros": MAX_COST_MICROS + 1})
    spec = ExperimentSpec.model_construct(
        environment=env.spec,
        participants=(AgentSpec(id="alice", implementation="a", policy_version="1"),),
        policy=policy,
        operations=(),
        purpose="evaluation",
        split="heldout",
    )
    with pytest.raises(ValueError, match="signed 64-bit"):
        spec.check()


class _Transport:
    def __init__(self, spec, protocol):
        self.spec = spec
        self.protocol = protocol
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        if request["method"] == "spec":
            result = self.spec.model_dump(mode="json")
        elif request["method"] == "plan_transition":
            result = {
                "plan": OperationPlan(plan_id="empty").model_dump(mode="json"),
                "rng": request["arguments"]["rng"],
            }
        elif request["method"] == "resolve_transition":
            result = {
                "transition": Transition(state={"ok": True}).model_dump(mode="json"),
                "rng": request["arguments"]["rng"],
            }
        else:
            result = {}
        return {"protocol": request["protocol"], "id": request["id"], "result": result}


def test_remote_v1_rejects_v2_methods_and_v2_receipts_before_transport():
    from environment_harness.fixtures import SyntheticEnvironment

    legacy_spec = SyntheticEnvironment().spec
    v1 = RemoteEnvironment(_Transport(legacy_spec, PROTOCOL))
    with pytest.raises(Unavailable, match="v1 workers"):
        v1.plan_transition({}, {}, random.Random(1), [])
    with pytest.raises(Unavailable, match="v1 workers"):
        v1.resolve_transition({}, {}, random.Random(1), [], OperationPlan(plan_id="empty"), {})

    operation = OperationSpecV2(name="world.sample", version="1", access="read")
    v2_spec = EnvironmentSpecV2(
        id="remote-v2",
        version="1",
        implementation="remote-v2@1",
        scheduling="sequential",
        operations=(operation,),
        capabilities=Capabilities(),
    )
    transport = _Transport(v2_spec, PROTOCOL_V2)
    remote = RemoteEnvironment(transport, expected_spec=v2_spec)
    plan = OperationPlan(
        plan_id="p",
        operations=(OperationRequest(key="x", operation="world.sample", version="1"),),
    )
    receipt = OperationReceipt(
        key="other",
        operation_id="receipt",
        operation="world.sample",
        version="1",
        receipt={"operation_id": "receipt", "cost_micros": 0},
        cost_micros=0,
    )
    before = len(transport.calls)
    with pytest.raises(ValueError, match="do not match"):
        remote.resolve_transition({}, {}, random.Random(1), [], plan, {"x": receipt})
    assert len(transport.calls) == before

    with pytest.raises(Unavailable, match="protocol does not match"):
        RemoteEnvironment(_Transport(v2_spec, PROTOCOL))


def test_worker_dispatch_rejects_receipt_key_not_in_plan():
    env = V2Environment(SampleOperation())
    plan = OperationPlan(
        plan_id="p",
        operations=(OperationRequest(key="planned", operation="world.sample", version="1"),),
    )
    receipt = OperationReceipt(
        key="wrong",
        operation_id="receipt",
        operation="world.sample",
        version="1",
        receipt={"operation_id": "receipt", "cost_micros": 0},
        cost_micros=0,
    )
    with pytest.raises(ValueError, match="do not match"):
        dispatch(
            env,
            "resolve_transition",
            {
                "rng": random.Random(1).getstate(),
                "plan": plan.model_dump(mode="json"),
                "receipts": {"planned": receipt.model_dump(mode="json")},
                "state": {},
                "actions": {},
                "events": [],
            },
        )


class _StopBeforePrepare(Exception):
    pass


def _planned_session(tmp_path, monkeypatch, implementation=None, *, max_cost_micros=20):
    provider = SampleOperation()
    implementation = implementation or V2Environment(provider)
    implementation.spec = implementation.spec.model_copy(update={"phase_deadline": "coordinator"})
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, implementation)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    selected = provider.spec
    experiment = ExperimentSpec(
        environment=implementation.spec,
        participants=(AgentSpec(id="alice", implementation="agent@1", policy_version="1"),),
        operations=(selected,),
        policy=RunPolicy(
            max_turns=10,
            max_cost_micros=max_cost_micros,
            allowed_endpoints=(ENDPOINT,),
            allowed_operations=(selected.name,),
        ),
    )
    environment = session.create(experiment, researcher)["id"]
    agent = Principal(
        tenant="tenant", subject="alice", role="agent", environment=environment, participant="alice"
    )
    observation = session.observe(environment, agent)
    session.submit(
        environment,
        agent,
        Action(
            operation_id="plan-action",
            participant="alice",
            observation_id=observation["id"],
            revision=0,
            payload={},
        ),
    )
    lease = session.lease(environment, researcher, "plan-worker")
    session.close_phase(environment, researcher, lease, revision=0)
    with monkeypatch.context() as patcher:
        patcher.setattr(
            Operations,
            "prepare_environment_plan",
            lambda *a, **k: (_ for _ in ()).throw(_StopBeforePrepare()),
        )
        with pytest.raises(_StopBeforePrepare):
            session.resolve(environment, researcher, lease)
    with store.transaction() as db:
        row = db.execute("SELECT * FROM transitions WHERE environment=?", (environment,)).fetchone()
        persisted = json.loads(row["request"])
    return store, session, implementation, researcher, environment, lease, row, persisted


def test_prepare_plan_fences_durable_identity_inputs_and_mapping(tmp_path, monkeypatch):
    store, session, _, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    journal = Operations(store)
    plan = OperationPlan.model_validate(persisted["plan"])
    operation_ids = persisted["operation_ids"]
    with pytest.raises(Conflict, match="mapping is incomplete"):
        journal.prepare_environment_plan(
            session,
            environment,
            researcher,
            lease,
            plan=plan,
            operation_ids={},
            transition_revision=0,
            transition_hash=row["input_hash"],
        )

    with store.transaction() as db:
        db.execute("UPDATE environments SET state=? WHERE id=?", (encode({"tampered": True}), environment))
    with pytest.raises(Conflict, match="inputs changed"):
        journal.prepare_environment_plan(
            session,
            environment,
            researcher,
            lease,
            plan=plan,
            operation_ids=operation_ids,
            transition_revision=0,
            transition_hash=row["input_hash"],
        )


def test_prepare_plan_rejects_mismatched_durable_plan_and_changed_action_record(tmp_path, monkeypatch):
    store, session, _, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    journal = Operations(store)
    plan = OperationPlan.model_validate(persisted["plan"])
    ids = persisted["operation_ids"]
    changed_plan = dict(persisted)
    changed_plan["plan"] = {**changed_plan["plan"], "plan_id": "tampered"}
    with store.transaction() as db:
        db.execute(
            "UPDATE transitions SET request=? WHERE environment=? AND revision=0",
            (encode(changed_plan), environment),
        )
    with pytest.raises(Conflict, match="differs from its durable"):
        journal.prepare_environment_plan(
            session,
            environment,
            researcher,
            lease,
            plan=plan,
            operation_ids=ids,
            transition_revision=0,
            transition_hash=row["input_hash"],
        )
    with store.transaction() as db:
        db.execute(
            "UPDATE transitions SET request=? WHERE environment=? AND revision=0",
            (encode(persisted), environment),
        )
        action = db.execute("SELECT id FROM actions WHERE environment=?", (environment,)).fetchone()
        db.execute(
            "UPDATE actions SET request=? WHERE environment=? AND id=?",
            (encode({"payload": {"tampered": True}}), environment, action["id"]),
        )
    with pytest.raises(Conflict, match="actions changed after planning"):
        journal.prepare_environment_plan(
            session,
            environment,
            researcher,
            lease,
            plan=plan,
            operation_ids=ids,
            transition_revision=0,
            transition_hash=row["input_hash"],
        )


def test_prepare_plan_checks_provider_and_prior_reservation_fences(tmp_path, monkeypatch):
    store, session, implementation, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    journal = Operations(store)
    plan = OperationPlan.model_validate(persisted["plan"])
    operation_ids = persisted["operation_ids"]
    provider = implementation.operations["world.sample"]
    provider.endpoint = "https://other.invalid"
    with pytest.raises(Forbidden, match="frozen environment contract"):
        journal.prepare_environment_plan(
            session,
            environment,
            researcher,
            lease,
            plan=plan,
            operation_ids=operation_ids,
            transition_revision=0,
            transition_hash=row["input_hash"],
        )
    provider.endpoint = ENDPOINT

    # A previously journaled ID is accepted only when generation and reservation match.
    journal.prepare_environment_plan(
        session,
        environment,
        researcher,
        lease,
        plan=plan,
        operation_ids=operation_ids,
        transition_revision=0,
        transition_hash=row["input_hash"],
    )
    with store.transaction() as db:
        db.execute(
            "UPDATE operations SET reservation=reservation-1 WHERE environment=? AND id=?",
            (environment, operation_ids["first"]),
        )
    with pytest.raises(Conflict, match="identifier reused"):
        journal.prepare_environment_plan(
            session,
            environment,
            researcher,
            lease,
            plan=plan,
            operation_ids=operation_ids,
            transition_revision=0,
            transition_hash=row["input_hash"],
        )


def test_prepare_plan_rejects_reservation_overflow_and_stale_intent_status(tmp_path, monkeypatch):
    store, session, _, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    journal = Operations(store)
    plan = OperationPlan.model_validate(persisted["plan"])
    operation_ids = persisted["operation_ids"]
    with store.transaction() as db:
        db.execute("UPDATE environments SET reserved=19 WHERE id=?", (environment,))
    with pytest.raises(BudgetExceeded, match="remaining session budget"):
        journal.prepare_environment_plan(
            session,
            environment,
            researcher,
            lease,
            plan=plan,
            operation_ids=operation_ids,
            transition_revision=0,
            transition_hash=row["input_hash"],
        )
    with store.transaction() as db:
        db.execute("UPDATE environments SET reserved=0 WHERE id=?", (environment,))
        db.execute(
            "UPDATE transitions SET status='failed' WHERE environment=? AND revision=0", (environment,)
        )
    with pytest.raises(Conflict, match="not durably prepared"):
        journal.prepare_environment_plan(
            session,
            environment,
            researcher,
            lease,
            plan=plan,
            operation_ids=operation_ids,
            transition_revision=0,
            transition_hash=row["input_hash"],
        )


def test_prepare_plan_rejects_revision_drift_and_v1_contracts(tmp_path, monkeypatch):
    store, session, _, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    with pytest.raises(Conflict, match="inputs changed"):
        Operations(store).prepare_environment_plan(
            session,
            environment,
            researcher,
            lease,
            plan=OperationPlan.model_validate(persisted["plan"]),
            operation_ids=persisted["operation_ids"],
            transition_revision=1,
            transition_hash=row["input_hash"],
        )

    from environment_harness.fixtures import SyntheticEnvironment

    v1 = SyntheticEnvironment()
    v1_store = EvidenceStore(tmp_path / "v1")
    v1_session = EnvironmentSession(v1_store, v1)
    experiment = ExperimentSpec(
        environment=v1.spec,
        participants=(AgentSpec(id="a", implementation="a", policy_version="1"),),
    )
    v1_env = v1_session.create(experiment, researcher)["id"]
    v1_lease = v1_session.lease(v1_env, researcher, "worker")
    plan = OperationPlan(
        plan_id="illegal-v1",
        operations=(OperationRequest(key="k", operation="world.read", version="1"),),
    )
    with pytest.raises(Conflict, match="require environment-session.v2"):
        Operations(v1_store).prepare_environment_plan(
            v1_session,
            v1_env,
            researcher,
            v1_lease,
            plan=plan,
            operation_ids={"k": "operation-id"},
            transition_revision=0,
            transition_hash="input-hash",
        )


def test_operations_fail_closed_on_unknown_status_endpoint_and_dependencies(tmp_path, monkeypatch):
    store, session, implementation, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    journal = Operations(store)
    with pytest.raises(Forbidden, match="unavailable"):
        journal.dispatch(session, environment, researcher, lease, "missing-operation")
    plan = OperationPlan.model_validate(persisted["plan"])
    operation_ids = persisted["operation_ids"]
    journal.prepare_environment_plan(
        session,
        environment,
        researcher,
        lease,
        plan=plan,
        operation_ids=operation_ids,
        transition_revision=0,
        transition_hash=row["input_hash"],
    )
    first_id, second_id = operation_ids["first"], operation_ids["second"]
    with store.transaction() as db:
        db.execute("DELETE FROM operations WHERE environment=? AND id=?", (environment, first_id))
    with pytest.raises(Conflict, match="dependency has not settled"):
        journal.dispatch(
            session,
            environment,
            researcher,
            lease,
            second_id,
            provider=implementation.operations["world.sample"],
        )


def test_runtime_v2_rejects_missing_hooks_operation_limit_and_caches_committed_result(tmp_path):
    implementation = V2Environment()
    session = EnvironmentSession(EvidenceStore(tmp_path / "missing"), implementation)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    implementation.plan_transition = None
    experiment = ExperimentSpec(
        environment=implementation.spec,
        participants=(AgentSpec(id="alice", implementation="a", policy_version="1"),),
    )
    with pytest.raises(Conflict, match="requires plan_transition"):
        session.create(experiment, researcher)

    provider = SampleOperation()
    limited = V2Environment(provider)
    limited.spec = limited.spec.model_copy(
        update={"phase_deadline": "coordinator", "max_transition_operations": 1}
    )
    session = EnvironmentSession(EvidenceStore(tmp_path / "limit"), limited)
    spec = ExperimentSpec(
        environment=limited.spec,
        participants=(AgentSpec(id="alice", implementation="a", policy_version="1"),),
        operations=(provider.spec,),
        policy=RunPolicy(
            max_turns=5,
            max_cost_micros=20,
            allowed_endpoints=(ENDPOINT,),
            allowed_operations=(provider.spec.name,),
        ),
    )
    identity = session.create(spec, researcher)["id"]
    agent = Principal(
        tenant="tenant", subject="alice", role="agent", environment=identity, participant="alice"
    )
    observation = session.observe(identity, agent)
    session.submit(
        identity,
        agent,
        Action(
            operation_id="a", participant="alice", observation_id=observation["id"], revision=0, payload={}
        ),
    )
    lease = session.lease(identity, researcher, "worker")
    session.close_phase(identity, researcher, lease, revision=0)
    with pytest.raises(Conflict, match="transition limit"):
        session.resolve(identity, researcher, lease)
    assert provider.calls == []


def test_pending_v2_plan_fences_new_actions_and_changed_inputs(tmp_path, monkeypatch):
    store, session, _, researcher, environment, lease, _, _ = _planned_session(tmp_path, monkeypatch)
    agent = Principal(
        tenant="tenant", subject="alice", role="agent", environment=environment, participant="alice"
    )
    with pytest.raises(Conflict, match="pending reconciliation"):
        session.submit(
            environment,
            agent,
            Action(
                operation_id="second-action",
                participant="alice",
                observation_id="unknown-observation",
                revision=0,
                payload={},
            ),
        )
    with store.transaction() as db:
        db.execute("UPDATE environments SET state=? WHERE id=?", (encode({"changed": True}), environment))
    with pytest.raises(Conflict, match="before transition inputs change"):
        session.resolve(environment, researcher, lease)


def test_v2_runtime_uses_cached_transition_and_succeeded_receipts_without_redispatch(tmp_path, monkeypatch):
    store, session, implementation, researcher, environment, lease, _, _ = _planned_session(
        tmp_path, monkeypatch
    )
    provider = implementation.operations["world.sample"]
    original_resolve = implementation.resolve_transition

    def fail_after_receipts(*_args, **_kwargs):
        raise RuntimeError("synthetic post-receipt interruption")

    implementation.resolve_transition = fail_after_receipts
    with pytest.raises(RuntimeError, match="post-receipt"):
        session.resolve(environment, researcher, lease)
    call_count = len(provider.calls)
    assert call_count == 2
    implementation.resolve_transition = original_resolve

    result, _rng = _direct_resolve_v2(store, session, researcher, environment, lease)
    assert result.state["value"] == 2
    assert len(provider.calls) == call_count
    session.resolve(environment, researcher, lease)

    with store.transaction() as db:
        cached = db.execute(
            "SELECT * FROM transitions WHERE environment=? AND revision=0", (environment,)
        ).fetchone()
    replayed, replay_rng = _direct_resolve_v2(store, session, researcher, environment, lease, cached=cached)
    assert replayed == result
    assert replay_rng.getstate() == tuples(json.loads(cached["rng"]))
    assert len(provider.calls) == call_count


@pytest.mark.parametrize("mutation", ["delete", "fail"])
def test_v2_runtime_rejects_disappeared_or_failed_operation_intents(tmp_path, monkeypatch, mutation):
    store, session, _, researcher, environment, lease, _, _ = _planned_session(tmp_path, monkeypatch)
    original_prepare = Operations.prepare_environment_plan

    def tamper_after_prepare(journal, *args, **kwargs):
        prepared = original_prepare(journal, *args, **kwargs)
        operation_id = prepared[0]["id"]
        with store.transaction() as db:
            if mutation == "delete":
                db.execute("DELETE FROM operations WHERE environment=? AND id=?", (environment, operation_id))
            else:
                db.execute(
                    "UPDATE operations SET status='failed' WHERE environment=? AND id=?",
                    (environment, operation_id),
                )
        return prepared

    monkeypatch.setattr(Operations, "prepare_environment_plan", tamper_after_prepare)
    message = "intent disappeared" if mutation == "delete" else "not safely dispatchable"
    with pytest.raises(Conflict, match=message):
        _direct_resolve_v2(store, session, researcher, environment, lease)


def test_v2_runtime_enforces_aggregate_receipt_limit_after_individual_receipts_settle(tmp_path, monkeypatch):
    operations = tuple(
        OperationRequest(key=f"item-{index}", operation="world.sample", version="1", max_cost_micros=3)
        for index in range(64)
    )
    plan = OperationPlan(plan_id="large-receipts", operations=operations)
    implementation = V2Environment(SampleOperation())
    implementation.spec = implementation.spec.model_copy(
        update={"phase_deadline": "coordinator", "max_transition_operations": 64}
    )
    implementation.plan_transition = lambda *_args: plan
    store, session, implementation, researcher, environment, lease, _, _ = _planned_session(
        tmp_path, monkeypatch, implementation, max_cost_micros=200
    )

    provider = implementation.operations["world.sample"]
    execute = provider.execute

    def padded_receipt(operation_id, request, maximum_cost_micros, *, authority):
        receipt = execute(operation_id, request, maximum_cost_micros, authority=authority)
        return {**receipt, "padding": "x" * 130_719}

    provider.execute = padded_receipt
    with pytest.raises(Conflict, match="8 MiB transition limit"):
        session.resolve(environment, researcher, lease)
    assert len(provider.calls) == 64


def test_non_v2_compute_failure_is_durable_and_remote_transport_mismatch_is_rejected(tmp_path):
    from environment_harness.fixtures import SyntheticEnvironment

    class Broken(SyntheticEnvironment):
        def resolve(self, state, actions, random_source, events):
            raise RuntimeError("synthetic failure")

    implementation = Broken()
    implementation.spec = implementation.spec.model_copy(update={"phase_deadline": "coordinator"})
    session = EnvironmentSession(EvidenceStore(tmp_path), implementation)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    spec = ExperimentSpec(
        environment=implementation.spec,
        participants=(AgentSpec(id="a", implementation="a", policy_version="1"),),
    )
    identity = session.create(spec, researcher)["id"]
    agent = Principal(tenant="tenant", subject="a", role="agent", environment=identity, participant="a")
    observation = session.observe(identity, agent)
    session.submit(
        identity,
        agent,
        Action(
            operation_id="a",
            participant="a",
            observation_id=observation["id"],
            revision=0,
            payload={"value": 1},
        ),
    )
    lease = session.lease(identity, researcher, "worker")
    session.close_phase(identity, researcher, lease, revision=0)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        session.resolve(identity, researcher, lease)
    with session.store.transaction() as db:
        assert (
            db.execute("SELECT status FROM transitions WHERE environment=?", (identity,)).fetchone()["status"]
            == "failed"
        )


def _direct_resolve_v2(
    store,
    session,
    who,
    environment,
    lease,
    *,
    experiment=None,
    cached=None,
    durable_override=None,
    input_hash_override=None,
):
    with store.transaction() as db:
        row = db.execute(
            "SELECT * FROM transitions WHERE environment=? AND revision=0", (environment,)
        ).fetchone()
        durable = durable_override if row is None else json.loads(row["request"])
        manifest = db.execute("SELECT manifest FROM environments WHERE id=?", (environment,)).fetchone()
    request = durable["input"]
    spec = experiment or ExperimentSpec.model_validate_json(manifest["manifest"])
    return session._resolve_v2(
        environment,
        who,
        lease,
        spec,
        request,
        0,
        row["input_hash"] if row is not None else input_hash_override,
        json.loads(request["scheduler"]),
        cached,
        random.Random(),
    )


def test_v2_runtime_rejects_stale_intent_input_and_missing_plan_authority(tmp_path, monkeypatch):
    store, session, _, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    with store.transaction() as db:
        db.execute("UPDATE environments SET state=? WHERE id=?", (encode({"changed": True}), environment))
    with pytest.raises(Conflict, match="inputs changed before planning"):
        _direct_resolve_v2(store, session, researcher, environment, lease)

    store, session, _, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path / "missing-intent", monkeypatch
    )
    with store.transaction() as db:
        db.execute("DELETE FROM transitions WHERE environment=?", (environment,))
    with pytest.raises(Conflict, match="intent is unavailable"):
        _direct_resolve_v2(
            store,
            session,
            researcher,
            environment,
            lease,
            durable_override=persisted,
            input_hash_override=row["input_hash"],
        )

    store, session, _, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path / "hash-mismatch", monkeypatch
    )
    mutated = dict(persisted)
    mutated["input_hash"] = "different"
    with store.transaction() as db:
        db.execute("UPDATE transitions SET request=? WHERE environment=?", (encode(mutated), environment))
    with pytest.raises(Conflict, match="does not match its input hash"):
        _direct_resolve_v2(store, session, researcher, environment, lease)


def test_v2_runtime_no_plan_requires_v2_spec_and_rechecks_planning_authority(tmp_path, monkeypatch):
    store, session, implementation, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    no_plan = {
        key: value
        for key, value in persisted.items()
        if key not in ("plan", "operation_ids", "rng_after_plan")
    }
    with store.transaction() as db:
        db.execute("UPDATE transitions SET request=? WHERE environment=?", (encode(no_plan), environment))
    legacy = implementation.spec.model_dump(mode="python")
    legacy.pop("max_transition_operations")
    legacy["protocol"] = "environment-session.v1"
    legacy["operations"] = tuple(
        OperationSpec(name=item.name, version=item.version) for item in implementation.spec.operations
    )
    legacy_spec = EnvironmentSpec.model_validate(legacy)
    fake_experiment = ExperimentSpec.model_construct(environment=legacy_spec)
    with pytest.raises(Conflict, match="planner is unavailable"):
        _direct_resolve_v2(store, session, researcher, environment, lease, experiment=fake_experiment)

    # Recreate the original intent, then make the lease epoch stale while the
    # supplier computes its plan. The plan itself must never become durable.
    store, session, implementation, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path / "expired", monkeypatch
    )
    no_plan = {
        key: value
        for key, value in persisted.items()
        if key not in ("plan", "operation_ids", "rng_after_plan")
    }
    with store.transaction() as db:
        db.execute("UPDATE transitions SET request=? WHERE environment=?", (encode(no_plan), environment))

    original_plan = implementation.plan_transition

    def expire_authority(state, actions, rng, events):
        with store.transaction() as db:
            db.execute("UPDATE transitions SET lease_epoch=lease_epoch+1 WHERE environment=?", (environment,))
        return original_plan(state, actions, rng, events)

    implementation.plan_transition = expire_authority
    with pytest.raises(Conflict, match="planning authority expired"):
        _direct_resolve_v2(store, session, researcher, environment, lease)


def test_v2_runtime_rechecks_state_during_planning_and_rejects_plan_races(tmp_path, monkeypatch):
    store, session, implementation, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    no_plan = {
        key: value
        for key, value in persisted.items()
        if key not in ("plan", "operation_ids", "rng_after_plan")
    }
    with store.transaction() as db:
        db.execute("UPDATE transitions SET request=? WHERE environment=?", (encode(no_plan), environment))
    original_plan = implementation.plan_transition

    def mutate_state(state, actions, rng, events):
        with store.transaction() as db:
            db.execute("UPDATE environments SET state=? WHERE id=?", (encode({"raced": True}), environment))
        return original_plan(state, actions, rng, events)

    implementation.plan_transition = mutate_state
    with pytest.raises(Conflict, match="changed while planning"):
        _direct_resolve_v2(store, session, researcher, environment, lease)

    store, session, implementation, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path / "plan-race", monkeypatch
    )
    no_plan = {
        key: value
        for key, value in persisted.items()
        if key not in ("plan", "operation_ids", "rng_after_plan")
    }
    with store.transaction() as db:
        db.execute("UPDATE transitions SET request=? WHERE environment=?", (encode(no_plan), environment))
    original_plan = implementation.plan_transition

    def competing_plan(state, actions, rng, events):
        own = original_plan(state, actions, rng, events)
        conflicting = OperationPlan(plan_id="racer", operations=())
        with store.transaction() as db:
            stored = {
                **no_plan,
                "plan": conflicting.model_dump(mode="json"),
                "operation_ids": {},
                "rng_after_plan": rng.getstate(),
            }
            db.execute(
                "UPDATE transitions SET request=?,status='planned' WHERE environment=?",
                (encode(stored), environment),
            )
        return own

    implementation.plan_transition = competing_plan
    with pytest.raises(Conflict, match="different persisted plan"):
        _direct_resolve_v2(store, session, researcher, environment, lease)


def test_v2_runtime_adopts_a_matching_concurrent_persisted_plan(tmp_path, monkeypatch):
    store, session, implementation, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    no_plan = {
        key: value
        for key, value in persisted.items()
        if key not in ("plan", "operation_ids", "rng_after_plan")
    }
    with store.transaction() as db:
        db.execute("UPDATE transitions SET request=? WHERE environment=?", (encode(no_plan), environment))
    original_plan = implementation.plan_transition

    def concurrent_same_plan(state, actions, rng, events):
        plan = original_plan(state, actions, rng, events)
        operation_ids = {
            operation.key: "v2_"
            + digest(
                {
                    "environment": environment,
                    "revision": 0,
                    "input_hash": row["input_hash"],
                    "plan_id": plan.plan_id,
                    "operation": operation.model_dump(mode="json"),
                }
            )[:60]
            for operation in plan.operations
        }
        existing = {
            **no_plan,
            "plan": plan.model_dump(mode="json"),
            "operation_ids": operation_ids,
            "rng_after_plan": rng.getstate(),
        }
        with store.transaction() as db:
            db.execute(
                "UPDATE transitions SET request=?,status='planned' WHERE environment=?",
                (encode(existing), environment),
            )
        return plan

    implementation.plan_transition = concurrent_same_plan
    result, _rng = _direct_resolve_v2(store, session, researcher, environment, lease)
    assert result.state == {"value": 2}
    assert len(implementation.operations["world.sample"].calls) == 2


def test_host_transition_fence_catches_mutated_revision_and_inputs(tmp_path, monkeypatch):
    store, session, _, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    journal = Operations(store)
    plan = OperationPlan.model_validate(persisted["plan"])
    ids = persisted["operation_ids"]
    journal.prepare_environment_plan(
        session,
        environment,
        researcher,
        lease,
        plan=plan,
        operation_ids=ids,
        transition_revision=0,
        transition_hash=row["input_hash"],
    )
    with store.transaction() as db:
        env_row = db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()
        operation = db.execute(
            "SELECT * FROM operations WHERE environment=? AND id=?", (environment, ids["first"])
        ).fetchone()
        request = json.loads(operation["request"])
        original = env_row["state"]
        db.execute("UPDATE environments SET state=? WHERE id=?", (encode({"tampered": 1}), environment))
        current = db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()
        with pytest.raises(Forbidden, match="input generation changed"):
            journal._check_host_transition(db, environment, current, request, operation)
        db.execute("UPDATE environments SET state=? WHERE id=?", (original, environment))

        current = db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()
        db.execute("UPDATE environments SET revision=1 WHERE id=?", (environment,))
        expired = db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()
        with pytest.raises(Forbidden, match="generation expired"):
            journal._check_host_transition(db, environment, expired, request, operation)
        db.execute("UPDATE environments SET revision=0 WHERE id=?", (environment,))

        intent_row = db.execute(
            "SELECT request FROM transitions WHERE environment=?", (environment,)
        ).fetchone()
        intent = json.loads(intent_row["request"])
        intent["input_hash"] = "wrong-hash"
        db.execute("UPDATE transitions SET request=? WHERE environment=?", (encode(intent), environment))
        with pytest.raises(Forbidden, match="identity changed"):
            journal._check_host_transition(
                db,
                environment,
                db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone(),
                request,
                operation,
            )
        intent["input_hash"] = row["input_hash"]
        db.execute("UPDATE transitions SET request=? WHERE environment=?", (encode(intent), environment))

        changed_request = dict(request)
        changed_request["payload"] = {"tampered": 1}
        with pytest.raises(Forbidden, match="differs from the durable plan"):
            journal._check_host_transition(
                db,
                environment,
                db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone(),
                changed_request,
                operation,
            )

        action = db.execute("SELECT id FROM actions WHERE environment=?", (environment,)).fetchone()
        db.execute(
            "UPDATE actions SET request=? WHERE environment=? AND id=?",
            (encode({"payload": {"tampered": 2}}), environment, action["id"]),
        )
        with pytest.raises(Forbidden, match="action generation changed"):
            journal._check_host_transition(
                db,
                environment,
                db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone(),
                request,
                operation,
            )

    # A transition whose durable row disappeared cannot authorize host-side IO.
    with store.transaction() as db:
        db.execute("DELETE FROM transitions WHERE environment=? AND revision=0", (environment,))
        env_row = db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()
        operation = db.execute(
            "SELECT * FROM operations WHERE environment=? AND id=?", (environment, ids["first"])
        ).fetchone()
        request = json.loads(operation["request"])
        with pytest.raises(Forbidden, match="transition is unavailable"):
            journal._check_host_transition(db, environment, env_row, request, operation)


def test_v2_write_capability_requires_frozen_write_entitlement(tmp_path, monkeypatch):
    provider = SampleOperation()
    provider.spec = OperationSpecV2(name="world.sample", version="1", access="write")
    implementation = V2Environment(provider)
    implementation.spec = implementation.spec.model_copy(update={"phase_deadline": "coordinator"})
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, implementation)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    experiment = ExperimentSpec(
        environment=implementation.spec,
        participants=(AgentSpec(id="alice", implementation="a", policy_version="1"),),
        operations=(provider.spec,),
        policy=RunPolicy(
            max_turns=5,
            max_cost_micros=20,
            allowed_endpoints=(ENDPOINT,),
            allowed_operations=(provider.spec.name,),
            external_writes=False,
        ),
    )
    identity = session.create(experiment, researcher)["id"]
    agent = Principal(
        tenant="tenant", subject="alice", role="agent", environment=identity, participant="alice"
    )
    observation = session.observe(identity, agent)
    session.submit(
        identity,
        agent,
        Action(
            operation_id="write",
            participant="alice",
            observation_id=observation["id"],
            revision=0,
            payload={},
        ),
    )
    lease = session.lease(identity, researcher, "worker")
    session.close_phase(identity, researcher, lease, revision=0)
    with monkeypatch.context() as patcher:
        patcher.setattr(
            Operations,
            "prepare_environment_plan",
            lambda *a, **k: (_ for _ in ()).throw(_StopBeforePrepare()),
        )
        with pytest.raises(_StopBeforePrepare):
            session.resolve(identity, researcher, lease)
    with store.transaction() as db:
        transition = db.execute("SELECT * FROM transitions WHERE environment=?", (identity,)).fetchone()
        intent = json.loads(transition["request"])
    with pytest.raises(Forbidden, match="denies environment operation writes"):
        Operations(store).prepare_environment_plan(
            session,
            identity,
            researcher,
            lease,
            plan=OperationPlan.model_validate(intent["plan"]),
            operation_ids=intent["operation_ids"],
            transition_revision=0,
            transition_hash=transition["input_hash"],
        )


def test_prepare_plan_rejects_corrupt_operation_access_class(tmp_path, monkeypatch):
    import environment_harness.operations as operation_module

    store, session, implementation, researcher, environment, lease, row, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    plan = OperationPlan.model_validate(persisted["plan"])
    ids = persisted["operation_ids"]
    with store.transaction() as db:
        manifest = json.loads(
            db.execute("SELECT manifest FROM environments WHERE id=?", (environment,)).fetchone()["manifest"]
        )
        manifest["operations"][0]["access"] = "corrupt"
        db.execute("UPDATE environments SET manifest=? WHERE id=?", (encode(manifest), environment))

    class MalformedExperiment:
        @staticmethod
        def model_validate(_manifest):
            return type(
                "Experiment",
                (),
                {
                    "environment": implementation.spec,
                    "policy": RunPolicy(
                        max_turns=5,
                        max_cost_micros=20,
                        allowed_endpoints=(ENDPOINT,),
                        allowed_operations=("world.sample",),
                    ),
                },
            )()

    provider = implementation.operations["world.sample"]
    provider.spec = type(
        "MalformedSpec",
        (),
        {
            "name": "world.sample",
            "version": "1",
            "model_dump": lambda self, **_kwargs: {
                "name": "world.sample",
                "version": "1",
                "config": {},
                "access": "corrupt",
            },
        },
    )()
    monkeypatch.setattr(operation_module, "ExperimentSpec", MalformedExperiment)
    with pytest.raises(Forbidden, match="access class is missing"):
        Operations(store).prepare_environment_plan(
            session,
            environment,
            researcher,
            lease,
            plan=plan,
            operation_ids=ids,
            transition_revision=0,
            transition_hash=row["input_hash"],
        )


class _PlainProvider:
    endpoint = ENDPOINT

    def execute(self, operation_id, request, maximum_cost_micros):
        return {"operation_id": operation_id, "cost_micros": 0}

    def lookup(self, operation_id):
        return None


class _ParticipantOperation(EnvironmentOperation):
    endpoint = ENDPOINT
    spec = OperationSpec(name="participant.inspect", version="1")

    def __init__(self, store=None, environment=None, mutation=None):
        self.store = store
        self.environment = environment
        self.mutation = mutation
        self.calls = []

    def execute(self, operation_id, request, maximum_cost_micros, *, authority):
        self.calls.append(operation_id)
        if self.mutation:
            with self.store.transaction() as db:
                row = db.execute(
                    "SELECT participants FROM environments WHERE id=?", (self.environment,)
                ).fetchone()
                participants = json.loads(row["participants"])
                if self.mutation == "inactive":
                    participants["alice"]["active"] = False
                else:
                    participants["alice"]["generation"] = 1
                db.execute(
                    "UPDATE environments SET participants=? WHERE id=?",
                    (encode(participants), self.environment),
                )
        authority(request["payload"])
        return {"operation_id": operation_id, "cost_micros": 0}


class _ParticipantOperationEnvironment:
    def __init__(self, provider):
        from environment_harness.fixtures import SyntheticEnvironment

        self.inner = SyntheticEnvironment()
        self.spec = self.inner.spec.model_copy(update={"operations": (provider.spec,)})
        self.operations = {provider.spec.name: provider}

    def initialize(self, experiment):
        return self.inner.initialize(experiment)

    def observe(self, state, participant):
        return self.inner.observe(state, participant)

    def resolve(self, state, actions, rng, events):
        return self.inner.resolve(state, actions, rng, events)

    def intervene(self, state, changes):
        return self.inner.intervene(state, changes)


def _prepared_v1_operation(tmp_path):
    from environment_harness.fixtures import SyntheticEnvironment

    environment_impl = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, environment_impl)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    experiment = ExperimentSpec(
        environment=environment_impl.spec,
        participants=(AgentSpec(id="alice", implementation="a", policy_version="1"),),
        policy=RunPolicy(
            max_turns=5,
            max_cost_micros=20,
            allowed_endpoints=(ENDPOINT,),
            allowed_operations=("plain.call",),
        ),
    )
    identity = session.create(experiment, researcher)["id"]
    agent = Principal(
        tenant="tenant", subject="alice", role="agent", environment=identity, participant="alice"
    )
    Operations(store).prepare(
        identity,
        agent,
        "plain-id",
        endpoint=ENDPOINT,
        operation="plain.call",
        payload={},
    )
    lease = session.lease(identity, researcher, "dispatch-worker")
    return store, session, researcher, identity, lease


def test_dispatch_fences_status_and_provider_endpoint_and_reconcile_capability(tmp_path):
    store, session, researcher, identity, lease = _prepared_v1_operation(tmp_path)
    journal = Operations(store)
    with pytest.raises(Forbidden, match="endpoint does not match"):
        journal.dispatch(
            session,
            identity,
            researcher,
            lease,
            "plain-id",
            provider=type("BadEndpoint", (), {"endpoint": "https://different.invalid"})(),
        )
    with store.transaction() as db:
        db.execute("UPDATE environments SET status='paused' WHERE id=?", (identity,))
    with pytest.raises(Forbidden, match="dispatch authority expired"):
        journal.dispatch(session, identity, researcher, lease, "plain-id", provider=_PlainProvider())

    # A v2 host receipt may only be reconciled through the exact typed provider.
    v2store, v2session, _, who, env, v2lease, intent, persisted = _planned_session(
        tmp_path / "host", pytest.MonkeyPatch()
    )
    v2journal = Operations(v2store)
    v2journal.prepare_environment_plan(
        v2session,
        env,
        who,
        v2lease,
        plan=OperationPlan.model_validate(persisted["plan"]),
        operation_ids=persisted["operation_ids"],
        transition_revision=0,
        transition_hash=intent["input_hash"],
    )
    with pytest.raises(Forbidden, match="differs from the frozen experiment"):
        v2journal.reconcile(env, who, persisted["operation_ids"]["first"], _PlainProvider())


def test_participant_operation_authority_is_rechecked_during_provider_execution(tmp_path):
    for index, mutation in enumerate((None, "inactive", "generation")):
        store = EvidenceStore(tmp_path / str(index))
        provider = _ParticipantOperation()
        implementation = _ParticipantOperationEnvironment(provider)
        session = EnvironmentSession(store, implementation)
        researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
        experiment = ExperimentSpec(
            environment=implementation.spec,
            participants=(AgentSpec(id="alice", implementation="a", policy_version="1"),),
            operations=(provider.spec,),
            policy=RunPolicy(
                max_turns=5,
                max_cost_micros=20,
                allowed_endpoints=(ENDPOINT,),
                allowed_operations=(provider.spec.name,),
            ),
        )
        environment = session.create(experiment, researcher)["id"]
        provider.store = store
        provider.environment = environment
        provider.mutation = mutation
        agent = Principal(
            tenant="tenant", subject="alice", role="agent", environment=environment, participant="alice"
        )
        journal = Operations(store)
        journal.prepare(
            environment,
            agent,
            f"participant-{index}",
            endpoint=ENDPOINT,
            operation=provider.spec.name,
            payload={"read": True},
        )
        lease = session.lease(environment, researcher, "worker")
        if mutation:
            with pytest.raises(Forbidden, match="dispatch authority expired"):
                journal.dispatch(
                    session, environment, researcher, lease, f"participant-{index}", provider=provider
                )
            with store.transaction() as db:
                status = db.execute(
                    "SELECT status FROM operations WHERE environment=? AND id=?",
                    (environment, f"participant-{index}"),
                ).fetchone()["status"]
            assert status == "unknown"
        else:
            receipt = journal.dispatch(
                session, environment, researcher, lease, f"participant-{index}", provider=provider
            )
            assert receipt["cost_micros"] == 0
            assert journal.reconcile(environment, researcher, f"participant-{index}", provider) == receipt


def test_host_dispatch_and_reconcile_recheck_access_class(tmp_path, monkeypatch):
    store, session, implementation, who, env, lease, transition, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    journal = Operations(store)
    ids = persisted["operation_ids"]
    journal.prepare_environment_plan(
        session,
        env,
        who,
        lease,
        plan=OperationPlan.model_validate(persisted["plan"]),
        operation_ids=ids,
        transition_revision=0,
        transition_hash=transition["input_hash"],
    )
    with store.transaction() as db:
        db.execute(
            "UPDATE operations SET request=json_set(request, '$.write', true) WHERE environment=? AND id=?",
            (env, ids["first"]),
        )
    with pytest.raises(Forbidden, match="access differs"):
        journal.dispatch(
            session, env, who, lease, ids["first"], provider=implementation.operations["world.sample"]
        )


def test_settle_and_reconcile_reject_oversize_and_wrong_access_receipts(tmp_path, monkeypatch):
    store, session, implementation, who, env, lease, transition, persisted = _planned_session(
        tmp_path, monkeypatch
    )
    journal = Operations(store)
    plan = OperationPlan.model_validate(persisted["plan"])
    ids = persisted["operation_ids"]
    journal.prepare_environment_plan(
        session,
        env,
        who,
        lease,
        plan=plan,
        operation_ids=ids,
        transition_revision=0,
        transition_hash=transition["input_hash"],
    )
    with pytest.raises(Conflict, match="128 KiB"):
        journal.settle(
            env,
            who,
            ids["first"],
            {"operation_id": f"{env}:{ids['first']}", "cost_micros": 0, "large": "x" * 131_000},
        )

    with store.transaction() as db:
        op = db.execute(
            "SELECT request FROM operations WHERE environment=? AND id=?", (env, ids["first"])
        ).fetchone()
        request = json.loads(op["request"])
        request["write"] = True
        db.execute(
            "UPDATE operations SET request=? WHERE environment=? AND id=?",
            (encode(request), env, ids["first"]),
        )
    with pytest.raises(Forbidden, match="access differs"):
        journal.reconcile(env, who, ids["first"], implementation.operations["world.sample"])

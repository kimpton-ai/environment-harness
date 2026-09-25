import json
from types import SimpleNamespace

import pytest

from environment_harness import (
    AgentSpec,
    EnvironmentHarness,
    EvidenceStore,
    ExperimentSpec,
    presentation,
)
from environment_harness.access import _AccessContext
from environment_harness.contracts import OperationSpec, RunPolicy
from environment_harness.errors import BudgetExceeded, Conflict, Forbidden, Unsupported
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.operations import EnvironmentOperation, Operations, environment_operations
from environment_harness.runtime import _SessionRuntime
from environment_harness.store import encode


def setup(tmp_path):
    store = EvidenceStore(tmp_path)
    environment_impl = SyntheticEnvironment()
    session = _SessionRuntime(store, environment_impl)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    spec = ExperimentSpec(
        environment=environment_impl.spec,
        participants=(AgentSpec(id="a", implementation="synthetic", policy_version="1"),),
        policy=RunPolicy(
            max_cost_micros=10,
            allowed_endpoints=("https://provider.invalid",),
            allowed_operations=("lookup",),
        ),
    )
    environment = session.create(spec, researcher)["id"]
    agent = _AccessContext(
        tenant="tenant", subject="a", policy="participant", session=environment, participant="a"
    )
    return store, session, researcher, environment, agent


def prepare(operations, environment, agent, identifier="operation", **changes):
    arguments = {
        "endpoint": "https://provider.invalid",
        "operation": "lookup",
        "payload": {"synthetic": True},
        "maximum_cost_micros": 5,
    }
    arguments.update(changes)
    return operations.prepare(environment, agent, identifier, **arguments)


def test_prepare_is_idempotent_bounded_and_policy_scoped(tmp_path):
    store, session, researcher, environment, agent = setup(tmp_path)
    operations = Operations(store)
    for identifier, maximum in (("", 1), ("x" * 129, 1), ("negative", -1)):
        with pytest.raises(ValueError, match="invalid operation"):
            prepare(operations, environment, agent, identifier, maximum_cost_micros=maximum)
    with pytest.raises(Forbidden, match="frozen endpoint policy"):
        prepare(operations, environment, agent, endpoint="https://other.invalid")
    with pytest.raises(Forbidden, match="frozen endpoint policy"):
        prepare(operations, environment, agent, operation="delete")
    with pytest.raises(Forbidden, match="external writes"):
        prepare(operations, environment, agent, write=True)
    with pytest.raises(BudgetExceeded):
        prepare(operations, environment, agent, maximum_cost_micros=11)

    assert prepare(operations, environment, agent) == {"id": "operation", "status": "prepared"}
    assert prepare(operations, environment, agent) == {"id": "operation", "status": "prepared"}
    with pytest.raises(Conflict, match="identifier reused"):
        prepare(operations, environment, agent, payload={"changed": True})

    session.cancel(environment, researcher)
    with pytest.raises(Conflict, match="not running"):
        prepare(operations, environment, agent, "after-cancel")


def test_dispatch_and_settle_fail_closed_then_become_idempotent(tmp_path):
    store, session, researcher, environment, agent = setup(tmp_path)
    operations = Operations(store)
    lease = session.lease(environment, researcher, "worker")

    provider = type("Provider", (), {"endpoint": "https://provider.invalid"})()
    with pytest.raises(Forbidden, match="unavailable"):
        operations.dispatch(session, environment, researcher, lease, "missing", provider)

    prepare(operations, environment, agent)
    provider.endpoint = "https://other.invalid"
    with pytest.raises(Forbidden, match="endpoint"):
        operations.dispatch(session, environment, researcher, lease, "operation", provider)
    provider.endpoint = "https://provider.invalid"

    for receipt in (None, {}, {"cost_micros": -1}, {"cost_micros": "one"}):
        with pytest.raises(Conflict, match="integer nonnegative"):
            operations.settle(environment, researcher, "operation", receipt)
    with pytest.raises(Conflict, match="identity mismatch"):
        operations.settle(
            environment,
            researcher,
            "operation",
            {"operation_id": "wrong", "cost_micros": 1},
        )
    with pytest.raises(Forbidden, match="unavailable"):
        operations.settle(
            environment,
            researcher,
            "missing",
            {"operation_id": f"{environment}:missing", "cost_micros": 1},
        )
    with pytest.raises(Conflict, match="not dispatched"):
        operations.settle(
            environment,
            researcher,
            "operation",
            {"operation_id": f"{environment}:operation", "cost_micros": 1},
        )

    class Provider:
        endpoint = "https://provider.invalid"

        def execute(self, operation_id, request, maximum):
            assert request["operation"] == "lookup" and maximum == 5
            return {"operation_id": operation_id, "cost_micros": 2, "result": "synthetic"}

    receipt = operations.dispatch(session, environment, researcher, lease, "operation", Provider())
    assert receipt["result"] == "synthetic"
    assert operations.dispatch(session, environment, researcher, lease, "operation", Provider()) == receipt
    assert operations.settle(environment, researcher, "operation", receipt) == receipt
    with pytest.raises(Conflict, match="conflicting receipts"):
        operations.settle(environment, researcher, "operation", receipt | {"result": "changed"})


def test_environment_package_supplies_custom_operation_without_core_registration(tmp_path):
    class WorldOperation(EnvironmentOperation):
        endpoint = "world"
        spec = OperationSpec(name="world.teleport", version="unreal-1", config={"map": "Arena"})

        def validate(self, request, manifest):
            assert manifest["operations"] == [self.spec.model_dump(mode="json")]
            return request["payload"]

        def execute(self, operation_id, request, maximum, *, authority):
            authority(request["payload"])
            return {"operation_id": operation_id, "cost_micros": 0, "location": request["payload"]}

    operation = WorldOperation()
    environment_impl = SyntheticEnvironment()
    environment_impl.spec = environment_impl.spec.model_copy(update={"operations": (operation.spec,)})
    environment_impl.operations = {operation.spec.name: operation}
    store = EvidenceStore(tmp_path)
    session = _SessionRuntime(store, environment_impl)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    spec = ExperimentSpec(
        environment=environment_impl.spec,
        participants=(AgentSpec(id="a", implementation="synthetic", policy_version="1"),),
        operations=(operation.spec,),
        policy=RunPolicy(allowed_endpoints=("world",), allowed_operations=("world.teleport",)),
    )
    environment = session.create(spec, researcher)["id"]
    agent = _AccessContext(
        tenant="tenant", subject="a", policy="participant", session=environment, participant="a"
    )
    operations = Operations(store)
    operations.prepare(
        environment,
        agent,
        "teleport",
        endpoint="world",
        operation="world.teleport",
        payload={"x": 4, "y": 2},
    )
    lease = session.lease(environment, researcher, "worker")

    frozen = operation.spec
    operation.spec = operation.spec.model_copy(update={"config": {"map": "Other"}})
    with pytest.raises(Forbidden, match="frozen experiment"):
        operations.dispatch(session, environment, researcher, lease, "teleport")
    operation.spec = frozen
    receipt = operations.dispatch(session, environment, researcher, lease, "teleport")

    assert receipt["location"] == {"x": 4, "y": 2}


def test_environment_operation_defaults_and_runtime_contract_validation():
    class WorldOperation(EnvironmentOperation):
        endpoint = "world"
        spec = OperationSpec(name="world.teleport", version="1")

        def execute(self, operation_id, request, maximum_cost_micros, *, authority):
            return {"operation_id": operation_id, "cost_micros": 0}

    operation = WorldOperation()
    assert operation.lookup("missing") is None

    def configured(runtime, *, advertised=(operation.spec,)):
        environment = SyntheticEnvironment()
        environment.spec = environment.spec.model_copy(update={"operations": advertised})
        environment.operations = runtime
        return environment

    for runtime, message in (
        ([], "must be a mapping"),
        ({"world.teleport": operation, "world.extra": operation}, "not advertised"),
        ({"world.teleport": object()}, "must extend EnvironmentOperation"),
    ):
        with pytest.raises(Conflict, match=message):
            environment_operations(configured(runtime))

    changed_identity = WorldOperation()
    changed_identity.spec = changed_identity.spec.model_copy(update={"name": "world.other"})
    with pytest.raises(Conflict, match="advertised identity"):
        environment_operations(configured({"world.teleport": changed_identity}))

    missing_endpoint = WorldOperation()
    missing_endpoint.endpoint = " "
    with pytest.raises(Conflict, match="requires an endpoint"):
        environment_operations(configured({"world.teleport": missing_endpoint}))


def test_operation_supporting_guards_and_presentation(tmp_path):
    with pytest.raises(TypeError, match="session_runner must be callable"):
        EnvironmentHarness(
            tmp_path,
            environment_factory=SyntheticEnvironment,
            agent_factories={"agent": object},
            session_runner=None,
        )
    assert (
        presentation.describe(
            {
                "kind": "operation.dispatched",
                "revision": 1,
                "payload": {"id": "inspect", "epoch": 2},
            }
        )
        == "Operation inspect dispatched under lease epoch 2."
    )


def test_dispatch_rejects_missing_provider_and_stale_or_expired_authority(tmp_path):
    store, session, researcher, environment, agent = setup(tmp_path)
    operations = Operations(store)
    lease = session.lease(environment, researcher, "worker")

    prepare(operations, environment, agent, "missing-provider")
    with pytest.raises(Forbidden, match="environment operation is unavailable"):
        operations.dispatch(session, environment, researcher, lease, "missing-provider")

    class StaleProvider:
        endpoint = "https://provider.invalid"

        def validate(self, request, manifest):
            return SimpleNamespace(goal_revision="stale")

    prepare(operations, environment, agent, "stale")
    with pytest.raises(Conflict, match="goal revision is stale"):
        operations.dispatch(session, environment, researcher, lease, "stale", StaleProvider())

    class ExpiringOperation(EnvironmentOperation):
        endpoint = "https://provider.invalid"
        spec = OperationSpec(name="lookup", version="1")

        def execute(self, operation_id, request, maximum_cost_micros, *, authority):
            with store.transaction() as db:
                row = db.execute(
                    "SELECT participants FROM environments WHERE id=?", (environment,)
                ).fetchone()
                participants = json.loads(row["participants"])
                participants["a"]["active"] = False
                db.execute(
                    "UPDATE environments SET participants=? WHERE id=?",
                    (encode(participants), environment),
                )
            authority(request["payload"])
            return {"operation_id": operation_id, "cost_micros": 0}

    expiring = ExpiringOperation()
    session.environment.spec = session.environment.spec.model_copy(update={"operations": (expiring.spec,)})
    session.environment.operations = {expiring.spec.name: expiring}
    with store.transaction() as db:
        manifest = json.loads(
            db.execute("SELECT manifest FROM environments WHERE id=?", (environment,)).fetchone()[0]
        )
        manifest["operations"] = [expiring.spec.model_dump(mode="json")]
        db.execute("UPDATE environments SET manifest=? WHERE id=?", (encode(manifest), environment))
    prepare(operations, environment, agent, "expired", maximum_cost_micros=0)
    with pytest.raises(Forbidden, match="dispatch authority expired"):
        operations.dispatch(session, environment, researcher, lease, "expired", expiring)


def test_session_rejects_runtime_operation_configuration_drift(tmp_path):
    class WorldOperation(EnvironmentOperation):
        endpoint = "world"
        spec = OperationSpec(name="world.teleport", version="1", config={"map": "runtime"})

        def execute(self, operation_id, request, maximum_cost_micros, *, authority):
            return {"operation_id": operation_id, "cost_micros": 0}

    declared = OperationSpec(name="world.teleport", version="1", config={"map": "frozen"})
    environment = SyntheticEnvironment()
    environment.spec = environment.spec.model_copy(update={"operations": (declared,)})
    environment.operations = {declared.name: WorldOperation()}
    spec = ExperimentSpec(
        environment=environment.spec,
        participants=(AgentSpec(id="a", implementation="synthetic", policy_version="1"),),
        operations=(declared,),
    )

    with pytest.raises(Conflict, match="no matching runtime implementation"):
        _SessionRuntime(EvidenceStore(tmp_path), environment).create(
            spec, _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
        )


def test_experiment_rejects_operations_the_environment_does_not_supply():
    environment = SyntheticEnvironment().spec
    with pytest.raises(ValueError, match="operation is not supplied by the environment"):
        ExperimentSpec(
            environment=environment,
            participants=(AgentSpec(id="a", implementation="synthetic", policy_version="1"),),
            operations=(OperationSpec(name="world.teleport", version="1"),),
        )


def test_session_rejects_missing_runtime_operation(tmp_path):
    environment_impl = SyntheticEnvironment()
    advertised = OperationSpec(name="world.teleport", version="unreal-1")
    environment_impl.spec = environment_impl.spec.model_copy(update={"operations": (advertised,)})
    spec = ExperimentSpec(
        environment=environment_impl.spec,
        participants=(AgentSpec(id="a", implementation="synthetic", policy_version="1"),),
        operations=(advertised,),
    )

    with pytest.raises(Conflict, match="runtime implementation"):
        _SessionRuntime(EvidenceStore(tmp_path), environment_impl).create(
            spec, _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
        )


def test_budget_overrun_reconciliation_and_prepared_cancellation(tmp_path):
    store, session, researcher, environment, agent = setup(tmp_path)
    operations = Operations(store)
    lease = session.lease(environment, researcher, "worker")
    prepare(operations, environment, agent)
    with store.transaction() as db:
        db.execute("UPDATE operations SET status='unknown' WHERE id='operation'")

    class Provider:
        endpoint = "https://provider.invalid"
        receipt = None

        def lookup(self, operation_id):
            return self.receipt

    provider = Provider()
    with pytest.raises(Unsupported, match="cannot prove"):
        operations.reconcile(environment, researcher, "operation", provider)
    provider.endpoint = "https://other.invalid"
    with pytest.raises(Forbidden, match="endpoint mismatch"):
        operations.reconcile(environment, researcher, "operation", provider)
    provider.endpoint = "https://provider.invalid"
    provider.receipt = {"operation_id": f"{environment}:operation", "cost_micros": 6}
    with pytest.raises(BudgetExceeded, match="exceeded"):
        operations.reconcile(environment, researcher, "operation", provider)
    provider.receipt["cost_micros"] = 3
    assert operations.reconcile(environment, researcher, "operation", provider) == provider.receipt
    assert operations.reconcile(environment, researcher, "operation", provider) == provider.receipt

    with pytest.raises(Forbidden, match="unavailable"):
        operations.reconcile(environment, researcher, "missing", provider)
    prepare(operations, environment, agent, "prepared")
    with pytest.raises(Conflict, match="only dispatched"):
        operations.reconcile(environment, researcher, "prepared", provider)
    prepare(operations, environment, agent, "cancel-me", maximum_cost_micros=1)
    assert operations.cancel_prepared(environment, researcher)["cancelled"] == 2
    with store.transaction() as db:
        statuses = {
            row["id"]: row["status"]
            for row in db.execute("SELECT id,status FROM operations WHERE environment=?", (environment,))
        }
        assert json.loads(db.execute("SELECT manifest FROM environments").fetchone()[0])["policy"]
    assert statuses["prepared"] == statuses["cancel-me"] == "failed"
    session.release(environment, researcher, lease)

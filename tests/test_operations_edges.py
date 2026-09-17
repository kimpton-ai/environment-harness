import json

import pytest

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.contracts import RunPolicy
from environment_harness.errors import BudgetExceeded, Conflict, Forbidden, Unsupported
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.operations import Operations


def setup(tmp_path):
    store = EvidenceStore(tmp_path)
    environment_impl = SyntheticEnvironment()
    session = EnvironmentSession(store, environment_impl)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
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
    agent = Principal(tenant="tenant", subject="a", role="agent", environment=environment, participant="a")
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

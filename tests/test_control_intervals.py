import pytest

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec
from environment_harness.contracts import Principal, RunPolicy, Transition
from environment_harness.errors import Conflict, Forbidden
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.operations import EnvironmentOperation, Operations
from environment_harness.store import digest


class StepOperation(EnvironmentOperation):
    endpoint = "simulator"

    def __init__(self, run):
        from environment_harness.contracts import OperationSpec

        self.spec = OperationSpec(name="spatial.step", version="3")
        self.run = run
        self.calls = 0

    def execute(self, operation_id, request, maximum_cost_micros, *, authority):
        self.calls += 1
        authority(request["payload"])
        return self.run(operation_id, request)


class StepEnvironment(SyntheticEnvironment):
    def __init__(self, operation):
        super().__init__("sequential")
        self.operations = {operation.spec.name: operation}
        self.spec = self.spec.model_copy(update={"operations": (operation.spec,)})

    def resolve(self, state, actions, random, events):
        return Transition(state=state)


def setup(tmp_path, *, operation_run):
    store = EvidenceStore(tmp_path)
    researcher = Principal(tenant="local", subject="researcher", role="researcher")
    saved = {}

    def execute(operation_id, request):
        return operation_run(saved, operation_id, request)

    provider = StepOperation(execute)
    environment_impl = StepEnvironment(provider)
    session = EnvironmentSession(store, environment_impl)
    spec = ExperimentSpec(
        environment=environment_impl.spec,
        participants=(AgentSpec(id="robot", implementation="robot@1", policy_version="1"),),
        operations=(provider.spec,),
        policy=RunPolicy(allowed_endpoints=(provider.endpoint,), allowed_operations=(provider.spec.name,)),
    )
    environment = session.create(spec, researcher)["id"]
    lease = session.lease(environment, researcher, "worker", ttl=60)
    agent = session.transfer(environment, researcher, lease, "robot", "controller-1")
    start_data = b"start checkpoint v1"
    start = store.artifact(environment, researcher, start_data)
    op_id = "step-operation-1"
    Operations(store).prepare(
        environment,
        agent,
        op_id,
        endpoint=provider.endpoint,
        operation=provider.spec.name,
        payload={"command": "step"},
    )
    saved.update(
        session=session,
        store=store,
        researcher=researcher,
        environment=environment,
        lease=lease,
        controller=agent,
        operation_id=op_id,
        start={"artifact_id": start["id"], "sha256": start["sha256"]},
        provider=provider,
    )
    return saved


def begin(saved, *, seconds=1.0, ttl=30):
    session = saved["session"]
    environment = saved["environment"]
    researcher = saved["researcher"]
    lease = saved["lease"]
    interval = session.prepare_control_interval(
        environment,
        researcher,
        lease,
        interval_id="interval-1",
        operation_id=saved["operation_id"],
        runtime_identity={"engine": "fixture", "version": "1", "profile": "cpu"},
        start_checkpoint=saved["start"],
        simulated_seconds=seconds,
    )
    grant = session.grant_control(
        environment,
        researcher,
        lease,
        interval_id=interval["interval_id"],
        grant_id=interval["grant_id"],
        controller="controller-1",
        participant="robot",
        ttl=ttl,
    )
    saved.update(interval=interval, grant=grant)


def finish(saved, receipt_holder, *, lose_response=False, replace_worker=False):
    session = saved["session"]
    store = saved["store"]
    researcher = saved["researcher"]
    environment = saved["environment"]
    lease = saved["lease"]
    end_data = b"end checkpoint v1"
    end = store.artifact(environment, researcher, end_data)
    end_ref = {"artifact_id": end["id"], "sha256": end["sha256"]}

    def execute(operation_id, request):
        receipt = {
            "operation_id": f"{environment}:{saved['operation_id']}",
            "cost_micros": 0,
            "result": "advanced",
        }
        batch = receipt_holder["ack"]
        log_digest = digest(
            [{"sequence": batch["sequence"], "batch_id": batch["batch_id"], "input_hash": batch["input_hash"]}]
        )
        session.seal_control_interval(
            environment,
            researcher,
            lease,
            interval_id="interval-1",
            end_checkpoint=end_ref,
            measurements={"simulated_seconds": 1.0},
            control_log_digest=log_digest,
        )
        if lose_response:
            raise TimeoutError("provider committed but response was lost")
        if replace_worker:
            session.release(environment, researcher, lease)
        return receipt

    saved["provider"].run = execute
    saved["end"] = end_ref
    receipt = Operations(store).dispatch(
        session, environment, researcher, lease, saved["operation_id"], provider=saved["provider"]
    )
    return receipt


def test_interval_inputs_are_fenced_durable_and_receipt_recovery_does_not_reexecute(tmp_path):
    saved = setup(tmp_path, operation_run=lambda *_: None)
    session = saved["session"]
    environment = saved["environment"]
    with pytest.raises(ValueError, match="at most one second"):
        begin(saved, seconds=1.01)

    begin(saved)
    ack = session.accept_control_inputs(
        environment,
        saved["controller"],
        interval_id="interval-1",
        grant_id=saved["grant"]["grant_id"],
        batch_id="batch-1",
        sequence=1,
        inputs={"joint_targets": [0.25, -0.5]},
    )
    assert ack["status"] == "accepted" and ack["provisional"] is True
    assert session.accept_control_inputs(
        environment,
        saved["controller"],
        interval_id="interval-1",
        grant_id=saved["grant"]["grant_id"],
        batch_id="batch-1",
        sequence=1,
        inputs={"joint_targets": [0.25, -0.5]},
    ) == ack
    assert not any(
        event["kind"] == "control.input_accepted"
        for event in session.store.events(environment, saved["controller"])
    )
    with pytest.raises(Conflict, match="identifier reused"):
        session.accept_control_inputs(
            environment,
            saved["controller"],
            interval_id="interval-1",
            grant_id=saved["grant"]["grant_id"],
            batch_id="batch-1",
            sequence=1,
            inputs={"joint_targets": [0.75]},
        )
    with pytest.raises(Conflict, match="sequence"):
        session.accept_control_inputs(
            environment,
            saved["controller"],
            interval_id="interval-1",
            grant_id=saved["grant"]["grant_id"],
            batch_id="batch-gap",
            sequence=3,
            inputs={"joint_targets": [0.1]},
        )
    unauthorized = Principal(
        tenant="local", subject="other-controller", role="worker", environment=environment
    )
    with pytest.raises(Forbidden, match="grant"):
        session.accept_control_inputs(
            environment,
            unauthorized,
            interval_id="interval-1",
            grant_id=saved["grant"]["grant_id"],
            batch_id="batch-unauthorized",
            sequence=2,
            inputs={"joint_targets": [0.1]},
        )

    holder = {"ack": ack}
    receipt = finish(saved, holder)
    recovered = session.recover_control_interval(environment, saved["researcher"], "interval-1")
    assert recovered["status"] == "committed"
    assert recovered["receipt"]["operation_receipt"] == receipt
    assert recovered["receipt"]["provisional"] is False
    assert saved["provider"].calls == 1

    with pytest.raises(Conflict, match="differs from committed"):
        session.commit_control_interval(
            environment,
            saved["researcher"],
            saved["lease"],
            interval_id="interval-1",
            operation_receipt={**receipt, "result": "different"},
        )


def test_expired_grant_and_unknown_operation_stop_without_reexecution(tmp_path):
    saved = setup(tmp_path, operation_run=lambda *_: None)
    begin(saved, ttl=30)
    with saved["store"].transaction() as db:
        db.execute(
            "UPDATE control_grants SET expires=0 WHERE environment=? AND id=?",
            (saved["environment"], saved["grant"]["grant_id"]),
        )
    with pytest.raises(Forbidden, match="expired"):
        saved["session"].accept_control_inputs(
            saved["environment"],
            saved["controller"],
            interval_id="interval-1",
            grant_id=saved["grant"]["grant_id"],
            batch_id="batch-expired",
            sequence=1,
            inputs={"joint_targets": [0]},
        )

    # Restore a valid grant and persist a batch that the provider then applies once.
    with saved["store"].transaction() as db:
        db.execute(
            "UPDATE control_grants SET expires=? WHERE environment=? AND id=?",
            (saved["lease"]["expires"], saved["environment"], saved["grant"]["grant_id"]),
        )
    ack = saved["session"].accept_control_inputs(
        saved["environment"],
        saved["controller"],
        interval_id="interval-1",
        grant_id=saved["grant"]["grant_id"],
        batch_id="batch-1",
        sequence=1,
        inputs={"joint_targets": [0.2]},
    )
    with pytest.raises(TimeoutError, match="response was lost"):
        finish(saved, {"ack": ack}, lose_response=True)
    recovered = saved["session"].recover_control_interval(
        saved["environment"], saved["researcher"], "interval-1"
    )
    assert recovered["status"] == "recovery_required"
    assert recovered["branch_required"] is True
    assert recovered["provider_reexecution_allowed"] is False
    assert recovered["operation_status"] == "unknown"
    assert recovered["last_committed_checkpoint"] == saved["start"]
    assert saved["provider"].calls == 1


def test_replaced_worker_cannot_accept_inputs_under_an_old_controller_grant(tmp_path):
    saved = setup(tmp_path, operation_run=lambda *_: None)
    begin(saved)
    saved["session"].release(saved["environment"], saved["researcher"], saved["lease"])
    replacement = saved["session"].lease(saved["environment"], saved["researcher"], "replacement-worker")
    with pytest.raises(Conflict, match="stale or expired"):
        saved["session"].accept_control_inputs(
            saved["environment"],
            saved["controller"],
            interval_id="interval-1",
            grant_id=saved["grant"]["grant_id"],
            batch_id="stale-batch",
            sequence=1,
            inputs={"joint_targets": [0]},
        )
    assert replacement["epoch"] > saved["lease"]["epoch"]


def test_replacement_worker_commits_only_the_settled_receipt(tmp_path):
    saved = setup(tmp_path, operation_run=lambda *_: None)
    begin(saved)
    ack = saved["session"].accept_control_inputs(
        saved["environment"],
        saved["controller"],
        interval_id="interval-1",
        grant_id=saved["grant"]["grant_id"],
        batch_id="batch-1",
        sequence=1,
        inputs={"joint_targets": [0.2]},
    )
    with pytest.raises(Conflict, match="fenced"):
        finish(saved, {"ack": ack}, replace_worker=True)
    replacement = saved["session"].lease(
        saved["environment"], saved["researcher"], "replacement-worker"
    )
    recovered = saved["session"].recover_control_interval(
        saved["environment"],
        saved["researcher"],
        "interval-1",
        lease=replacement,
    )
    assert recovered["status"] == "committed"
    assert recovered["receipt"]["operation_receipt"]["result"] == "advanced"
    assert saved["provider"].calls == 1

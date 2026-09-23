import json
import random

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from environment_harness import (
    AgentSpec,
    EnvironmentSession,
    EnvironmentSpecV2,
    EvidenceStore,
    ExperimentSpec,
    OperationPlan,
    OperationReceipt,
    OperationRequest,
    OperationSpecV2,
    Principal,
)
from environment_harness.adapters.remote import PROTOCOL_V2, RemoteEnvironment
from environment_harness.contracts import Action, Capabilities, OperationSpec, RunPolicy, Transition
from environment_harness.errors import BudgetExceeded, Conflict, Forbidden, Unsupported
from environment_harness.operations import EnvironmentOperation
from environment_harness.server import create_app
from environment_harness.worker_server import create_worker_app

TOKEN = "synthetic-v2-worker-credential-long-enough"
ENDPOINT = "https://provider.invalid"


class SampleOperation(EnvironmentOperation):
    endpoint = ENDPOINT

    def __init__(self, *, lose_first_response=False):
        self.spec = OperationSpecV2(name="world.sample", version="1", access="read")
        self.calls = []
        self.lookups = []
        self.receipts = {}
        self.lose_first_response = lose_first_response

    def execute(self, operation_id, request, maximum_cost_micros, *, authority):
        authority(request["payload"])
        self.calls.append((operation_id, request, maximum_cost_micros))
        dependencies = request.get("dependency_receipts", {})
        value = request["payload"].get("value", 0) + sum(
            receipt["value"] for receipt in dependencies.values()
        )
        receipt = {"operation_id": operation_id, "cost_micros": 3, "value": value}
        self.receipts[operation_id] = receipt
        if self.lose_first_response:
            self.lose_first_response = False
            raise TimeoutError("receipt delivery was lost")
        return receipt

    def lookup(self, operation_id):
        self.lookups.append(operation_id)
        return self.receipts.get(operation_id)


class V2Environment:
    def __init__(self, operation=None):
        self.operation = operation
        self.operations = {operation.spec.name: operation} if operation else {}
        self.spec = EnvironmentSpecV2(
            id="synthetic-adaptive-world",
            version="2",
            implementation="synthetic-adaptive-world@2",
            scheduling="sequential",
            operations=(operation.spec,) if operation else (),
            capabilities=Capabilities(checkpoint=True, resume=True),
            action_schema={"type": "object"},
            max_transition_operations=4,
        )

    def initialize(self, experiment):
        return {"value": 0}

    def observe(self, state, participant):
        return {"value": state["value"]}

    def intervene(self, state, changes):
        return {**state, **changes}

    def plan_transition(self, state, actions, random_source, events):
        return OperationPlan(
            plan_id="phase-plan-1",
            operations=(
                OperationRequest(
                    key="first",
                    operation="world.sample",
                    version="1",
                    payload={"value": 2},
                    max_cost_micros=8,
                ),
                OperationRequest(
                    key="second",
                    operation="world.sample",
                    version="1",
                    payload={"value": 4},
                    max_cost_micros=8,
                    depends_on=("first",),
                ),
            )
            if self.operation
            else (),
            continuation={"events_seen": len(events)},
        )

    def resolve_transition(self, state, actions, random_source, events, plan, receipts):
        sampled = next(iter(receipts.values())).receipt["value"] if receipts else 1
        return Transition(
            state={"value": state["value"] + sampled},
            outcomes={participant: {"sampled": sampled} for participant in actions},
        )


def test_operation_plan_rejects_bad_dependencies_and_extra_authority_fields():
    first = OperationRequest(key="first", operation="world.sample", version="1")
    second = OperationRequest(key="second", operation="world.sample", version="1", depends_on=("first",))
    with pytest.raises(ValidationError, match="acyclic"):
        OperationPlan(
            plan_id="loop", operations=(first.model_copy(update={"depends_on": ("second",)}), second)
        )
    with pytest.raises(ValidationError, match="dependency"):
        OperationPlan(plan_id="missing", operations=(second,))
    with pytest.raises(ValidationError):
        OperationRequest(
            key="forbidden",
            operation="world.sample",
            version="1",
            payload={},
            participant="owner",
        )
    with pytest.raises(ValidationError):
        OperationRequest(key="boolean-budget", operation="world.sample", version="1", max_cost_micros=True)
    with pytest.raises(ValidationError, match="1 MiB"):
        OperationPlan(
            plan_id="too-large",
            operations=(
                OperationRequest(
                    key="large",
                    operation="world.sample",
                    version="1",
                    payload={"value": "x" * 1_048_576},
                ),
            ),
        )


def test_v2_operation_access_is_frozen_and_unselected_plan_cannot_dispatch(tmp_path):
    provider = SampleOperation()
    implementation = V2Environment(provider)
    participant = AgentSpec(id="alice", implementation="agent@1", policy_version="1")
    with pytest.raises(ValidationError, match="exactly match"):
        ExperimentSpec(
            environment=implementation.spec,
            participants=(participant,),
            operations=(OperationSpec(name=provider.spec.name, version=provider.spec.version),),
        )
    with pytest.raises(ValidationError, match="exactly match"):
        ExperimentSpec(
            environment=implementation.spec,
            participants=(participant,),
            operations=(provider.spec.model_copy(update={"access": "write"}),),
        )

    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, implementation)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    experiment = ExperimentSpec(
        environment=implementation.spec,
        participants=(participant,),
        policy=RunPolicy(
            max_turns=10,
            max_cost_micros=20,
            allowed_endpoints=(ENDPOINT,),
            allowed_operations=(provider.spec.name,),
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
            operation_id="action-unselected",
            participant="alice",
            observation_id=observation["id"],
            revision=0,
            payload={},
        ),
    )
    lease = session.lease(environment, researcher, "unselected-worker")
    with pytest.raises(Forbidden, match="outside the frozen environment contract"):
        session.resolve(environment, researcher, lease)
    assert provider.calls == []
    with store.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM operations WHERE environment=?", (environment,)).fetchone()[0]
            == 0
        )


def test_v2_runtime_journals_plan_and_reconciles_unknown_without_redispatch(tmp_path):
    provider = SampleOperation(lose_first_response=True)
    implementation = V2Environment(provider)
    implementation.spec = implementation.spec.model_copy(update={"phase_deadline": "coordinator"})
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, implementation)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    operation_spec = provider.spec
    experiment = ExperimentSpec(
        environment=implementation.spec,
        participants=(AgentSpec(id="alice", implementation="agent@1", policy_version="1"),),
        operations=(operation_spec,),
        policy=RunPolicy(
            max_turns=10,
            max_cost_micros=20,
            allowed_endpoints=(ENDPOINT,),
            allowed_operations=(operation_spec.name,),
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
            operation_id="action-1",
            participant="alice",
            observation_id=observation["id"],
            revision=0,
            payload={},
        ),
    )
    lease = session.lease(environment, researcher, "v2-worker")
    session.close_phase(environment, researcher, lease, revision=0)

    with pytest.raises(TimeoutError, match="lost"):
        session.resolve(environment, researcher, lease)
    with pytest.raises(Conflict, match="pending reconciliation"):
        session.memory(environment, agent, {"changed": True})
    with pytest.raises(Conflict, match="pending reconciliation"):
        session.close_phase(environment, researcher, lease, revision=0)
    with pytest.raises(Conflict, match="pending reconciliation"):
        session.external_event(
            environment,
            researcher,
            lease,
            source="synthetic-feed",
            cursor=1,
            event_time=1.0,
            payload={"value": 1},
        )
    with pytest.raises(Conflict, match="pending reconciliation"):
        session.transfer(environment, researcher, lease, "alice", "replacement-controller")
    assert session.control(environment, researcher, lease, "pause")["status"] == "paused"
    assert session.resume(environment, researcher, lease)["status"] == "running"

    with store.transaction() as db:
        db.execute("UPDATE environments SET lease_until=0 WHERE id=?", (environment,))
    replacement_lease = session.lease(environment, researcher, "recovery-worker")
    with pytest.raises(Conflict, match="fenced"):
        session.resolve(environment, researcher, lease)
    assert len(provider.calls) == 1
    assert provider.lookups == []
    lease = replacement_lease
    first_operation_call = provider.calls[0][0]
    first_receipt = provider.receipts.pop(first_operation_call)
    with pytest.raises(Unsupported, match="cannot prove outcome"):
        session.resolve(environment, researcher, lease)
    assert len(provider.calls) == 1
    assert provider.lookups == [first_operation_call]
    assert session.get(environment, researcher)["reserved_micros"] == 16
    provider.receipts[first_operation_call] = first_receipt

    with store.transaction() as db:
        intent = db.execute("SELECT * FROM transitions WHERE environment=?", (environment,)).fetchone()
        persisted = json.loads(intent["request"])
        operation_rows = db.execute(
            "SELECT * FROM operations WHERE environment=? ORDER BY id", (environment,)
        ).fetchall()
        assert intent["status"] == "planned"
        assert persisted["input_hash"] == intent["input_hash"]
        assert persisted["plan"]["operations"][1]["depends_on"] == ["first"]
        assert len(operation_rows) == 2
        assert {row["participant"] for row in operation_rows} == {"@environment"}
        assert {row["generation"] for row in operation_rows} == {0}
        stable_ids = persisted["operation_ids"].copy()
        action_record = persisted["input"]["action_records"][0]
        action_row = db.execute(
            "SELECT request FROM actions WHERE environment=? AND id=?",
            (environment, action_record["id"]),
        ).fetchone()
        db.execute(
            "UPDATE actions SET request=? WHERE environment=? AND id=?",
            (json.dumps({"payload": {"tampered": True}}), environment, action_record["id"]),
        )
        with pytest.raises(Conflict, match="actions changed after planning"):
            session._assert_v2_action_records(db, environment, 0, persisted["input"]["action_records"])
        db.execute(
            "UPDATE actions SET request=? WHERE environment=? AND id=?",
            (action_row["request"], environment, action_record["id"]),
        )

    resolved = session.resolve(environment, researcher, lease)
    assert resolved["revision"] == 1
    assert len(provider.calls) == 2
    assert provider.lookups == [first_operation_call, first_operation_call]
    assert provider.calls[1][1]["dependency_receipts"]["first"]["value"] == 2
    assert provider.calls[0][0] == f"{environment}:{stable_ids['first']}"
    assert provider.calls[1][0] == f"{environment}:{stable_ids['second']}"
    assert session.get(environment, researcher)["spent_micros"] == 6
    assert session.get(environment, researcher)["reserved_micros"] == 0
    assert OperationReceipt.model_validate(
        {
            "key": "first",
            "operation_id": provider.calls[0][0],
            "operation": "world.sample",
            "version": "1",
            "receipt": provider.receipts[provider.calls[0][0]],
            "cost_micros": 3,
        }
    )
    first_close = session.close_phase(environment, researcher, lease, revision=1)
    second_close = session.close_phase(environment, researcher, lease, revision=1)
    assert first_close == second_close == {"revision": 1, "closed": True}


def test_v2_plan_reserves_all_requests_atomically_within_the_frozen_budget(tmp_path):
    provider = SampleOperation()
    implementation = V2Environment(provider)
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, implementation)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    spec = ExperimentSpec(
        environment=implementation.spec,
        participants=(AgentSpec(id="alice", implementation="agent@1", policy_version="1"),),
        operations=(provider.spec,),
        policy=RunPolicy(
            max_turns=10,
            max_cost_micros=15,
            allowed_endpoints=(ENDPOINT,),
            allowed_operations=(provider.spec.name,),
        ),
    )
    environment = session.create(spec, researcher)["id"]
    agent = Principal(
        tenant="tenant", subject="alice", role="agent", environment=environment, participant="alice"
    )
    observation = session.observe(environment, agent)
    session.submit(
        environment,
        agent,
        Action(
            operation_id="action-budget",
            participant="alice",
            observation_id=observation["id"],
            revision=0,
            payload={},
        ),
    )
    lease = session.lease(environment, researcher, "budget-worker")
    with pytest.raises(BudgetExceeded, match="plan exceeds"):
        session.resolve(environment, researcher, lease)
    assert provider.calls == []
    assert session.get(environment, researcher)["reserved_micros"] == 0
    with store.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM operations WHERE environment=?", (environment,)).fetchone()[0]
            == 0
        )


def test_v2_remote_worker_path_round_trips_typed_plans_and_receipts():
    implementation = V2Environment(SampleOperation())
    app = create_worker_app(implementation, TOKEN)
    client = TestClient(app)
    assert (
        client.get("/health", headers={"Authorization": "Bearer " + TOKEN}).json()["protocol"] == PROTOCOL_V2
    )
    assert (
        client.post(
            "/v1/worker/call",
            json={"protocol": "environment-worker.v1", "id": "a" * 32, "method": "spec", "arguments": {}},
            headers={"Authorization": "Bearer " + TOKEN},
        ).status_code
        == 404
    )

    def send(body):
        response = client.post(
            "/v2/worker/call",
            json=body,
            headers={"Authorization": "Bearer " + TOKEN},
        )
        return response.json()

    remote = RemoteEnvironment(send, expected_spec=implementation.spec)
    rng = random.Random(7)
    plan = remote.plan_transition({"value": 0}, {"alice": {}}, rng, [])
    receipts = {
        key: OperationReceipt(
            key=key,
            operation_id=f"stable-{key}",
            operation="world.sample",
            version="1",
            receipt={"operation_id": f"stable-{key}", "cost_micros": 0, "value": 5},
            cost_micros=0,
        )
        for key in ("first", "second")
    }
    result = remote.resolve_transition(
        {"value": 0},
        {"alice": {}},
        rng,
        [],
        plan,
        receipts,
    )
    assert isinstance(plan, OperationPlan)
    assert result.state == {"value": 5}


def test_v2_session_uses_existing_authenticated_api_routes(tmp_path):
    implementation = V2Environment()
    store = EvidenceStore(tmp_path)
    session = EnvironmentSession(store, implementation)
    researcher = Principal(tenant="tenant", subject="researcher", role="researcher")
    experiment = ExperimentSpec(
        environment=implementation.spec,
        participants=(AgentSpec(id="alice", implementation="agent@1", policy_version="1"),),
    )
    client = TestClient(create_app(session))
    researcher_headers = {"Authorization": "Bearer " + store.issue(researcher)}
    spec_response = client.get("/v1/environment", headers=researcher_headers)
    assert spec_response.status_code == 200
    assert spec_response.json()["protocol"] == "environment-session.v2"

    created = client.post(
        "/v1/environments",
        json=experiment.model_dump(mode="json"),
        headers={**researcher_headers, "X-Operation-ID": "a" * 32},
    )
    assert created.status_code == 200
    environment = created.json()["id"]
    agent = Principal(
        tenant="tenant", subject="alice", role="agent", environment=environment, participant="alice"
    )
    agent_headers = {"Authorization": "Bearer " + store.issue(agent)}
    observation = client.get(f"/v1/environments/{environment}/observation", headers=agent_headers).json()
    action = client.post(
        f"/v1/environments/{environment}/actions",
        json={
            "operation_id": "http-action",
            "participant": "alice",
            "observation_id": observation["id"],
            "revision": 0,
            "payload": {},
        },
        headers=agent_headers,
    )
    assert action.status_code == 200
    lease = client.post(
        f"/v1/environments/{environment}/commands",
        json={"operation": "lease", "arguments": {"owner": "api-worker"}},
        headers=researcher_headers,
    ).json()
    resolved = client.post(
        f"/v1/environments/{environment}/commands",
        json={"operation": "resolve", "arguments": {"lease": lease}},
        headers=researcher_headers,
    )
    assert resolved.status_code == 200
    assert resolved.json()["revision"] == 1

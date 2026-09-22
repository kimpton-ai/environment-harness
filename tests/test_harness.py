from threading import Event, Lock, current_thread

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from environment_harness import (
    AgentSpec,
    EnvironmentHarness,
    EnvironmentOperation,
    EnvironmentSession,
    ExperimentResult,
    ExperimentSpec,
    OperationSpec,
    Principal,
    Scenario,
)
from environment_harness.contracts import Capabilities, EnvironmentSpec, Transition
from environment_harness.errors import Conflict, Forbidden
from environment_harness.operations import Operations
from environment_harness.runner import run as run_session
from environment_harness.server import create_app

SESSION_COMPLETION_TIMEOUT = 10


class ScenarioInput(BaseModel):
    difficulty: int


def test_typed_scenario_serializes_and_validates_input():
    scenario = Scenario[ScenarioInput](
        id="addition-1",
        input={"difficulty": 3},
        reference={"answer": 4},
        metadata={"split": "heldout"},
    )

    assert scenario.model_dump(mode="json") == {
        "id": "addition-1",
        "input": {"difficulty": 3},
        "reference": {"answer": 4},
        "metadata": {"split": "heldout"},
    }
    assert scenario.input.difficulty == 3


class ScenarioEnvironment:
    scenario_type = ScenarioInput
    spec = EnvironmentSpec(
        id="scenario-environment",
        version="1",
        implementation="scenario-environment@1",
        scheduling="simultaneous",
        capabilities=Capabilities(checkpoint=True, resume=True),
        missing_action="noop",
    )

    def initialize(self, experiment):
        return {"turn": 0, "difficulty": experiment.scenario_input["difficulty"]}

    def observe(self, state, participant):
        return state

    def resolve(self, state, actions, random, events):
        return Transition(state={**state, "turn": state["turn"] + 1})

    def intervene(self, state, changes):
        return state | changes


class ScenarioAgent:
    implementation = "scenario-agent@1"
    policy_version = "1"

    def act(self, observation):
        return {}


def test_harness_runs_a_standalone_environment_session(tmp_path):
    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=ScenarioEnvironment,
        agent_factories={"agent": ScenarioAgent},
    )

    session = harness.run(Scenario(id="standalone", input={"difficulty": 2}), turns=1)

    assert isinstance(session, EnvironmentSession)
    assert session.status == "succeeded"
    assert session.scenario_id == "standalone"
    assert session.experiment_id is None


def test_harness_rejects_a_session_runner_that_does_not_return_the_current_record(tmp_path):
    def invalid_runner(session, environment, researcher, agents, *, turns):
        return {"status": "completed"}

    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=ScenarioEnvironment,
        agent_factories={"agent": ScenarioAgent},
        session_runner=invalid_runner,
    )

    session = harness.run(Scenario(id="invalid-runner", input={"difficulty": 2}), turns=1)

    assert session.status == "failed"
    record = next(
        item
        for item in harness.store.activity_snapshot(harness.researcher)["standalone"]
        if item["id"] == session.id
    )
    assert record["failure"] == "Conflict"


def test_harness_freezes_environment_supplied_operation_specs(tmp_path):
    class InspectOperation(EnvironmentOperation):
        endpoint = "world"
        spec = OperationSpec(name="world.inspect", version="unreal-1", config={"level": "Arena"})

        def execute(self, operation_id, request, maximum_cost_micros, *, authority):
            authority(request["payload"])
            return {"operation_id": operation_id, "cost_micros": 0}

    class OperableEnvironment(ScenarioEnvironment):
        def __init__(self):
            operation = InspectOperation()
            self.operations = {operation.spec.name: operation}
            self.spec = ScenarioEnvironment.spec.model_copy(
                update={
                    "operations": (OperationSpec(name=operation.spec.name, version=operation.spec.version),)
                }
            )

    def run_with_inspection(session, environment, researcher, agents, *, turns):
        run_session(session, environment, researcher, agents, turns=1)
        agent = Principal(
            tenant=researcher.tenant,
            subject="agent",
            role="agent",
            environment=environment,
            participant="agent",
        )
        operations = Operations(session.store)
        operations.prepare(
            environment,
            agent,
            "inspect",
            endpoint="world",
            operation="world.inspect",
            payload={"location": "Arena"},
        )
        lease = session.lease(environment, researcher, "example-operation")
        try:
            operations.dispatch(session, environment, researcher, lease, "inspect")
        finally:
            session.release(environment, researcher, lease)
        return run_session(session, environment, researcher, agents, turns=turns - 1)

    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=OperableEnvironment,
        agent_factories={"agent": ScenarioAgent},
        session_runner=run_with_inspection,
    )

    session = harness.run(Scenario(id="operable", input={"difficulty": 2}), turns=2)

    manifest = session.get(session.id, harness.researcher)["experiment"]
    assert manifest["operations"] == [InspectOperation.spec.model_dump(mode="json")]
    assert manifest["policy"]["allowed_endpoints"] == ["world"]
    assert manifest["policy"]["allowed_operations"] == ["world.inspect"]
    evidence = list(harness.store.replay(session.id, harness.researcher))
    assert (
        next(event for event in evidence if event["kind"] == "operation.receipt")["payload"]["receipt"][
            "operation_id"
        ]
        == f"{session.id}:inspect"
    )


def test_experiment_rejects_invalid_environment_operations_before_queueing(tmp_path):
    class BrokenEnvironment(ScenarioEnvironment):
        spec = ScenarioEnvironment.spec.model_copy(
            update={"operations": (OperationSpec(name="world.inspect", version="1"),)}
        )

    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=BrokenEnvironment,
        agent_factories={"agent": ScenarioAgent},
    )
    experiment = harness.experiment(
        "Broken operation",
        (Scenario(id="broken", input={"difficulty": 2}),),
        turns=1,
    )

    with pytest.raises(Conflict, match="world.inspect.*runtime implementation"):
        experiment.start()
    assert harness.store.activity_snapshot(harness.researcher)["experiments"] == []


def test_standalone_rejects_invalid_environment_operations_before_queueing(tmp_path):
    class BrokenEnvironment(ScenarioEnvironment):
        spec = ScenarioEnvironment.spec.model_copy(
            update={"operations": (OperationSpec(name="world.inspect", version="1"),)}
        )

    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=BrokenEnvironment,
        agent_factories={"agent": ScenarioAgent},
    )

    with pytest.raises(Conflict, match="world.inspect.*runtime implementation"):
        harness.start(Scenario(id="broken", input={"difficulty": 2}), turns=1)
    assert harness.store.activity_snapshot(harness.researcher)["standalone"] == []


def test_experiment_expands_scenarios_and_trials_reproducibly(tmp_path):
    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=ScenarioEnvironment,
        agent_factories={"agent": ScenarioAgent},
        max_concurrency=2,
    )
    scenarios = [
        Scenario(id="easy", input={"difficulty": 1}),
        Scenario(id="hard", input={"difficulty": 5}),
    ]

    result = harness.experiment("difficulty", scenarios, trials=2, seed=41, turns=1).run()

    assert isinstance(result, ExperimentResult)
    assert result.status == "succeeded"
    assert [(session.scenario_id, session.trial) for session in result.sessions] == [
        ("easy", 0),
        ("easy", 1),
        ("hard", 0),
        ("hard", 1),
    ]
    repeated = harness.experiment("repeat", scenarios, trials=2, seed=41, turns=1).run()
    assert [session.seed for session in repeated.sessions] == [session.seed for session in result.sessions]
    assert len({session.seed for session in result.sessions}) == 4


def test_experiments_share_a_fair_bounded_scheduler(tmp_path):
    started = Event()
    release = Event()
    order = []
    lock = Lock()

    class OrderingAgent(ScenarioAgent):
        def act(self, observation):
            with lock:
                order.append(observation["payload"]["difficulty"])
                if len(order) == 1:
                    started.set()
            release.wait(2)
            return {}

    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=ScenarioEnvironment,
        agent_factories={"agent": OrderingAgent},
        max_concurrency=1,
    )
    first = harness.experiment(
        "first",
        [Scenario(id="a", input={"difficulty": 1}), Scenario(id="b", input={"difficulty": 2})],
        turns=1,
    ).start()
    assert started.wait(2)
    second = harness.experiment("second", [Scenario(id="c", input={"difficulty": 3})], turns=1).start()

    release.set()
    first.wait(SESSION_COMPLETION_TIMEOUT)
    second.wait(SESSION_COMPLETION_TIMEOUT)

    assert order == [1, 3, 2]


def test_interrupted_experiment_requires_explicit_resume(tmp_path):
    interrupted = False

    def environment_factory():
        nonlocal interrupted
        if current_thread().name.startswith("environment-session") and not interrupted:
            interrupted = True
            raise InterruptedError("synthetic worker restart")
        return ScenarioEnvironment()

    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=environment_factory,
        agent_factories={"agent": ScenarioAgent},
    )
    experiment = harness.experiment("restart", [Scenario(id="recoverable", input={"difficulty": 2})], turns=1)

    interrupted_result = experiment.run()

    assert interrupted_result.status == "interrupted"
    assert interrupted_result.sessions[0].status == "interrupted"
    interrupted_session = interrupted_result.sessions[0]
    harness._futures.pop(interrupted_session.id)
    with pytest.raises(Conflict, match="explicit resume"):
        interrupted_session.wait()
    reconnected = harness.experiment(
        "restart", [Scenario(id="recoverable", input={"difficulty": 2})], turns=1
    )
    reconnected.id = experiment.id
    resumed = reconnected.resume().wait(SESSION_COMPLETION_TIMEOUT)
    assert resumed.status == "succeeded"


def test_activity_outbox_is_resumable_for_global_experiment_and_session_streams(tmp_path):
    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=ScenarioEnvironment,
        agent_factories={"agent": ScenarioAgent},
    )
    result = harness.experiment("streamed", [Scenario(id="one", input={"difficulty": 1})], turns=1).run()
    token = harness.store.issue(Principal(tenant="local", subject="reader", role="researcher"))
    client = TestClient(create_app(EnvironmentSession(harness.store, ScenarioEnvironment())))
    headers = {"Authorization": f"Bearer {token}"}

    page = client.get("/v1/activity/events", headers=headers).json()

    assert page["events"]
    assert page["cursor"] == page["events"][-1]["id"]
    assert any(event["kind"] == "experiment.updated" for event in page["events"])
    caught_up = client.get(
        f"/v1/experiments/{result.id}/events",
        headers=headers | {"Accept": "text/event-stream", "Last-Event-ID": str(page["cursor"])},
    )
    assert caught_up.headers["content-type"].startswith("text/event-stream")
    assert "retry: 2000" in caught_up.text
    assert ": heartbeat" in caught_up.text
    session_page = client.get(f"/v1/environments/{result.sessions[0].id}/activity", headers=headers).json()
    assert all(event["environment"] == result.sessions[0].id for event in session_page["events"])


def test_activity_snapshot_groups_experiments_and_keeps_standalone_sessions_top_level(tmp_path):
    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=ScenarioEnvironment,
        agent_factories={"agent": ScenarioAgent},
    )
    grouped = harness.experiment(
        "grouped", [Scenario(id="one", input={"difficulty": 1})], trials=2, turns=1
    ).run()
    standalone = harness.run(Scenario(id="solo", input={"difficulty": 2}), turns=1)
    legacy_runtime = EnvironmentSession(harness.store, ScenarioEnvironment())
    legacy = legacy_runtime.create(
        ExperimentSpec(
            environment=ScenarioEnvironment.spec,
            participants=(AgentSpec(id="agent", implementation="scenario-agent@1", policy_version="1"),),
            scenario_input={"difficulty": 1},
        ),
        Principal(tenant="local", subject="legacy", role="researcher"),
    )
    token = harness.store.issue(Principal(tenant="local", subject="reader", role="researcher"))
    client = TestClient(create_app(EnvironmentSession(harness.store, ScenarioEnvironment())))

    snapshot = client.get("/v1/activity/snapshot", headers={"Authorization": f"Bearer {token}"}).json()

    assert snapshot["summary"] == {"running": 0, "queued": 0, "failed": 0}
    assert snapshot["experiments"][0]["id"] == grouped.id
    assert snapshot["experiments"][0]["kind"] == "experiment"
    assert snapshot["experiments"][0]["progress"] == {"completed": 2, "total": 2}
    frozen = snapshot["experiments"][0]["frozen"]
    assert frozen["environment"]["implementation"] == "scenario-environment@1"
    assert frozen["participants"] == [
        {
            "id": "agent",
            "implementation": "scenario-agent@1",
            "policy_version": "1",
            "config": {},
            "checkpoint": False,
        }
    ]
    assert frozen["execution"] == {
        "seed": 0,
        "trials": 2,
        "turns": 1,
        "max_concurrency": 4,
    }
    assert frozen["policy"]["max_turns"] == 1
    assert frozen["scoring_versions"] == []
    assert len(snapshot["experiments"][0]["sessions"]) == 2
    scenario = snapshot["experiments"][0]["scenarios"][0]
    assert scenario == {
        "kind": "scenario",
        "id": "one",
        "input": {"difficulty": 1},
        "reference": None,
        "metadata": {},
        "status": "succeeded",
        "completed": 2,
        "total": 2,
        "running": 0,
        "queued": 0,
        "failed": 0,
        "latest_activity": "Completed",
        "sessions": snapshot["experiments"][0]["sessions"],
        "updated": scenario["updated"],
    }
    assert {session["kind"] for session in scenario["sessions"]} == {"session"}
    assert snapshot["standalone"][0]["id"] == standalone.id
    assert snapshot["standalone"][0]["kind"] == "session"
    assert "experiment" not in snapshot["standalone"][0]
    legacy_snapshot = next(session for session in snapshot["standalone"] if session["id"] == legacy["id"])
    assert legacy_snapshot["target_turns"] is None
    with pytest.raises(Forbidden, match="activity authority"):
        harness.store.activity_snapshot(
            Principal(tenant="local", subject="agent", role="agent", participant="agent")
        )
    agent = Principal(tenant="local", subject="agent", role="agent", participant="agent")
    outsider = Principal(tenant="other", subject="researcher", role="researcher")
    with pytest.raises(Forbidden, match="activity authority"):
        harness.store.activity(agent)
    with pytest.raises(ValueError, match="activity page"):
        harness.store.activity(harness.researcher, after=-1)
    with pytest.raises(ValueError, match="activity page"):
        harness.store.activity(harness.researcher, limit=0)
    with pytest.raises(Forbidden, match="experiment unavailable"):
        harness.store.activity(outsider, experiment=grouped.id)
    with pytest.raises(Forbidden, match="environment session unavailable"):
        harness.store.activity(outsider, environment=standalone.id)


def test_concurrent_progress_counts_and_failure_isolation(tmp_path):
    release = Event()
    two_running = Event()
    lock = Lock()
    active = 0

    class BlockingAgent(ScenarioAgent):
        def act(self, observation):
            nonlocal active
            with lock:
                active += 1
                if active == 2:
                    two_running.set()
            release.wait(2)
            with lock:
                active -= 1
            if observation["payload"]["difficulty"] == 9:
                raise RuntimeError("isolated synthetic failure")
            return {}

    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=ScenarioEnvironment,
        agent_factories={"agent": BlockingAgent},
        max_concurrency=2,
    )
    experiment = harness.experiment(
        "bounded",
        [
            Scenario(id="one", input={"difficulty": 1}),
            Scenario(id="fails", input={"difficulty": 9}),
            Scenario(id="three", input={"difficulty": 3}),
        ],
        turns=1,
    ).start()

    assert two_running.wait(2)
    progress = experiment.result()
    assert (progress.running, progress.queued) == (2, 1)
    release.set()
    result = experiment.wait(SESSION_COMPLETION_TIMEOUT)

    assert result.failed == 1
    assert result.completed == 3
    assert sorted(session.status for session in result.sessions) == ["failed", "succeeded", "succeeded"]


def test_stopping_a_running_environment_session_is_terminal(tmp_path):
    started = Event()
    release = Event()

    class BlockingAgent(ScenarioAgent):
        def act(self, observation):
            started.set()
            release.wait(2)
            return {}

    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=ScenarioEnvironment,
        agent_factories={"agent": BlockingAgent},
    )
    session = harness.start(Scenario(id="stop", input={"difficulty": 1}), turns=2)
    assert started.wait(2)

    session.stop()
    release.set()
    session.wait(SESSION_COMPLETION_TIMEOUT)

    assert session.status == "stopped"


def test_harness_lifecycle_guards_and_validation_edges(tmp_path):
    low_level = EnvironmentSession(tmp_path / "low-level", ScenarioEnvironment())
    with pytest.raises(AttributeError, match="do not have an ID"):
        _ = low_level.id
    with pytest.raises(AttributeError, match="lifecycle properties"):
        _ = low_level.status
    with pytest.raises(AttributeError, match="wait is available"):
        low_level.wait()
    with pytest.raises(AttributeError, match="stop is available"):
        low_level.stop()
    with pytest.raises(AttributeError, match="resume is available"):
        low_level.resume()

    with pytest.raises(ValueError, match="limits must be positive"):
        EnvironmentHarness(
            tmp_path / "limits",
            environment_factory=ScenarioEnvironment,
            agent_factories={"agent": ScenarioAgent},
            max_concurrency=0,
        )
    with pytest.raises(ValueError, match="agent factory"):
        EnvironmentHarness(tmp_path / "agents", environment_factory=ScenarioEnvironment, agent_factories={})

    harness = EnvironmentHarness(
        tmp_path / "valid",
        environment_factory=ScenarioEnvironment,
        agent_factories={"agent": ScenarioAgent},
        max_sessions=2,
    )
    missing = EnvironmentSession(harness.store, ScenarioEnvironment(), harness=harness, session_id="f" * 32)
    with pytest.raises(Conflict, match="record is unavailable"):
        _ = missing.status
    scenario = Scenario(id="one", input={"difficulty": 1})
    with pytest.raises(ValueError, match="turns must be positive"):
        harness.start(scenario, turns=0)
    for name, scenarios, trials, turns, message in (
        (" ", [scenario], 1, 1, "name is required"),
        ("bad trials", [scenario], 0, 1, "trials and turns"),
        ("bad turns", [scenario], 1, 0, "trials and turns"),
        ("empty", [], 1, 1, "at least one scenario"),
        ("duplicates", [scenario, scenario], 1, 1, "IDs must be unique"),
    ):
        with pytest.raises(ValueError, match=message):
            harness.experiment(name, scenarios, trials=trials, turns=turns)
    with pytest.raises(Conflict, match="exceeds max_sessions"):
        harness.experiment("large", [scenario], trials=3)

    pending = harness.experiment("pending", [scenario], turns=1)
    with pytest.raises(Conflict, match="before waiting"):
        pending.wait()
    with pytest.raises(Conflict, match="before stopping"):
        pending.stop()
    with pytest.raises(Conflict, match="before resuming"):
        pending.resume()
    with pytest.raises(Conflict, match="unavailable"):
        pending.result()
    assert pending.start().start() is pending
    result = pending.wait(SESSION_COMPLETION_TIMEOUT)
    harness._futures.pop(result.sessions[0].id)
    assert result.sessions[0].wait().status == "succeeded"
    with pytest.raises(Conflict, match="only interrupted"):
        result.sessions[0].resume()

    class SchemaEnvironment(ScenarioEnvironment):
        scenario_type = None
        spec = ScenarioEnvironment.spec.model_copy(
            update={
                "scenario_schema": {
                    "type": "object",
                    "properties": {"difficulty": {"type": "integer"}},
                    "required": ["difficulty"],
                    "additionalProperties": False,
                }
            }
        )

    schema_harness = EnvironmentHarness(
        tmp_path / "schema",
        environment_factory=SchemaEnvironment,
        agent_factories={"agent": ScenarioAgent},
    )
    assert schema_harness._validate_scenario(scenario) == scenario


def test_harness_stops_queued_work_and_enforces_active_limits(tmp_path):
    started = Event()
    release = Event()

    class BlockingAgent(ScenarioAgent):
        def act(self, observation):
            started.set()
            release.wait(2)
            return {}

    harness = EnvironmentHarness(
        tmp_path / "queued",
        environment_factory=ScenarioEnvironment,
        agent_factories={"agent": BlockingAgent},
        max_concurrency=1,
        max_sessions=3,
    )
    running = harness.start(Scenario(id="running", input={"difficulty": 1}), turns=1)
    assert started.wait(2)
    queued = harness.start(Scenario(id="queued", input={"difficulty": 2}), turns=1)
    queued.stop().wait(SESSION_COMPLETION_TIMEOUT)
    assert queued.status == "stopped"
    experiment = harness.experiment(
        "queued experiment", [Scenario(id="experiment", input={"difficulty": 3})], turns=1
    ).start()
    assert experiment.stop().status == "stopped"
    release.set()
    running.wait(SESSION_COMPLETION_TIMEOUT)

    limit_started = Event()
    limit_release = Event()

    class LimitAgent(ScenarioAgent):
        def act(self, observation):
            limit_started.set()
            limit_release.wait(2)
            return {}

    limited = EnvironmentHarness(
        tmp_path / "limited",
        environment_factory=ScenarioEnvironment,
        agent_factories={"agent": LimitAgent},
        max_concurrency=1,
        max_sessions=1,
    )
    active = limited.start(Scenario(id="active", input={"difficulty": 1}), turns=1)
    assert limit_started.wait(2)
    with pytest.raises(Conflict, match="max_sessions limit"):
        limited.start(Scenario(id="second", input={"difficulty": 2}), turns=1)
    with pytest.raises(Conflict, match="max_sessions limit"):
        limited.experiment("second", [Scenario(id="experiment", input={"difficulty": 3})], turns=1).start()
    limit_release.set()
    active.wait(SESSION_COMPLETION_TIMEOUT)

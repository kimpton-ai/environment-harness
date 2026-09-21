from threading import Event, Lock, current_thread

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from environment_harness import (
    AgentSpec,
    EnvironmentHarness,
    EnvironmentSession,
    ExperimentResult,
    ExperimentSpec,
    Principal,
    Scenario,
)
from environment_harness.contracts import Capabilities, EnvironmentSpec, Transition
from environment_harness.errors import Forbidden
from environment_harness.server import create_app


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
    first.wait(2)
    second.wait(2)

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
    resumed = experiment.resume().wait(2)
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
    result = experiment.wait(2)

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
    session.wait(2)

    assert session.status == "stopped"

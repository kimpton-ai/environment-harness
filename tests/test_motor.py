import os
import threading
from copy import deepcopy

import pytest

from environment_harness import MotorExecutor, MotorProfile, MotorRequest
from environment_harness.contracts import AgentSpec, Capabilities, ExperimentSpec, Principal, RunPolicy
from environment_harness.errors import Conflict, Forbidden
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.motor import MotorOutcomeUnknown
from environment_harness.motor_adapters import BrowserMotor
from environment_harness.motor_contracts import (
    MotorCandidate,
    MotorControlPermission,
    MotorSelection,
    MotorStep,
)
from environment_harness.operations import Operations
from environment_harness.runtime import EnvironmentSession
from environment_harness.store import EvidenceStore

pytestmark = pytest.mark.skipif(os.name == "nt", reason="MotorExecutor uses POSIX application locking")


class Driver:
    def __init__(self):
        self.state = {
            "revision": "0",
            "elements": [{"element_id": "input", "selector": "#input", "value": ""}],
        }
        self.calls = []
        self.stopped = threading.Event()
        self.fail = False

    def observe(self):
        return deepcopy(self.state)

    def execute(self, operation, payload, *, operation_id, cancel, deadline):
        self.calls.append(operation_id)
        if self.fail:
            raise TimeoutError("lost receipt")
        self.state["revision"] = str(int(self.state["revision"]) + 1)
        self.state["elements"][0]["value"] = payload.get("text", "")
        return {"operation_id": operation_id, "status": "completed"}

    def stop(self):
        self.stopped.set()

    def lookup(self, operation_id):
        return None


def request(**changes):
    return MotorRequest(
        skill="fill",
        target={"element_id": "input"},
        arguments={"text": "hello"},
        expected={"elements": [{"element_id": "input", "selector": "#input", "value": "hello"}]},
        observation_revision="0",
        goal_revision="0",
        **changes,
    )


def setup(tmp_path, *, selector=None, adapter_class=BrowserMotor):
    driver = Driver()
    adapter = adapter_class(driver)
    profile = MotorProfile(
        adapter=adapter.implementation,
        mode="jev" if selector else "deterministic",
        selector_model=selector.model if selector else None,
    )
    motor = MotorExecutor(adapter, profile, journal=tmp_path / "motor.sqlite", selector=selector)
    env = SyntheticEnvironment()
    env.spec = env.spec.model_copy(
        update={"motor_skills": adapter.skills, "capabilities": Capabilities(external_writes=True)}
    )
    session = EnvironmentSession(EvidenceStore(tmp_path / "evidence"), env)
    who = Principal(tenant="t", subject="r", role="researcher")
    spec = ExperimentSpec(
        environment=env.spec,
        participants=(AgentSpec(id="a", implementation="test", policy_version="1"),),
        motor=profile,
        policy=RunPolicy(
            allowed_endpoints=("motor", "https://selector.test"),
            allowed_operations=("motor.execute", "motor.select"),
            max_cost_micros=100,
            external_writes=True,
        ),
    )
    environment = session.create(spec, who)["id"]
    agent = Principal(tenant="t", subject="a", role="agent", participant="a", environment=environment)
    lease = session.lease(environment, who, "writer")
    operations = Operations(session.store)

    def prepare(req=None, oid="fill", **overrides):
        values = dict(
            endpoint="motor",
            operation="motor.execute",
            payload=(req or request()).model_dump(mode="json"),
            maximum_cost_micros=100,
            write=True,
        )
        values.update(overrides)
        operations.prepare(environment, agent, oid, **values)

    def dispatch(oid="fill"):
        return operations.dispatch(session, environment, who, lease, oid, motor)

    return driver, motor, prepare, dispatch, session, who, environment, lease


def test_motor_operations_receipt_replay_and_profile(tmp_path):
    driver, motor, prepare, dispatch, session, who, environment, _ = setup(tmp_path)
    prepare()
    receipt = dispatch()
    assert receipt["status"] == "completed"
    assert receipt["profile"]["adapter"] == "browser-motor@1"
    assert receipt["steps"][0]["receipt"]["operation_id"] == driver.calls[0]
    assert dispatch() == receipt
    assert motor.lookup(receipt["operation_id"]) == receipt
    assert len(driver.calls) == 1


def test_motor_stale_observation_and_write_permission(tmp_path):
    driver, _, prepare, dispatch, *_ = setup(tmp_path)
    driver.state["revision"] = "changed"
    prepare()
    assert dispatch()["status"] == "blocked"
    assert not driver.calls
    prepare(oid="unauthorized", write=False)
    with pytest.raises(Forbidden, match="write"):
        dispatch("unauthorized")


def test_motor_stop_fences_queued_requests(tmp_path):
    driver, motor, prepare, dispatch, *_ = setup(tmp_path)
    prepare()
    motor.stop()
    assert dispatch()["status"] == "cancelled"
    assert not driver.calls
    prepare(request(stop_epoch=motor.stop_epoch), oid="new")
    assert dispatch("new")["status"] == "completed"


def test_unknown_effect_keeps_reservation_and_blocks_replay_after_restart(tmp_path):
    driver, motor, prepare, dispatch, session, who, eid, _ = setup(tmp_path)
    driver.fail = True
    prepare()
    with pytest.raises(TimeoutError):
        dispatch()
    assert motor.lookup(f"{eid}:fill") is None
    assert motor.progress(f"{eid}:fill")["status"] == "unknown"
    with pytest.raises(Conflict, match="reconciliation"):
        dispatch()
    restarted = MotorExecutor(BrowserMotor(driver), motor.profile, journal=motor.journal)
    with pytest.raises(MotorOutcomeUnknown):
        restarted.execute("another", {"payload": request().model_dump()}, 100, authority=lambda _: None)
    with session.store.transaction() as db:
        row = session.store.environment(db, eid, who)
        assert row["reserved"] == 100
        assert len(driver.calls) == 1


def test_unverified_postcondition_quarantines_completed_effect(tmp_path):
    driver, motor, prepare, dispatch, session, who, environment, _ = setup(tmp_path)
    prepare(
        request().model_copy(
            update={"expected": {"elements": [{"element_id": "input", "value": "different"}]}}
        )
    )
    with pytest.raises(MotorOutcomeUnknown, match="postcondition"):
        dispatch()
    assert motor.progress(f"{environment}:fill")["status"] == "unknown"
    assert len(driver.calls) == 1


def test_cancellation_during_native_action_releases_controls(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    driver, _, prepare, dispatch, session, who, eid, lease = setup(tmp_path)
    entered = threading.Event()

    def blocking(operation, payload, *, operation_id, cancel, deadline):
        entered.set()
        assert cancel.wait(3)
        return {"operation_id": operation_id, "status": "cancelled"}

    driver.execute = blocking
    prepare()
    with ThreadPoolExecutor() as pool:
        future = pool.submit(dispatch)
        assert entered.wait(3)
        session.control(eid, who, lease, "cancel")
        assert future.result(timeout=3)["status"] == "cancelled"
    assert driver.stopped.is_set()


class Alternatives(BrowserMotor):
    def plan(self, req, observation):
        super().plan(req, observation)
        step = MotorStep(operation="fill", target=req.target, arguments=req.arguments)
        return (
            MotorCandidate(id="a", description="first legal plan", steps=(step,)),
            MotorCandidate(id="b", description="second legal plan", steps=(step,)),
        )


class Selector:
    model = "test-pinned"
    endpoint = "https://selector.test"

    def maximum_cost(self, *args):
        return 10

    def select(self, *args, **kwargs):
        return MotorSelection(candidate_id="invented", model=self.model, cost_micros=1)


def test_selector_cannot_invent_steps_and_cost_is_recorded(tmp_path):
    driver, _, prepare, dispatch, *_ = setup(tmp_path, selector=Selector(), adapter_class=Alternatives)
    prepare()
    receipt = dispatch()
    assert receipt["status"] == "blocked" and receipt["cost_micros"] == 1
    assert not driver.calls


def test_intermediate_controls_are_explicit_and_semantic_target_stays_bound(tmp_path):
    class Controlled(BrowserMotor):
        def plan(self, req, observation):
            return (
                MotorCandidate(
                    id="controlled",
                    description="bounded focus",
                    steps=(
                        MotorStep(
                            operation="fill",
                            target=req.target,
                            arguments=req.arguments,
                            controls={"approach": "left", "distance": 1, "travel": 1},
                        ),
                    ),
                ),
            )

    permission = MotorControlPermission(
        id="left", controls={"approach": "left", "distance": 1, "travel": 1}, max_travel=2
    )
    driver, _, prepare, dispatch, *_ = setup(tmp_path, adapter_class=Controlled)
    prepared = request(control_permissions=(permission,))
    prepare(prepared)
    assert dispatch()["status"] == "completed"

    driver, _, prepare, dispatch, *_ = setup(tmp_path, adapter_class=Controlled)
    prepare(request(control_permissions=()))
    receipt = dispatch()
    assert receipt["status"] == "blocked" and receipt["reason_code"] == "unauthorized_control"
    assert not driver.calls


@pytest.mark.parametrize("travel", [1.0, float("nan"), -1.0, 3.0])
def test_control_travel_bounds_are_finite_nonnegative_and_total(tmp_path, travel):
    class Controlled(BrowserMotor):
        def plan(self, req, observation):
            controls = {"approach": "left", "travel": travel}
            return (
                MotorCandidate(
                    id="controlled",
                    description="bounded",
                    steps=(
                        MotorStep(
                            operation="fill", target=req.target, arguments=req.arguments, controls=controls
                        ),
                    ),
                ),
            )

    permission = MotorControlPermission(
        id="left", controls={"approach": "left", "travel": travel}, max_travel=2
    )
    driver, _, prepare, dispatch, *_ = setup(tmp_path, adapter_class=Controlled)
    prepare(request(control_permissions=(permission,)))
    receipt = dispatch()
    valid = travel == 1.0
    assert receipt["status"] == ("completed" if valid else "blocked")
    assert bool(driver.calls) is valid


def test_jev_abstention_has_a_distinct_reason_code(tmp_path):
    class Abstain(Selector):
        def select(self, *args, **kwargs):
            return MotorSelection(candidate_id=None, model=self.model, cost_micros=1)

    driver, _, prepare, dispatch, *_ = setup(tmp_path, selector=Abstain())
    prepare()
    receipt = dispatch()
    assert receipt["status"] == "blocked" and receipt["reason_code"] == "abstention"
    assert not driver.calls


def test_profile_mismatch_and_stale_goal_block_before_dispatch(tmp_path):
    driver, motor, prepare, dispatch, *_ = setup(tmp_path)
    prepare(request().model_copy(update={"goal_revision": "old"}))
    with pytest.raises(Conflict, match="goal revision"):
        dispatch()
    motor.profile = motor.profile.model_copy(update={"adapter": "other"})
    with pytest.raises(Forbidden, match="frozen experiment"):
        dispatch()
    assert not driver.calls


def test_comparison_separates_motor_assistance_profiles(tmp_path):
    import json

    from environment_harness.evaluation import compare

    _, motor, _, _, session, who, eid, _ = setup(tmp_path)
    with session.store.transaction() as db:
        manifest = json.loads(session.store.environment(db, eid, who)["manifest"])
    spec = ExperimentSpec.model_validate(manifest).model_copy(
        update={
            "motor": motor.profile.model_copy(update={"mode": "jev", "selector_model": "pinned"}),
        }
    )
    other = session.create(spec, who)["id"]
    records = compare(session.store, [eid, other], who)["environments"]
    assert records[0]["cohort"] != records[1]["cohort"]


def test_jev_can_select_bounded_focus_then_fill(tmp_path):
    class ChooseFocus(Selector):
        def select(self, state, candidates, **kwargs):
            return MotorSelection(candidate_id=candidates[1].id, model=self.model, cost_micros=1)

    driver, _, prepare, dispatch, *_ = setup(tmp_path, selector=ChooseFocus())
    driver.state["elements"][0]["supported_operations"] = ["focus", "fill"]
    expected = deepcopy(driver.state["elements"])
    expected[0]["value"] = "hello"
    prepare(request().model_copy(update={"expected": {"elements": expected}}))
    receipt = dispatch()
    assert receipt["status"] == "completed"
    assert [entry["step"]["operation"] for entry in receipt["steps"]] == ["focus", "fill"]
    assert len(driver.calls) == 2


def test_target_replacement_during_selection_prevents_effect(tmp_path):
    selector = Selector()
    driver, _, prepare, dispatch, *_ = setup(tmp_path, selector=selector, adapter_class=Alternatives)

    def replace(*args, **kwargs):
        driver.state["revision"] = "replacement"
        driver.state["elements"][0]["element_id"] = "different"
        return MotorSelection(candidate_id="a", model=selector.model, cost_micros=1)

    selector.select = replace
    prepare()
    receipt = dispatch()
    assert receipt["status"] == "blocked" and receipt["cost_micros"] == 1
    assert not driver.calls


def test_explicit_recovery_allows_new_intents_but_never_replays_unknown(tmp_path):
    driver, motor, prepare, dispatch, _, _, eid, _ = setup(tmp_path)
    driver.fail = True
    prepare()
    with pytest.raises(TimeoutError):
        dispatch()
    recovery = motor.acknowledge_unknown()
    assert recovery["operation_ids"] == [f"{eid}:fill"]
    driver.fail = False
    prepare(request(stop_epoch=recovery["stop_epoch"]), oid="fresh", maximum_cost_micros=0)
    assert dispatch("fresh")["status"] == "completed"
    assert motor.lookup(f"{eid}:fill") is None
    with pytest.raises(Conflict):
        dispatch()
    assert len(driver.calls) == 2


def test_live_goal_change_revokes_prepared_motor_authority(tmp_path):
    driver, motor, prepare, dispatch, session, _, environment, _ = setup(tmp_path)
    original = motor.adapter.observe

    def observe_after_goal_change():
        with session.store.transaction() as db:
            db.execute("UPDATE environments SET revision=revision+1 WHERE id=?", (environment,))
        return original()

    motor.adapter.observe = observe_after_goal_change
    prepare()
    receipt = dispatch()
    assert receipt["status"] == "cancelled"
    assert receipt["reason_code"] == "authority_expired"
    assert not driver.calls

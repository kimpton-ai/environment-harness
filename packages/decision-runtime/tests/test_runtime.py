"""Browser and stepped drone conformance; these are not full-game acceptance."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from environment_harness.errors import Conflict, Forbidden

from environment_harness_decisions import (
    Admission,
    Answer,
    AuthorityBinding,
    BoundedInvocation,
    ChoiceOption,
    CompiledCommand,
    DecisionOperation,
    DecisionPolicy,
    DecisionQuestion,
    DecisionSet,
    FakeSelector,
    InvocationLimits,
    NativeReceipt,
    Objective,
    Observation,
    OutcomeUncertain,
    ProviderFailure,
    Selection,
    Verification,
)


class Browser:
    identity = "browser.fixture"
    implementation = "browser.v1"

    def __init__(self):
        self.revision = 0
        self.submissions = []
        self.receipts = {}
        self.released = False
        self.queue_hook = lambda: None
        self.unknown = False
        self.fail_verification = False

    def observe(self, objective):
        return Observation(
            revision=str(self.revision), model_input={"page": "home", "revision": self.revision}
        )

    def decisions(self, objective, observation):
        return DecisionSet(
            id="page",
            observation_revision=observation.revision,
            questions=(
                DecisionQuestion(
                    id="button",
                    kind="choice",
                    prompt="Select a visible button",
                    options=(
                        ChoiceOption(id="save", label="Save"),
                        ChoiceOption(id="cancel", label="Cancel"),
                    ),
                ),
            ),
        )

    def compile(self, objective, observation, decisions, selection):
        return CompiledCommand(
            id="click", adapter_version=self.implementation, payload={"click": selection.answers[0].value}
        )

    def admit(self, execution_id, command, binding):
        return Admission(
            execution_id=execution_id,
            binding=binding,
            accepted=binding.observation_revision == str(self.revision),
            reason="stale_browser",
        )

    def execute(self, execution_id, command, *, before_dispatch, cancel, deadline):
        self.queue_hook()
        before_dispatch()
        self.submissions.append(execution_id)
        self.revision += 1
        receipt = NativeReceipt(execution_id=execution_id, outcome="applied", evidence={"page": "saved"})
        self.receipts[execution_id] = receipt
        if self.unknown:
            return NativeReceipt(execution_id=execution_id, outcome="unknown")
        return receipt

    def verify(self, objective, before, after, receipt):
        return Verification(status="failed" if self.fail_verification else "completed", reason="readback")

    def lookup(self, execution_id):
        return self.receipts.get(execution_id)

    def stop(self):
        self.released = True


class Drone(Browser):
    identity = "drone.fixture"
    implementation = "drone.v1"

    def __init__(self):
        super().__init__()
        self.position = 0.0
        self.orientation = {axis: 0.0 for axis in ("yaw", "pitch", "roll")}
        self.ticks = 0

    def decisions(self, objective, observation):
        return DecisionSet(
            id="flight",
            observation_revision=observation.revision,
            questions=tuple(
                DecisionQuestion(id=name, kind="score", prompt=name, minimum=0, maximum=bound)
                for name, bound in [("throttle", 1), ("yaw", 1), ("pitch", 1), ("roll", 1), ("duration", 10)]
            ),
        )

    def compile(self, objective, observation, decisions, selection):
        controls = {a.question_id: a.value for a in selection.answers}
        if controls["throttle"] * controls["duration"] > 2:
            raise ValueError("combined impulse exceeds native bound")
        return CompiledCommand(id="flight", adapter_version=self.implementation, payload=controls)

    def execute(self, execution_id, command, *, before_dispatch, cancel, deadline):
        before_dispatch()
        self.submissions.append(execution_id)
        controls = command.payload
        remaining = controls["duration"]
        while remaining > 1e-9:
            if cancel.is_set() or time.monotonic() >= deadline:
                self.stop()
                return NativeReceipt(execution_id=execution_id, outcome="cancelled")
            dt = min(0.1, remaining)
            self.position += controls["throttle"] * dt
            for axis in self.orientation:
                self.orientation[axis] += controls[axis] * dt
            remaining -= dt
            self.ticks += 1
        self.revision += 1
        receipt = NativeReceipt(
            execution_id=execution_id,
            outcome="applied",
            evidence={"position": self.position, "orientation": self.orientation, "ticks": self.ticks},
        )
        self.receipts[execution_id] = receipt
        return receipt

    def verify(self, objective, before, after, receipt):
        return Verification(
            status="completed" if receipt.evidence["ticks"] > 0 else "failed",
            reason="stepped_simulator_readback",
        )


def setup(tmp_path, control=None, selector=None, *, steps=1, corrections=0):
    control = control or Browser()
    selector = selector or FakeSelector()
    policy = DecisionPolicy(
        profile="fixture",
        selector_model=selector.model,
        endpoint="fixture",
        adapter_version=control.implementation,
    )
    op = DecisionOperation(control, selector, journal=tmp_path / "decisions.sqlite", policy=policy)
    invocation = BoundedInvocation(
        objective=Objective(
            id="goal",
            revision="directive-9",
            authorized_scope=("control",),
            completion_conditions=("native readback",),
            limits=InvocationLimits(
                max_steps=steps, timeout_ms=5000, max_cost_micros=20, max_corrections=corrections
            ),
        ),
        binding=AuthorityBinding(
            environment_id=control.identity,
            session_revision="2",
            directive_revision="directive-9",
            observation_revision="0",
            recovery_generation=0,
            owner="worker-1",
            stop_epoch=0,
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=5),
    )
    return op, invocation


def execute(op, invocation, authority=lambda _: None):
    return op.execute(
        "session:op-1", {"payload": invocation.model_dump(mode="json")}, 20, authority=authority
    )


def test_browser_selection_receipt_and_deep_immutability(tmp_path):
    op, invocation = setup(tmp_path)
    with pytest.raises(TypeError):
        invocation.objective.parameters["x"] = 1
    receipt = execute(op, invocation)
    assert receipt["status"] == "completed"
    assert receipt["cost_micros"] == 0
    assert len(op.control.submissions) == 1
    assert op.lookup("session:op-1") == receipt == execute(op, invocation)
    assert set(receipt["timings_ms"]) == {
        "observation",
        "selection",
        "admission",
        "execution",
        "verification",
    }
    assert invocation.goal_revision == "2" and invocation.objective.revision == "directive-9"
    with op.ledger.db() as db:
        artifact = db.execute(
            "SELECT payload FROM artifacts WHERE hash=?", (receipt["artifact_hashes"][-1],)
        ).fetchone()[0]
    assert json.loads(artifact)["verification"]["status"] == "completed"


@pytest.mark.parametrize("duration,expected", [(2, "completed"), (10, "rejected")])
def test_composed_drone_combination(tmp_path, duration, expected):
    values = {"throttle": 1, "yaw": 0.2, "pitch": 0, "roll": 0, "duration": duration}
    selection = Selection(
        model="fixture.v1",
        cost_micros=2,
        answers=tuple(Answer(question_id=k, value=v) for k, v in values.items()),
    )
    op, invocation = setup(tmp_path, Drone(), FakeSelector([selection], cost_micros=2))
    receipt = execute(op, invocation)
    assert receipt["status"] == expected
    assert receipt["cost_micros"] == 2
    assert len(op.control.submissions) == (expected == "completed")
    assert op.control.position == pytest.approx(2 if expected == "completed" else 0)
    assert op.control.orientation["yaw"] == pytest.approx(0.4 if expected == "completed" else 0)


def test_stale_sdk_authority_after_native_queue(tmp_path):
    op, invocation = setup(tmp_path)
    authorized = True

    def authority(binding):
        assert binding.session_revision == "2"
        if not authorized:
            raise Forbidden("stale session")

    def queue_hook():
        nonlocal authorized
        authorized = False

    op.control.queue_hook = queue_hook
    receipt = execute(op, invocation, authority)
    assert receipt["status"] == "cancelled"
    assert op.control.submissions == []


def test_stale_native_observation_after_queue(tmp_path):
    op, invocation = setup(tmp_path)
    op.control.queue_hook = lambda: setattr(op.control, "revision", 1)
    receipt = execute(op, invocation)
    assert receipt["status"] == "rejected" and not op.control.submissions


def test_abstention_and_verification_handoff(tmp_path):
    op, invocation = setup(
        tmp_path,
        selector=FakeSelector([Selection(model="fixture.v1", cost_micros=0, abstention="no legal option")]),
    )
    assert execute(op, invocation)["status"] == "abstained"
    assert not op.control.submissions
    op, invocation = setup(tmp_path / "second")
    op.control.fail_verification = True
    assert execute(op, invocation)["status"] == "handoff"


def test_unknown_effect_lookup_never_repeats_native_submit(tmp_path):
    op, invocation = setup(tmp_path)
    op.control.unknown = True
    with pytest.raises(OutcomeUncertain):
        execute(op, invocation)
    with pytest.raises(OutcomeUncertain):
        execute(op, invocation)
    receipt = op.lookup("session:op-1")
    assert receipt["effects_resolved"] and len(op.control.submissions) == 1
    assert op.lookup("session:op-1") == receipt


def test_unknown_charge_reservation_retained_no_retry(tmp_path):
    op, invocation = setup(tmp_path, selector=FakeSelector([ProviderFailure("read timeout")], cost_micros=4))
    with pytest.raises(OutcomeUncertain):
        execute(op, invocation)
    assert op.lookup("session:op-1") is None
    assert len(op.selector.calls) == 1 and not op.control.submissions
    with op.ledger.db() as db:
        assert op.ledger.charges(db, "session:op-1") == (0, 4, False)


def test_one_proven_uncharged_retry(tmp_path):
    op, invocation = setup(
        tmp_path,
        selector=FakeSelector([ProviderFailure("preflight", submitted=False, uncharged=True)], cost_micros=3),
    )
    assert execute(op, invocation)["cost_micros"] == 3
    assert len(op.selector.calls) == 2


def test_stop_during_inference_releases_native_and_late_charge_settles(tmp_path):
    started, finish = threading.Event(), threading.Event()

    class Slow(FakeSelector):
        def select(self, *args, **kwargs):
            started.set()
            finish.wait(3)
            selection = Selection(
                model=self.model, cost_micros=3, answers=(Answer(question_id="button", value="save"),)
            )
            self.receipts[args[0]] = selection
            return selection

    op, invocation = setup(tmp_path, selector=Slow(cost_micros=3))
    errors = []

    def work():
        try:
            execute(op, invocation)
        except OutcomeUncertain as error:
            errors.append(error)

    thread = threading.Thread(target=work)
    thread.start()
    assert started.wait(1)
    op.stop()
    thread.join(1)
    assert not thread.is_alive() and op.control.released and errors
    assert not op.control.submissions and op.lookup("session:op-1") is None
    finish.set()
    for _ in range(100):
        receipt = op.lookup("session:op-1")
        if receipt:
            break
        time.sleep(0.01)
    assert receipt["cost_micros"] == 3 and receipt["status"] == "cancelled"
    assert not op.control.submissions


def test_exclusive_ownership(tmp_path):
    op, invocation = setup(tmp_path)
    with op.ledger.ownership():
        with pytest.raises(Conflict):
            execute(op, invocation)


def test_hard_budget_requires_verified_bound(tmp_path):
    selector = FakeSelector()
    selector.maximum_charge_micros = lambda *args: None
    op, invocation = setup(tmp_path, selector=selector)
    assert execute(op, invocation)["status"] == "rejected"
    assert not selector.calls and not op.control.submissions

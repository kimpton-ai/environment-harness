import pytest
from test_motor import Alternatives, request, setup

from environment_harness import MotorExecutor, MotorProfile
from environment_harness.errors import BudgetExceeded, Conflict, Forbidden
from environment_harness.motor import MotorError, MotorOutcomeUnknown
from environment_harness.motor_contracts import (
    MotorCandidate,
    MotorControlPermission,
    MotorSelection,
    MotorStep,
)


def direct(motor, req, *, operation_id="direct", write=True, budget=100, authority=None):
    return motor.execute(
        operation_id,
        {
            "endpoint": "motor",
            "operation": "motor.execute",
            "payload": req.model_dump(mode="json"),
            "write": write,
        },
        budget,
        authority=authority or (lambda _: None),
    )


def test_constructor_and_lookup_guards(tmp_path):
    driver, motor, _, _, *_ = setup(tmp_path)
    with pytest.raises(ValueError, match="adapter"):
        MotorExecutor(motor.adapter, MotorProfile(adapter="other"), journal=tmp_path / "bad.sqlite")
    with pytest.raises(ValueError, match="Jev"):
        MotorExecutor(
            motor.adapter,
            MotorProfile(adapter=motor.adapter.implementation, mode="jev", selector_model="m"),
            journal=tmp_path / "bad2.sqlite",
        )
    with pytest.raises(ValueError, match="pinned"):
        MotorExecutor(
            motor.adapter,
            MotorProfile(adapter=motor.adapter.implementation, selector_model="m"),
            journal=tmp_path / "bad3.sqlite",
            selector=type("S", (), {"model": "other"})(),
        )
    with pytest.raises(ValueError, match="pinned"):
        MotorExecutor(
            motor.adapter,
            MotorProfile(adapter=motor.adapter.implementation, mode="jev", selector_model="m"),
            journal=tmp_path / "bad4.sqlite",
            selector=type("S", (), {"model": "other"})(),
        )
    assert motor.lookup("missing") is None
    assert motor.progress("missing") is None
    with pytest.raises(Forbidden, match="Operations"):
        motor.execute("no-authority", {"payload": request().model_dump(mode="json")}, 1)
    motor._lock.acquire()
    try:
        with pytest.raises(Conflict, match="settling"):
            motor.acknowledge_unknown()
    finally:
        motor._lock.release()


def test_validate_rejects_unfrozen_requests_and_selector_policy(tmp_path):
    _, motor, *_ = setup(tmp_path)
    manifest = {
        "motor": motor.profile.model_dump(mode="json"),
        "environment": {"motor_skills": ["fill"]},
        "policy": {"allowed_endpoints": [], "allowed_operations": []},
    }
    base = {
        "endpoint": "motor",
        "operation": "motor.execute",
        "payload": request().model_dump(mode="json"),
        "write": True,
    }
    with pytest.raises(Forbidden, match="motor.execute"):
        motor.validate({**base, "operation": "other"}, manifest)
    with pytest.raises(Forbidden, match="frozen"):
        motor.validate(base, {**manifest, "motor": {}})
    with pytest.raises(Forbidden, match="declared"):
        motor.validate(
            {**base, "payload": request().model_copy(update={"skill": "read"}).model_dump(mode="json")},
            manifest,
        )
    with pytest.raises(Forbidden, match="write"):
        motor.validate({**base, "write": False}, manifest)

    class Selector:
        model = "selector"
        endpoint = "https://selector.test"

    _, selected, *_ = setup(tmp_path / "selected", selector=Selector())
    with pytest.raises(Forbidden, match="selection"):
        selected.validate(base, {**manifest, "motor": selected.profile.model_dump(mode="json")})


def test_candidate_authority_and_revalidation_fail_closed(tmp_path):
    class Bad(Alternatives):
        def plan(self, req, observation):
            step = MotorStep(operation="fill", target={"element_id": "other"}, arguments={"text": "wrong"})
            return (MotorCandidate(id="bad", description="bad", steps=(step,)),)

    _, _, prepare, dispatch, *_ = setup(tmp_path, adapter_class=Bad)
    prepare()
    assert dispatch()["reason_code"] == "target_changed"

    class Args(Alternatives):
        def plan(self, req, observation):
            step = MotorStep(operation="fill", target=req.target, arguments={"text": "different"})
            return (MotorCandidate(id="args", description="bad args", steps=(step,)),)

    _, _, prepare, dispatch, *_ = setup(tmp_path / "args", adapter_class=Args)
    prepare()
    assert dispatch()["reason_code"] == "unauthorized_arguments"

    class Revalidate(Alternatives):
        def revalidate(self, req, observation, selected, index=0):
            return (MotorCandidate(id="other", description="changed", steps=selected.steps),)

    _, _, prepare, dispatch, *_ = setup(tmp_path / "revalidate", adapter_class=Revalidate)
    prepare()
    assert dispatch()["reason_code"] == "plan_changed"

    class RevalidateInvalid(Alternatives):
        def revalidate(self, req, observation, selected, index=0):
            raise MotorError("revalidation failed")

    _, _, prepare, dispatch, *_ = setup(tmp_path / "revalidate-invalid", adapter_class=RevalidateInvalid)
    prepare()
    assert dispatch()["reason_code"] == "invalid_plan"

    class Protected(Alternatives):
        def plan(self, req, observation):
            return (
                MotorCandidate(
                    id="protected",
                    description="protected",
                    steps=(
                        MotorStep(
                            operation="fill",
                            target=req.target,
                            arguments=req.arguments,
                            controls={"travel": 1},
                        ),
                    ),
                ),
            )

        def revalidate(self, req, observation, selected, index=0):
            observation["protected_region_revision"] = "changed"
            return (selected,)

    protected = request(
        control_permissions=(
            MotorControlPermission(id="p", controls={"travel": 1}, protected_region_revision="ok"),
        )
    )
    driver, _, prepare, dispatch, *_ = setup(tmp_path / "protected", adapter_class=Protected)
    driver.state["protected_region_revision"] = "ok"
    prepare(protected)
    assert dispatch()["reason_code"] == "protection_changed"


def test_invalid_plan_and_candidate_shapes_are_blocked(tmp_path):
    class Raises(Alternatives):
        def plan(self, req, observation):
            raise ValueError("planner failed")

    _, _, prepare, dispatch, *_ = setup(tmp_path, adapter_class=Raises)
    prepare()
    assert dispatch()["reason_code"] == "invalid_plan"

    class Empty(Alternatives):
        def plan(self, req, observation):
            return ()

    _, _, prepare, dispatch, *_ = setup(tmp_path / "empty", adapter_class=Empty)
    prepare()
    assert dispatch()["reason_code"] == "no_plan"

    class Duplicate(Alternatives):
        def plan(self, req, observation):
            step = MotorStep(operation="fill", target=req.target, arguments=req.arguments)
            return (
                MotorCandidate(id="same", description="one", steps=(step,)),
                MotorCandidate(id="same", description="two", steps=(step,)),
            )

    _, _, prepare, dispatch, *_ = setup(tmp_path / "duplicate", adapter_class=Duplicate)
    prepare()
    assert dispatch()["reason_code"] == "invalid_candidate"


def test_control_bounds_and_protected_region_are_enforced(tmp_path):
    class Controlled(Alternatives):
        def plan(self, req, observation):
            step = MotorStep(
                operation="fill", target=req.target, arguments=req.arguments, controls={"travel": 1}
            )
            return (MotorCandidate(id="controlled", description="bounded", steps=(step,)),)

    permission = MotorControlPermission(
        id="p", controls={"travel": 1}, max_steps=1, max_travel=1, protected_region_revision="new"
    )
    _, _, prepare, dispatch, *_ = setup(tmp_path, adapter_class=Controlled)
    prepare(request(control_permissions=(permission,)))
    assert dispatch()["reason_code"] == "protection_changed"

    class Limited(Controlled):
        def plan(self, req, observation):
            step = MotorStep(
                operation="fill", target=req.target, arguments=req.arguments, controls={"travel": 1}
            )
            return (MotorCandidate(id="limited", description="two steps", steps=(step, step)),)

    permission = MotorControlPermission(id="p", controls={"travel": 1}, max_steps=1)
    _, _, prepare, dispatch, *_ = setup(tmp_path / "limited", adapter_class=Limited)
    prepare(request(control_permissions=(permission,)))
    assert dispatch()["reason_code"] == "control_limit_exceeded"

    class NoTravel(Controlled):
        def plan(self, req, observation):
            step = MotorStep(
                operation="fill", target=req.target, arguments=req.arguments, controls={"travel": 1}
            )
            return (MotorCandidate(id="no-travel", description="no travel cap", steps=(step,)),)

    driver, _, prepare, dispatch, *_ = setup(tmp_path / "no-travel", adapter_class=NoTravel)
    driver.state["protected_region_revision"] = "ok"
    permission = MotorControlPermission(id="p", controls={"travel": 1}, protected_region_revision="ok")
    unmatched = MotorControlPermission(id="other", controls={"other": 1})
    prepare(request(control_permissions=(permission, unmatched)))
    assert dispatch()["status"] == "completed"


def test_unknown_step_receipt_is_quarantined(tmp_path):
    class Unknown(Alternatives):
        def execute(self, step, *, operation_id, cancel, deadline):
            return {"operation_id": "wrong", "status": "completed"}

    _, motor, prepare, dispatch, _, _, environment, _ = setup(tmp_path, adapter_class=Unknown)
    prepare()
    with pytest.raises(MotorOutcomeUnknown, match="prove"):
        dispatch()
    assert motor.progress(f"{environment}:fill") is not None

    with pytest.raises(MotorOutcomeUnknown, match="reconciliation"):
        direct(motor, request(), operation_id=f"{environment}:fill")


def test_direct_execution_guards_and_authority_fences(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    driver, motor, *_ = setup(tmp_path)
    with motor.journal.with_suffix(motor.journal.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Conflict, match="input owner"):
            direct(motor, request(), operation_id="file-lock")
        fcntl.flock(lock, fcntl.LOCK_UN)

    with motor.journal.with_suffix(motor.journal.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Conflict, match="settling"):
            motor.acknowledge_unknown()
        fcntl.flock(lock, fcntl.LOCK_UN)

    receipt = direct(motor, request(), operation_id="replay")
    assert direct(motor, request(), operation_id="replay") == receipt
    with pytest.raises(Conflict, match="reused"):
        direct(motor, request().model_copy(update={"arguments": {"text": "other"}}), operation_id="replay")

    class ReadOnly(Alternatives):
        def plan(self, req, observation):
            return (
                MotorCandidate(
                    id="effect",
                    description="effect",
                    steps=(MotorStep(operation="click", target=req.target),),
                ),
            )

    _, read_motor, *_ = setup(tmp_path / "readonly", adapter_class=ReadOnly)
    assert (
        direct(read_motor, request(), operation_id="readonly", write=False)["reason_code"]
        == "write_authority_required"
    )

    _, duplicate, *_ = setup(tmp_path / "duplicate-step")
    duplicate._step_recorded = lambda steps, step_id: True
    assert direct(duplicate, request(), operation_id="duplicate-step")["reason_code"] == "duplicate_effect"


def test_selector_budget_and_model_guards(tmp_path):
    class Selector:
        model = "selector"
        endpoint = "https://selector.test"

        def maximum_cost(self, *_):
            return 2

        def select(self, *args, **kwargs):
            return MotorSelection(candidate_id="fill-direct", model=self.model, cost_micros=5)

    _, motor, *_ = setup(tmp_path, selector=Selector())
    assert direct(motor, request(), operation_id="budget", budget=1)["reason_code"] == "budget_exceeded"
    with pytest.raises(BudgetExceeded, match="reservation"):
        direct(motor, request(), operation_id="selector-cost", budget=4)

    class WrongModel(Selector):
        def select(self, *args, **kwargs):
            return MotorSelection(candidate_id="fill-direct", model="wrong", cost_micros=0)

    _, wrong, *_ = setup(tmp_path / "wrong-model", selector=WrongModel())
    assert direct(wrong, request(), operation_id="wrong-model")["reason_code"] == "selector_model_changed"

    class Expires(Selector):
        def select(self, *args, **kwargs):
            raise AssertionError("selector should not be called")

    _, expires, *_ = setup(tmp_path / "expires", selector=Expires())
    calls = 0

    def authority(_):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise Forbidden("expired")

    assert (
        direct(expires, request(), operation_id="before-selection", authority=authority)["reason_code"]
        == "authority_expired"
    )


def test_authority_expiry_is_recorded_at_each_boundary(tmp_path):
    _, motor, *_ = setup(tmp_path)
    calls = 0

    def expires(_):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise Forbidden("expired")

    assert (
        direct(motor, request(), operation_id="before-step", authority=expires)["reason_code"]
        == "authority_expired"
    )

    _, after_motor, *_ = setup(tmp_path / "after", adapter_class=Alternatives)
    calls = 0

    def expires_after(_):
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise Forbidden("expired")

    assert (
        direct(after_motor, request(), operation_id="after-step", authority=expires_after)["reason_code"]
        == "authority_expired"
    )

    _, before_effect, *_ = setup(tmp_path / "before-effect", adapter_class=Alternatives)
    calls = 0

    def expires_before_effect(_):
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise Forbidden("expired")

    assert (
        direct(before_effect, request(), operation_id="before-effect", authority=expires_before_effect)[
            "reason_code"
        ]
        == "authority_expired"
    )

    _, slow_motor, *_ = setup(tmp_path / "slow")
    original_execute = slow_motor.adapter.driver.execute

    def slow_execute(*args, **kwargs):
        import time

        time.sleep(0.08)
        return original_execute(*args, **kwargs)

    slow_motor.adapter.driver.execute = slow_execute
    assert direct(slow_motor, request(), operation_id="slow")["status"] == "completed"

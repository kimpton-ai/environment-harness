from pathlib import Path

import pytest

from environment_harness.errors import Conflict, Forbidden
from environment_harness.motor import MotorExecutor, MotorOutcomeUnknown
from environment_harness.motor_adapters import BrowserMotor
from environment_harness.motor_contracts import (
    MotorCandidate,
    MotorExecutionMetadata,
    MotorProfile,
    MotorReceipt,
    MotorRequest,
    MotorSelection,
    MotorStep,
    PreparedSuccessorAdmission,
    PreparedSuccessorIntent,
    UnsupportedPreparation,
)


class Driver:
    def observe(self):
        return {"revision": "r1", "elements": [{"element_id": "button", "selector": "#button"}]}

    def execute(self, operation, payload, **kwargs):
        return {"operation_id": kwargs["operation_id"], "status": "completed"}

    def stop(self):
        pass

    def lookup(self, operation_id):
        return None


class PreparedBrowser(BrowserMotor):
    supports_prepared_successors = True

    def __init__(self, driver):
        super().__init__(driver)
        self.admissions = 0
        self.fail_prepare = False
        self.reconciled = False

    def prepare_successor(self, intent):
        if self.fail_prepare:
            raise TimeoutError("lost preparation acknowledgement")
        return intent

    def reconcile(self, operation_id):
        self.reconciled = True
        return {"operation_id": operation_id, "status": "completed"}

    def admit_successor(self, intent, *, predecessor):
        self.admissions += 1
        return PreparedSuccessorAdmission(
            intent_id=intent.intent_id,
            predecessor_operation_id=intent.predecessor_operation_id,
            operation_id=intent.operation_id,
            metadata=intent.metadata,
            status="admitted",
            admitted_at_ms=2,
        )


class NativePreparedBrowser(PreparedBrowser):
    native_admits_prepared_successors = True

    def __init__(self, driver):
        super().__init__(driver)
        self.native_admission = None
        self.stops = 0

    def stop(self):
        self.stops += 1
        super().stop()

    def reconcile_prepared_successor(self, intent):
        return self.native_admission


class BadPreparedBrowser(PreparedBrowser):
    def __init__(self, driver, mode):
        super().__init__(driver)
        self.mode = mode

    def prepare_successor(self, intent):
        if self.mode == "unsupported":
            raise UnsupportedPreparation("unsupported")
        if self.mode == "error":
            raise RuntimeError("lost")
        if self.mode == "invalid":
            return {"intent_id": intent.intent_id}
        return intent

    def admit_successor(self, intent, *, predecessor):
        if self.mode == "error":
            raise RuntimeError("lost")
        if self.mode == "invalid":
            return {"status": "admitted"}
        return super().admit_successor(intent, predecessor=predecessor)


def successor_setup(tmp_path: Path):
    driver = Driver()
    adapter = PreparedBrowser(driver)
    executor = MotorExecutor(
        adapter, MotorProfile(adapter=adapter.implementation), journal=tmp_path / "motor.sqlite"
    )
    request = MotorRequest(
        skill="click",
        target={"element_id": "button", "selector": "#button"},
        expected={"ok": True},
        observation_revision="r1",
        goal_revision="g1",
        stop_epoch=executor.stop_epoch,
    )
    selection = MotorSelection(candidate_id="click-direct", model="test")
    metadata = MotorExecutionMetadata(
        owner="owner",
        epoch=1,
        goal_revision="g1",
        observation_revision="r1",
        stop_epoch=executor.stop_epoch,
    )
    intent = PreparedSuccessorIntent(
        intent_id="intent",
        operation_id="next",
        predecessor_operation_id="current",
        candidate_id="click-direct",
        metadata=metadata,
        step=MotorStep(operation="click", target=request.target),
        prepared_at_ms=1e13,
    )

    def authority(_):
        return {
            "owner": "owner",
            "epoch": 1,
            "goal_revision": "g1",
            "observation_revision": "r1",
            "stop_epoch": executor.stop_epoch,
        }

    predecessor = MotorReceipt(
        operation_id="current",
        status="completed",
        outcome="completed",
        effect="applied",
        profile=executor.profile,
        request=request,
        elapsed_ms=1,
    )
    return executor, adapter, request, selection, intent, authority, predecessor


def test_prepared_successor_contract_carries_fencing_metadata():
    metadata = MotorExecutionMetadata(
        owner="goal-owner",
        epoch=4,
        goal_revision="g7",
        observation_revision="o8",
        stop_epoch=2,
        ui_revision="screen-3",
    )
    intent = PreparedSuccessorIntent(
        intent_id="intent-1",
        operation_id="op-2",
        predecessor_operation_id="op-1",
        candidate_id="move-direct",
        metadata=metadata,
        step=MotorStep(operation="move", target={"x": 1}),
        prepared_at_ms=12.5,
    )
    assert intent.contract_version == "motor.prepared-successor.v1"
    assert intent.metadata.stop_epoch == 2


def test_sequential_adapters_explicitly_reject_preparation():
    adapter = BrowserMotor(Driver())
    metadata = MotorExecutionMetadata(owner="owner", goal_revision="g", observation_revision="r1")
    intent = PreparedSuccessorIntent(
        intent_id="intent",
        operation_id="next",
        predecessor_operation_id="current",
        candidate_id="click-direct",
        metadata=metadata,
        step=MotorStep(operation="click", target={"element_id": "x"}),
        prepared_at_ms=0,
    )
    with pytest.raises(UnsupportedPreparation):
        adapter.prepare_successor(intent)


def test_prepared_admission_is_one_shot_and_checks_predecessor_identity(tmp_path):
    executor, adapter, request, selection, intent, authority, predecessor = successor_setup(tmp_path)
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    with pytest.raises(Conflict):
        executor.admit_successor(
            intent.intent_id,
            request=request,
            selection=selection,
            authority=authority,
            predecessor={**predecessor.model_dump(mode="json"), "operation_id": "wrong"},
        )
    first = executor.admit_successor(
        intent.intent_id,
        request=request,
        selection=selection,
        authority=authority,
        predecessor=predecessor.model_dump(mode="json"),
    )
    second = executor.admit_successor(
        intent.intent_id,
        request=request,
        selection=selection,
        authority=authority,
        predecessor=predecessor.model_dump(mode="json"),
    )
    assert first.status == second.status == "admitted"
    assert adapter.admissions == 1


def test_restart_discards_uncommitted_successor_and_unknown_cannot_retry(tmp_path):
    executor, adapter, request, selection, intent, authority, _ = successor_setup(tmp_path)
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    restarted = MotorExecutor(
        PreparedBrowser(Driver()), executor.profile, journal=executor.journal, discard_prepared=True
    )
    with pytest.raises(MotorOutcomeUnknown):
        restarted.admit_successor(intent.intent_id, request=request, selection=selection, authority=authority)


def test_preparation_fault_is_quarantined_until_native_reconciliation(tmp_path):
    executor, adapter, request, selection, intent, authority, _ = successor_setup(tmp_path)
    adapter.fail_prepare = True
    with pytest.raises(MotorOutcomeUnknown):
        executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    with pytest.raises(MotorOutcomeUnknown):
        executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    admission = executor.reconcile_successor(intent.intent_id)
    assert admission.status == "admitted" and adapter.reconciled


def test_owner_epoch_change_rejects_admission(tmp_path):
    executor, adapter, request, selection, intent, authority, _ = successor_setup(tmp_path)
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)

    def changed(_):
        return {**authority(request), "epoch": 2}

    admission = executor.admit_successor(
        intent.intent_id, request=request, selection=selection, authority=changed
    )
    assert admission.status == "rejected"
    assert adapter.admissions == 0


def test_native_admission_proof_wins_after_freshness_expiry(tmp_path):
    executor, _, request, selection, intent, authority, predecessor = successor_setup(tmp_path)
    native = NativePreparedBrowser(Driver())
    executor = MotorExecutor(native, executor.profile, journal=executor.journal)
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    native.native_admission = PreparedSuccessorAdmission(
        intent_id=intent.intent_id,
        predecessor_operation_id=intent.predecessor_operation_id,
        operation_id=intent.operation_id,
        metadata=intent.metadata,
        status="admitted",
    )
    admission = executor.admit_successor(
        intent.intent_id,
        request=request,
        selection=selection,
        authority=authority,
        predecessor=predecessor.model_dump(mode="json"),
    )
    assert admission.status == "admitted"
    assert native.admissions == 0


def test_native_restart_stops_then_reconciles_accepted_intent(tmp_path):
    executor, _, request, selection, intent, authority, _ = successor_setup(tmp_path)
    native = NativePreparedBrowser(Driver())
    executor = MotorExecutor(native, executor.profile, journal=executor.journal)
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    native.native_admission = PreparedSuccessorAdmission(
        intent_id=intent.intent_id,
        predecessor_operation_id=intent.predecessor_operation_id,
        operation_id=intent.operation_id,
        metadata=intent.metadata,
        status="admitted",
    )
    restarted = MotorExecutor(native, executor.profile, journal=executor.journal, discard_prepared=True)
    assert native.stops == 1
    assert (
        restarted.admit_successor(
            intent.intent_id, request=request, selection=selection, authority=authority
        ).status
        == "admitted"
    )


def test_unknown_successor_blocks_new_prepare_and_execute_until_reconciled(tmp_path):
    executor, _, request, selection, intent, authority, _ = successor_setup(tmp_path)
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    with executor._db() as db:
        db.execute("UPDATE motor_successors SET status='unknown' WHERE id=?", (intent.intent_id,))
    with pytest.raises(MotorOutcomeUnknown, match="unknown successor"):
        executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    envelope = {"payload": request.model_dump(mode="json"), "write": True}
    with pytest.raises(MotorOutcomeUnknown, match="unknown successor"):
        executor.execute("new-operation", envelope, 1000, authority=authority)


def test_successor_authority_rejects_each_stale_fence(tmp_path):
    executor, _, request, selection, intent, authority, _ = successor_setup(tmp_path)
    cases = [
        ("request", object()),
        ("selection", MotorSelection(candidate_id="wrong", model="test")),
        ("goal", request.model_copy(update={"goal_revision": "g2"})),
        ("observation", request.model_copy(update={"observation_revision": "r2"})),
        ("stop", request.model_copy(update={"stop_epoch": 3})),
    ]
    for name, value in cases:
        if name == "request":
            with pytest.raises(TypeError):
                executor._authorize_successor(intent, value, selection, authority)
        elif name == "selection":
            with pytest.raises(Exception):
                executor._authorize_successor(intent, request, value, authority)
        else:
            with pytest.raises(Conflict):
                executor._authorize_successor(intent, value, selection, authority)
    with pytest.raises(Forbidden):
        executor._authorize_successor(intent, request, selection, None)
    with pytest.raises(MotorOutcomeUnknown):
        executor._authorize_successor(
            intent, request, selection, lambda _: (_ for _ in ()).throw(RuntimeError())
        )
    with pytest.raises(Forbidden):
        executor._authorize_successor(intent, request, selection, lambda _: None)


def test_successor_authority_rejects_freshness_plan_and_permission_changes(tmp_path):
    executor, _, request, selection, intent, authority, _ = successor_setup(tmp_path)
    old = intent.model_copy(update={"prepared_at_ms": 0, "freshness_ms": 1})
    with pytest.raises(Conflict, match="freshness"):
        executor._authorize_successor(old, request, selection, authority)
    old_meta = intent.metadata.model_copy(update={"observed_at_ms": 0})
    with pytest.raises(Conflict, match="observation freshness"):
        executor._authorize_successor(
            intent.model_copy(update={"metadata": old_meta}), request, selection, authority
        )
    tick_meta = intent.metadata.model_copy(update={"native_tick": 5})
    with pytest.raises(Conflict, match="native observation"):
        executor._authorize_successor(
            intent.model_copy(update={"metadata": tick_meta}), request, selection, authority
        )
    tick_intent = intent.model_copy(update={"expires_tick": 1})
    with pytest.raises(Conflict, match="tick window"):
        executor._authorize_successor(
            tick_intent, request, selection, lambda _: {**authority(request), "native_tick": 2}
        )

    class Invalid(PreparedBrowser):
        def plan(self, request, observation):
            raise ValueError("invalid")

    bad = MotorExecutor(Invalid(Driver()), executor.profile, journal=tmp_path / "invalid.sqlite")
    with pytest.raises(Forbidden, match="plan"):
        bad._authorize_successor(intent, request, selection, authority)


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("unsupported", UnsupportedPreparation),
        ("error", MotorOutcomeUnknown),
        ("invalid", MotorOutcomeUnknown),
    ],
)
def test_preparation_faults_are_durable(mode, expected, tmp_path):
    executor, _, request, selection, intent, authority, _ = successor_setup(tmp_path)
    adapter = BadPreparedBrowser(Driver(), mode)
    executor = MotorExecutor(adapter, executor.profile, journal=tmp_path / f"{mode}.sqlite")
    with pytest.raises(expected):
        executor.prepare_successor(intent, request=request, selection=selection, authority=authority)


def test_sequential_adapter_explicit_prepare_and_admit_guards(tmp_path):
    adapter = BrowserMotor(Driver())
    metadata = MotorExecutionMetadata(owner="owner", goal_revision="g", observation_revision="r1")
    intent = PreparedSuccessorIntent(
        intent_id="intent",
        operation_id="next",
        predecessor_operation_id="current",
        candidate_id="click-direct",
        metadata=metadata,
        step=MotorStep(operation="click", target={"element_id": "x"}),
        prepared_at_ms=0,
    )
    adapter.supports_prepared_successors = True
    with pytest.raises(TypeError):
        adapter.prepare_successor(object())
    assert adapter.prepare_successor(intent) == intent
    with pytest.raises(NotImplementedError):
        adapter.admit_successor(intent, predecessor=None)
    assert adapter.reconcile_prepared_successor(intent) is None


def test_prepare_idempotency_slot_and_authorization_guards(tmp_path):
    executor, _, request, selection, intent, authority, _ = successor_setup(tmp_path)
    with pytest.raises(TypeError):
        executor.prepare_successor(object(), request=request, selection=selection, authority=authority)
    with pytest.raises(UnsupportedPreparation):
        MotorExecutor(
            BrowserMotor(Driver()), executor.profile, journal=tmp_path / "unsupported.sqlite"
        ).prepare_successor(intent, request=request, selection=selection, authority=authority)
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    assert (
        executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
        == intent
    )
    changed = intent.model_copy(update={"operation_id": "different"})
    with pytest.raises(Conflict, match="ID reused"):
        executor.prepare_successor(changed, request=request, selection=selection, authority=authority)
    with pytest.raises(Conflict, match="authorization changed"):
        executor.prepare_successor(
            intent,
            request=request,
            selection=selection.model_copy(update={"model": "other"}),
            authority=authority,
        )
    other = intent.model_copy(update={"intent_id": "other"})
    with pytest.raises(Conflict, match="one-slot"):
        executor.prepare_successor(other, request=request, selection=selection, authority=authority)


@pytest.mark.parametrize(
    "kind",
    ["missing", "auth", "unknown", "invalid", "native_error", "native_none", "native_bad", "native_unknown"],
)
def test_admission_and_reconciliation_outcomes_are_fail_closed(kind, tmp_path):
    executor, adapter, request, selection, intent, authority, predecessor = successor_setup(tmp_path)
    if kind == "missing":
        with pytest.raises(Conflict):
            executor.admit_successor("missing", request=request, selection=selection, authority=authority)
        return
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    if kind == "auth":
        result = executor.admit_successor(
            intent.intent_id, request=request, selection=selection, authority=lambda _: {"owner": "other"}
        )
        assert result.status == "rejected"
        return
    if kind == "unknown":
        with executor._db() as db:
            db.execute("UPDATE motor_successors SET status='unknown' WHERE id=?", (intent.intent_id,))
        with pytest.raises(MotorOutcomeUnknown):
            executor.admit_successor(
                intent.intent_id, request=request, selection=selection, authority=authority
            )
        return
    if kind == "invalid":
        with executor._db() as db:
            db.execute(
                "UPDATE motor_successors SET status='rejected',admission=? WHERE id=?",
                ("{}", intent.intent_id),
            )
        with pytest.raises(MotorOutcomeUnknown):
            executor.admit_successor(
                intent.intent_id, request=request, selection=selection, authority=authority
            )
        return
    if kind == "native_error" or kind.startswith("native_"):
        native = NativePreparedBrowser(Driver())
        if kind == "native_error":
            native.reconcile_prepared_successor = lambda _: (_ for _ in ()).throw(RuntimeError())
        elif kind == "native_none":
            native.native_admission = None
        elif kind == "native_bad":
            native.native_admission = object()
        else:
            native.native_admission = PreparedSuccessorAdmission(
                intent_id=intent.intent_id,
                predecessor_operation_id=intent.predecessor_operation_id,
                operation_id=intent.operation_id,
                metadata=intent.metadata,
                status="unknown",
            )
        native_executor = MotorExecutor(native, executor.profile, journal=tmp_path / f"{kind}.sqlite")
        native_executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
        with pytest.raises(MotorOutcomeUnknown):
            native_executor.admit_successor(
                intent.intent_id, request=request, selection=selection, authority=authority
            )


def test_additional_authority_and_admission_guards(tmp_path):
    executor, _, request, selection, intent, authority, predecessor = successor_setup(tmp_path)
    selector_profile = MotorProfile(mode="jev", adapter=executor.profile.adapter, selector_model="model")
    selected = MotorExecutor(
        PreparedBrowser(Driver()),
        selector_profile,
        journal=tmp_path / "selected.sqlite",
        selector=type("S", (), {"model": "model"})(),
    )
    with pytest.raises(Forbidden, match="selector model"):
        selected._authorize_successor(
            intent, request, selection.model_copy(update={"model": "other"}), authority
        )
    frame_meta = intent.metadata.model_copy(update={"camera_revision": "camera-1"})
    with pytest.raises(Conflict, match="camera_revision"):
        executor._authorize_successor(
            intent.model_copy(update={"metadata": frame_meta}), request, selection, authority
        )

    class Stale(PreparedBrowser):
        def observe(self):
            return {"revision": "changed", "elements": []}

    stale = MotorExecutor(Stale(Driver()), executor.profile, journal=tmp_path / "stale.sqlite")
    with pytest.raises(Conflict, match="observation is stale"):
        stale._authorize_successor(intent, request, selection, authority)

    class WrongPlan(PreparedBrowser):
        def plan(self, request, observation):
            return ()

    wrong = MotorExecutor(WrongPlan(Driver()), executor.profile, journal=tmp_path / "wrong.sqlite")
    with pytest.raises(Forbidden, match="selected plan"):
        wrong._authorize_successor(intent, request, selection, authority)

    with pytest.raises(UnsupportedPreparation):
        MotorExecutor(
            BrowserMotor(Driver()), executor.profile, journal=tmp_path / "unsupported-admit.sqlite"
        ).admit_successor(intent.intent_id, request=request, selection=selection, authority=authority)
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    with pytest.raises(Conflict, match="authorization differs"):
        executor.admit_successor(
            intent.intent_id,
            request=request,
            selection=selection.model_copy(update={"candidate_id": "x"}),
            authority=authority,
        )
    with pytest.raises(Conflict, match="predecessor receipt"):
        executor.admit_successor(
            intent.intent_id,
            request=request,
            selection=selection,
            authority=authority,
            predecessor={"operation_id": "wrong"},
        )
    assert (
        executor.admit_successor(
            intent.intent_id, request=request, selection=selection, authority=authority
        ).status
        == "unknown"
    )
    executor2, _, request2, selection2, intent2, authority2, predecessor2 = successor_setup(
        tmp_path / "normal2"
    )
    executor2.prepare_successor(intent2, request=request2, selection=selection2, authority=authority2)
    rejected = executor2.admit_successor(
        intent2.intent_id,
        request=request2,
        selection=selection2,
        authority=authority2,
        predecessor={**predecessor2.model_dump(mode="json"), "status": "blocked"},
    )
    assert rejected.status == "rejected"


def test_remaining_preparation_and_reconciliation_outcomes(tmp_path):
    executor, _, request, selection, intent, authority, predecessor = successor_setup(tmp_path)
    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    with executor._db() as db:
        db.execute("UPDATE motor_successors SET status='discarded' WHERE id=?", (intent.intent_id,))
    with pytest.raises(MotorOutcomeUnknown):
        executor.prepare_successor(intent, request=request, selection=selection, authority=authority)

    error_executor, _, req, sel, err_intent, auth, pred = successor_setup(tmp_path / "error")
    error_adapter = BadPreparedBrowser(Driver(), "error")
    error_executor = MotorExecutor(error_adapter, error_executor.profile, journal=tmp_path / "error.sqlite")
    with pytest.raises(MotorOutcomeUnknown):
        error_executor.prepare_successor(err_intent, request=req, selection=sel, authority=auth)

    invalid_executor, _, req2, sel2, invalid_intent, auth2, pred2 = successor_setup(
        tmp_path / "invalid-admission"
    )
    invalid_adapter = BadPreparedBrowser(Driver(), "invalid")
    invalid_executor = MotorExecutor(
        invalid_adapter, invalid_executor.profile, journal=tmp_path / "invalid-admission.sqlite"
    )
    with pytest.raises(MotorOutcomeUnknown):
        invalid_executor.prepare_successor(invalid_intent, request=req2, selection=sel2, authority=auth2)

    with pytest.raises(Conflict):
        executor.reconcile_successor("missing")
    with executor._db() as db:
        db.execute("UPDATE motor_successors SET status='unknown' WHERE id=?", (intent.intent_id,))
    executor.adapter.reconcile = lambda operation_id: {"operation_id": operation_id, "status": "pending"}
    with pytest.raises(MotorOutcomeUnknown):
        executor.reconcile_successor(intent.intent_id)

    native = NativePreparedBrowser(Driver())
    native.native_admission = PreparedSuccessorAdmission(
        intent_id=intent.intent_id,
        predecessor_operation_id=intent.predecessor_operation_id,
        operation_id=intent.operation_id,
        metadata=intent.metadata,
        status="unknown",
    )
    native_executor = MotorExecutor(native, executor.profile, journal=tmp_path / "native-reconcile.sqlite")
    native_executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    with native_executor._db() as db:
        db.execute("UPDATE motor_successors SET status='unknown' WHERE id=?", (intent.intent_id,))
    with pytest.raises(MotorOutcomeUnknown):
        native_executor.reconcile_successor(intent.intent_id)


def test_prepared_successor_remaining_branch_guards(tmp_path):
    executor, adapter, request, selection, intent, authority, predecessor = successor_setup(tmp_path)

    bad_step = MotorStep(operation="click", target={"element_id": "other"})

    class UnsafePlan(PreparedBrowser):
        def plan(self, request, observation):
            return (MotorCandidate(id="click-direct", description="bad", steps=(bad_step,)),)

    unsafe = MotorExecutor(UnsafePlan(Driver()), executor.profile, journal=tmp_path / "unsafe.sqlite")
    unsafe_intent = intent.model_copy(update={"step": bad_step})
    with pytest.raises(Forbidden, match="target_changed"):
        unsafe._authorize_successor(unsafe_intent, request, selection, authority)

    executor.prepare_successor(intent, request=request, selection=selection, authority=authority)
    with pytest.raises(Conflict, match="not awaiting"):
        executor.reconcile_successor(intent.intent_id)
    admitted = executor.admit_successor(
        intent.intent_id,
        request=request,
        selection=selection,
        authority=authority,
        predecessor=predecessor.model_dump(mode="json"),
    )
    assert executor.reconcile_successor(intent.intent_id).status == admitted.status
    assert executor.reconcile("current") is not None

    unknown_executor, _, req, sel, unknown_intent, auth, pred = successor_setup(tmp_path / "auth-unknown")
    unknown_executor.prepare_successor(unknown_intent, request=req, selection=sel, authority=auth)

    def unknown_authority(_):
        raise MotorOutcomeUnknown("authority unavailable")

    with pytest.raises(MotorOutcomeUnknown):
        unknown_executor.admit_successor(
            unknown_intent.intent_id, request=req, selection=sel, authority=unknown_authority
        )

    class FaultyAdmission(PreparedBrowser):
        def __init__(self, driver, result):
            super().__init__(driver)
            self.result = result

        def admit_successor(self, intent, *, predecessor):
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result

    for name, result in (("error", RuntimeError("lost")), ("invalid", {"status": "admitted"})):
        ex, _, req, sel, inx, auth, pred = successor_setup(tmp_path / name)
        fault = FaultyAdmission(Driver(), result)
        ex = MotorExecutor(fault, ex.profile, journal=tmp_path / f"{name}-fault.sqlite")
        ex.prepare_successor(inx, request=req, selection=sel, authority=auth)
        if name == "error":
            with pytest.raises(MotorOutcomeUnknown):
                ex.admit_successor(
                    inx.intent_id,
                    request=req,
                    selection=sel,
                    authority=auth,
                    predecessor=pred.model_dump(mode="json"),
                )
        else:
            with pytest.raises(MotorOutcomeUnknown, match="did not return"):
                ex.admit_successor(
                    inx.intent_id,
                    request=req,
                    selection=sel,
                    authority=auth,
                    predecessor=pred.model_dump(mode="json"),
                )

    native = NativePreparedBrowser(Driver())
    ex, _, req, sel, inx, auth, pred = successor_setup(tmp_path / "native-identity")
    native.native_admission = PreparedSuccessorAdmission(
        intent_id="wrong",
        predecessor_operation_id=inx.predecessor_operation_id,
        operation_id=inx.operation_id,
        metadata=inx.metadata,
        status="admitted",
    )
    nex = MotorExecutor(native, ex.profile, journal=tmp_path / "native-identity.sqlite")
    nex.prepare_successor(inx, request=req, selection=sel, authority=auth)
    with pytest.raises(MotorOutcomeUnknown, match="identity"):
        nex.admit_successor(inx.intent_id, request=req, selection=sel, authority=auth)

    native = NativePreparedBrowser(Driver())
    native.reconcile_prepared_successor = lambda _: (_ for _ in ()).throw(RuntimeError("lost"))
    rex = MotorExecutor(native, ex.profile, journal=tmp_path / "native-error.sqlite")
    rex.prepare_successor(inx, request=req, selection=sel, authority=auth)
    with rex._db() as db:
        db.execute("UPDATE motor_successors SET status='unknown' WHERE id=?", (inx.intent_id,))
    with pytest.raises(MotorOutcomeUnknown, match="remains unknown"):
        rex.reconcile_successor(inx.intent_id)

    native = NativePreparedBrowser(Driver())
    native.native_admission = PreparedSuccessorAdmission(
        intent_id="wrong",
        predecessor_operation_id=inx.predecessor_operation_id,
        operation_id=inx.operation_id,
        metadata=inx.metadata,
        status="admitted",
    )
    rex = MotorExecutor(native, ex.profile, journal=tmp_path / "native-reconcile-identity.sqlite")
    rex.prepare_successor(inx, request=req, selection=sel, authority=auth)
    with rex._db() as db:
        db.execute("UPDATE motor_successors SET status='unknown' WHERE id=?", (inx.intent_id,))
    with pytest.raises(MotorOutcomeUnknown, match="identity"):
        rex.reconcile_successor(inx.intent_id)

    native = NativePreparedBrowser(Driver())
    native.native_admission = PreparedSuccessorAdmission(
        intent_id=inx.intent_id,
        predecessor_operation_id=inx.predecessor_operation_id,
        operation_id=inx.operation_id,
        metadata=inx.metadata,
        status="unknown",
    )
    restart = MotorExecutor(native, ex.profile, journal=tmp_path / "restart-unknown.sqlite")
    restart.prepare_successor(inx, request=req, selection=sel, authority=auth)
    recovered = MotorExecutor(
        native, ex.profile, journal=tmp_path / "restart-unknown.sqlite", discard_prepared=True
    )
    with recovered._db() as db:
        assert (
            db.execute("SELECT status FROM motor_successors WHERE id=?", (inx.intent_id,)).fetchone()[0]
            == "unknown"
        )

    ex, adapter, req, sel, inx, auth, pred = successor_setup(tmp_path / "bad-reconcile")
    ex.prepare_successor(inx, request=req, selection=sel, authority=auth)
    with ex._db() as db:
        db.execute("UPDATE motor_successors SET status='unknown' WHERE id=?", (inx.intent_id,))
    adapter.reconcile = lambda _: {"operation_id": "other", "status": "completed"}
    with pytest.raises(MotorOutcomeUnknown, match="identify"):
        ex.reconcile_successor(inx.intent_id)

    rejected, adapter, req, sel, inx, auth, pred = successor_setup(tmp_path / "rejected-reconcile")
    rejected.prepare_successor(inx, request=req, selection=sel, authority=auth)
    with rejected._db() as db:
        db.execute("UPDATE motor_successors SET status='unknown' WHERE id=?", (inx.intent_id,))
    adapter.reconcile = lambda _: {"operation_id": inx.operation_id, "status": "rejected"}
    assert rejected.reconcile_successor(inx.intent_id).status == "rejected"

    adapter = BrowserMotor(Driver())
    direct_intent = inx
    with pytest.raises(UnsupportedPreparation):
        adapter.admit_successor(direct_intent, predecessor=None)
    adapter.supports_prepared_successors = True
    for status in ("completed", "rejected", "pending"):
        adapter.driver.lookup = lambda operation_id, status=status: {
            "operation_id": operation_id,
            "status": status,
        }
        result = adapter.reconcile_prepared_successor(direct_intent)
        assert (
            result.status == ({"completed": "admitted", "rejected": "rejected", "pending": "unknown"}[status])
        )

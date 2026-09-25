"""Sequential decision orchestration with durable charge and native-effect identities."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from environment_harness.contracts import OperationSpec
from environment_harness.errors import BudgetExceeded, Conflict, Forbidden
from environment_harness.operations import EnvironmentOperation

from .contracts import (
    Admission,
    BoundedInvocation,
    DecisionPolicy,
    DecisionReceipt,
    NativeReceipt,
    Observation,
    OutcomeUncertain,
    PreparedSuccessor,
    ProviderFailure,
    Verification,
)
from .ledger import Ledger, encode


class _AdmissionRejected(Forbidden):
    """A proven native rejection with no effect."""


class DecisionOperation(EnvironmentOperation):
    """Environment-owned operation; the control owns final native-thread admission.

    A selector has no execution authority. Unknown charges and effects block a
    final SDK receipt. Reopening a ledger never extends an invocation's UTC expiry.
    """

    def __init__(
        self,
        control,
        selector,
        *,
        journal: Path,
        policy: DecisionPolicy,
        name="decision",
        endpoint="decision",
    ):
        if control.implementation != policy.adapter_version:
            raise ValueError("control does not match frozen adapter version")
        if selector is None or selector.model != policy.selector_model:
            raise ValueError("configured profile requires its exact selector; no fallback")
        if getattr(selector, "endpoint", policy.endpoint) != policy.endpoint:
            raise ValueError("selector endpoint differs from frozen policy")
        self.control, self.selector, self.policy = control, selector, policy
        self.endpoint, self.name = endpoint, name
        self.ledger = Ledger(journal)
        self._cancel = threading.Event()
        self._mutex = threading.Lock()

    @property
    def spec(self):
        # Return a fresh SDK record so nested config mutation cannot alter identity.
        return OperationSpec(
            name=self.name,
            version="decision-operation.v1",
            config={"policy": self.policy.model_dump(mode="json"), "environment_id": self.control.identity},
        )

    @property
    def stop_epoch(self):
        return self.ledger.stop_epoch

    def validate(self, request, manifest):
        invocation = BoundedInvocation.model_validate(request["payload"])
        if invocation.binding.environment_id != self.control.identity:
            raise Forbidden("decision environment identity mismatch")
        return invocation

    def stop(self):
        self._cancel.set()
        # Release native controls before any storage wait, independently of inference.
        self.control.stop()
        with self.ledger.db() as db:
            db.execute("UPDATE control SET stop_epoch=stop_epoch+1 WHERE id=1")
            db.execute("UPDATE successors SET status='invalidated' WHERE status='prepared'")
        # Never waits for a provider request or the invocation ownership lock.

    def _reason(self, operation_id, status, reason):
        with self.ledger.db() as db:
            db.execute(
                "UPDATE invocations SET status=?,reason=? WHERE id=? AND receipt IS NULL",
                (status, reason, operation_id),
            )

    def _timing(self, operation_id, stage, start):
        with self.ledger.db() as db:
            row = db.execute("SELECT timings FROM invocations WHERE id=?", (operation_id,)).fetchone()
            timings = json.loads(row[0])
            timings[stage] = timings.get(stage, 0) + (time.monotonic() - start) * 1000
            db.execute("UPDATE invocations SET timings=? WHERE id=?", (encode(timings), operation_id))

    def _active(self, invocation, deadline, authority, binding=None):
        if self._cancel.is_set() or self.stop_epoch != invocation.binding.stop_epoch:
            raise Forbidden("cancelled by stop epoch")
        if time.monotonic() >= deadline or datetime.now(timezone.utc) >= invocation.expires_at:
            raise Forbidden("invocation deadline expired")
        authority(binding or invocation.binding)

    @staticmethod
    def _response_evidence(value):
        if isinstance(value, bytes):
            import base64

            return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
        return value

    def _record_selection(self, attempt_id, selection):
        with self.ledger.db() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row["status"] == "resolved":
                if row["selection"] != encode(selection):
                    raise Conflict("conflicting provider receipt")
                return
            cost = selection.cost_micros
            resolved = cost is not None and cost <= row["reservation"]
            db.execute(
                "UPDATE attempts SET status=?,cost=?,selection=? WHERE id=?",
                ("resolved" if resolved else "unknown", cost, encode(selection), attempt_id),
            )
            self.ledger.event(
                db,
                row["invocation"],
                "selector.response",
                {
                    "attempt_id": attempt_id,
                    "selection_id": row["selection_id"],
                    "contribution": "selector",
                    "selection": selection.model_dump(mode="json"),
                },
            )

    def _select(self, operation_id, invocation, observation, decisions, selection_id, deadline, authority):
        bound = self.selector.maximum_charge_micros(invocation.objective, observation, decisions)
        if type(bound) is not int or bound < 0:
            raise BudgetExceeded("verified conservative charge bound is required")
        for attempt_number in range(2):
            self._active(invocation, deadline, authority)
            attempt_id = f"{selection_id}:attempt:{attempt_number}"
            with self.ledger.db() as db:
                spent, reserved, _ = self.ledger.charges(db, operation_id)
                maximum = db.execute(
                    "SELECT maximum FROM invocations WHERE id=?", (operation_id,)
                ).fetchone()[0]
                if spent + reserved + bound > maximum:
                    raise BudgetExceeded("decision exceeds allocated operation allowance")
                db.execute(
                    "INSERT INTO attempts VALUES (?,?,?,?, 'reserved',NULL,NULL,NULL)",
                    (attempt_id, operation_id, selection_id, bound),
                )
                self.ledger.event(
                    db,
                    operation_id,
                    "selector.reservation",
                    {
                        "attempt_id": attempt_id,
                        "selection_id": selection_id,
                        "maximum_cost_micros": bound,
                        "observation": observation.model_dump(mode="json"),
                        "questions": decisions.model_dump(mode="json"),
                        "policy": self.policy.model_dump(mode="json"),
                        "contribution": "selector",
                    },
                )
            self._active(invocation, deadline, authority)
            model_input = getattr(self.selector, "model_input", None)
            with self.ledger.db() as db:
                if callable(model_input):
                    self.ledger.event(
                        db,
                        operation_id,
                        "selector.input",
                        {
                            "attempt_id": attempt_id,
                            "model_input": model_input(observation, decisions),
                        },
                    )
                db.execute("UPDATE attempts SET status='submitted' WHERE id=?", (attempt_id,))
            finished = threading.Event()
            result = {}

            def infer(attempt=attempt_id, output=result, done=finished):
                try:
                    selected = self.selector.select(
                        attempt,
                        invocation.objective,
                        observation,
                        decisions,
                        cancel=self._cancel,
                        deadline=deadline,
                    )
                    self._record_selection(attempt, selected)
                    output["selection"] = selected
                except BaseException as error:
                    output["error"] = error
                    proven = isinstance(error, ProviderFailure) and not error.submitted and error.uncharged
                    actual_cost = 0 if proven else getattr(error, "cost_micros", None)
                    resolved = type(actual_cost) is int and 0 <= actual_cost <= bound
                    with self.ledger.db() as db:
                        db.execute(
                            "UPDATE attempts SET status=?,cost=?,error=? WHERE id=?",
                            (
                                "resolved" if resolved else "unknown",
                                actual_cost if resolved else None,
                                type(error).__name__,
                                attempt,
                            ),
                        )
                        self.ledger.event(
                            db,
                            operation_id,
                            "selector.failure",
                            {
                                "attempt_id": attempt,
                                "classification": type(error).__name__,
                                "proven_uncharged_before_submission": proven,
                                "model_input": getattr(error, "model_input", {}),
                                "raw_response": getattr(error, "raw_response", {}),
                                "provider_response": self._response_evidence(
                                    getattr(error, "provider_response", None)
                                ),
                                "cost_micros": actual_cost,
                            },
                        )
                finally:
                    done.set()

            start = time.monotonic()
            threading.Thread(target=infer, daemon=True, name="bounded-decision-inference").start()
            while not finished.wait(0.01):
                try:
                    self._active(invocation, deadline, authority)
                except BaseException:
                    self._cancel.set()
                    self.control.stop()
                    self._timing(operation_id, "selection", start)
                    raise
            self._timing(operation_id, "selection", start)
            error = result.get("error")
            if error is not None:
                if (
                    isinstance(error, ProviderFailure)
                    and not error.submitted
                    and error.uncharged
                    and attempt_number == 0
                    and self.policy.retry_before_submission
                ):
                    continue
                raise error
            self._active(invocation, deadline, authority)
            selected = result["selection"]
            if selected.model != self.policy.selector_model:
                raise Forbidden("selector returned a different model")
            if selected.cost_micros is None or selected.cost_micros > bound:
                raise OutcomeUncertain("provider charge unresolved or exceeds verified bound")
            selected.validate_answers(decisions)
            return selected
        raise AssertionError("unreachable selector retry")

    def execute(self, operation_id, request, maximum_cost_micros, *, authority):
        invocation = self.validate(request, {})
        if type(maximum_cost_micros) is not int or maximum_cost_micros < 0:
            raise ValueError("invalid operation allocation")
        maximum = min(maximum_cost_micros, invocation.objective.limits.max_cost_micros)
        if not self._mutex.acquire(blocking=False):
            raise Conflict("decision operation already active")
        try:
            with self.ledger.ownership():
                with self.ledger.db() as db:
                    prior = db.execute("SELECT * FROM invocations WHERE id=?", (operation_id,)).fetchone()
                    if prior:
                        if (
                            prior["request"] != encode(invocation)
                            or prior["policy"] != encode(self.policy)
                            or prior["maximum"] != maximum
                        ):
                            raise Conflict("invocation identifier reused")
                        if prior["receipt"]:
                            return json.loads(prior["receipt"])
                        raise OutcomeUncertain("existing invocation requires lookup; never resubmit")
                    if db.execute(
                        "SELECT 1 FROM effects WHERE status IN ('intent','dispatching','unknown') LIMIT 1"
                    ).fetchone():
                        raise OutcomeUncertain("an earlier native effect requires lookup")
                    db.execute(
                        "INSERT INTO invocations(id,request,policy,maximum,expires_at,status,reason) "
                        "VALUES (?,?,?,?,?,'running','in_progress')",
                        (
                            operation_id,
                            encode(invocation),
                            encode(self.policy),
                            maximum,
                            invocation.expires_at.isoformat(),
                        ),
                    )
                    self.ledger.event(db, operation_id, "invocation.authorized", invocation)
                self._cancel = threading.Event()
                remaining = (invocation.expires_at - datetime.now(timezone.utc)).total_seconds()
                deadline = time.monotonic() + min(remaining, invocation.objective.limits.timeout_ms / 1000)
                finished = threading.Event()

                def watch_authority():
                    while not finished.wait(0.025):
                        try:
                            self._active(invocation, deadline, authority)
                        except BaseException:
                            self._cancel.set()
                            self.control.stop()
                            return

                watcher = threading.Thread(target=watch_authority, daemon=True, name="decision-authority")
                watcher.start()
                try:
                    self._run(operation_id, invocation, deadline, authority)
                except Forbidden as error:
                    self.control.stop()
                    self._reason(operation_id, "cancelled", str(error))
                except BudgetExceeded as error:
                    self._reason(operation_id, "rejected", str(error))
                except (ValueError, ProviderFailure) as error:
                    self._reason(operation_id, "rejected", f"{type(error).__name__}:{error}")
                except Exception as error:
                    self.control.stop()
                    # `_finalize` rewrites an unresolved `uncertain` row to
                    # "reconciled_after_interruption", so without the cause here an
                    # unhandled defect is indistinguishable from an ordinary
                    # interruption and its traceback is gone.
                    self._reason(
                        operation_id,
                        "uncertain",
                        f"execution_or_charge_uncertain:{type(error).__name__}:{error}",
                    )
                    if self._finalize(operation_id) is None:
                        raise OutcomeUncertain("execution or charge requires lookup") from None
                except BaseException:
                    self.control.stop()
                    self._reason(operation_id, "uncertain", "interrupted")
                    raise
                finally:
                    finished.set()
                receipt = self._finalize(operation_id)
                if receipt is None:
                    raise OutcomeUncertain("charges or native effects remain unresolved")
                return receipt
        finally:
            self._mutex.release()

    def _run(self, operation_id, invocation, deadline, authority):
        objective = invocation.objective
        corrections = 0
        for step in range(objective.limits.max_steps):
            self._active(invocation, deadline, authority)
            start = time.monotonic()
            before = self.control.observe(objective)
            self._timing(operation_id, "observation", start)
            if step == 0 and before.revision != invocation.binding.observation_revision:
                raise Forbidden("stale observation revision")
            binding = invocation.binding.model_copy(update={"observation_revision": before.revision})
            decisions = self.control.decisions(objective, before)
            if decisions.observation_revision != before.revision:
                raise Forbidden("decision set has stale observation")
            if any(q.version != self.policy.question_version for q in decisions.questions):
                raise Forbidden("question version differs from frozen policy")
            selection_id = f"{operation_id}:selection:{step}"
            selection = self._select(
                operation_id, invocation, before, decisions, selection_id, deadline, authority
            )
            if selection.abstention:
                self._reason(operation_id, "abstained", selection.abstention)
                return
            command = self.control.compile(objective, before, decisions, selection)
            if command.adapter_version != self.policy.adapter_version:
                raise Forbidden("compiled command has different adapter identity")
            execution_id = f"{operation_id}:execution:{step}"
            with self.ledger.db() as db:
                db.execute(
                    "INSERT INTO effects VALUES (?,?,?,?,?,?,'intent',NULL,NULL)",
                    (
                        execution_id,
                        operation_id,
                        selection_id,
                        encode(command),
                        encode(binding),
                        encode(before),
                    ),
                )
                self.ledger.event(
                    db,
                    operation_id,
                    "command.compiled",
                    {
                        "execution_id": execution_id,
                        "selection_id": selection_id,
                        "command": command.model_dump(mode="json"),
                        "binding": binding.model_dump(mode="json"),
                    },
                )
            # Compilation validates the entire combination. Admission is repeated
            # inside before_dispatch, including after queueing on a native thread.
            self._effect(
                operation_id, invocation, binding, command, before, execution_id, deadline, authority
            )
            with self.ledger.db() as db:
                effect = db.execute("SELECT * FROM effects WHERE id=?", (execution_id,)).fetchone()
            native = (
                NativeReceipt.model_validate_json(effect["native_receipt"])
                if effect["native_receipt"]
                else None
            )
            if native is None or native.outcome == "unknown":
                raise OutcomeUncertain("native effect requires lookup")
            if native.outcome != "applied":
                if native.outcome == "rejected" and corrections < objective.limits.max_corrections:
                    corrections += 1
                    continue
                self._reason(
                    operation_id,
                    "cancelled" if native.outcome == "cancelled" else "rejected",
                    native.reason or native.outcome,
                )
                return
            verification = Verification.model_validate_json(effect["verification"])
            if verification.status != "continue":
                self._reason(
                    operation_id,
                    "handoff" if verification.status == "failed" else verification.status,
                    verification.reason,
                )
                return
        self._reason(operation_id, "handoff", "step_limit_reached")

    def _effect(self, operation_id, invocation, binding, command, before, execution_id, deadline, authority):
        admitted = False
        rejected = False

        def before_dispatch():
            nonlocal admitted, rejected
            if admitted:
                raise Conflict("native dispatch callback may be used only once")
            start = time.monotonic()
            try:
                self._active(invocation, deadline, authority, binding)
                admission = self.control.admit(execution_id, command, binding)
                if admission.execution_id != execution_id or admission.binding != binding:
                    raise Forbidden("native admission authority mismatch")
                with self.ledger.db() as db:
                    self.ledger.event(db, operation_id, "native.admission", admission)
                if not admission.accepted:
                    raise _AdmissionRejected(admission.reason or "native admission rejected")
                with self.ledger.db() as db:
                    self.ledger.event(
                        db,
                        operation_id,
                        "native.dispatch_intent",
                        {
                            "execution_id": execution_id,
                            "command": command.model_dump(mode="json"),
                            "binding": binding.model_dump(mode="json"),
                        },
                    )
                    db.execute("UPDATE effects SET status='dispatching' WHERE id=?", (execution_id,))
                admitted = True
                return admission
            except BaseException:
                rejected = True
                raise
            finally:
                self._timing(operation_id, "admission", start)

        start = time.monotonic()
        try:
            native = self.control.execute(
                execution_id, command, before_dispatch=before_dispatch, cancel=self._cancel, deadline=deadline
            )
            if native.execution_id != execution_id:
                raise OutcomeUncertain("native receipt identity mismatch")
            if not admitted and native.outcome == "applied":
                raise OutcomeUncertain("native effect bypassed required admission")
        except BaseException as error:
            if rejected and not admitted:
                native = NativeReceipt(
                    execution_id=execution_id, outcome="rejected", reason=str(error) or "admission_rejected"
                )
                self._native(operation_id, native)
                if isinstance(error, _AdmissionRejected):
                    return
            else:
                self._native(operation_id, NativeReceipt(execution_id=execution_id, outcome="unknown"))
            raise
        finally:
            self._timing(operation_id, "execution", start)
        self._native(operation_id, native)
        if native.outcome == "applied":
            self._verify(operation_id, invocation, before, native)

    def _native(self, operation_id, native):
        with self.ledger.db() as db:
            row = db.execute(
                "SELECT native_receipt FROM effects WHERE id=?", (native.execution_id,)
            ).fetchone()
            if row[0]:
                prior = NativeReceipt.model_validate_json(row[0])
                if prior.outcome != "unknown" and prior != native:
                    raise Conflict("conflicting native receipt")
            db.execute(
                "UPDATE effects SET status=?,native_receipt=? WHERE id=?",
                (native.outcome, encode(native), native.execution_id),
            )
            self.ledger.event(db, operation_id, "native.receipt", native)

    def _verify(self, operation_id, invocation, before, native):
        start = time.monotonic()
        after = self.control.observe(invocation.objective)
        self._timing(operation_id, "observation", start)
        start = time.monotonic()
        verification = self.control.verify(invocation.objective, before, after, native)
        with self.ledger.db() as db:
            db.execute(
                "UPDATE effects SET verification=? WHERE id=?", (encode(verification), native.execution_id)
            )
            self.ledger.event(
                db,
                operation_id,
                "native.verification",
                {
                    "execution_id": native.execution_id,
                    "after": after.model_dump(mode="json"),
                    "verification": verification.model_dump(mode="json"),
                },
            )
        self._timing(operation_id, "verification", start)
        return verification

    def _finalize(self, operation_id):
        with self.ledger.db() as db:
            row = db.execute("SELECT * FROM invocations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                return None
            if row["receipt"]:
                return json.loads(row["receipt"])
            # `charges_resolved` implies nothing is still reserved, which is why the
            # receipt no longer carries a reservation field.
            cost, _reserved, charges_resolved = self.ledger.charges(db, operation_id)
            effects = db.execute("SELECT * FROM effects WHERE invocation=?", (operation_id,)).fetchall()
            effects_resolved = all(
                e["status"] in ("applied", "rejected", "cancelled")
                and (e["status"] != "applied" or e["verification"])
                for e in effects
            )
            successor_pending = db.execute(
                "SELECT 1 FROM successors WHERE invocation=? AND status IN ('prepared','unknown') LIMIT 1",
                (operation_id,),
            ).fetchone()
            if not charges_resolved or not effects_resolved or successor_pending:
                return None
            hashes = tuple(
                r[0]
                for r in db.execute(
                    "SELECT artifact_hash FROM events WHERE invocation=? ORDER BY sequence", (operation_id,)
                )
            )
            status = row["status"]
            reason = row["reason"]
            if status in ("running", "uncertain"):
                status, reason = "handoff", "reconciled_after_interruption"
            receipt = DecisionReceipt(
                operation_id=operation_id,
                status=status,
                reason=reason,
                cost_micros=cost,
                charge_resolved=True,
                effects_resolved=True,
                execution_ids=tuple(e["id"] for e in effects),
                artifact_hashes=hashes,
                timings_ms=json.loads(row["timings"]),
                accounting_id=operation_id,
            ).model_dump(mode="json")
            db.execute("UPDATE invocations SET receipt=? WHERE id=?", (encode(receipt), operation_id))
            return receipt

    def lookup(self, operation_id):
        # No inference and no native resubmission, including across process restart.
        if not self._mutex.acquire(blocking=False):
            return None
        try:
            with self.ledger.ownership():
                with self.ledger.db() as db:
                    row = db.execute("SELECT * FROM invocations WHERE id=?", (operation_id,)).fetchone()
                    if row is None:
                        return None
                    if row["receipt"]:
                        return json.loads(row["receipt"])
                    attempts = db.execute(
                        "SELECT * FROM attempts WHERE invocation=?", (operation_id,)
                    ).fetchall()
                    effects = db.execute(
                        "SELECT * FROM effects WHERE invocation=?", (operation_id,)
                    ).fetchall()
                invocation = BoundedInvocation.model_validate_json(row["request"])
                for attempt in attempts:
                    if attempt["status"] == "reserved":
                        with self.ledger.db() as db:
                            db.execute(
                                "UPDATE attempts SET status='resolved',cost=0 WHERE id=? AND status='reserved'",
                                (attempt["id"],),
                            )
                    elif attempt["status"] != "resolved":
                        lookup = getattr(self.selector, "lookup", None)
                        selection = lookup(attempt["id"]) if lookup else None
                        if selection is not None:
                            self._record_selection(attempt["id"], selection)
                for effect in effects:
                    native = (
                        NativeReceipt.model_validate_json(effect["native_receipt"])
                        if effect["native_receipt"]
                        else None
                    )
                    if native is None or native.outcome == "unknown":
                        native = self.control.lookup(effect["id"])
                        if native is None:
                            continue
                        if native.execution_id != effect["id"]:
                            raise Conflict("lookup returned a different execution ID")
                        self._native(operation_id, native)
                    if native.outcome == "applied" and not effect["verification"]:
                        self._verify(
                            operation_id,
                            invocation,
                            Observation.model_validate_json(effect["before_observation"]),
                            native,
                        )
                with self.ledger.db() as db:
                    db.execute(
                        "UPDATE successors SET status='settled' WHERE invocation=? AND status='unknown' "
                        "AND json_extract(intent,'$.execution_id') IN (SELECT id FROM effects WHERE status IN ('applied','rejected','cancelled'))",
                        (operation_id,),
                    )
                return self._finalize(operation_id)
        finally:
            self._mutex.release()

    def prepare_successor(self, intent: PreparedSuccessor, *, authority=None):
        """Persist a selected, compiled successor for an active bounded invocation.

        A successor must refer to a command already recorded by the decision
        pipeline. Preparing an arbitrary command cannot create new authority.
        The native control still performs its final admission at dispatch.
        """
        if not self.policy.prepared_successor or not getattr(
            self.control, "native_admits_prepared_successors", False
        ):
            raise Forbidden("native prepared successors are disabled")
        if not callable(authority):
            raise Forbidden("prepared successor requires live SDK authority")
        if intent.expires_at <= datetime.now(timezone.utc) or intent.binding.stop_epoch != self.stop_epoch:
            raise Forbidden("prepared successor authority expired")
        with self.ledger.db() as db:
            operation = db.execute("SELECT * FROM invocations WHERE id=?", (intent.operation_id,)).fetchone()
            prior = db.execute(
                "SELECT * FROM effects WHERE id=?", (intent.predecessor_execution_id,)
            ).fetchone()
            command = db.execute("SELECT * FROM effects WHERE id=?", (intent.execution_id,)).fetchone()
            if operation is None or operation["receipt"] or operation["status"] != "running":
                raise Forbidden("prepared successor invocation is no longer active")
            invocation = BoundedInvocation.model_validate_json(operation["request"])
            if (
                prior is None
                or prior["invocation"] != intent.operation_id
                or prior["status"] != "dispatching"
            ):
                raise Forbidden("prepared successor predecessor is not active")
            if (
                command is None
                or command["invocation"] != intent.operation_id
                or command["status"] != "intent"
            ):
                raise Forbidden("successor command was not selected and compiled")
            if command["command"] != encode(intent.command) or command["binding"] != encode(intent.binding):
                raise Forbidden("prepared successor command or authority changed")
            if intent.expires_at > invocation.expires_at:
                raise Forbidden("successor expiry exceeds bounded invocation")
            count = db.execute(
                "SELECT COUNT(*) FROM effects WHERE invocation=?", (intent.operation_id,)
            ).fetchone()[0]
            if count > invocation.objective.limits.max_steps:
                raise Forbidden("successor exceeds step allowance")
            selected = db.execute(
                "SELECT 1 FROM attempts WHERE selection_id=? AND status='resolved' AND selection IS NOT NULL",
                (command["selection_id"],),
            ).fetchone()
            if selected is None:
                raise Forbidden("successor selection charge unresolved")
        authority(intent.binding)
        with self.ledger.db() as db:
            db.execute(
                "INSERT INTO successors VALUES (?,?,?,'prepared',NULL)",
                (intent.id, intent.operation_id, encode(intent)),
            )
            self.ledger.event(db, intent.operation_id, "successor.prepared", intent)
        return intent

    def admit_successor(self, intent_id, *, authority):
        """Called by native execution after queueing, immediately before the effect."""
        with self.ledger.db() as db:
            row = db.execute("SELECT * FROM successors WHERE id=?", (intent_id,)).fetchone()
            if not row or row["status"] != "prepared":
                raise Forbidden("successor is not prepared")
            intent = PreparedSuccessor.model_validate_json(row["intent"])
            operation = db.execute("SELECT * FROM invocations WHERE id=?", (intent.operation_id,)).fetchone()
            if not operation or operation["receipt"]:
                raise Forbidden("successor invocation has ended")
        if intent.expires_at <= datetime.now(timezone.utc) or intent.binding.stop_epoch != self.stop_epoch:
            admission = Admission(
                execution_id=intent.execution_id,
                binding=intent.binding,
                accepted=False,
                reason="successor_expired",
            )
        else:
            authority(intent.binding)
            admission = self.control.admit(intent.execution_id, intent.command, intent.binding)
            if admission.execution_id != intent.execution_id or admission.binding != intent.binding:
                raise Forbidden("successor admission identity mismatch")
        with self.ledger.db() as db:
            updated = db.execute(
                "UPDATE successors SET status=?,admission=? WHERE id=? AND status='prepared'",
                ("unknown" if admission.accepted else "rejected", encode(admission), intent_id),
            )
            if updated.rowcount != 1:
                raise Forbidden("successor was invalidated before dispatch")
            if admission.accepted:
                db.execute("UPDATE effects SET status='dispatching' WHERE id=?", (intent.execution_id,))
            self.ledger.event(db, intent.operation_id, "successor.admission", admission)
        return admission

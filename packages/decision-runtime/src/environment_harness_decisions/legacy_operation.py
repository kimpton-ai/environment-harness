"""Compatibility facade for the version-one motor operation.

The facade keeps the historical request and receipt shapes for Civ and
Minecraft, while ordinary execution is owned by :class:`DecisionOperation`.
Prepared successor methods remain supplied by ``LegacySuccessorLedger``.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from environment_harness.contracts import OperationSpec
from environment_harness.errors import BudgetExceeded, Conflict, Forbidden
from environment_harness.operations import EnvironmentOperation

from .contracts import (
    Admission,
    Answer,
    AuthorityBinding,
    BoundedInvocation,
    ChoiceOption,
    CompiledCommand,
    DecisionPolicy,
    DecisionQuestion,
    DecisionSet,
    InvocationLimits,
    NativeReceipt,
    Objective,
    Observation,
    Selection,
    Verification,
)
from .contracts import (
    OutcomeUncertain as _OutcomeUncertain,
)
from .legacy_contracts import (
    MotorAdapter,
    MotorCandidate,
    MotorProfile,
    MotorReceipt,
    MotorRequest,
    MotorSelection,
    MotorStep,
)
from .legacy_successors import LegacySuccessorLedger, _matches, _validate_candidate
from .runtime import DecisionOperation


def _mutable(value):
    """Convert frozen contract containers back to adapter-owned values."""
    if isinstance(value, dict):
        return {key: _mutable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_mutable(item) for item in value]
    return value


class _LegacySelector:
    """Translate the old candidate selector into shared typed selections."""

    def __init__(self, selector, profile: MotorProfile, adapter, request):
        self.old = selector
        self.model = profile.selector_model or "deterministic.v1"
        self.endpoint = getattr(selector, "endpoint", "motor") if selector else "motor"
        self.records: dict[str, MotorSelection] = {}
        self.adapter, self.request = adapter, request

    def _state(self, observation):
        projection = getattr(self.adapter, "selection_observation", None)
        if not callable(projection):
            raise Forbidden("legacy adapter must provide selection_observation")
        state = projection(self.request, _mutable(observation.model_input))
        if not isinstance(state, dict):
            raise Forbidden("selection_observation must return an object")
        # Preserve the historical selector envelope.  Legacy selectors receive
        # the projected view beneath ``observation`` so private world state
        # never enters their input.
        return {"observation": state}

    def model_input(self, observation, decisions):
        state = self._state(observation)
        candidates = tuple(getattr(decisions, "_candidates", ()))
        if callable(getattr(self.old, "model_input", None)):
            return self.old.model_input(state, candidates)
        return {**state, "questions": decisions.model_dump(mode="json")}

    def maximum_charge_micros(self, objective, observation, decisions):
        if self.old is None:
            return 0
        verified = getattr(self.old, "verified_maximum_cost", None)
        if not callable(verified):
            verified = getattr(self.old, "verified_charge_bound", None)
        if not callable(verified):
            return None
        candidates = tuple(getattr(decisions, "_candidates", ()))
        if not candidates:
            candidates = tuple(getattr(decisions.questions[0], "options", ()))
        if not candidates:
            return None
        state = self._state(observation)
        value = verified(state, candidates)
        if type(value) is not int or value < 0:
            return None
        return value

    def select(self, selection_id, objective, observation, decisions, *, cancel, deadline):
        candidates = tuple(getattr(decisions, "_candidates", ()))
        if self.old is None:
            chosen = candidates[0]
            selected = MotorSelection(candidate_id=chosen.id, model=self.model, cost_micros=0)
            self.records[selection_id.rsplit(":selection:", 1)[0]] = selected
            return Selection(
                model=self.model,
                cost_micros=0,
                answers=(Answer(question_id="candidate", value=chosen.id),),
                raw_response={"candidate_id": chosen.id},
                model_input=self.model_input(observation, decisions),
                versions={"adapter": "legacy-selector.v1"},
            )
        if cancel.is_set() or time.monotonic() >= deadline:
            return Selection(
                model=self.model,
                cost_micros=0,
                answers=(),
                abstention="cancelled",
                model_input=self.model_input(observation, decisions),
            )
        state = self._state(observation)
        bound = self.maximum_charge_micros(objective, observation, decisions)
        if bound is None:
            raise BudgetExceeded("legacy selector has no verified charge bound")
        selected = self.old.select(
            state, candidates, maximum_cost_micros=bound, cancel=cancel, deadline=deadline
        )
        if not isinstance(selected, MotorSelection):
            raise ValueError("legacy selector returned an invalid selection")
        self.records[selection_id.rsplit(":selection:", 1)[0]] = selected
        answers = (
            ()
            if selected.candidate_id is None
            else (
                Answer(
                    question_id="candidate", value=selected.candidate_id, probabilities=selected.probabilities
                ),
            )
        )
        return Selection(
            model=selected.model,
            cost_micros=selected.cost_micros,
            answers=answers,
            abstention=None if selected.candidate_id is not None else "abstention",
            raw_response=selected.model_dump(mode="json"),
            model_input=self.model_input(observation, decisions),
            versions={"adapter": "legacy-selector.v1"},
        )

    def lookup(self, attempt_id):
        return None


class _LegacyControl:
    identity = "legacy-motor"

    def __init__(self, adapter: MotorAdapter, request: MotorRequest, write: bool):
        self.adapter, self.request, self.write = adapter, request, write
        self.identity = adapter.implementation
        self.implementation = adapter.implementation
        self._candidates: dict[str, MotorCandidate] = {}
        self._before: dict[str, dict[str, Any]] = {}
        self.last_candidate: MotorCandidate | None = None
        self.last_receipts: list[dict[str, Any]] = []
        self.last_observation: dict[str, Any] = {}
        self._binding = None

    def observe(self, objective):
        value = self.adapter.observe()
        self.last_observation = value
        return Observation(
            revision=str(value.get("revision", self.request.observation_revision)), model_input=value
        )

    def decisions(self, objective, observation):
        candidates = tuple(self.adapter.plan(self.request, _mutable(observation.model_input)))
        if not candidates or len({candidate.id for candidate in candidates}) != len(candidates):
            raise ValueError("adapter supplied no valid bounded plans")
        if any(len(candidate.steps) != 1 for candidate in candidates):
            raise ValueError("legacy compatibility requires one-step candidates")
        if any(len(candidate.steps) > self.request.max_steps for candidate in candidates):
            raise ValueError("candidate exceeds request step limit")
        for candidate in candidates:
            invalid = _validate_candidate(self.request, candidate, _mutable(observation.model_input))
            if invalid:
                raise ValueError(f"candidate violates request authority: {invalid}")
        self._candidates = {candidate.id: candidate for candidate in candidates}
        question = DecisionQuestion(
            id="candidate",
            kind="choice",
            prompt="Choose one supplied motor candidate",
            options=tuple(
                ChoiceOption(id=candidate.id, label=candidate.description) for candidate in candidates
            ),
        )
        result = DecisionSet(
            id="legacy-candidates", observation_revision=observation.revision, questions=(question,)
        )
        object.__setattr__(result, "_candidates", candidates)
        return result

    def compile(self, objective, observation, decisions, selection):
        answers = {answer.question_id: answer for answer in selection.answers}
        candidate_id = answers.get("candidate").value if answers.get("candidate") else None
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise ValueError("selection does not identify an authorized candidate")
        invalid = _validate_candidate(self.request, candidate, _mutable(observation.model_input))
        if invalid:
            raise Forbidden(f"candidate violates request authority: {invalid}")
        if not self.write and any(step.operation != "read" for step in candidate.steps):
            raise Forbidden("write authority required")
        self._before[candidate.id] = observation.model_input
        self.last_candidate = candidate
        return CompiledCommand(
            id=candidate.id,
            adapter_version=self.implementation,
            payload={"candidate": candidate.model_dump(mode="json")},
        )

    def admit(self, execution_id, command, binding):
        candidate = MotorCandidate.model_validate(command.payload["candidate"])
        fresh = self.adapter.observe()
        if str(fresh.get("revision", "")) != binding.observation_revision:
            return Admission(
                execution_id=execution_id,
                binding=binding,
                accepted=False,
                reason="stale_observation_revision",
            )
        revalidate = getattr(self.adapter, "revalidate", None)
        if not callable(revalidate):
            return Admission(
                execution_id=execution_id, binding=binding, accepted=False, reason="illegal_at_dispatch"
            )
        checked = revalidate(self.request, _mutable(fresh), candidate, 0)
        if not checked or not any(item == candidate for item in checked):
            return Admission(
                execution_id=execution_id, binding=binding, accepted=False, reason="illegal_at_dispatch"
            )
        invalid = _validate_candidate(self.request, candidate, _mutable(fresh))
        if invalid:
            return Admission(
                execution_id=execution_id, binding=binding, accepted=False, reason="illegal_at_dispatch"
            )
        self._binding = binding
        return Admission(execution_id=execution_id, binding=binding, accepted=True)

    def execute(self, execution_id, command, *, before_dispatch, cancel, deadline):
        candidate = MotorCandidate.model_validate(command.payload["candidate"])
        receipts = []
        self.last_receipts = receipts
        for index, step in enumerate(candidate.steps):
            # Compiled commands are immutable contract records. Legacy
            # adapters mutate and deepcopy their step payloads, so hand them
            # a validated mutable projection at the native boundary.
            step = MotorStep.model_validate(_mutable(step.model_dump(mode="json")))
            if cancel.is_set() or time.monotonic() >= deadline:
                return NativeReceipt(execution_id=execution_id, outcome="cancelled", reason="deadline")
            admitted = False

            def dispatch_authority(*args, **kwargs):
                nonlocal admitted
                before_dispatch()
                admitted = True

            previous_authority = getattr(self.adapter, "dispatch_authority", None)
            self.adapter.dispatch_authority = dispatch_authority
            try:
                parameters = inspect.signature(self.adapter.execute).parameters
                arguments = {"operation_id": execution_id, "cancel": cancel, "deadline": deadline}
                if "before_dispatch" in parameters:
                    arguments["before_dispatch"] = dispatch_authority
                result = self.adapter.execute(step, **arguments)
            finally:
                self.adapter.dispatch_authority = previous_authority
            if not admitted:
                raise Forbidden("legacy adapter did not invoke dispatch_authority at its native boundary")
            if not isinstance(result, dict) or result.get("status") not in {
                "completed",
                "blocked",
                "cancelled",
            }:
                return NativeReceipt(
                    execution_id=execution_id, outcome="unknown", reason="invalid_native_receipt"
                )
            receipts.append(result)
            if result["status"] != "completed":
                return NativeReceipt(
                    execution_id=execution_id, outcome="rejected", reason=result.get("reason")
                )
        return NativeReceipt(execution_id=execution_id, outcome="applied", evidence={"steps": receipts})

    def verify(self, objective, before, after, receipt):
        if receipt.evidence.get("steps") and all(
            item.get("effect_verified") is True for item in receipt.evidence["steps"]
        ):
            return Verification(status="completed", reason="native_receipt_verified")
        if not _matches(self.request.expected, after.model_input):
            return Verification(status="failed", reason="postcondition_not_confirmed")
        return Verification(status="completed", reason="postcondition_confirmed")

    def stop(self):
        self.adapter.stop()

    def lookup(self, execution_id):
        result = self.adapter.lookup(execution_id)
        if not isinstance(result, dict):
            return None
        status = result.get("status")
        outcome = "applied" if status == "completed" else "rejected" if status == "blocked" else "unknown"
        return NativeReceipt(execution_id=execution_id, outcome=outcome, evidence=result)


class LegacyMotorOperation(EnvironmentOperation, LegacySuccessorLedger):
    """Old MotorExecutor facade backed by one shared DecisionOperation."""

    endpoint = "motor"

    def __init__(self, adapter, profile, *, journal, selector=None, discard_prepared=False):
        LegacySuccessorLedger.__init__(
            self, adapter, profile, journal=journal, selector=selector, discard_prepared=discard_prepared
        )
        self.adapter, self.profile, self.selector = adapter, profile, selector
        self.journal = Path(journal)
        self._legacy_controls: dict[str, _LegacyControl] = {}
        self._decision_ops: dict[str, DecisionOperation] = {}
        self._legacy_requests: dict[str, MotorRequest] = {}
        self._legacy_selections: dict[str, MotorSelection] = {}
        self._legacy_receipts()

    def _legacy_receipts(self):
        import sqlite3

        db = sqlite3.connect(self.journal)
        db.execute("CREATE TABLE IF NOT EXISTS legacy_receipts (id TEXT PRIMARY KEY, receipt TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS legacy_operations (id TEXT PRIMARY KEY, request TEXT NOT NULL, maximum INTEGER NOT NULL, invocation TEXT NOT NULL)"
        )
        db.commit()
        db.close()

    @property
    def spec(self):
        return OperationSpec(
            name="motor.execute",
            version="decision-operation.v1",
            config={
                "profile": self.profile.model_dump(mode="json"),
                "adapter": self.adapter.implementation,
                "decision_policy": self.decision_policy.model_dump(mode="json"),
            },
        )

    @property
    def decision_policy(self):
        return DecisionPolicy(
            profile=self.profile.mode,
            selector_model=self.profile.selector_model or "deterministic.v1",
            endpoint=getattr(self.selector, "endpoint", self.endpoint) if self.selector else self.endpoint,
            adapter_version=self.adapter.implementation,
        )

    def validate(self, request, manifest):
        payload = MotorRequest.model_validate(request["payload"])
        # Preserve the old direct executor envelope used by legacy fixtures.
        if request.get("endpoint") is None and request.get("operation") is None:
            request = {**request, "endpoint": self.endpoint, "operation": "motor.execute"}
        if request.get("endpoint") != self.endpoint or request.get("operation") != "motor.execute":
            raise Forbidden("motor operation identity mismatch")
        if manifest.get("motor") not in (None, self.profile.model_dump(mode="json")):
            raise Forbidden("motor profile differs from frozen manifest")
        return payload

    def _invocation(self, request: MotorRequest, operation_id: str, existing=None, maximum_cost_micros=0):
        directive = request.goal_context.revision if request.goal_context else request.goal_revision
        objective = Objective(
            id=request.skill,
            revision=directive,
            authorized_scope=("motor",),
            completion_conditions=("expected postcondition",),
            limits=InvocationLimits(
                max_steps=request.max_steps,
                timeout_ms=request.timeout_ms,
                max_cost_micros=maximum_cost_micros,
                max_corrections=request.max_recovery_attempts,
            ),
        )
        control = _LegacyControl(self.adapter, request, True)
        self._legacy_controls[operation_id] = control
        binding = AuthorityBinding(
            environment_id=control.identity,
            session_revision=request.goal_revision,
            directive_revision=directive,
            observation_revision=request.observation_revision,
            recovery_generation=0,
            owner="legacy",
            stop_epoch=request.stop_epoch,
        )
        expires = datetime.now(timezone.utc) + timedelta(milliseconds=request.timeout_ms)
        invocation = existing or BoundedInvocation(objective=objective, binding=binding, expires_at=expires)
        return objective, control, invocation

    def execute(self, operation_id, request, maximum_cost_micros, *, authority):
        payload = self.validate(request, {})
        request_identity = json.dumps(
            {**request, "payload": payload.model_dump(mode="json")}, sort_keys=True, separators=(",", ":")
        )
        db = sqlite3.connect(self.journal)
        row = db.execute("SELECT receipt FROM legacy_receipts WHERE id=?", (operation_id,)).fetchone()
        if row:
            prior = db.execute(
                "SELECT request,maximum FROM legacy_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if prior and (prior[0] != request_identity or prior[1] != maximum_cost_micros):
                db.close()
                raise Conflict("legacy operation identifier reused with different input")
            db.close()
            return json.loads(row[0])
        prior = db.execute(
            "SELECT request,maximum,invocation FROM legacy_operations WHERE id=?", (operation_id,)
        ).fetchone()
        if prior:
            if prior[0] != request_identity or prior[1] != maximum_cost_micros:
                db.close()
                raise Conflict("legacy operation identifier reused with different input")
            invocation = BoundedInvocation.model_validate_json(prior[2])
            db.close()
            objective, control, invocation = self._invocation(payload, operation_id, invocation)
        else:
            objective, control, invocation = self._invocation(
                payload, operation_id, maximum_cost_micros=maximum_cost_micros
            )
            db.execute(
                "INSERT INTO legacy_operations VALUES (?,?,?,?)",
                (operation_id, request_identity, maximum_cost_micros, invocation.model_dump_json()),
            )
            db.commit()
            db.close()
        selector = _LegacySelector(self.selector, self.profile, self.adapter, payload)
        decision = DecisionOperation(
            control,
            selector,
            journal=self.journal.with_name(self.journal.stem + ".decision.sqlite"),
            policy=self.decision_policy,
        )
        self._decision_ops[operation_id] = decision
        self._legacy_requests[operation_id] = payload
        started = time.monotonic()
        receipt = decision.execute(
            operation_id,
            {"payload": invocation.model_dump(mode="json")},
            maximum_cost_micros,
            authority=authority,
        )
        result = self._project(operation_id, payload, receipt, started)
        if operation_id in selector.records:
            self._legacy_selections[operation_id] = selector.records[operation_id]
            result = self._project(operation_id, payload, receipt, started)
        db = sqlite3.connect(self.journal)
        db.execute("INSERT OR REPLACE INTO legacy_receipts VALUES (?,?)", (operation_id, json.dumps(result)))
        db.commit()
        db.close()
        return result

    def _project(self, operation_id, request, receipt, started):
        status = receipt["status"]
        stale = status == "cancelled" and receipt.get("reason") in {
            "stale observation revision",
            "cancelled by stop epoch",
        }
        old_status = (
            "completed"
            if status == "completed"
            else "blocked"
            if stale
            else "cancelled"
            if status == "cancelled"
            else "blocked"
        )
        outcome = (
            "completed"
            if status == "completed"
            else "rejected"
            if stale
            else "cancelled"
            if status == "cancelled"
            else "rejected"
        )
        selection = self._legacy_selections.get(operation_id)
        control = self._legacy_controls.get(operation_id)
        steps = []
        applied = False
        possible = False
        decision = self._decision_ops.get(operation_id)
        if decision is not None:
            with decision.ledger.db() as db:
                effects = db.execute(
                    "SELECT command,native_receipt,status FROM effects WHERE invocation=? ORDER BY rowid",
                    (operation_id,),
                ).fetchall()
            for effect in effects:
                native = json.loads(effect["native_receipt"]) if effect["native_receipt"] else None
                if native is None or native.get("outcome") == "unknown":
                    possible = True
                    continue
                command = json.loads(effect["command"])
                candidate = command.get("payload", {}).get("candidate", {})
                native_steps = native.get("evidence", {}).get("steps", ())
                for index, step in enumerate(candidate.get("steps", ())):
                    if index < len(native_steps):
                        value = dict(step)
                        value["receipt"] = native_steps[index]
                        steps.append(value)
                if native.get("outcome") == "applied":
                    applied = True
                elif native.get("outcome") == "unknown":
                    possible = True
        if control and control.last_candidate:
            if not steps:
                for index, step in enumerate(control.last_candidate.steps):
                    if index < len(control.last_receipts):
                        value = step.model_dump(mode="json")
                        value["receipt"] = control.last_receipts[index]
                        steps.append(value)
        before = (
            control._before.get(control.last_candidate.id, {}) if control and control.last_candidate else {}
        )
        after = control.last_observation if control else {}
        reason_code = receipt.get("reason") if status == "rejected" else None
        if isinstance(reason_code, str) and reason_code.startswith("ValueError:"):
            detail = reason_code.split(":", 1)[1]
            reason_code = next(
                (known for known in ("protection_changed", "no_plan") if known in detail),
                reason_code,
            )
        if isinstance(reason_code, str) and "no valid bounded plans" in reason_code:
            reason_code = "no_plan"
        return MotorReceipt(
            operation_id=operation_id,
            status=old_status,
            outcome=outcome,
            effect="applied" if applied or status == "completed" else "possible" if possible else "none",
            cost_micros=receipt["cost_micros"],
            profile=self.profile,
            request=request,
            reason=receipt["reason"],
            reason_code=reason_code,
            selection=selection,
            before=before,
            after=after,
            steps=tuple(steps),
            elapsed_ms=(time.monotonic() - started) * 1000,
        ).model_dump(mode="json")

    def lookup(self, operation_id):
        import sqlite3

        db = sqlite3.connect(self.journal)
        row = db.execute("SELECT receipt FROM legacy_receipts WHERE id=?", (operation_id,)).fetchone()
        db.close()
        if row:
            return json.loads(row[0])
        decision = self._decision_ops.get(operation_id)
        request = self._legacy_requests.get(operation_id)
        if decision is None or request is None:
            db = sqlite3.connect(self.journal)
            row = db.execute(
                "SELECT request,maximum,invocation FROM legacy_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if not row:
                db.close()
                return None
            envelope = json.loads(row[0])
            request = MotorRequest.model_validate(envelope["payload"])
            invocation = BoundedInvocation.model_validate_json(row[2])
            db.close()
            _, control, invocation = self._invocation(request, operation_id, invocation)
            selector = _LegacySelector(self.selector, self.profile, self.adapter, request)
            decision = DecisionOperation(
                control,
                selector,
                journal=self.journal.with_name(self.journal.stem + ".decision.sqlite"),
                policy=self.decision_policy,
            )
            self._decision_ops[operation_id] = decision
            self._legacy_requests[operation_id] = request
        receipt = decision.lookup(operation_id)
        if not receipt:
            return None
        result = self._project(operation_id, request, receipt, 0)
        db = sqlite3.connect(self.journal)
        db.execute("INSERT OR REPLACE INTO legacy_receipts VALUES (?,?)", (operation_id, json.dumps(result)))
        db.commit()
        db.close()
        return result

    def progress(self, operation_id):
        receipt = self.lookup(operation_id)
        return {"status": receipt["status"], "steps": list(receipt.get("steps", ()))} if receipt else None

    def stop(self):
        # Preserve the legacy epoch fence and propagate cancellation into each
        # shared decision ledger. Both paths are durable; native stop is
        # idempotent for the adapters this facade supports.
        LegacySuccessorLedger.stop(self)
        for decision in self._decision_ops.values():
            decision.stop()


MotorExecutor = LegacyMotorOperation

# Historical callers used this name for unresolved native outcomes.
MotorOutcomeUnknown = _OutcomeUncertain
OutcomeUncertain = MotorOutcomeUnknown

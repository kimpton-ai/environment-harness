"""Version 1 successor admission and read-only motor compatibility.

Drivers must deduplicate step IDs and honor cancellation/deadlines. A crashed or
uncertain effect is never retried automatically. Use one journal per controlled
application to share its input lease between workers.
"""

from __future__ import annotations

import errno
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from environment_harness.errors import Conflict, Forbidden
from environment_harness.store import encode

from .legacy_contracts import (
    MotorAdapter,
    MotorControlPermission,
    MotorGroup,
    MotorProfile,
    MotorReceipt,
    MotorRequest,
    MotorSelection,
    PreparedSuccessorAdmission,
    PreparedSuccessorIntent,
    UnsupportedPreparation,
)
from .legacy_errors import MotorError, MotorOutcomeUnknown


def _matches(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _matches(v, actual[k]) for k, v in expected.items()
        )
    return type(expected) is type(actual) and expected == actual


def _control_permission(request, step):
    """Return whether a step's bounded controls were explicitly authorized."""
    if not step.controls:
        return True
    permissions = request.control_permissions

    def same(left, right):
        return json.dumps(left, sort_keys=True, default=list) == json.dumps(
            right, sort_keys=True, default=list
        )

    return any(
        isinstance(permission, MotorControlPermission) and same(permission.controls, step.controls)
        for permission in permissions
    )


def _validate_candidate(request, candidate, observation):
    if any(not _matches(request.target, step.target) for step in candidate.steps):
        return "target_changed"
    if any(not _control_permission(request, step) for step in candidate.steps):
        return "unauthorized_control"
    if any(
        key in request.arguments and not _matches(value, request.arguments[key])
        for step in candidate.steps
        for key, value in step.arguments.items()
    ):
        return "unauthorized_arguments"
    for permission in request.control_permissions:
        matched = [step for step in candidate.steps if permission.controls == step.controls]
        if not matched:
            continue
        if len(matched) > permission.max_steps:
            return "control_limit_exceeded"
        if permission.max_travel is not None:
            travels = [step.controls.get("travel") for step in matched]
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for value in travels
            ):
                return "control_limit_exceeded"
            if sum(travels) > permission.max_travel:
                return "control_limit_exceeded"
        if (
            permission.protected_region_revision is not None
            and observation.get("protected_region_revision") != permission.protected_region_revision
        ):
            return "protection_changed"
    return None


class LegacySuccessorLedger:
    endpoint = "motor"
    implementation = "motor.v1"

    def __init__(
        self,
        adapter: MotorAdapter,
        profile: MotorProfile,
        *,
        journal: Path,
        selector=None,
        discard_prepared: bool = False,
    ):
        if profile.adapter != adapter.implementation:
            raise ValueError("motor adapter does not match the frozen profile")
        if (profile.mode == "jev") != (selector is not None):
            raise ValueError("Jev mode requires a selector; deterministic mode forbids one")
        if selector is not None and profile.selector_model != selector.model:
            raise ValueError("selector does not match the pinned model")
        if os.name == "nt":
            import msvcrt

            self._file_lock = (msvcrt, True)
        else:
            import fcntl

            self._file_lock = (fcntl, False)
        self.adapter, self.profile, self.selector = adapter, profile, selector
        self.journal = Path(journal)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS motor (id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT, progress TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS recoveries (epoch INTEGER PRIMARY KEY, observation TEXT NOT NULL, operations TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO control VALUES (1,0)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS motor_successors (id TEXT PRIMARY KEY, predecessor TEXT NOT NULL, intent TEXT NOT NULL, status TEXT NOT NULL, admission TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(motor_successors)")}
            for name in ("request", "selection"):
                if name not in columns:
                    db.execute(f"ALTER TABLE motor_successors ADD COLUMN {name} TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_prepared_successor "
                "ON motor_successors((1)) WHERE status='prepared'"
            )
        if discard_prepared:
            self.discard_prepared_successors()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.journal, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _acquire_file_lock(self, lock):
        module, windows = self._file_lock
        if windows:
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write("0")
                lock.flush()
            lock.seek(0)
            try:
                module.locking(lock.fileno(), module.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                    raise BlockingIOError from exc
                raise
        else:
            module.flock(lock, module.LOCK_EX | module.LOCK_NB)

    def _release_file_lock(self, lock):
        module, windows = self._file_lock
        if windows:
            lock.seek(0)
            module.locking(lock.fileno(), module.LK_UNLCK, 1)
        else:
            module.flock(lock, module.LOCK_UN)

    @staticmethod
    def _discard_prepared(db):
        """Fence uncommitted successor intents at an explicit controller restart."""
        db.execute("UPDATE motor_successors SET status='discarded' WHERE status='prepared'")

    def discard_prepared_successors(self):
        """Stop native input, then reconcile pending native intents."""
        if getattr(self.adapter, "native_admits_prepared_successors", False):
            self.adapter.stop()
            with self._db() as db:
                ids = [
                    row["id"] for row in db.execute("SELECT id FROM motor_successors WHERE status='prepared'")
                ]
            for intent_id in ids:
                try:
                    self.reconcile_successor(intent_id)
                except MotorOutcomeUnknown:
                    with self._db() as db:
                        db.execute(
                            "UPDATE motor_successors SET status='unknown',admission=? WHERE id=? AND status='prepared'",
                            (encode({"reason_code": "restart_reconciliation_unknown"}), intent_id),
                        )
            return
        with self._db() as db:
            self._discard_prepared(db)

    def _unknown_successor_exists(self, db):
        return (
            db.execute("SELECT 1 FROM motor_successors WHERE status='unknown' LIMIT 1").fetchone() is not None
        )

    @property
    def stop_epoch(self):
        with self._db() as db:
            return db.execute("SELECT epoch FROM control WHERE id=1").fetchone()[0]

    def stop(self):
        """Fence queued work immediately and release native controls without a model call."""
        with self._db() as db:
            db.execute("UPDATE control SET epoch=epoch+1 WHERE id=1")
        self._cancel.set()
        self.adapter.stop()

    def acknowledge_unknown(self):
        raise Forbidden("unknown effects require native lookup; acknowledgement is disabled")

    def validate(self, request, manifest):
        if request.get("endpoint") != self.endpoint or request.get("operation") != "motor.execute":
            raise Forbidden("motor executor requires motor.execute")
        payload = MotorRequest.model_validate(request["payload"])
        if manifest.get("motor") != self.profile.model_dump(mode="json"):
            raise Forbidden("motor assistance differs from the frozen experiment")
        if (
            payload.skill not in manifest["environment"].get("motor_skills", ())
            or payload.skill not in self.adapter.skills
        ):
            raise Forbidden("motor skill is not declared")
        if payload.skill != "read" and not request.get("write"):
            raise Forbidden("motor effects require explicit write authorization")
        if self.selector is not None:
            policy = manifest["policy"]
            if (
                self.selector.endpoint not in policy["allowed_endpoints"]
                or "motor.select" not in policy["allowed_operations"]
            ):
                raise Forbidden("Jev endpoint and selection require frozen permission")
        if payload.group is not None:
            self.validate_group(payload.group, payload, manifest)
        return payload

    def validate_group(self, group: MotorGroup, request: MotorRequest, manifest):
        """Validate a coordinated contract before a runtime plans dispatch."""
        if not isinstance(group, MotorGroup):
            group = MotorGroup.model_validate(group)
        self._validate_group_contract(group, request)
        if manifest.get("motor") != self.profile.model_dump(mode="json"):
            raise Forbidden("motor assistance differs from the frozen experiment")
        return group

    def _validate_group_contract(self, group: MotorGroup, request: MotorRequest):
        """Recheck runtime-owned group invariants at the effect boundary."""
        if "coordinated-control.v1" not in getattr(self.adapter, "group_capabilities", ()):
            raise Forbidden("motor adapter does not advertise coordinated-control.v1")
        if group.stop_epoch != self.stop_epoch:
            raise Conflict("motor group stop epoch is stale")
        if request.goal_context != group.goal_context:
            raise Conflict("motor request and group goal contexts differ")
        if request.stop_epoch != group.stop_epoch:
            raise Conflict("motor request and group stop epochs differ")
        if request.timeout_ms > group.deadline_ms:
            raise Conflict("motor request exceeds group deadline")
        permissions = {encode(permission.controls): permission for permission in request.control_permissions}
        for claim in group.claims:
            if encode(claim.controls) not in permissions:
                raise Forbidden("group claim is not backed by a motor control permission")
        return group

    def lookup(self, operation_id):
        """Return only a durable final receipt. Never replay missing effects."""
        with self._db() as db:
            row = db.execute("SELECT receipt FROM motor WHERE id=?", (operation_id,)).fetchone()
        return json.loads(row["receipt"]) if row and row["receipt"] else None

    def _authorize_successor(self, intent, request, selection, authority):
        if not isinstance(request, MotorRequest):
            raise TypeError("request must be MotorRequest")
        if not isinstance(selection, MotorSelection) or selection.candidate_id != intent.candidate_id:
            raise Forbidden("prepared successor selection does not identify its candidate")
        if self.selector is not None and selection.model != self.profile.selector_model:
            raise Forbidden("prepared successor selector model is not authorized")
        if request.goal_revision != intent.metadata.goal_revision:
            raise Conflict("prepared successor goal revision differs from request")
        if request.observation_revision != intent.metadata.observation_revision:
            raise Conflict("prepared successor observation revision differs from request")
        if request.stop_epoch != intent.metadata.stop_epoch or request.stop_epoch != self.stop_epoch:
            raise Conflict("prepared successor stop epoch is stale")
        if not callable(authority):
            raise Forbidden("prepared successor requires live authority")
        try:
            live = authority(request)
        except Exception as exc:
            raise MotorOutcomeUnknown("live successor authority could not be checked") from exc
        if not isinstance(live, dict):
            raise Forbidden("live successor authority must return ownership context")
        for key, expected in (
            ("owner", intent.metadata.owner),
            ("epoch", intent.metadata.epoch),
            ("goal_revision", intent.metadata.goal_revision),
            ("observation_revision", intent.metadata.observation_revision),
            ("stop_epoch", intent.metadata.stop_epoch),
        ):
            if live.get(key) != expected:
                raise Conflict(f"prepared successor {key} changed")
        for key in ("observation_frame_id", "camera_revision", "ui_revision"):
            expected = getattr(intent.metadata, key)
            if expected is not None and live.get(key) != expected:
                raise Conflict(f"prepared successor {key} changed")
        if (
            intent.metadata.observed_at_ms is not None
            and intent.metadata.observed_at_ms + intent.freshness_ms < time.time() * 1000
        ):
            raise Conflict("prepared successor observation freshness expired")
        if intent.metadata.native_tick is not None:
            live_tick = live.get("native_tick")
            if not isinstance(live_tick, int) or live_tick < intent.metadata.native_tick:
                raise Conflict("prepared successor native observation tick changed")
        if intent.expires_tick is not None:
            native_tick = live.get("native_tick")
            if not isinstance(native_tick, int) or native_tick > intent.expires_tick:
                raise Conflict("prepared successor native tick window expired")
        if intent.prepared_at_ms + intent.freshness_ms < time.time() * 1000:
            raise Conflict("prepared successor freshness window expired")
        before = self.adapter.observe()
        if str(before.get("revision", "")) != request.observation_revision:
            raise Conflict("prepared successor observation is stale")
        try:
            candidates = tuple(self.adapter.plan(request, before))
        except (MotorError, ValueError) as exc:
            raise Forbidden("prepared successor plan is no longer legal") from exc
        candidate = next((item for item in candidates if item.id == intent.candidate_id), None)
        if candidate is None or intent.step not in candidate.steps:
            raise Forbidden("prepared successor step is not the selected plan")
        invalid = _validate_candidate(request, candidate, before)
        if invalid:
            raise Forbidden(f"prepared successor violates request authority: {invalid}")
        return live

    def prepare_successor(self, intent, *, request, selection, authority):
        """Persist one authorized successor intent without applying controls."""
        if not isinstance(intent, PreparedSuccessorIntent):
            raise TypeError("intent must be PreparedSuccessorIntent")
        if not getattr(self.adapter, "supports_prepared_successors", False):
            raise UnsupportedPreparation(f"{self.profile.adapter} does not support prepared successors")
        with self._db() as db:
            if self._unknown_successor_exists(db):
                raise MotorOutcomeUnknown("an unknown successor requires reconciliation")
        self._authorize_successor(intent, request, selection, authority)
        encoded = encode(intent.model_dump(mode="json"))
        request_encoded = encode(request.model_dump(mode="json"))
        selection_encoded = encode(selection.model_dump(mode="json"))
        with self._db() as db:
            prior = db.execute("SELECT * FROM motor_successors WHERE id=?", (intent.intent_id,)).fetchone()
            if prior:
                if prior["intent"] != encoded:
                    raise Conflict("prepared successor ID reused with different input")
                if prior["request"] != request_encoded or prior["selection"] != selection_encoded:
                    raise Conflict("prepared successor authorization changed")
                if prior["status"] == "prepared":
                    return intent
                raise MotorOutcomeUnknown("prepared successor was already settled or discarded")
            try:
                db.execute(
                    "INSERT INTO motor_successors(id,predecessor,intent,status,admission,request,selection) "
                    "VALUES (?,?,?,?,NULL,?,?)",
                    (
                        intent.intent_id,
                        intent.predecessor_operation_id,
                        encoded,
                        "prepared",
                        request_encoded,
                        selection_encoded,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("another prepared successor already owns the one-slot boundary") from exc
        try:
            prepared = self.adapter.prepare_successor(intent)
        except UnsupportedPreparation:
            with self._db() as db:
                db.execute(
                    "UPDATE motor_successors SET status='rejected',admission=? WHERE id=?",
                    (encode({"reason_code": "unsupported_preparation"}), intent.intent_id),
                )
            raise
        except Exception as exc:
            with self._db() as db:
                db.execute(
                    "UPDATE motor_successors SET status='unknown',admission=? WHERE id=?",
                    (encode({"reason_code": "preparation_unknown"}), intent.intent_id),
                )
            raise MotorOutcomeUnknown("successor preparation outcome is unknown") from exc
        if not isinstance(prepared, PreparedSuccessorIntent):
            with self._db() as db:
                db.execute(
                    "UPDATE motor_successors SET status='unknown',admission=? WHERE id=?",
                    (encode({"reason_code": "preparation_unknown"}), intent.intent_id),
                )
            raise MotorOutcomeUnknown("adapter did not return the prepared successor intent")
        return prepared

    def admit_successor(self, intent_id, *, request, selection, authority, predecessor=None):
        """Admit one prepared successor at the adapter boundary."""
        if not getattr(self.adapter, "supports_prepared_successors", False):
            raise UnsupportedPreparation(f"{self.profile.adapter} does not support prepared successors")
        with self._db() as db:
            row = db.execute("SELECT * FROM motor_successors WHERE id=?", (intent_id,)).fetchone()
        if row is None:
            raise Conflict("unknown prepared successor")
        if row["status"] != "prepared":
            if row["status"] in {"admitted", "rejected"} and row["admission"]:
                value = json.loads(row["admission"])
                if value.get("intent_id") == intent_id:
                    return PreparedSuccessorAdmission.model_validate(value)
            raise MotorOutcomeUnknown("prepared successor is no longer retryable")
        intent = PreparedSuccessorIntent.model_validate(json.loads(row["intent"]))
        if row["request"] != encode(request.model_dump(mode="json")) or row["selection"] != encode(
            selection.model_dump(mode="json")
        ):
            raise Conflict("prepared successor authorization differs from the prepared request")
        if getattr(self.adapter, "native_admits_prepared_successors", False):
            try:
                native = self.adapter.reconcile_prepared_successor(intent)
            except Exception as exc:
                native = None
                with self._db() as db:
                    db.execute(
                        "UPDATE motor_successors SET status='unknown',admission=? WHERE id=? AND status='prepared'",
                        (encode({"reason_code": "native_admission_unknown"}), intent_id),
                    )
                raise MotorOutcomeUnknown("native successor admission outcome is unknown") from exc
            if native is None:
                with self._db() as db:
                    db.execute(
                        "UPDATE motor_successors SET status='unknown',admission=? WHERE id=? AND status='prepared'",
                        (encode({"reason_code": "native_admission_unknown"}), intent_id),
                    )
                raise MotorOutcomeUnknown("native successor admission remains unknown")
            if not isinstance(native, PreparedSuccessorAdmission):
                raise MotorOutcomeUnknown("native adapter returned an invalid successor admission")
            if (
                native.intent_id != intent.intent_id
                or native.operation_id != intent.operation_id
                or native.predecessor_operation_id != intent.predecessor_operation_id
                or native.status not in {"admitted", "rejected", "unknown"}
            ):
                raise MotorOutcomeUnknown("native successor admission identity is invalid")
            with self._db() as db:
                db.execute(
                    "UPDATE motor_successors SET status=?, admission=? WHERE id=? AND status='prepared'",
                    (native.status, encode(native.model_dump(mode="json")), intent_id),
                )
            if native.status == "unknown":
                raise MotorOutcomeUnknown("native successor admission remains unknown")
            return native
        try:
            self._authorize_successor(intent, request, selection, authority)
        except MotorOutcomeUnknown as exc:
            with self._db() as db:
                db.execute(
                    "UPDATE motor_successors SET status='unknown',admission=? WHERE id=? AND status='prepared'",
                    (encode({"reason_code": "authority_unknown"}), intent_id),
                )
            raise exc
        except (Conflict, Forbidden):
            rejected = PreparedSuccessorAdmission(
                intent_id=intent.intent_id,
                predecessor_operation_id=intent.predecessor_operation_id,
                operation_id=intent.operation_id,
                metadata=intent.metadata,
                status="rejected",
                reason_code="admission_rejected",
            )
            with self._db() as db:
                db.execute(
                    "UPDATE motor_successors SET status='rejected',admission=? WHERE id=? AND status='prepared'",
                    (encode(rejected.model_dump(mode="json")), intent_id),
                )
            return rejected
        if predecessor is not None and predecessor.get("operation_id") != intent.predecessor_operation_id:
            raise Conflict("predecessor receipt identity does not match intent")
        prior = predecessor or self.lookup(intent.predecessor_operation_id)
        if prior is None:
            admission = PreparedSuccessorAdmission(
                intent_id=intent.intent_id,
                predecessor_operation_id=intent.predecessor_operation_id,
                operation_id=intent.operation_id,
                metadata=intent.metadata,
                status="unknown",
                reason_code="predecessor_unknown",
            )
        elif prior.get("status") != "completed" or prior.get("outcome", "completed") != "completed":
            admission = PreparedSuccessorAdmission(
                intent_id=intent.intent_id,
                predecessor_operation_id=intent.predecessor_operation_id,
                operation_id=intent.operation_id,
                metadata=intent.metadata,
                status="rejected",
                reason_code="predecessor_not_completed",
            )
        else:
            try:
                admission = self.adapter.admit_successor(
                    intent, predecessor=MotorReceipt.model_validate(prior)
                )
            except Exception as exc:
                unknown = PreparedSuccessorAdmission(
                    intent_id=intent.intent_id,
                    predecessor_operation_id=intent.predecessor_operation_id,
                    operation_id=intent.operation_id,
                    metadata=intent.metadata,
                    status="unknown",
                    reason_code="admission_unknown",
                )
                with self._db() as db:
                    db.execute(
                        "UPDATE motor_successors SET status='unknown',admission=? WHERE id=? AND status='prepared'",
                        (encode(unknown.model_dump(mode="json")), intent_id),
                    )
                raise MotorOutcomeUnknown("successor admission outcome is unknown") from exc
            if not isinstance(admission, PreparedSuccessorAdmission):
                raise MotorOutcomeUnknown("adapter did not return a successor admission")
        with self._db() as db:
            db.execute(
                "UPDATE motor_successors SET status=?, admission=? WHERE id=? AND status='prepared'",
                (admission.status, encode(admission.model_dump(mode="json")), intent_id),
            )
        return admission

    # Short generic names make adapters usable by schedulers that do not know
    # the Minecraft-specific terminology.
    prepare = prepare_successor
    admit = admit_successor

    def reconcile(self, operation_id):
        return self.adapter.reconcile(operation_id)

    def reconcile_successor(self, intent_id):
        """Resolve an unknown successor through the adapter ledger only.

        Reconciliation may settle an unknown record, but it never resends the
        prepared intent or calls native admission a second time.
        """
        with self._db() as db:
            row = db.execute("SELECT * FROM motor_successors WHERE id=?", (intent_id,)).fetchone()
        if row is None:
            raise Conflict("unknown prepared successor")
        native_restart_reconcile = (
            getattr(self.adapter, "native_admits_prepared_successors", False) and row["status"] == "prepared"
        )
        if row["status"] != "unknown" and not native_restart_reconcile:
            if row["admission"]:
                value = json.loads(row["admission"])
                if value.get("intent_id") == intent_id:
                    return PreparedSuccessorAdmission.model_validate(value)
            raise Conflict("successor is not awaiting reconciliation")
        intent = PreparedSuccessorIntent.model_validate(json.loads(row["intent"]))
        if getattr(self.adapter, "native_admits_prepared_successors", False):
            try:
                result = self.adapter.reconcile_prepared_successor(intent)
            except Exception as exc:
                raise MotorOutcomeUnknown("native reconciliation remains unknown") from exc
            if isinstance(result, PreparedSuccessorAdmission):
                if (
                    result.intent_id != intent.intent_id
                    or result.operation_id != intent.operation_id
                    or result.predecessor_operation_id != intent.predecessor_operation_id
                ):
                    raise MotorOutcomeUnknown("native reconciliation identity is invalid")
                admission = result
                if admission.status == "unknown":
                    raise MotorOutcomeUnknown("native reconciliation remains unknown")
                with self._db() as db:
                    db.execute(
                        "UPDATE motor_successors SET status=?,admission=? WHERE id=? AND status IN ('unknown','prepared')",
                        (admission.status, encode(admission.model_dump(mode="json")), intent_id),
                    )
                return admission
        else:
            result = self.adapter.reconcile(intent.operation_id)
        if not isinstance(result, dict) or result.get("operation_id") != intent.operation_id:
            raise MotorOutcomeUnknown("native reconciliation did not identify the successor")
        status = result.get("status")
        if status in {"completed", "admitted"}:
            settled = "admitted"
            reason = "reconciled"
        elif status in {"rejected", "cancelled", "blocked"}:
            settled = "rejected"
            reason = "reconciled_rejected"
        else:
            raise MotorOutcomeUnknown("native reconciliation remains unknown")
        admission = PreparedSuccessorAdmission(
            intent_id=intent.intent_id,
            predecessor_operation_id=intent.predecessor_operation_id,
            operation_id=intent.operation_id,
            metadata=intent.metadata,
            status=settled,
            reason_code=reason,
        )
        with self._db() as db:
            db.execute(
                "UPDATE motor_successors SET status=?,admission=? WHERE id=? AND status='unknown'",
                (settled, encode(admission.model_dump(mode="json")), intent_id),
            )
        return admission

    def progress(self, operation_id):
        with self._db() as db:
            row = db.execute("SELECT status,progress FROM motor WHERE id=?", (operation_id,)).fetchone()
        return {"status": row["status"], "steps": json.loads(row["progress"])} if row else None

"""Durable, bounded motor execution through the external-operation boundary.

Drivers must deduplicate step IDs and honor cancellation/deadlines. A crashed or
uncertain effect is never retried automatically. Use one journal per controlled
application to share its input lease between workers.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, cast

from .errors import BudgetExceeded, Conflict, Forbidden
from .motor_adapters import MotorError
from .motor_contracts import (
    MotorAdapter,
    MotorCandidate,
    MotorControlPermission,
    MotorProfile,
    MotorReceipt,
    MotorRequest,
)
from .store import encode


class MotorOutcomeUnknown(Conflict):
    """Execution or cost cannot be proven. Keep the operation reservation."""


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
    return any(
        isinstance(permission, MotorControlPermission) and permission.controls == step.controls
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


class MotorExecutor:
    endpoint = "motor"
    implementation = "motor.v1"

    def __init__(self, adapter: MotorAdapter, profile: MotorProfile, *, journal: Path, selector=None):
        if profile.adapter != adapter.implementation:
            raise ValueError("motor adapter does not match the frozen profile")
        if (profile.mode == "jev") != (selector is not None):
            raise ValueError("Jev mode requires a selector; deterministic mode forbids one")
        if selector is not None and profile.selector_model != selector.model:
            raise ValueError("selector does not match the pinned model")
        try:
            import fcntl
        except ImportError as exc:
            raise RuntimeError("MotorExecutor requires POSIX application locking") from exc
        self._fcntl = fcntl
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

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.journal, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

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
        """After explicit operator resume, stop and observe before allowing NEW IDs.

        Historical receipts and cost reservations remain unknown. This only
        releases the application input fence and never retries an old effect.
        """
        if not self._lock.acquire(blocking=False):
            raise Conflict("motor is still settling")
        try:
            with self.journal.with_suffix(self.journal.suffix + ".lock").open("a") as lock:
                try:
                    self._fcntl.flock(lock, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise Conflict("motor is still settling") from exc
                try:
                    self.stop()
                    observation = self.adapter.observe()
                    with self._db() as db:
                        rows = db.execute("SELECT id FROM motor WHERE receipt IS NULL").fetchall()
                        ids = [row["id"] for row in rows]
                        db.execute(
                            "INSERT INTO recoveries VALUES (?,?,?)",
                            (self.stop_epoch, encode(observation), encode(ids)),
                        )
                        db.execute("UPDATE motor SET status='acknowledged_unknown' WHERE receipt IS NULL")
                    return {"operation_ids": ids, "observation": observation, "stop_epoch": self.stop_epoch}
                finally:
                    self._fcntl.flock(lock, self._fcntl.LOCK_UN)
        finally:
            self._lock.release()

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
        return payload

    def lookup(self, operation_id):
        """Return only a durable final receipt. Never replay missing effects."""
        with self._db() as db:
            row = db.execute("SELECT receipt FROM motor WHERE id=?", (operation_id,)).fetchone()
        return json.loads(row["receipt"]) if row and row["receipt"] else None

    def progress(self, operation_id):
        with self._db() as db:
            row = db.execute("SELECT status,progress FROM motor WHERE id=?", (operation_id,)).fetchone()
        return {"status": row["status"], "steps": json.loads(row["progress"])} if row else None

    def execute(self, operation_id, request, maximum_cost_micros, *, authority=None):
        if authority is None:
            raise Forbidden("dispatch motors through Operations to supply live authority")
        payload = MotorRequest.model_validate(request["payload"])
        if not self._lock.acquire(blocking=False):
            raise Conflict("motor already has an input owner")
        try:
            with self.journal.with_suffix(self.journal.suffix + ".lock").open("a") as lock:
                try:
                    self._fcntl.flock(lock, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise Conflict("motor already has an input owner") from exc
                try:
                    return self._execute(operation_id, request, payload, maximum_cost_micros, authority)
                finally:
                    self._fcntl.flock(lock, self._fcntl.LOCK_UN)
        finally:
            self._lock.release()

    def _save_progress(self, operation_id, steps):
        with self._db() as db:
            db.execute("UPDATE motor SET progress=? WHERE id=?", (encode(steps), operation_id))

    @staticmethod
    def _step_recorded(steps, step_id):
        return any(entry["id"] == step_id for entry in steps)

    def _execute(self, operation_id, envelope, request, budget, authority):
        identity = encode(
            {"request": envelope, "profile": self.profile.model_dump(mode="json"), "budget": budget}
        )
        with self._db() as db:
            prior = db.execute("SELECT * FROM motor WHERE id=?", (operation_id,)).fetchone()
            if prior:
                if prior["request"] != identity:
                    raise Conflict("motor operation ID reused with different input")
                if prior["receipt"]:
                    return json.loads(prior["receipt"])
                raise MotorOutcomeUnknown("prior motor dispatch requires reconciliation")
            # An unresolved operation owns the application until explicitly reconciled.
            if db.execute(
                "SELECT 1 FROM motor WHERE receipt IS NULL AND status!='acknowledged_unknown' LIMIT 1"
            ).fetchone():
                raise MotorOutcomeUnknown("application has an unresolved motor operation")
            db.execute("INSERT INTO motor VALUES (?,?,'running',NULL,'[]')", (operation_id, identity))
        self._cancel = threading.Event()
        cancel = self._cancel
        started = time.monotonic()
        deadline = started + request.timeout_ms / 1000
        before, after, steps, selection = {}, {}, [], None
        cost = 0
        done = threading.Event()

        def check():
            if cancel.is_set() or time.monotonic() >= deadline or self.stop_epoch != request.stop_epoch:
                cancel.set()
                return False
            try:
                authority(request)
            except Exception:
                cancel.set()
                return False
            return True

        def watch():
            while not done.wait(0.025):
                if not check():
                    try:
                        self.adapter.stop()
                    finally:
                        return

        watcher = threading.Thread(target=watch, daemon=True, name="motor-fence")
        watcher.start()

        def finish(status, reason=None, reason_code=None):
            receipt = MotorReceipt(
                operation_id=operation_id,
                status=status,
                cost_micros=cost,
                profile=self.profile,
                request=request,
                reason=reason,
                before=before,
                after=after,
                selection=selection,
                steps=tuple(steps),
                reason_code=reason_code,
                elapsed_ms=(time.monotonic() - started) * 1000,
            ).model_dump(mode="json")
            with self._db() as db:
                db.execute(
                    "UPDATE motor SET status=?,receipt=? WHERE id=?", (status, encode(receipt), operation_id)
                )
            return receipt

        try:
            if not check():
                return finish("cancelled", "authority, deadline, or stop epoch expired", "authority_expired")
            before = self.adapter.observe()
            after = before
            if str(before.get("revision", "")) != request.observation_revision:
                return finish("blocked", "observation revision changed", "observation_changed")
            try:
                candidates = tuple(self.adapter.plan(request, before))
            except (MotorError, ValueError) as exc:
                return finish("blocked", str(exc), "invalid_plan")
            if not candidates or any(not isinstance(c, MotorCandidate) for c in candidates):
                return finish("blocked", "adapter supplied no valid bounded plans", "no_plan")
            if len({c.id for c in candidates}) != len(candidates) or any(
                len(c.steps) > request.max_steps for c in candidates
            ):
                return finish("blocked", "candidate IDs or step bounds are invalid", "invalid_candidate")
            invalid = None
            for candidate in candidates:
                invalid = _validate_candidate(request, candidate, before)
                if invalid:
                    break
            if invalid:
                return finish("blocked", "candidate violates the request authority", invalid)
            if not envelope.get("write") and any(
                step.operation != "read" for c in candidates for step in c.steps
            ):
                return finish(
                    "blocked", "read-only request cannot execute motor effects", "write_authority_required"
                )
            chosen = candidates[0]
            if self.selector is not None:
                projection = getattr(self.adapter, "selection_observation", None)
                state = {
                    "observation": projection(request, before) if projection else before,
                    "request": request.model_dump(mode="json"),
                }
                if self.selector.maximum_cost(state, candidates) > budget:
                    return finish(
                        "blocked", "selector reservation exceeds operation budget", "budget_exceeded"
                    )
                if not check():
                    return finish("cancelled", "authority expired before selection", "authority_expired")
                selection = self.selector.select(
                    state, candidates, maximum_cost_micros=budget, cancel=cancel, deadline=deadline
                )
                cost = selection.cost_micros
                if cost > budget:
                    raise BudgetExceeded("selector exceeded its reservation")
                if selection.model != self.profile.selector_model:
                    return finish("blocked", "selector model changed", "selector_model_changed")
                chosen = next((c for c in candidates if c.id == selection.candidate_id), None)
                if chosen is None:
                    if selection.candidate_id is None:
                        return finish("blocked", "selector abstained", "abstention")
                    return finish("blocked", "selector returned an unknown candidate", "invalid_candidate")
            for index, step in enumerate(chosen.steps):
                if not check():
                    return finish(
                        "cancelled", "authority, deadline, or stop epoch expired", "authority_expired"
                    )
                fresh = self.adapter.observe()
                if fresh.get("revision") != after.get("revision"):
                    return finish("blocked", "application changed before motor effect", "observation_changed")
                # Revalidate target identity against current state before every effect.
                try:
                    current_request = request.model_copy(
                        update={"observation_revision": str(fresh["revision"])}
                    )
                    revalidate = getattr(self.adapter, "revalidate", None)
                    replanned = (
                        cast(
                            Callable[
                                [MotorRequest, dict[str, Any], MotorCandidate, int],
                                tuple[MotorCandidate, ...],
                            ],
                            revalidate,
                        )(current_request, fresh, chosen, index)
                        if callable(revalidate)
                        else self.adapter.plan(current_request, fresh)
                    )
                    current = next((c for c in replanned if c.id == chosen.id), None)
                    if current is None or index >= len(current.steps) or current.steps[index] != step:
                        return finish("blocked", "planned control changed before effect", "plan_changed")
                    invalid = _validate_candidate(current_request, current, fresh)
                    if invalid:
                        return finish("blocked", "revalidated candidate violates request authority", invalid)
                except (MotorError, ValueError) as exc:
                    return finish("blocked", str(exc), "invalid_plan")
                if not check():
                    return finish("cancelled", "authority expired before motor effect", "authority_expired")
                step_id = f"{operation_id}:motor:{index}"
                if self._step_recorded(steps, step_id):
                    return finish("blocked", "step effect was already recorded", "duplicate_effect")
                entry = {
                    "id": step_id,
                    "candidate": chosen.id,
                    "step": step.model_dump(mode="json"),
                    "status": "dispatching",
                }
                steps.append(entry)
                self._save_progress(operation_id, steps)
                tick = time.monotonic()
                result = self.adapter.execute(step, operation_id=step_id, cancel=cancel, deadline=deadline)
                if (
                    not isinstance(result, dict)
                    or result.get("operation_id") != step_id
                    or result.get("status") not in {"completed", "blocked", "cancelled"}
                ):
                    raise MotorOutcomeUnknown("driver did not prove the step outcome")
                entry.update(
                    status=result["status"], receipt=result, elapsed_ms=(time.monotonic() - tick) * 1000
                )
                self._save_progress(operation_id, steps)
                after = self.adapter.observe()
                if result["status"] != "completed":
                    return finish(
                        result["status"],
                        "driver stopped the bounded skill",
                        "driver_cancelled" if result["status"] == "cancelled" else "effect_rejected",
                    )
                if not check():
                    return finish("cancelled", "authority expired after effect", "authority_expired")
            if not _matches(request.expected, after):
                raise MotorOutcomeUnknown("effect completed but its postcondition was not confirmed")
            return finish("completed", reason_code="completed")
        except BaseException:
            cancel.set()
            try:
                self.adapter.stop()
            finally:
                with self._db() as db:
                    db.execute("UPDATE motor SET status='unknown' WHERE id=?", (operation_id,))
            raise
        finally:
            done.set()
            watcher.join(timeout=1)

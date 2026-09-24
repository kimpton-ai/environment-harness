"""Durable controller input batches and fenced execution intervals.

This journal records intent and committed receipts. It never invokes a simulator,
controller, or operation provider itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .errors import Conflict, Forbidden
from .store import digest, encode, uid

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RUNTIME_IDENTITY_BYTES = 16_384


class ControlIntervals:
    """Mixin for the native EnvironmentSession durability boundary."""

    if TYPE_CHECKING:
        store: Any

        def _fence(self, row: Any, lease: Any) -> None: ...

    def prepare_control_interval(
        self,
        environment,
        who,
        lease,
        *,
        interval_id,
        operation_id,
        runtime_identity,
        start_checkpoint,
        simulated_seconds,
    ):
        """Bind one bounded interval to a prepared Harness operation and worker profile."""
        self._validate_identifier(interval_id, "interval")
        self._validate_identifier(operation_id, "operation")
        duration = self._duration(simulated_seconds)
        runtime, runtime_hash = self._runtime_identity(runtime_identity)
        checkpoint = self._checkpoint_reference(environment, who, start_checkpoint)
        created = time.time()
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("researcher", "worker"))
            operation = db.execute(
                "SELECT request,status FROM operations WHERE environment=? AND id=?",
                (environment, operation_id),
            ).fetchone()
            if not operation:
                raise Conflict("control interval operation is unavailable")
            operation_request_hash = digest(json.loads(operation["request"]))
            prior = db.execute(
                "SELECT * FROM control_intervals WHERE environment=? AND id=?",
                (environment, interval_id),
            ).fetchone()
            contract = (
                operation_id,
                operation_request_hash,
                runtime,
                runtime_hash,
                lease["owner"],
                lease["epoch"],
                checkpoint,
                duration,
            )
            if prior:
                existing = (
                    prior["operation_id"],
                    prior["operation_request_hash"],
                    json.loads(prior["runtime_identity"]),
                    prior["runtime_identity_hash"],
                    prior["lease_owner"],
                    prior["lease_epoch"],
                    json.loads(prior["start_checkpoint"]),
                    prior["simulated_seconds"],
                )
                if existing != contract:
                    raise Conflict("control interval identifier reused")
                return self._interval_receipt(prior)
            self._fence(row, lease)
            if row["status"] != "running":
                raise Conflict("session is not running")
            if operation["status"] != "prepared":
                raise Conflict("control interval requires an unsent prepared operation")
            previous = db.execute(
                "SELECT status,end_checkpoint FROM control_intervals WHERE environment=? "
                "ORDER BY sequence DESC LIMIT 1",
                (environment,),
            ).fetchone()
            if previous and previous["status"] != "committed":
                raise Conflict("previous control interval requires reconciliation")
            if previous and json.loads(previous["end_checkpoint"]) != checkpoint:
                raise Conflict("control interval must start at the last committed checkpoint")
            sequence = db.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM control_intervals WHERE environment=?",
                (environment,),
            ).fetchone()[0]
            grant_id = uid()
            db.execute(
                "INSERT INTO control_intervals (environment,id,sequence,revision,operation_id,"
                "operation_request_hash,runtime_identity,runtime_identity_hash,lease_owner,lease_epoch,"
                "start_checkpoint,simulated_seconds,controller_grant,status,created) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?)",
                (
                    environment,
                    interval_id,
                    sequence,
                    row["revision"],
                    operation_id,
                    operation_request_hash,
                    encode(runtime),
                    runtime_hash,
                    lease["owner"],
                    lease["epoch"],
                    encode(checkpoint),
                    duration,
                    grant_id,
                    created,
                ),
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "control.interval_prepared",
                {
                    "interval_id": interval_id,
                    "sequence": sequence,
                    "operation_id": operation_id,
                    "operation_request_hash": operation_request_hash,
                    "runtime_identity_hash": runtime_hash,
                    "start_checkpoint": checkpoint,
                    "simulated_seconds": duration,
                    "lease_epoch": lease["epoch"],
                    "status": "provisional",
                },
            )
            saved = db.execute(
                "SELECT * FROM control_intervals WHERE environment=? AND id=?",
                (environment, interval_id),
            ).fetchone()
            return self._interval_receipt(saved)

    def grant_control(
        self,
        environment,
        who,
        lease,
        *,
        interval_id,
        grant_id,
        controller,
        ttl=30,
        participant=None,
    ):
        """Issue one short-lived controller grant fenced to the interval's worker epoch."""
        self._validate_identifier(interval_id, "interval")
        self._validate_identifier(grant_id, "grant")
        if not isinstance(controller, str) or not controller.strip() or len(controller) > 256:
            raise ValueError("controller identity is required")
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not 0 < ttl <= 300:
            raise ValueError("invalid controller grant lifetime")
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("researcher", "worker"))
            interval = db.execute(
                "SELECT * FROM control_intervals WHERE environment=? AND id=?",
                (environment, interval_id),
            ).fetchone()
            if not interval:
                raise Conflict("control interval is unavailable for a grant")
            if interval["controller_grant"] != grant_id:
                raise Conflict("controller grant ID does not match the prepared interval")
            existing = db.execute(
                "SELECT * FROM control_grants WHERE environment=? AND interval_id=?",
                (environment, interval_id),
            ).fetchone()
            if existing:
                if (
                    existing["id"] != grant_id
                    or existing["controller"] != controller
                    or existing["participant"] != participant
                    or existing["lease_epoch"] != lease["epoch"]
                    or existing["requested_ttl"] != float(ttl)
                ):
                    raise Conflict("control interval already has a different controller grant")
                return self._grant_receipt(existing)
            self._fence(row, lease)
            if interval["status"] != "prepared":
                raise Conflict("control interval is unavailable for a grant")
            if interval["lease_epoch"] != lease["epoch"] or interval["lease_owner"] != lease["owner"]:
                raise Conflict("control interval worker authority expired")
            participants = json.loads(row["participants"])
            if participant is not None and (
                participant not in participants or not participants[participant]["active"]
            ):
                raise Forbidden("controller participant is unavailable")
            generation = participants[participant]["generation"] if participant else 0
            expires = min(time.time() + float(ttl), float(row["lease_until"]))
            db.execute(
                "INSERT INTO control_grants (environment,id,interval_id,controller,participant,"
                "generation,lease_epoch,expires,requested_ttl,last_sequence) VALUES (?,?,?,?,?,?,?,?,?,0)",
                (
                    environment,
                    grant_id,
                    interval_id,
                    controller,
                    participant,
                    generation,
                    lease["epoch"],
                    expires,
                    float(ttl),
                ),
            )
            db.execute(
                "UPDATE control_intervals SET status='open' WHERE environment=? AND id=? AND status='prepared'",
                (environment, interval_id),
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "control.grant_issued",
                {
                    "interval_id": interval_id,
                    "grant_id": grant_id,
                    "controller": controller,
                    "participant": participant,
                    "generation": generation,
                    "lease_epoch": lease["epoch"],
                    "expires": expires,
                    "status": "provisional",
                },
            )
            saved = db.execute(
                "SELECT * FROM control_grants WHERE environment=? AND id=?",
                (environment, grant_id),
            ).fetchone()
            return self._grant_receipt(saved)

    def accept_control_inputs(
        self,
        environment,
        who,
        *,
        interval_id,
        grant_id,
        batch_id,
        sequence,
        inputs,
    ):
        """Durably acknowledge one exact input batch before a caller applies it."""
        for value, kind in ((interval_id, "interval"), (grant_id, "grant"), (batch_id, "batch")):
            self._validate_identifier(value, kind)
        if type(sequence) is not int or sequence < 1:
            raise ValueError("control input sequence must be a positive integer")
        if not isinstance(inputs, Mapping):
            raise ValueError("control inputs must be a JSON object")
        request = {
            "interval_id": interval_id,
            "grant_id": grant_id,
            "batch_id": batch_id,
            "sequence": sequence,
            "inputs": dict(inputs),
        }
        request_json = encode(request)
        request_hash = hashlib.sha256(request_json.encode()).hexdigest()
        now = time.time()
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who)
            grant = db.execute(
                "SELECT * FROM control_grants WHERE environment=? AND id=? AND interval_id=?",
                (environment, grant_id, interval_id),
            ).fetchone()
            if not grant or who.subject != grant["controller"]:
                raise Forbidden("controller grant unavailable")
            if who.role == "agent" and (
                who.participant != grant["participant"] or who.generation != grant["generation"]
            ):
                raise Forbidden("controller grant unavailable")
            prior = db.execute(
                "SELECT * FROM control_inputs WHERE environment=? AND batch_id=?",
                (environment, batch_id),
            ).fetchone()
            if prior:
                if (
                    prior["interval_id"] != interval_id
                    or prior["grant_id"] != grant_id
                    or prior["request"] != request_json
                ):
                    raise Conflict("control batch identifier reused")
                return json.loads(prior["acknowledgement"])
            interval = db.execute(
                "SELECT * FROM control_intervals WHERE environment=? AND id=?",
                (environment, interval_id),
            ).fetchone()
            if not interval or interval["status"] != "open":
                raise Conflict("control interval is not accepting input")
            if (
                row["status"] != "running"
                or row["revision"] != interval["revision"]
                or row["lease_owner"] != interval["lease_owner"]
                or row["lease_epoch"] != grant["lease_epoch"]
                or row["lease_until"] <= now
            ):
                raise Conflict("control input authority is stale or expired")
            if grant["expires"] <= now:
                raise Forbidden("controller grant expired")
            if sequence != grant["last_sequence"] + 1:
                raise Conflict("control input sequence is stale or has a gap")
            if len(request_json.encode()) > json.loads(row["manifest"])["policy"]["max_event_bytes"]:
                raise Conflict("control input size limit exceeded")
            ack = {
                "batch_id": batch_id,
                "interval_id": interval_id,
                "grant_id": grant_id,
                "sequence": sequence,
                "input_hash": request_hash,
                "status": "accepted",
                "provisional": True,
                "committed": False,
            }
            db.execute(
                "INSERT INTO control_inputs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    environment,
                    interval_id,
                    batch_id,
                    grant_id,
                    sequence,
                    request_json,
                    request_hash,
                    encode(ack),
                    now,
                ),
            )
            db.execute(
                "UPDATE control_grants SET last_sequence=? WHERE environment=? AND id=?",
                (sequence, environment, grant_id),
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "control.input_accepted",
                {
                    "interval_id": interval_id,
                    "batch_id": batch_id,
                    "sequence": sequence,
                    "input_hash": request_hash,
                    "provisional": True,
                },
            )
            return ack

    def seal_control_interval(
        self,
        environment,
        who,
        lease,
        *,
        interval_id,
        end_checkpoint,
        measurements,
        control_log_digest,
    ):
        """Seal a completed interval with an artifact reference and journal digest."""
        self._validate_identifier(interval_id, "interval")
        if not isinstance(control_log_digest, str) or not _SHA256.fullmatch(control_log_digest):
            raise ValueError("control log digest must be a SHA-256 hex digest")
        if not isinstance(measurements, Mapping):
            raise ValueError("measurements must be a JSON object")
        checkpoint = self._checkpoint_reference(environment, who, end_checkpoint)
        measurement_json = encode(dict(measurements))
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("researcher", "worker"))
            interval = db.execute(
                "SELECT * FROM control_intervals WHERE environment=? AND id=?",
                (environment, interval_id),
            ).fetchone()
            if not interval:
                raise Forbidden("control interval unavailable")
            inputs = db.execute(
                "SELECT sequence,batch_id,request_hash FROM control_inputs WHERE environment=? AND interval_id=? "
                "ORDER BY sequence",
                (environment, interval_id),
            ).fetchall()
            journal_digest = digest(
                [
                    {
                        "sequence": item["sequence"],
                        "batch_id": item["batch_id"],
                        "input_hash": item["request_hash"],
                    }
                    for item in inputs
                ]
            )
            if control_log_digest != journal_digest:
                raise Conflict("control log digest does not match accepted input journal")
            if interval["lease_epoch"] != lease["epoch"] or interval["lease_owner"] != lease["owner"]:
                raise Conflict("control interval worker authority expired")
            operation = db.execute(
                "SELECT request,status FROM operations WHERE environment=? AND id=?",
                (environment, interval["operation_id"]),
            ).fetchone()
            if (
                not operation
                or digest(json.loads(operation["request"])) != interval["operation_request_hash"]
            ):
                raise Conflict("control operation intent changed")
            if interval["status"] in ("sealed", "committed"):
                sealed = (
                    interval["control_log_digest"],
                    json.loads(interval["end_checkpoint"]),
                    json.loads(interval["measurements"]),
                )
                if sealed != (control_log_digest, checkpoint, dict(measurements)):
                    raise Conflict("sealed control interval differs from retry")
                return self._interval_receipt(interval)
            self._fence(row, lease)
            if interval["status"] != "open":
                raise Conflict("control interval cannot be sealed")
            db.execute(
                "UPDATE control_intervals SET status='sealed',control_log_digest=?,end_checkpoint=?,"
                "measurements=?,sealed=? WHERE environment=? AND id=? AND status='open'",
                (
                    control_log_digest,
                    encode(checkpoint),
                    measurement_json,
                    time.time(),
                    environment,
                    interval_id,
                ),
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "control.interval_sealed",
                {
                    "interval_id": interval_id,
                    "operation_id": interval["operation_id"],
                    "control_log_digest": control_log_digest,
                    "end_checkpoint": checkpoint,
                    "measurements_hash": digest(dict(measurements)),
                    "status": "provisional",
                },
            )
            saved = db.execute(
                "SELECT * FROM control_intervals WHERE environment=? AND id=?",
                (environment, interval_id),
            ).fetchone()
            return self._interval_receipt(saved)

    def commit_control_interval(self, environment, who, lease, *, interval_id, operation_receipt):
        """Commit a sealed interval only after its exact original operation receipt settled."""
        self._validate_identifier(interval_id, "interval")
        if not isinstance(operation_receipt, Mapping):
            raise ValueError("settled operation receipt is required")
        receipt_json = encode(dict(operation_receipt))
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("researcher", "worker"))
            interval = db.execute(
                "SELECT * FROM control_intervals WHERE environment=? AND id=?",
                (environment, interval_id),
            ).fetchone()
            if not interval:
                raise Forbidden("control interval unavailable")
            if interval["status"] == "committed":
                if interval["receipt"] != receipt_json:
                    raise Conflict("control interval receipt differs from committed receipt")
                return self._interval_receipt(interval)
            self._fence(row, lease)
            if interval["status"] != "sealed":
                raise Conflict("control interval must be sealed before commit")
            if row["revision"] != interval["revision"] or row["status"] not in ("running", "paused"):
                raise Conflict("control interval revision is stale")
            operation = db.execute(
                "SELECT request,status,receipt FROM operations WHERE environment=? AND id=?",
                (environment, interval["operation_id"]),
            ).fetchone()
            if (
                not operation
                or digest(json.loads(operation["request"])) != interval["operation_request_hash"]
            ):
                raise Conflict("control operation intent changed")
            if operation["status"] != "succeeded" or operation["receipt"] != receipt_json:
                raise Conflict("control interval requires the exact settled operation receipt")
            saved_checkpoint = json.loads(interval["end_checkpoint"])
            self._checkpoint_reference_in(db, environment, who, saved_checkpoint)
            committed_at = time.time()
            db.execute(
                "UPDATE control_intervals SET status='committed',receipt=?,committed=? "
                "WHERE environment=? AND id=? AND status='sealed'",
                (receipt_json, committed_at, environment, interval_id),
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "control.interval_committed",
                {
                    "interval_id": interval_id,
                    "sequence": interval["sequence"],
                    "operation_id": interval["operation_id"],
                    "operation_receipt_hash": digest(dict(operation_receipt)),
                    "end_checkpoint": saved_checkpoint,
                    "control_log_digest": interval["control_log_digest"],
                    "status": "committed",
                    "provisional": False,
                },
            )
            committed = db.execute(
                "SELECT * FROM control_intervals WHERE environment=? AND id=?",
                (environment, interval_id),
            ).fetchone()
            return self._interval_receipt(committed)

    def commit_control_interval_for_operation(self, environment, who, lease, operation_id, receipt):
        """Commit an interval bound to this operation before its transition can advance."""
        with self.store.transaction() as db:
            self.store.environment(db, environment, who, ("researcher", "worker"))
            interval = db.execute(
                "SELECT id FROM control_intervals WHERE environment=? AND operation_id=?",
                (environment, operation_id),
            ).fetchone()
        if not interval:
            return None
        return self.commit_control_interval(
            environment, who, lease, interval_id=interval["id"], operation_receipt=receipt
        )

    def control_interval_for_operation(self, environment, who, operation_id):
        """Return the private interval already bound to an operation, if one exists."""
        self._validate_identifier(operation_id, "operation")
        with self.store.transaction() as db:
            self.store.environment(db, environment, who, ("researcher", "worker"))
            interval = db.execute(
                "SELECT * FROM control_intervals WHERE environment=? AND operation_id=?",
                (environment, operation_id),
            ).fetchone()
            if interval is None:
                return None
            self._checkpoint_reference_in(db, environment, who, json.loads(interval["start_checkpoint"]))
            if interval["end_checkpoint"]:
                self._checkpoint_reference_in(db, environment, who, json.loads(interval["end_checkpoint"]))
            return self._interval_receipt(interval)

    def control_input_digest(self, environment, who, interval_id):
        """Return the canonical digest of accepted input batches for one interval."""
        self._validate_identifier(interval_id, "interval")
        with self.store.transaction() as db:
            self.store.environment(db, environment, who, ("researcher", "worker"))
            interval = db.execute(
                "SELECT id FROM control_intervals WHERE environment=? AND id=?",
                (environment, interval_id),
            ).fetchone()
            if interval is None:
                raise Forbidden("control interval unavailable")
            inputs = db.execute(
                "SELECT sequence,batch_id,request_hash FROM control_inputs "
                "WHERE environment=? AND interval_id=? ORDER BY sequence",
                (environment, interval_id),
            ).fetchall()
            return digest(
                [
                    {
                        "sequence": item["sequence"],
                        "batch_id": item["batch_id"],
                        "input_hash": item["request_hash"],
                    }
                    for item in inputs
                ]
            )

    def validate_control_grant(
        self,
        environment,
        who,
        lease,
        *,
        interval_id,
        grant_id,
        controller,
        participant,
        generation,
        acknowledgement,
    ):
        """Fence provider work to the live grant and the exact persisted input ack."""
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("researcher", "worker"))
            self._fence(row, lease)
            grant = db.execute(
                "SELECT * FROM control_grants WHERE environment=? AND id=? AND interval_id=?",
                (environment, grant_id, interval_id),
            ).fetchone()
            interval = db.execute(
                "SELECT status,lease_owner,lease_epoch,revision FROM control_intervals "
                "WHERE environment=? AND id=?",
                (environment, interval_id),
            ).fetchone()
            if (
                not grant
                or not interval
                or interval["status"] != "open"
                or grant["controller"] != controller
                or grant["participant"] != participant
                or grant["generation"] != generation
                or grant["expires"] <= time.time()
                or grant["lease_epoch"] != lease["epoch"]
                or interval["lease_owner"] != lease["owner"]
                or interval["lease_epoch"] != lease["epoch"]
                or interval["revision"] != row["revision"]
            ):
                raise Forbidden("control grant is expired or fenced")
            if participant is not None:
                participants = json.loads(row["participants"])
                actor = participants.get(participant)
                if not actor or not actor["active"] or actor["generation"] != generation:
                    raise Forbidden("control participant authority expired")
            saved = db.execute(
                "SELECT acknowledgement FROM control_inputs WHERE environment=? AND batch_id=? "
                "AND interval_id=? AND grant_id=?",
                (environment, acknowledgement.get("batch_id"), interval_id, grant_id),
            ).fetchone()
            if not saved or json.loads(saved["acknowledgement"]) != dict(acknowledgement):
                raise Forbidden("control input acknowledgement is unavailable")
            return True

    def read_committed_control_intervals(self, environment, who, *, after_sequence=0, limit=100):
        """Read verified committed intervals and their transition event ancestry."""
        if who.role not in ("researcher", "scorer"):
            raise Forbidden("committed interval evidence requires grader authority")
        if (
            type(after_sequence) is not int
            or after_sequence < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError("invalid committed interval page")
        self.store.verify(environment, who)
        with self.store.transaction() as db:
            self.store.environment(db, environment, who, ("researcher", "scorer"))
            intervals = db.execute(
                "SELECT * FROM control_intervals WHERE environment=? AND status='committed' "
                "AND sequence>? ORDER BY sequence LIMIT ?",
                (environment, after_sequence, limit),
            ).fetchall()
            result = []
            for interval in intervals:
                start = json.loads(interval["start_checkpoint"])
                end = json.loads(interval["end_checkpoint"])
                self._checkpoint_reference_in(db, environment, who, start)
                self._checkpoint_reference_in(db, environment, who, end)
                for reference in (start, end):
                    data = self.store._read_artifact(environment, reference["artifact_id"])
                    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
                        raise Conflict("committed checkpoint artifact integrity failure")
                commit_event = next(
                    (
                        event
                        for event in db.execute(
                            "SELECT seq,revision,previous,hash,body FROM events WHERE environment=? "
                            "AND kind='control.interval_committed' ORDER BY seq",
                            (environment,),
                        ).fetchall()
                        if json.loads(event["body"]).get("interval_id") == interval["id"]
                    ),
                    None,
                )
                if commit_event is None:
                    raise Conflict("committed interval event ancestry is missing")
                event_body = json.loads(commit_event["body"])
                if event_body.get("operation_receipt_hash") != digest(json.loads(interval["receipt"])):
                    raise Conflict("committed interval event does not match its receipt")
                operation = db.execute(
                    "SELECT request FROM operations WHERE environment=? AND id=?",
                    (environment, interval["operation_id"]),
                ).fetchone()
                operation_request = json.loads(operation["request"]) if operation else {}
                transition_hash = operation_request.get("transition_hash")
                transition_revision = operation_request.get("transition_revision")
                ancestry = None
                if isinstance(transition_hash, str) and type(transition_revision) is int:
                    transition = db.execute(
                        "SELECT status,result FROM transitions WHERE environment=? AND revision=? AND input_hash=?",
                        (environment, transition_revision, transition_hash),
                    ).fetchone()
                    transition_event = db.execute(
                        "SELECT seq,revision,previous,hash,body FROM events WHERE environment=? "
                        "AND revision=? AND kind='transition.committed' ORDER BY seq DESC LIMIT 1",
                        (environment, transition_revision + 1),
                    ).fetchone()
                    if transition and transition["status"] == "committed" and transition_event:
                        result_body = json.loads(transition["result"])
                        state_hash = digest(result_body["state"])
                        event_body = json.loads(transition_event["body"])
                        if event_body.get("state_hash") != state_hash:
                            raise Conflict("transition state hash does not match its committed event")
                        ancestry = {
                            "status": "committed",
                            "input_hash": transition_hash,
                            "revision": transition_revision + 1,
                            "state_hash": state_hash,
                            "event": {
                                "sequence": transition_event["seq"],
                                "previous": transition_event["previous"],
                                "hash": transition_event["hash"],
                            },
                        }
                result.append(
                    {
                        "sequence": interval["sequence"],
                        "interval": self._interval_receipt(interval),
                        "event": {
                            "sequence": commit_event["seq"],
                            "revision": commit_event["revision"],
                            "previous": commit_event["previous"],
                            "hash": commit_event["hash"],
                        },
                        "transition": ancestry or {"status": "pending"},
                    }
                )
            return result

    def recover_control_interval(self, environment, who, interval_id, *, lease=None):
        """Return a settled receipt once, or identify a safe checkpoint for a new branch."""
        self._validate_identifier(interval_id, "interval")
        commit = None
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("researcher", "worker"))
            interval = db.execute(
                "SELECT * FROM control_intervals WHERE environment=? AND id=?",
                (environment, interval_id),
            ).fetchone()
            if not interval:
                raise Forbidden("control interval unavailable")
            if interval["status"] == "committed":
                return {"status": "committed", "receipt": self._interval_receipt(interval)}
            operation = db.execute(
                "SELECT status,receipt FROM operations WHERE environment=? AND id=?",
                (environment, interval["operation_id"]),
            ).fetchone()
            last = db.execute(
                "SELECT id,end_checkpoint FROM control_intervals WHERE environment=? AND status='committed' "
                "ORDER BY sequence DESC LIMIT 1",
                (environment,),
            ).fetchone()
            checkpoint = (
                json.loads(last["end_checkpoint"]) if last else json.loads(interval["start_checkpoint"])
            )
            if interval["status"] == "sealed" and operation and operation["status"] == "succeeded":
                recovery_lease = lease or {
                    "owner": interval["lease_owner"],
                    "epoch": interval["lease_epoch"],
                }
                receipt = json.loads(operation["receipt"])
                # Recovery only commits the already persisted receipt. It never calls a provider.
                can_commit = (
                    row["lease_owner"] == recovery_lease["owner"]
                    and row["lease_epoch"] == recovery_lease["epoch"]
                    and row["lease_until"] > time.time()
                )
                if can_commit:
                    commit = (recovery_lease, receipt)
            else:
                can_commit = False
                receipt = None
            operation_status = operation["status"] if operation else "missing"
            sealed_receipt = interval["status"] == "sealed" and operation_status == "succeeded"
            known_unsent = interval["status"] in ("prepared", "open") and operation_status == "prepared"
            result = {
                "status": "receipt_available"
                if sealed_receipt
                else "known_unsent"
                if known_unsent
                else "recovery_required",
                "operation_id": interval["operation_id"],
                "operation_status": operation_status,
                "last_committed_checkpoint": checkpoint,
                "branch_required": not (sealed_receipt or known_unsent),
                "provider_reexecution_allowed": False,
            }
            if sealed_receipt:
                result["receipt"] = receipt
        if commit:
            recovery_lease, receipt = commit
            saved = self.commit_control_interval(
                environment, who, recovery_lease, interval_id=interval_id, operation_receipt=receipt
            )
            return {"status": "committed", "receipt": saved, "provider_reexecution_allowed": False}
        return result

    def _checkpoint_reference(self, environment, who, reference):
        if not isinstance(reference, Mapping):
            raise ValueError("checkpoint reference must contain an artifact ID and SHA-256")
        artifact_id, sha = reference.get("artifact_id"), reference.get("sha256")
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or not isinstance(sha, str)
            or not _SHA256.fullmatch(sha)
        ):
            raise ValueError("checkpoint reference requires an artifact ID and SHA-256")
        with self.store.transaction() as db:
            self._checkpoint_reference_in(db, environment, who, reference)
        data = self.store._read_artifact(environment, artifact_id)
        if hashlib.sha256(data).hexdigest() != sha:
            raise Conflict("checkpoint artifact integrity failure")
        return {"artifact_id": artifact_id, "sha256": sha}

    def _checkpoint_limit(self, environment, who):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("researcher", "worker"))
            return json.loads(row["manifest"])["policy"].get("max_checkpoint_bytes", 67108864)

    def _checkpoint_reference_in(self, db, environment, who, reference):
        row = self.store.environment(db, environment, who, ("researcher", "worker"))
        artifact = db.execute(
            "SELECT * FROM artifacts WHERE environment=? AND id=?",
            (environment, reference["artifact_id"]),
        ).fetchone()
        if (
            not artifact
            or artifact["sha256"] != reference["sha256"]
            or json.loads(artifact["audience"]) != []
        ):
            raise Conflict("checkpoint artifact is unavailable or not content addressed")
        limit = json.loads(row["manifest"])["policy"].get("max_checkpoint_bytes", 67108864)
        if artifact["size"] > limit:
            raise Conflict("checkpoint artifact exceeds the frozen checkpoint limit")
        data = self.store._read_artifact(environment, reference["artifact_id"])
        if hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise Conflict("checkpoint artifact integrity failure")

    @staticmethod
    def _validate_identifier(value, label):
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"invalid {label} identifier")

    @staticmethod
    def _duration(value):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("simulated interval duration must be numeric")
        if not math.isfinite(value) or not 0 < value <= 1:
            raise ValueError("simulated interval duration must be greater than zero and at most one second")
        return float(value)

    @staticmethod
    def _runtime_identity(identity):
        if not isinstance(identity, Mapping) or not identity:
            raise ValueError("runtime identity must be a nonempty JSON object")
        canonical = encode(dict(identity))
        if len(canonical.encode()) > _MAX_RUNTIME_IDENTITY_BYTES:
            raise ValueError("runtime identity exceeds 16 KiB")
        return dict(identity), hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _interval_receipt(row):
        return {
            "interval_id": row["id"],
            "sequence": row["sequence"],
            "revision": row["revision"],
            "operation_id": row["operation_id"],
            "operation_request_hash": row["operation_request_hash"],
            "grant_id": row["controller_grant"],
            "runtime_identity": json.loads(row["runtime_identity"]),
            "runtime_identity_hash": row["runtime_identity_hash"],
            "start_checkpoint": json.loads(row["start_checkpoint"]),
            "simulated_seconds": row["simulated_seconds"],
            "status": row["status"],
            "control_log_digest": row["control_log_digest"],
            "end_checkpoint": json.loads(row["end_checkpoint"]) if row["end_checkpoint"] else None,
            "measurements": json.loads(row["measurements"]) if row["measurements"] else None,
            "operation_receipt": json.loads(row["receipt"]) if row["receipt"] else None,
            "provisional": row["status"] != "committed",
        }

    @staticmethod
    def _grant_receipt(row):
        return {
            "grant_id": row["id"],
            "interval_id": row["interval_id"],
            "controller": row["controller"],
            "participant": row["participant"],
            "generation": row["generation"],
            "lease_epoch": row["lease_epoch"],
            "expires": row["expires"],
            "requested_ttl": row["requested_ttl"],
        }

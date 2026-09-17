"""External-operation intents, conservative reservations and receipt reconciliation."""

import json

from .errors import BudgetExceeded, Conflict, Forbidden, Unsupported
from .store import encode


class Operations:
    def __init__(self, store):
        self.store = store

    def prepare(
        self,
        environment,
        who,
        operation_id,
        *,
        endpoint,
        operation,
        payload,
        maximum_cost_micros=0,
        write=False,
    ):
        if not operation_id or len(operation_id) > 128 or maximum_cost_micros < 0:
            raise ValueError("invalid operation")
        request = {"endpoint": endpoint, "operation": operation, "payload": payload, "write": write}
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("agent",))
            policy = json.loads(row["manifest"])["policy"]
            if row["status"] != "running":
                raise Conflict("session is not running")
            if endpoint not in policy["allowed_endpoints"] or operation not in policy["allowed_operations"]:
                raise Forbidden("operation is outside the frozen endpoint policy")
            if write and not policy["external_writes"]:
                raise Forbidden("live observation does not authorize external writes")
            prior = db.execute(
                "SELECT * FROM operations WHERE environment=? AND id=?", (environment, operation_id)
            ).fetchone()
            if prior:
                if (
                    prior["request"] != encode(request)
                    or prior["participant"] != who.participant
                    or prior["reservation"] != maximum_cost_micros
                ):
                    raise Conflict("operation identifier reused")
                return {"id": operation_id, "status": prior["status"]}
            if row["spent"] + row["reserved"] + maximum_cost_micros > policy["max_cost_micros"]:
                raise BudgetExceeded("operation exceeds remaining budget")
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,'prepared',NULL,?)",
                (
                    environment,
                    operation_id,
                    who.participant,
                    who.generation,
                    encode(request),
                    maximum_cost_micros,
                ),
            )
            db.execute(
                "UPDATE environments SET reserved=reserved+? WHERE id=?", (maximum_cost_micros, environment)
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "operation.intent",
                {
                    "id": operation_id,
                    "participant": who.participant,
                    "request": request,
                    "maximum_cost_micros": maximum_cost_micros,
                },
                (who.participant,),
            )
            return {"id": operation_id, "status": "prepared"}

    def dispatch(self, session, environment, who, lease, operation_id, provider):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("worker", "researcher"))
            session._fence(row, lease)
            op = db.execute(
                "SELECT * FROM operations WHERE environment=? AND id=?", (environment, operation_id)
            ).fetchone()
            if not op:
                raise Forbidden("operation unavailable")
            if op["status"] == "succeeded":
                return json.loads(op["receipt"])
            if op["status"] != "prepared":
                raise Conflict("operation requires receipt reconciliation")
            participant = json.loads(row["participants"])[op["participant"]]
            if (
                row["status"] != "running"
                or not participant["active"]
                or participant["generation"] != op["generation"]
            ):
                raise Forbidden("dispatch authority expired")
            request = json.loads(op["request"])
            if getattr(provider, "endpoint", None) != request["endpoint"]:
                raise Forbidden("provider endpoint does not match frozen intent")
            db.execute(
                "UPDATE operations SET status='dispatching' WHERE environment=? AND id=?",
                (environment, operation_id),
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "operation.dispatched",
                {"id": operation_id, "epoch": lease["epoch"]},
                (op["participant"],),
            )
        # No transaction during IO. Never auto-repeat a dispatch with an uncertain outcome.
        try:
            receipt = provider.execute(f"{environment}:{operation_id}", request, op["reservation"])
        except BaseException:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE operations SET status='unknown' WHERE environment=? AND id=? AND status='dispatching'",
                    (environment, operation_id),
                )
            raise
        return self.settle(environment, who, operation_id, receipt)

    def settle(self, environment, who, operation_id, receipt):
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("cost_micros"), int)
            or receipt["cost_micros"] < 0
        ):
            raise Conflict("provider receipt requires integer nonnegative cost")
        if receipt.get("operation_id") != f"{environment}:{operation_id}":
            raise Conflict("receipt operation identity mismatch")
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("worker", "researcher"))
            op = db.execute(
                "SELECT * FROM operations WHERE environment=? AND id=?", (environment, operation_id)
            ).fetchone()
            if not op:
                raise Forbidden("operation unavailable")
            if op["status"] == "succeeded":
                if op["receipt"] != encode(receipt):
                    raise Conflict("provider returned conflicting receipts")
                return receipt
            if op["status"] not in ("dispatching", "unknown"):
                raise Conflict("operation was not dispatched")
            if receipt["cost_micros"] > op["reservation"]:
                raise BudgetExceeded("provider exceeded its reservation; operator reconciliation required")
            db.execute(
                "UPDATE operations SET status='succeeded',receipt=? WHERE environment=? AND id=?",
                (encode(receipt), environment, operation_id),
            )
            db.execute(
                "UPDATE environments SET reserved=reserved-?,spent=spent+? WHERE id=?",
                (op["reservation"], receipt["cost_micros"], environment),
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "operation.receipt",
                {"id": operation_id, "receipt": receipt},
                (op["participant"],),
            )
            return receipt

    def reconcile(self, environment, who, operation_id, provider):
        with self.store.transaction() as db:
            self.store.environment(db, environment, who, ("worker", "researcher"))
            op = db.execute(
                "SELECT * FROM operations WHERE environment=? AND id=?", (environment, operation_id)
            ).fetchone()
            if not op:
                raise Forbidden("operation unavailable")
            if getattr(provider, "endpoint", None) != json.loads(op["request"])["endpoint"]:
                raise Forbidden("provider endpoint mismatch")
            if op["status"] == "succeeded":
                return json.loads(op["receipt"])
            if op["status"] not in ("unknown", "dispatching"):
                raise Conflict("only dispatched operations require reconciliation")
        receipt = provider.lookup(f"{environment}:{operation_id}")
        if receipt is None:
            raise Unsupported("provider cannot prove outcome; dispatch stays blocked")
        return self.settle(environment, who, operation_id, receipt)

    def cancel_prepared(self, environment, who):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("researcher", "worker"))
            return self._cancel_prepared(db, environment, row)

    def _cancel_prepared(self, db, environment, row):
        ops = db.execute(
            "SELECT * FROM operations WHERE environment=? AND status='prepared'", (environment,)
        ).fetchall()
        for op in ops:
            db.execute(
                "UPDATE operations SET status='failed' WHERE environment=? AND id=?", (environment, op["id"])
            )
            db.execute(
                "UPDATE environments SET reserved=reserved-? WHERE id=?", (op["reservation"], environment)
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "operation.cancelled",
                {"id": op["id"], "dispatched": False},
                (op["participant"],),
            )
        return {"cancelled": len(ops)}

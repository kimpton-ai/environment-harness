"""External-operation intents, conservative reservations and receipt reconciliation."""

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping

from .contracts import OperationSpec
from .errors import BudgetExceeded, Conflict, Forbidden, Unsupported
from .store import encode


class EnvironmentOperation(ABC):
    """An environment-owned capability executed through the durable operation boundary.

    Environment packages subclass this type and expose instances through their
    ``operations`` mapping. The harness records ``spec`` in each experiment and
    supplies the fenced ``authority`` callback at execution time.
    """

    endpoint: str
    spec: OperationSpec

    def validate(self, request, manifest):
        """Validate frozen configuration and return the payload used for fencing."""
        return request["payload"]

    @abstractmethod
    def execute(self, operation_id, request, maximum_cost_micros, *, authority):
        """Perform the effect once and return a durable receipt."""

    def lookup(self, operation_id):
        """Return a durable receipt during reconciliation, or ``None`` if unknown."""
        return None


def environment_operations(environment):
    """Return advertised runtime operations after checking the package contract."""
    advertised = {operation.name: operation for operation in environment.spec.operations}
    runtime = getattr(environment, "operations", {})
    if not isinstance(runtime, Mapping):
        raise Conflict("environment operations must be a mapping")
    missing = advertised.keys() - runtime.keys()
    if missing:
        name = sorted(missing)[0]
        raise Conflict(f"environment operation {name!r} has no runtime implementation")
    extra = runtime.keys() - advertised.keys()
    if extra:
        name = sorted(extra)[0]
        raise Conflict(f"runtime operation {name!r} is not advertised by the environment")
    checked = {}
    for name, declaration in advertised.items():
        operation = runtime[name]
        if not isinstance(operation, EnvironmentOperation):
            raise Conflict(f"environment operation {name!r} must extend EnvironmentOperation")
        if operation.spec.name != name or operation.spec.version != declaration.version:
            raise Conflict(f"environment operation {name!r} does not match its advertised identity")
        if not isinstance(operation.endpoint, str) or not operation.endpoint.strip():
            raise Conflict(f"environment operation {name!r} requires an endpoint")
        checked[name] = operation
    return checked


class Operations:
    def __init__(self, store):
        self.store = store

    def prepare(
        self,
        environment,
        access,
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
            row = self.store.environment(db, environment, access, "operation.prepare")
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
                    or prior["participant"] != access.participant
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
                    access.participant,
                    access.generation,
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
                    "participant": access.participant,
                    "request": request,
                    "maximum_cost_micros": maximum_cost_micros,
                },
                (access.participant,),
            )
            return {"id": operation_id, "status": "prepared"}

    def dispatch(self, session, environment, access, lease, operation_id, provider=None):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
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
            if provider is None:
                provider = getattr(session.environment, "operations", {}).get(request["operation"])
                if provider is None:
                    raise Forbidden("environment operation is unavailable")
            if getattr(provider, "endpoint", None) != request["endpoint"]:
                raise Forbidden("provider endpoint does not match frozen intent")
            uses_environment_contract = isinstance(provider, EnvironmentOperation)
            manifest = json.loads(row["manifest"])
            if uses_environment_contract and provider.spec.model_dump(mode="json") not in manifest.get(
                "operations", ()
            ):
                raise Forbidden("environment operation differs from the frozen experiment")
            validate = getattr(provider, "validate", None)
            validated = validate(request, manifest) if callable(validate) else None
            goal_revision = getattr(validated, "goal_revision", None)
            if goal_revision is not None and goal_revision != str(row["revision"]):
                raise Conflict("operation goal revision is stale")
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
            if uses_environment_contract:

                def authority(validated):
                    with self.store.transaction() as db:
                        current = self.store.environment(db, environment, access, "session.write")
                        session._fence(current, lease)
                        actor = json.loads(current["participants"])[op["participant"]]
                        revision = getattr(validated, "goal_revision", None)
                        if (
                            current["status"] != "running"
                            or not actor["active"]
                            or actor["generation"] != op["generation"]
                            or (revision is not None and str(current["revision"]) != revision)
                        ):
                            raise Forbidden("operation dispatch authority expired")

                receipt = provider.execute(
                    f"{environment}:{operation_id}", request, op["reservation"], authority=authority
                )
            else:
                receipt = provider.execute(f"{environment}:{operation_id}", request, op["reservation"])
        except BaseException:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE operations SET status='unknown' WHERE environment=? AND id=? AND status='dispatching'",
                    (environment, operation_id),
                )
            raise
        return self.settle(environment, access, operation_id, receipt)

    def settle(self, environment, access, operation_id, receipt):
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("cost_micros"), int)
            or receipt["cost_micros"] < 0
        ):
            raise Conflict("provider receipt requires integer nonnegative cost")
        if receipt.get("operation_id") != f"{environment}:{operation_id}":
            raise Conflict("receipt operation identity mismatch")
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
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

    def reconcile(self, environment, access, operation_id, provider):
        with self.store.transaction() as db:
            self.store.environment(db, environment, access, "session.write")
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
        return self.settle(environment, access, operation_id, receipt)

    def cancel_prepared(self, environment, access):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
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

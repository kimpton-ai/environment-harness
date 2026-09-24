"""External-operation intents, conservative reservations and receipt reconciliation."""

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import (
    MAX_OPERATION_RECEIPT_BYTES,
    EnvironmentSpecV2,
    ExperimentSpec,
    OperationPlan,
    OperationSpec,
    Principal,
)
from .errors import BudgetExceeded, Conflict, Forbidden, Unsupported
from .store import digest, encode

HOST_ACTOR = "@environment"


@dataclass(frozen=True)
class ControlIntervalOperationContext:
    """Trusted transition identity supplied to an interval-aware provider."""

    operation_id: str
    journal_operation_id: str
    revision: int
    transition_input_hash: str | None
    participant: str | None
    participants: tuple[str, ...] = ()
    max_checkpoint_bytes: int = 67108864


@dataclass(frozen=True)
class ControlIntervalIntent:
    """Provider description of a bounded execution interval, before it is dispatched."""

    runtime_identity: Mapping[str, Any]
    start_checkpoint: bytes | Mapping[str, str]
    simulated_seconds: float
    controller: str


@dataclass(frozen=True)
class ControlIntervalExecutionContext:
    """Harness-issued grant and durable input acknowledgement passed to execution."""

    interval_id: str
    grant_id: str
    controller: str
    participant: str | None
    generation: int
    start_checkpoint: Mapping[str, str]
    start_checkpoint_bytes: bytes
    runtime_identity: Mapping[str, Any]
    simulated_seconds: float
    accepted_input: Mapping[str, Any]
    acknowledgement: Mapping[str, Any]
    control_log_digest: str


@dataclass(frozen=True)
class ControlIntervalResult:
    """Checkpoint and factual measurements extracted from a provider receipt."""

    end_checkpoint: bytes | Mapping[str, str]
    measurements: Mapping[str, Any]


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

    def control_interval_intent(
        self,
        operation_id: str,
        request: Mapping[str, Any],
        *,
        context: ControlIntervalOperationContext,
    ) -> ControlIntervalIntent | None:
        """Opt in to Harness checkpoint and controller interval journaling."""
        return None

    def execute_control_interval(
        self,
        operation_id: str,
        request: Mapping[str, Any],
        maximum_cost_micros: int,
        *,
        authority: Callable[[Any], None],
        control_interval: ControlIntervalExecutionContext,
    ) -> Mapping[str, Any]:
        """Execute an acknowledged control batch. Called only after interval opt-in."""
        raise NotImplementedError("interval-aware operations must implement execute_control_interval")

    def control_interval_result(
        self, receipt: Mapping[str, Any], *, context: ControlIntervalOperationContext
    ) -> ControlIntervalResult:
        """Extract the completed checkpoint and measurements from a durable receipt."""
        raise NotImplementedError("interval-aware operations must implement control_interval_result")


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

    def prepare_environment_plan(
        self,
        session,
        environment,
        who,
        lease,
        *,
        plan: OperationPlan,
        operation_ids: Mapping[str, str],
        transition_revision: int,
        transition_hash: str,
    ):
        """Journal every operation in a persisted v2 plan atomically before dispatch."""
        if set(operation_ids) != {operation.key for operation in plan.operations}:
            raise Conflict("operation plan identity mapping is incomplete")
        runtime = environment_operations(session.environment)
        prepared = []
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("worker", "researcher"))
            session._fence(row, lease)
            if row["status"] != "running" or row["revision"] != transition_revision:
                raise Conflict("transition inputs changed before operation preparation")
            manifest = json.loads(row["manifest"])
            experiment = ExperimentSpec.model_validate(manifest)
            environment_spec = experiment.environment
            if not isinstance(environment_spec, EnvironmentSpecV2):
                raise Conflict("environment operation plans require environment-session.v2")
            transition = db.execute(
                "SELECT request,status,lease_epoch FROM transitions "
                "WHERE environment=? AND revision=? AND input_hash=?",
                (environment, transition_revision, transition_hash),
            ).fetchone()
            if (
                not transition
                or transition["status"] not in ("planned", "computed")
                or transition["lease_epoch"] != lease["epoch"]
            ):
                raise Conflict("transition plan is not durably prepared")
            persisted = json.loads(transition["request"])
            if persisted.get("input_hash") != transition_hash or persisted.get("plan") != plan.model_dump(
                mode="json"
            ):
                raise Conflict("operation plan differs from its durable transition intent")
            current = {key: row[key] for key in ("state", "rng", "scheduler", "participants")}
            if any(persisted.get("input", {}).get(key) != value for key, value in current.items()):
                raise Conflict("transition inputs changed before operation preparation")
            session._assert_v2_action_records(
                db,
                environment,
                transition_revision,
                persisted["input"].get("action_records", []),
            )

            # `environment.operations` is the supplier's full catalog. Only the
            # top-level experiment selection grants this session operation use.
            selected = {item["name"]: item for item in manifest["operations"]}
            requests = []
            additional_reservation = 0
            for item in plan.operations:
                declaration = selected.get(item.operation)
                provider = runtime.get(item.operation)
                if (
                    declaration is None
                    or provider is None
                    or declaration.get("version") != item.version
                    or provider.spec.model_dump(mode="json") != declaration
                    or getattr(provider, "endpoint", None) not in experiment.policy.allowed_endpoints
                    or item.operation not in experiment.policy.allowed_operations
                ):
                    raise Forbidden("operation is outside the frozen environment contract")
                write = declaration.get("access") == "write"
                if declaration.get("access") not in ("read", "write"):
                    raise Forbidden("v2 operation access class is missing")
                if write and (
                    not experiment.policy.external_writes or not environment_spec.capabilities.external_writes
                ):
                    raise Forbidden("frozen policy denies environment operation writes")
                dependency_ids = {key: operation_ids[key] for key in item.depends_on}
                request = {
                    "endpoint": provider.endpoint,
                    "operation": item.operation,
                    "version": item.version,
                    "payload": item.payload,
                    "write": write,
                    "dependency_ids": dependency_ids,
                    "transition_revision": transition_revision,
                    "transition_hash": transition_hash,
                    "plan_id": plan.plan_id,
                    "request_key": item.key,
                }
                prior = db.execute(
                    "SELECT * FROM operations WHERE environment=? AND id=?",
                    (environment, operation_ids[item.key]),
                ).fetchone()
                if prior:
                    if (
                        prior["request"] != encode(request)
                        or prior["participant"] != HOST_ACTOR
                        or prior["generation"] != transition_revision
                        or prior["reservation"] != item.max_cost_micros
                    ):
                        raise Conflict("environment operation identifier reused")
                else:
                    additional_reservation += item.max_cost_micros
                requests.append((item, request, prior))
            policy = manifest["policy"]
            if row["spent"] + row["reserved"] + additional_reservation > policy["max_cost_micros"]:
                raise BudgetExceeded("operation plan exceeds the remaining session budget")

            for item, request, prior in requests:
                operation_id = operation_ids[item.key]
                if prior:
                    prepared.append({"key": item.key, "id": operation_id, "status": prior["status"]})
                    continue
                db.execute(
                    "INSERT INTO operations (environment,id,participant,generation,request,status,receipt,reservation) "
                    "VALUES (?,?,?, ?,?,'prepared',NULL,?)",
                    (
                        environment,
                        operation_id,
                        HOST_ACTOR,
                        transition_revision,
                        encode(request),
                        item.max_cost_micros,
                    ),
                )
                self.store.append(
                    db,
                    environment,
                    transition_revision,
                    "operation.intent",
                    {
                        "id": operation_id,
                        "operation": item.operation,
                        "version": item.version,
                        "request_key": item.key,
                        "plan_id": plan.plan_id,
                        "transition_input_hash": transition_hash,
                        "request_hash": digest(item.payload),
                        "maximum_cost_micros": item.max_cost_micros,
                        "authority": "environment-session.v2",
                    },
                )
                prepared.append({"key": item.key, "id": operation_id, "status": "prepared"})
            if additional_reservation:
                db.execute(
                    "UPDATE environments SET reserved=reserved+? WHERE id=?",
                    (additional_reservation, environment),
                )
        return prepared

    def dispatch(self, session, environment, who, lease, operation_id, provider=None):
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
            request = json.loads(op["request"])
            host_operation = op["participant"] == HOST_ACTOR
            participants = json.loads(row["participants"])
            participant = participants.get(op["participant"])
            if host_operation:
                self._check_host_transition(db, environment, row, request, op)
            elif participant is None or (
                not participant["active"] or participant["generation"] != op["generation"]
            ):
                raise Forbidden("dispatch authority expired")
            if row["status"] != "running":
                raise Forbidden("dispatch authority expired")
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
            if host_operation:
                declaration = next(
                    (
                        item
                        for item in manifest.get("operations", ())
                        if item.get("name") == request["operation"]
                    ),
                    None,
                )
                if declaration is None or request.get("write") != (declaration.get("access") == "write"):
                    raise Forbidden("environment operation access differs from the frozen contract")
            dependency_receipts = {}
            for key, dependency_id in request.get("dependency_ids", {}).items():
                dependency = db.execute(
                    "SELECT status,receipt FROM operations WHERE environment=? AND id=?",
                    (environment, dependency_id),
                ).fetchone()
                if dependency is None or dependency["status"] != "succeeded" or not dependency["receipt"]:
                    raise Conflict("operation dependency has not settled")
                dependency_receipts[key] = json.loads(dependency["receipt"])
            provider_request = (
                {**request, "dependency_receipts": dependency_receipts}
                if request.get("dependency_ids")
                else request
            )
            validate = getattr(provider, "validate", None)
            validated = validate(provider_request, manifest) if callable(validate) else None
            goal_revision = getattr(validated, "goal_revision", None)
            if goal_revision is not None and goal_revision != str(row["revision"]):
                raise Conflict("operation goal revision is stale")
            participants_for_interval = (op["participant"],) if not host_operation else ()
            if host_operation:
                transition = db.execute(
                    "SELECT request FROM transitions WHERE environment=? AND revision=? AND input_hash=?",
                    (environment, request.get("transition_revision"), request.get("transition_hash")),
                ).fetchone()
                if transition:
                    transition_request = json.loads(transition["request"])
                    participants_for_interval = tuple(
                        sorted(
                            {
                                record["participant"]
                                for record in transition_request.get("input", {}).get("action_records", [])
                            }
                        )
                    )
            operation_context = ControlIntervalOperationContext(
                operation_id=f"{environment}:{operation_id}",
                journal_operation_id=operation_id,
                revision=request.get("transition_revision", row["revision"]),
                transition_input_hash=request.get("transition_hash"),
                participant=participants_for_interval[0] if len(participants_for_interval) == 1 else None,
                participants=participants_for_interval,
                max_checkpoint_bytes=manifest["policy"].get("max_checkpoint_bytes", 67108864),
            )
        interval_intent = None
        execution_context = None
        interval_capable = uses_environment_contract and callable(
            getattr(provider, "control_interval_intent", None)
        )
        if interval_capable:
            interval_intent = provider.control_interval_intent(
                f"{environment}:{operation_id}", provider_request, context=operation_context
            )
        if interval_intent is not None:
            if not isinstance(interval_intent, ControlIntervalIntent):
                raise Conflict("provider control interval intent has an invalid type")
            if not callable(getattr(provider, "execute_control_interval", None)) or not callable(
                getattr(provider, "control_interval_result", None)
            ):
                raise Conflict("interval-aware operation is missing execution or receipt extraction")
            if len(operation_context.participants) > 1:
                raise Conflict("control interval requires one unambiguous participant")
            execution_context = self._prepare_control_execution(
                session,
                environment,
                who,
                lease,
                operation_id,
                request,
                interval_intent,
                operation_context,
                provider.spec.version,
            )
        self._mark_dispatching(
            session,
            environment,
            who,
            lease,
            operation_id,
            request,
            op,
            host_operation,
            operation_context,
        )

        # No transaction during IO. Never auto-repeat a dispatch with an uncertain outcome.
        try:
            if interval_intent is not None:
                operation_authority = self._operation_authority(
                    session, environment, who, lease, operation_id, request, op, validated
                )

                def interval_authority(_validated=None):
                    operation_authority(_validated)
                    session.validate_control_grant(
                        environment,
                        who,
                        lease,
                        interval_id=execution_context.interval_id,
                        grant_id=execution_context.grant_id,
                        controller=execution_context.controller,
                        participant=execution_context.participant,
                        generation=execution_context.generation,
                        acknowledgement=execution_context.acknowledgement,
                    )

                receipt = provider.execute_control_interval(
                    f"{environment}:{operation_id}",
                    provider_request,
                    op["reservation"],
                    authority=interval_authority,
                    control_interval=execution_context,
                )
                self._seal_control_result(
                    session,
                    environment,
                    who,
                    lease,
                    provider,
                    receipt,
                    operation_context,
                    execution_context.interval_id,
                )
            elif uses_environment_contract:
                receipt = provider.execute(
                    f"{environment}:{operation_id}",
                    provider_request,
                    op["reservation"],
                    authority=self._operation_authority(
                        session, environment, who, lease, operation_id, request, op, validated
                    ),
                )
            else:
                receipt = provider.execute(
                    f"{environment}:{operation_id}", provider_request, op["reservation"]
                )
        except BaseException:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE operations SET status='unknown' WHERE environment=? AND id=? AND status='dispatching'",
                    (environment, operation_id),
                )
            raise
        settled = self.settle(environment, who, operation_id, receipt)
        commit_interval = getattr(session, "commit_control_interval_for_operation", None)
        if callable(commit_interval):
            commit_interval(environment, who, lease, operation_id, settled)
        return settled

    def _mark_dispatching(
        self, session, environment, who, lease, operation_id, request, original, host_operation, context
    ):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("worker", "researcher"))
            session._fence(row, lease)
            current = db.execute(
                "SELECT status,request,participant,generation FROM operations WHERE environment=? AND id=?",
                (environment, operation_id),
            ).fetchone()
            if not current or current["status"] != "prepared" or current["request"] != encode(request):
                raise Conflict("operation is no longer an unsent prepared intent")
            if host_operation:
                self._check_host_transition(db, environment, row, request, original)
            else:
                actor = json.loads(row["participants"]).get(current["participant"])
                if not actor or not actor["active"] or actor["generation"] != current["generation"]:
                    raise Forbidden("dispatch authority expired")
            if row["status"] != "running" or context.revision != request.get(
                "transition_revision", row["revision"]
            ):
                raise Forbidden("operation dispatch authority expired")
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
                () if host_operation else (current["participant"],),
            )

    def _operation_authority(
        self, session, environment, who, lease, operation_id, request, operation, validated
    ):
        def authority(_validated=None):
            with self.store.transaction() as db:
                current = self.store.environment(db, environment, who, ("worker", "researcher"))
                session._fence(current, lease)
                current_participants = json.loads(current["participants"])
                actor = current_participants.get(operation["participant"])
                revision = getattr(validated, "goal_revision", None)
                if operation["participant"] == HOST_ACTOR:
                    self._check_host_transition(db, environment, current, request, operation)
                    actor_valid = True
                else:
                    actor_valid = bool(
                        actor and actor["active"] and actor["generation"] == operation["generation"]
                    )
                if (
                    current["status"] != "running"
                    or not actor_valid
                    or (revision is not None and str(current["revision"]) != revision)
                ):
                    raise Forbidden("operation dispatch authority expired")

        return authority

    def _prepare_control_execution(
        self, session, environment, who, lease, operation_id, request, intent, context, provider_version
    ):
        if not isinstance(intent.runtime_identity, Mapping) or not isinstance(intent.controller, str):
            raise Conflict("provider control interval intent is malformed")
        if not isinstance(request.get("payload"), Mapping):
            raise Conflict("control interval operation payload must be an object")
        interval_id = "ci_" + digest({"environment": environment, "operation_id": operation_id})
        batch_id = "cb_" + digest({"environment": environment, "operation_id": operation_id})
        existing = session.control_interval_for_operation(environment, who, operation_id)
        start_checkpoint = intent.start_checkpoint
        if existing:
            if existing["interval_id"] != interval_id:
                raise Conflict("control interval operation identity differs from its journal")
            if isinstance(start_checkpoint, bytes):
                if hashlib.sha256(start_checkpoint).hexdigest() != existing["start_checkpoint"]["sha256"]:
                    raise Conflict("provider start checkpoint differs from the prepared interval")
            elif dict(start_checkpoint) != existing["start_checkpoint"]:
                raise Conflict("provider start checkpoint differs from the prepared interval")
            start_reference = existing["start_checkpoint"]
        else:
            start_reference = self._publish_checkpoint(session, environment, who, start_checkpoint)
        interval = session.prepare_control_interval(
            environment,
            who,
            lease,
            interval_id=interval_id,
            operation_id=operation_id,
            runtime_identity=intent.runtime_identity,
            start_checkpoint=start_reference,
            simulated_seconds=intent.simulated_seconds,
        )
        grant = session.grant_control(
            environment,
            who,
            lease,
            interval_id=interval_id,
            grant_id=interval["grant_id"],
            controller=intent.controller,
            participant=context.participant,
            ttl=30,
        )
        if context.participant:
            controller = Principal(
                tenant=who.tenant,
                subject=intent.controller,
                role="agent",
                environment=environment,
                participant=context.participant,
                generation=grant["generation"],
            )
        else:
            controller = Principal(
                tenant=who.tenant,
                subject=intent.controller,
                role="worker",
                environment=environment,
            )
        accepted_input = {
            "operation": request["operation"],
            "version": provider_version,
            "payload": request["payload"],
        }
        acknowledgement = session.accept_control_inputs(
            environment,
            controller,
            interval_id=interval_id,
            grant_id=grant["grant_id"],
            batch_id=batch_id,
            sequence=1,
            inputs=accepted_input,
        )
        return ControlIntervalExecutionContext(
            interval_id=interval_id,
            grant_id=grant["grant_id"],
            controller=intent.controller,
            participant=context.participant,
            generation=grant["generation"],
            start_checkpoint=interval["start_checkpoint"],
            start_checkpoint_bytes=self._read_checkpoint_bytes(
                session, environment, who, interval["start_checkpoint"]
            ),
            runtime_identity=interval["runtime_identity"],
            simulated_seconds=interval["simulated_seconds"],
            accepted_input=accepted_input,
            acknowledgement=acknowledgement,
            control_log_digest=session.control_input_digest(environment, who, interval_id),
        )

    @staticmethod
    def _publish_checkpoint(session, environment, who, checkpoint):
        if isinstance(checkpoint, bytes):
            artifact = session.store.checkpoint_artifact(
                environment,
                who,
                checkpoint,
                audience=(),
                media_type="application/vnd.environment-harness.checkpoint",
            )
            return {"artifact_id": artifact["id"], "sha256": artifact["sha256"]}
        if isinstance(checkpoint, Mapping):
            return {"artifact_id": checkpoint.get("artifact_id"), "sha256": checkpoint.get("sha256")}
        raise Conflict("provider checkpoint must be bytes or a Harness artifact reference")

    @staticmethod
    def _read_checkpoint_bytes(session, environment, who, reference):
        content, _media_type = session.store.read_artifact(environment, who, reference["artifact_id"])
        if len(content) > session._checkpoint_limit(environment, who):
            raise Conflict("checkpoint artifact exceeds the frozen checkpoint limit")
        if hashlib.sha256(content).hexdigest() != reference["sha256"]:
            raise Conflict("checkpoint artifact integrity failure")
        return content

    def _seal_control_result(self, session, environment, who, lease, provider, receipt, context, interval_id):
        control_log_digest = session.control_input_digest(environment, who, interval_id)
        if receipt.get("control_log_digest") != control_log_digest:
            raise Conflict("provider receipt control log digest differs from the accepted input journal")
        result = provider.control_interval_result(receipt, context=context)
        if not isinstance(result, ControlIntervalResult) or not isinstance(result.measurements, Mapping):
            raise Conflict("provider control interval receipt extraction is malformed")
        existing = session.control_interval_for_operation(environment, who, context.journal_operation_id)
        if existing and existing["status"] in ("sealed", "committed"):
            checkpoint = result.end_checkpoint
            if isinstance(checkpoint, bytes):
                if hashlib.sha256(checkpoint).hexdigest() != existing["end_checkpoint"]["sha256"]:
                    raise Conflict("provider end checkpoint differs from its sealed interval")
            elif dict(checkpoint) != existing["end_checkpoint"]:
                raise Conflict("provider end checkpoint differs from its sealed interval")
            end_reference = existing["end_checkpoint"]
            if existing["measurements"] != dict(result.measurements):
                raise Conflict("provider measurements differ from its sealed interval")
        else:
            end_reference = self._publish_checkpoint(session, environment, who, result.end_checkpoint)
        session.seal_control_interval(
            environment,
            who,
            lease,
            interval_id=interval_id,
            end_checkpoint=end_reference,
            measurements=dict(result.measurements),
            control_log_digest=control_log_digest,
        )

    def settle(self, environment, who, operation_id, receipt):
        if (
            not isinstance(receipt, dict)
            or type(receipt.get("cost_micros")) is not int
            or receipt["cost_micros"] < 0
        ):
            raise Conflict("provider receipt requires integer nonnegative cost")
        if receipt.get("operation_id") != f"{environment}:{operation_id}":
            raise Conflict("receipt operation identity mismatch")
        receipt_json = encode(receipt)
        if len(receipt_json.encode("utf-8")) > MAX_OPERATION_RECEIPT_BYTES:
            raise Conflict("provider receipt exceeds 128 KiB")
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
                (receipt_json, environment, operation_id),
            )
            db.execute(
                "UPDATE environments SET reserved=reserved-?,spent=spent+? WHERE id=?",
                (op["reservation"], receipt["cost_micros"], environment),
            )
            event_payload = (
                {
                    "id": operation_id,
                    "receipt_hash": digest(receipt),
                    "cost_micros": receipt["cost_micros"],
                }
                if op["participant"] == HOST_ACTOR
                else {"id": operation_id, "receipt": receipt}
            )
            audience = () if op["participant"] == HOST_ACTOR else (op["participant"],)
            self.store.append(db, environment, row["revision"], "operation.receipt", event_payload, audience)
            return receipt

    def reconcile(self, environment, who, operation_id, provider, *, session=None, lease=None):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, who, ("worker", "researcher"))
            op = db.execute(
                "SELECT * FROM operations WHERE environment=? AND id=?", (environment, operation_id)
            ).fetchone()
            if not op:
                raise Forbidden("operation unavailable")
            request = json.loads(op["request"])
            if getattr(provider, "endpoint", None) != request["endpoint"]:
                raise Forbidden("provider endpoint mismatch")
            if op["participant"] == HOST_ACTOR and (
                not isinstance(provider, EnvironmentOperation)
                or provider.spec.model_dump(mode="json")
                not in json.loads(row["manifest"]).get("operations", ())
            ):
                raise Forbidden("environment operation differs from the frozen experiment")
            if op["participant"] == HOST_ACTOR:
                declaration = next(
                    (
                        item
                        for item in json.loads(row["manifest"]).get("operations", ())
                        if item.get("name") == request["operation"]
                    ),
                    None,
                )
                if declaration is None or request.get("write") != (declaration.get("access") == "write"):
                    raise Forbidden("environment operation access differs from the frozen contract")
            if op["status"] == "succeeded":
                return json.loads(op["receipt"])
            if op["status"] not in ("unknown", "dispatching"):
                raise Conflict("only dispatched operations require reconciliation")
            interval = db.execute(
                "SELECT id FROM control_intervals WHERE environment=? AND operation_id=?",
                (environment, operation_id),
            ).fetchone()
            context = None
            if interval:
                if session is None or lease is None:
                    raise Conflict("control interval recovery requires the active Harness session lease")
                if op["participant"] == HOST_ACTOR:
                    transition = db.execute(
                        "SELECT request FROM transitions WHERE environment=? AND revision=? AND input_hash=?",
                        (environment, request.get("transition_revision"), request.get("transition_hash")),
                    ).fetchone()
                    records = (
                        json.loads(transition["request"]).get("input", {}).get("action_records", [])
                        if transition
                        else []
                    )
                    participants = tuple(sorted({item["participant"] for item in records}))
                else:
                    participants = (op["participant"],)
                context = ControlIntervalOperationContext(
                    operation_id=f"{environment}:{operation_id}",
                    journal_operation_id=operation_id,
                    revision=request.get("transition_revision", row["revision"]),
                    transition_input_hash=request.get("transition_hash"),
                    participant=participants[0] if len(participants) == 1 else None,
                    participants=participants,
                    max_checkpoint_bytes=json.loads(row["manifest"])["policy"].get(
                        "max_checkpoint_bytes", 67108864
                    ),
                )
        receipt = provider.lookup(f"{environment}:{operation_id}")
        if receipt is None:
            raise Unsupported("provider cannot prove outcome; dispatch stays blocked")
        if interval:
            self._seal_control_result(
                session,
                environment,
                who,
                lease,
                provider,
                receipt,
                context,
                session.control_interval_for_operation(environment, who, operation_id)["interval_id"],
            )
        settled = self.settle(environment, who, operation_id, receipt)
        if interval:
            session.commit_control_interval_for_operation(environment, who, lease, operation_id, settled)
        return settled

    @staticmethod
    def _check_host_transition(db, environment, row, request, operation):
        if (
            row["revision"] != operation["generation"]
            or row["revision"] != request.get("transition_revision")
            or row["status"] != "running"
        ):
            raise Forbidden("environment operation generation expired")
        transition = db.execute(
            "SELECT request,status,lease_epoch FROM transitions WHERE environment=? AND revision=? AND input_hash=?",
            (environment, request.get("transition_revision"), request.get("transition_hash")),
        ).fetchone()
        if (
            transition is None
            or transition["status"] not in ("planned", "computed")
            or transition["lease_epoch"] != row["lease_epoch"]
        ):
            raise Forbidden("environment operation transition is unavailable")
        persisted = json.loads(transition["request"])
        if persisted.get("input_hash") != request.get("transition_hash"):
            raise Forbidden("environment operation transition identity changed")
        request_key = request.get("request_key")
        plan = persisted.get("plan", {})
        planned = next(
            (item for item in plan.get("operations", ()) if item.get("key") == request_key),
            None,
        )
        if (
            planned is None
            or persisted.get("operation_ids", {}).get(request_key) != operation["id"]
            or plan.get("plan_id") != request.get("plan_id")
            or planned.get("operation") != request.get("operation")
            or planned.get("version") != request.get("version")
            or planned.get("payload") != request.get("payload")
            or request.get("dependency_ids")
            != {key: persisted["operation_ids"][key] for key in planned.get("depends_on", ())}
        ):
            raise Forbidden("environment operation differs from the durable plan")
        base = persisted.get("input", {})
        if any(row[key] != base.get(key) for key in ("state", "rng", "scheduler", "participants")):
            raise Forbidden("environment operation input generation changed")
        accepted = db.execute(
            "SELECT id,participant,revision,request FROM actions "
            "WHERE environment=? AND revision=? AND status='accepted' ORDER BY participant",
            (environment, request.get("transition_revision")),
        ).fetchall()
        action_records = [
            {
                "id": action["id"],
                "participant": action["participant"],
                "revision": action["revision"],
                "request_hash": digest(json.loads(action["request"])),
            }
            for action in accepted
        ]
        if action_records != base.get("action_records", []):
            raise Forbidden("environment operation action generation changed")

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
                () if op["participant"] == HOST_ACTOR else (op["participant"],),
            )
        return {"cancelled": len(ops)}

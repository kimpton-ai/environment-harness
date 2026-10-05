"""Optional PostgreSQL metadata and S3-compatible artifacts.

Initialize only in a dedicated harness schema. Platform migrations remain owned
by their platform. Constructing the store does not change a database schema.
"""

import hashlib
import json
import re
import time
from contextlib import contextmanager
from typing import cast

from .contracts import HostedArtifactBudget
from .errors import Conflict
from .store import EvidenceStore, digest, encode, uid

ARTIFACT_ATTEMPT_LEASE_SECONDS = 65
PURGE_PAGE_OBJECTS = 1000
PURGE_LIST_RESPONSE_MAX_BYTES = 1_048_576
PURGE_DELETE_RESPONSE_MAX_BYTES = 1_048_576
PURGE_CONTROL_RESPONSE_CHARGE_BYTES = PURGE_LIST_RESPONSE_MAX_BYTES + 4_096
MAX_ARTIFACT_READ_OVERREAD_BYTES = 65_536


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        row = self.cursor.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self):
        return [Row(row) for row in self.cursor.fetchall()]

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def __iter__(self):
        return (Row(row) for row in self.cursor)


class Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        # DB-API parameter markers only. SQL semantics live in explicit storage operations.
        return Cursor(self.connection.execute(sql.replace("?", "%s"), params))


class PostgresEvidenceStore(EvidenceStore):
    def __init__(
        self,
        dsn,
        object_store,
        *,
        schema="environment_harness",
        max_retained_bytes=None,
        require_hosted_artifact_budget=False,
    ):
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("invalid harness schema")
        if max_retained_bytes is not None and (
            not isinstance(max_retained_bytes, int) or max_retained_bytes < 1
        ):
            raise ValueError("positive retained storage allowance required")
        if type(require_hosted_artifact_budget) is not bool:
            raise ValueError("hosted artifact budget mode must be an explicit boolean")
        self.dsn, self.object_store, self.schema = dsn, object_store, schema
        self.max_retained_bytes = max_retained_bytes
        self.require_hosted_artifact_budget = require_hosted_artifact_budget
        # Numbered SQL migrations own PostgreSQL schema state; see initialize().

    def connect(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import DictRow, dict_row

        connection = cast(
            "psycopg.Connection[DictRow]",
            psycopg.connect(self.dsn, row_factory=dict_row),  # pyright: ignore[reportArgumentType]
        )
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        return connection

    @contextmanager
    def transaction(self):
        with self.connect() as connection:
            yield Connection(connection)

    def _environment_row(self, db, environment):
        return db.execute("SELECT * FROM environments WHERE id=? FOR UPDATE", (environment,)).fetchone()

    def environment(self, db, environment: str, access, action="session.read"):
        row = super().environment(db, environment, access, action)
        if row["status"] == "branch_copy_pending":
            raise Conflict("hosted branch copy is not yet exposed")
        budgeted = db.execute(
            "SELECT 1 FROM hosted_artifact_budgets WHERE environment=?", (environment,)
        ).fetchone()
        host_create_retry = (
            action == "session.create"
            and row["revision"] == 0
            and row["lease_owner"] is None
        )
        if budgeted is None and self._budget_required(db, environment) and not host_create_retry:
            raise Conflict("hosted session is missing its immutable artifact budget")
        if budgeted and db.execute(
            "SELECT purge_started FROM artifact_budget_usage WHERE environment=?", (environment,)
        ).fetchone()["purge_started"]:
            raise Conflict("hosted session is fenced for erasure")
        return row

    def _budget_required(self, db, environment):
        if self.require_hosted_artifact_budget:
            return True
        return db.execute(
            "SELECT 1 FROM hosted_artifact_requirements WHERE environment=?",
            (environment,),
        ).fetchone() is not None

    def require_hosted_artifact_budget_for(self, environment):
        """Durably mark one host-chosen ID strict before its native initialization.

        This keeps old unmarked sessions on their historical behavior while a
        new hosted admission cannot use the compatibility fallback if budget
        installation is interrupted.
        """
        if not isinstance(environment, str) or not re.fullmatch(r"[a-f0-9]{32}", environment):
            raise ValueError("valid stable hosted environment ID required")
        with self.transaction() as db:
            row = self._environment_row(db, environment)
            if row is not None:
                if self._hosted_artifact_budget(db, row) is not None:
                    return
                if row["lease_owner"] is not None or row["revision"] != 0:
                    raise Conflict("hosted artifact requirement must precede session activity")
            db.execute(
                "INSERT INTO hosted_artifact_requirements(environment,created) VALUES (?,?) "
                "ON CONFLICT(environment) DO NOTHING",
                (environment, time.time()),
            )

    def _event_page(self, db, environment, after, access, limit):
        return db.execute(
            """SELECT * FROM events WHERE environment=? AND seq>? AND
            (? = 1 OR audience='["*"]' OR
             EXISTS(SELECT 1 FROM jsonb_array_elements_text(audience::jsonb)
                    AS membership(value) WHERE value=?)) ORDER BY seq LIMIT ?""",
            (environment, after, int(access.full_evidence), access.participant, limit),
        ).fetchall()

    def initialize(self):
        import hashlib
        from importlib.resources import files

        from psycopg import sql as postgres_sql

        migrations = files("environment_harness").joinpath("migrations")
        with self.connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema,))
            connection.execute(
                postgres_sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    postgres_sql.Identifier(self.schema)
                )
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, sha256 TEXT NOT NULL)"
            )
            for migration in sorted(migrations.iterdir(), key=lambda path: path.name):
                if not migration.name.endswith(".sql"):
                    continue
                sql = migration.read_text()
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                applied = connection.execute(
                    "SELECT sha256 FROM schema_migrations WHERE version=%s", (migration.name,)
                ).fetchone()
                if applied:
                    if applied["sha256"] != checksum:
                        raise ValueError("applied harness migration checksum changed")
                    continue
                connection.execute(sql.encode())
                connection.execute("INSERT INTO schema_migrations VALUES (%s,%s)", (migration.name, checksum))

    def retained_bytes(self, db, environment):
        total = 0
        fields = {
            "environments": ("manifest", "state", "scheduler", "rng", "participants", "cursors", "lineage"),
            "events": ("body", "audience"),
            "observations": ("body",),
            "actions": ("request", "receipt"),
            "checkpoints": ("body",),
            "operations": ("request", "receipt"),
            "reports": ("body",),
            "transitions": ("request", "result", "rng"),
            "agent_work": ("observation", "response", "agent_state"),
            "session_runs": ("scenario", "error", "latest_activity", "scenario_body"),
        }
        for table, columns in fields.items():
            expression = "+".join(f"octet_length(coalesce({column},''))" for column in columns)
            key = "id" if table == "environments" else "environment"
            total += db.execute(
                f"SELECT coalesce(sum({expression}),0) FROM {table} WHERE {key}=?", (environment,)
            ).fetchone()[0]
        total += db.execute(
            "SELECT coalesce(sum(size),0) FROM artifacts WHERE environment=?", (environment,)
        ).fetchone()[0]
        total += db.execute(
            "SELECT coalesce(sum(octet_length(topic)+octet_length(kind)+octet_length(body)),0) "
            "FROM event_outbox WHERE environment=?",
            (environment,),
        ).fetchone()[0]
        return total

    def _check_artifact_budget(self, db, environment, size):
        if (
            self.max_retained_bytes is not None
            and self.retained_bytes(db, environment) + size > self.max_retained_bytes
        ):
            raise Conflict("retained storage allowance exceeded")

    def append(self, db, environment, revision, kind, payload, audience=(), event_time=None):
        self._check_artifact_budget(db, environment, len(encode(payload).encode()))
        return super().append(db, environment, revision, kind, payload, audience, event_time)

    @staticmethod
    def _hosted_artifact_budget(db, row):
        stored = db.execute(
            "SELECT manifest_sha256,budget FROM hosted_artifact_budgets WHERE environment=?",
            (row["id"],),
        ).fetchone()
        if stored is None:
            return None
        manifest_sha = hashlib.sha256(row["manifest"].encode()).hexdigest()
        if stored["manifest_sha256"] != manifest_sha:
            raise Conflict("hosted artifact budget is bound to another immutable session")
        return HostedArtifactBudget.model_validate_json(stored["budget"])

    def install_hosted_artifact_budget(
        self,
        environment,
        budget: HostedArtifactBudget,
        *,
        expected_manifest_sha256: str,
        authorized_expires_at: int,
        authorized_retention_deadline: int,
        authorized_cleanup_deadline: int,
    ):
        """Install the trusted host envelope before exposing a newly created session.

        The host supplies deadline caps read from its persisted admission record. This
        method does not accept customer credentials or change the frozen ExperimentSpec.
        """
        if not isinstance(budget, HostedArtifactBudget):
            raise TypeError("a validated hosted artifact budget is required")
        if not re.fullmatch(r"[a-f0-9]{64}", expected_manifest_sha256):
            raise ValueError("exact manifest digest required")
        if any(
            type(deadline) is not int
            for deadline in (authorized_expires_at, authorized_retention_deadline, authorized_cleanup_deadline)
        ):
            raise ValueError("authoritative integer deadlines required")
        with self.transaction() as db:
            row = self._environment_row(db, environment)
            if row is None:
                raise Conflict("hosted artifact budget target does not exist")
            manifest_sha = hashlib.sha256(row["manifest"].encode()).hexdigest()
            if manifest_sha != expected_manifest_sha256:
                raise Conflict("hosted artifact budget manifest identity mismatch")
            manifest = json.loads(row["manifest"])
            run_policy = manifest.get("policy", {})
            if budget.max_object_bytes > min(run_policy.get("max_artifact_bytes", 0), 16_777_216):
                raise Conflict("hosted object allowance exceeds the supported artifact limit")
            put_units = getattr(self.object_store, "put_request_units", None)
            if type(put_units) is not int or put_units < 1:
                raise Conflict("hosted artifact adapter lacks a fixed write-attempt cost")
            put_response_limit = self._control_response_limit("max_put_response_bytes")
            if budget.max_put_attempts < budget.max_lifetime_objects * put_units:
                raise Conflict("hosted PUT allowance cannot cover one write per lifetime object")
            if self.max_retained_bytes is not None and budget.max_live_bytes > self.max_retained_bytes:
                raise Conflict("hosted live allowance exceeds the deployment storage ceiling")
            if budget.max_egress_bytes < budget.max_get_attempts * (
                budget.max_object_bytes + self._get_overread_bound()
            ):
                raise Conflict("hosted egress allowance cannot cover its GET attempts")
            list_response_limit = self._control_response_limit("max_list_response_bytes")
            delete_response_limit = self._control_response_limit("max_delete_response_bytes")
            minimum_control = (
                (budget.max_put_attempts // put_units) * put_response_limit
                + budget.cleanup_reserve.list_attempts * list_response_limit
                + budget.cleanup_reserve.delete_attempts * delete_response_limit
            )
            if budget.max_control_response_bytes < minimum_control:
                raise Conflict("hosted response allowance cannot cover reserved cleanup")
            if budget.expires_at <= time.time():
                raise Conflict("hosted artifact expiry is already past")
            if budget.expires_at > authorized_expires_at:
                raise Conflict("hosted artifact expiry exceeds the persisted session deadline")
            if budget.retention_deadline > authorized_retention_deadline:
                raise Conflict("hosted artifact retention exceeds the persisted cleanup deadline")
            if budget.cleanup_deadline > authorized_cleanup_deadline:
                raise Conflict("hosted artifact cleanup exceeds the persisted erasure deadline")
            existing = db.execute(
                "SELECT manifest_sha256,budget FROM hosted_artifact_budgets WHERE environment=?",
                (environment,),
            ).fetchone()
            frozen = encode(budget.model_dump(mode="json"))
            if existing:
                if existing["manifest_sha256"] == manifest_sha and existing["budget"] == frozen:
                    return {"environment": environment, "manifest_sha256": manifest_sha, "installed": True}
                raise Conflict("hosted artifact budget is immutable")
            branch_copy_pending = row["status"] == "branch_copy_pending"
            if row["lease_owner"] is not None or (row["revision"] != 0 and not branch_copy_pending):
                raise Conflict("hosted artifact budget must be installed before session execution")
            if db.execute("SELECT 1 FROM artifacts WHERE environment=? LIMIT 1", (environment,)).fetchone():
                raise Conflict("hosted artifact budget must precede artifact creation")
            if db.execute(
                "SELECT 1 FROM observations WHERE environment=? UNION ALL "
                "SELECT 1 FROM actions WHERE environment=? LIMIT 1",
                (environment, environment),
            ).fetchone():
                raise Conflict("hosted artifact budget must precede session activity")
            if db.execute(
                "SELECT 1 FROM artifact_operations WHERE environment=? LIMIT 1", (environment,)
            ).fetchone() or db.execute(
                "SELECT 1 FROM artifact_budget_usage WHERE environment=? LIMIT 1", (environment,)
            ).fetchone():
                raise Conflict("hosted artifact ledger exists without its immutable budget")
            db.execute(
                "INSERT INTO hosted_artifact_budgets VALUES (?,?,?,?)",
                (environment, manifest_sha, frozen, time.time()),
            )
            self._ensure_artifact_usage(db, environment)
        return {"environment": environment, "manifest_sha256": manifest_sha, "installed": True}

    @staticmethod
    def _ensure_artifact_usage(db, environment):
        db.execute(
            "INSERT INTO artifact_budget_usage (environment) VALUES (?) ON CONFLICT DO NOTHING",
            (environment,),
        )
        return db.execute(
            "SELECT * FROM artifact_budget_usage WHERE environment=? FOR UPDATE", (environment,)
        ).fetchone()

    @staticmethod
    def _operation_key(environment, operation_id):
        return hashlib.sha256(f"{environment}\0{operation_id}".encode()).hexdigest()

    def _control_response_limit(self, attribute):
        value = getattr(self.object_store, attribute, None)
        if type(value) is not int or not 1 <= value <= PURGE_CONTROL_RESPONSE_CHARGE_BYTES:
            raise Conflict("hosted artifact adapter lacks a fixed control response bound")
        return value

    def _get_overread_bound(self):
        value = getattr(self.object_store, "max_get_overread_bytes", None)
        if type(value) is not int or not 0 <= value <= MAX_ARTIFACT_READ_OVERREAD_BYTES:
            raise Conflict("hosted artifact adapter lacks a fixed read overrun bound")
        return value

    @staticmethod
    def _check_deadline(budget, *, retained_read=False, cleanup=False):
        deadline = (
            budget.cleanup_deadline
            if cleanup
            else budget.retention_deadline
            if retained_read
            else budget.expires_at
        )
        if time.time() >= deadline:
            raise Conflict("hosted artifact operation deadline expired")

    def _budget_attempt(self, usage, budget, kind, *, units=1, response_reserve=0):
        if kind == "put":
            if usage["put_attempts"] + units > budget.max_put_attempts:
                raise Conflict("hosted artifact PUT allowance exhausted")
        elif kind == "get":
            if usage["get_attempts"] >= budget.max_get_attempts:
                raise Conflict("hosted artifact GET allowance exhausted")
            charge = budget.max_object_bytes + self._get_overread_bound()
            if usage["egress_bytes"] + charge > budget.max_egress_bytes:
                raise Conflict("hosted artifact egress allowance exhausted")
        elif kind == "purge_list":
            if usage["cleanup_list_attempts"] >= budget.cleanup_reserve.list_attempts:
                raise Conflict("hosted artifact cleanup list reserve exhausted")
            if usage["list_attempts"] >= budget.max_list_attempts:
                raise Conflict("hosted artifact list allowance exhausted")
        elif kind == "purge_delete":
            if usage["cleanup_delete_attempts"] >= budget.cleanup_reserve.delete_attempts:
                raise Conflict("hosted artifact cleanup delete reserve exhausted")
            if usage["delete_attempts"] >= budget.max_delete_attempts:
                raise Conflict("hosted artifact delete allowance exhausted")
        if response_reserve and (
            usage["control_response_bytes"] + response_reserve > budget.max_control_response_bytes
        ):
            raise Conflict("hosted artifact control response allowance exhausted")

    def _finish_operation(self, environment, operation_id, attempt_number, status, *, response_bytes=0):
        with self.transaction() as db:
            self._environment_row(db, environment)
            result = db.execute(
                "UPDATE artifact_operations SET status=?,response_bytes=?,attempt_until=0,updated=? "
                "WHERE environment=? AND operation_id=? AND attempts=? AND status='in_flight'",
                (status, response_bytes, time.time(), environment, operation_id, attempt_number),
            )
            return result.rowcount == 1

    def artifact(
        self,
        environment,
        access,
        data: bytes,
        audience=(),
        media_type="application/octet-stream",
        *,
        operation_id=None,
        _allow_branch_pending=False,
        _append_event=True,
    ):
        with self.transaction() as db:
            row = (
                super().environment(db, environment, access, "artifact.write")
                if _allow_branch_pending
                else self.environment(db, environment, access, "artifact.write")
            )
            policy = json.loads(row["manifest"]).get("policy", {})
            if len(data) > policy["max_artifact_bytes"]:
                raise Conflict("artifact size limit exceeded")
            participants = json.loads(row["participants"])
            if access.participant is not None:
                audience = (access.participant,)
            elif any(p not in participants and p != "*" for p in audience):
                raise ValueError("unknown artifact audience")
            budget = self._hosted_artifact_budget(db, row)
            if budget is None:
                if self._budget_required(db, environment):
                    raise Conflict("hosted session is missing its immutable artifact budget")
                key = uid()
                self._check_artifact_budget(db, environment, len(data))
                self._write_artifact(environment, key, data)
                sha = hashlib.sha256(data).hexdigest()
                db.execute(
                    "INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
                    (key, environment, encode(audience), sha, len(data), media_type),
                )
                self.append(
                    db, environment, row["revision"], "artifact",
                    {"id": key, "sha256": sha, "size": len(data)}, audience,
                )
                return {"id": key, "sha256": sha, "size": len(data), "media_type": media_type}
            put_once = getattr(self.object_store, "put_once", None)
            units = getattr(self.object_store, "put_request_units", None)
            if not callable(put_once) or type(units) is not int or units < 1:
                raise Conflict("hosted artifact adapter lacks bounded single-attempt writes")
            put_response_limit = self._control_response_limit("max_put_response_bytes")
            if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", operation_id):
                raise Conflict("hosted artifact operation requires a stable idempotency key")
            if len(data) > budget.max_object_bytes:
                raise Conflict("hosted artifact object allowance exceeded")
            sha = hashlib.sha256(data).hexdigest()
            key = self._operation_key(environment, operation_id)
            audience_json = encode(audience)
            event = {"id": key, "sha256": sha, "size": len(data)}
            event_bytes = len(encode(event).encode()) if _append_event else 0
            usage = self._ensure_artifact_usage(db, environment)
            if usage["purge_started"]:
                raise Conflict("hosted artifact writes are fenced for erasure")
            prior = db.execute(
                "SELECT * FROM artifact_operations WHERE environment=? AND operation_id=? FOR UPDATE",
                (environment, operation_id),
            ).fetchone()
            if prior:
                if (prior["kind"], prior["object_key"], prior["sha256"], prior["size"], prior["audience"], prior["media_type"]) != (
                    "put", key, sha, len(data), audience_json, media_type
                ):
                    raise Conflict("hosted artifact idempotency key was reused with different content")
                if prior["status"] == "committed":
                    return {"id": key, "sha256": sha, "size": len(data), "media_type": media_type}
                if prior["status"] == "failed":
                    raise Conflict("hosted artifact operation is terminal")
                if prior["status"] == "in_flight" and prior["attempt_until"] > time.time():
                    raise Conflict("hosted artifact operation is still in progress")
                self._check_deadline(budget)
                attempt_number = prior["attempts"] + 1
            else:
                self._check_deadline(budget)
                self._check_artifact_budget(db, environment, len(data) + event_bytes)
                if usage["live_bytes"] + len(data) > budget.max_live_bytes:
                    raise Conflict("hosted live artifact allowance exhausted")
                if usage["lifetime_uploaded_bytes"] + len(data) > budget.max_lifetime_uploaded_bytes:
                    raise Conflict("hosted lifetime upload allowance exhausted")
                if usage["lifetime_objects"] + 1 > budget.max_lifetime_objects:
                    raise Conflict("hosted lifetime object allowance exhausted")
                attempt_number = 1
            self._budget_attempt(
                usage,
                budget,
                "put",
                units=units,
                response_reserve=put_response_limit,
            )
            now = time.time()
            if prior:
                db.execute(
                    "UPDATE artifact_operations SET status='in_flight',attempts=attempts+1,attempt_until=?,updated=? "
                    "WHERE environment=? AND operation_id=?",
                    (now + ARTIFACT_ATTEMPT_LEASE_SECONDS, now, environment, operation_id),
                )
            else:
                db.execute(
                    "INSERT INTO artifact_operations "
                    "(environment,operation_id,kind,object_key,sha256,size,audience,media_type,status,attempts,attempt_until,created,updated) "
                    "VALUES (?,?,?,?,?,?,?,?,'in_flight',1,?,?,?)",
                    (environment, operation_id, "put", key, sha, len(data), audience_json, media_type,
                     now + ARTIFACT_ATTEMPT_LEASE_SECONDS, now, now),
                )
                db.execute(
                    "UPDATE artifact_budget_usage SET live_bytes=live_bytes+?,"
                    "lifetime_uploaded_bytes=lifetime_uploaded_bytes+?,lifetime_objects=lifetime_objects+1 "
                    "WHERE environment=?",
                    (len(data), len(data), environment),
                )
            db.execute(
                "UPDATE artifact_budget_usage SET put_attempts=put_attempts+?,"
                "control_response_bytes=control_response_bytes+? WHERE environment=?",
                (units, put_response_limit, environment),
            )
        try:
            response_bytes = put_once(f"{environment}/{key}", data)
            if response_bytes is not None and (
                type(response_bytes) is not int or not 0 <= response_bytes <= put_response_limit
            ):
                raise Conflict("hosted artifact write response exceeded its adapter bound")
        except Exception:
            self._finish_operation(environment, operation_id, attempt_number, "unknown")
            raise
        with self.transaction() as db:
            row = self._environment_row(db, environment)
            settled = db.execute(
                "UPDATE artifact_operations SET status='committed',response_bytes=?,attempt_until=0,updated=? "
                "WHERE environment=? AND operation_id=? AND attempts=? AND status='in_flight'",
                (response_bytes or 0, time.time(), environment, operation_id, attempt_number),
            )
            if settled.rowcount != 1:
                raise Conflict("hosted artifact response belongs to a stale attempt")
            saved = db.execute(
                "SELECT * FROM artifacts WHERE environment=? AND id=?", (environment, key)
            ).fetchone()
            if saved:
                if saved["sha256"] != sha or saved["size"] != len(data):
                    raise Conflict("hosted artifact key contains different content")
            else:
                db.execute(
                    "INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
                    (key, environment, audience_json, sha, len(data), media_type),
                )
                if _append_event:
                    self.append(db, environment, row["revision"], "artifact", event, audience)
        return {"id": key, "sha256": sha, "size": len(data), "media_type": media_type}

    def branch_artifact(self, environment, access, data, *, audience, media_type, operation_id):
        """Write one copied artifact into a private, budgeted pending branch."""
        return self.artifact(
            environment,
            access.replace(session=None),
            data,
            audience=audience,
            media_type=media_type,
            operation_id=operation_id,
            _allow_branch_pending=True,
            _append_event=False,
        )

    def read_artifact(self, environment, access, key, *, operation_id=None, _allow_branch_pending=False):
        with self.transaction() as db:
            row = (
                super().environment(db, environment, access, "artifact.read")
                if _allow_branch_pending
                else self.environment(db, environment, access, "artifact.read")
            )
            artifact = db.execute(
                "SELECT * FROM artifacts WHERE environment=? AND id=?", (environment, key)
            ).fetchone()
            if not artifact:
                artifact = db.execute(
                    "SELECT a.* FROM artifacts a JOIN artifact_aliases x ON x.artifact=a.id "
                    "AND x.environment=a.environment WHERE x.environment=? AND x.alias=?",
                    (environment, key),
                ).fetchone()
            if not artifact:
                raise Conflict("artifact unavailable")
            audience = json.loads(artifact["audience"])
            if not access.full_evidence and "*" not in audience and access.participant not in audience:
                raise Conflict("artifact unavailable")
            budget = self._hosted_artifact_budget(db, row)
            if budget is None:
                if self._budget_required(db, environment):
                    raise Conflict("hosted session is missing its immutable artifact budget")
                data = self._read_artifact(environment, artifact["id"])
            else:
                self._check_deadline(budget, retained_read=True)
                bounded_get = getattr(self.object_store, "get_bounded", None)
                if not callable(bounded_get):
                    raise Conflict("hosted artifact adapter lacks bounded reads")
                if artifact["size"] > budget.max_object_bytes:
                    raise Conflict("hosted artifact metadata exceeds the object allowance")
                usage = self._ensure_artifact_usage(db, environment)
                if usage["purge_started"]:
                    raise Conflict("hosted artifact reads are fenced for erasure")
                if operation_id is None:
                    operation_id = "get:" + uid()
                if not isinstance(operation_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9._:-]{1,128}", operation_id
                ):
                    raise Conflict("hosted artifact read requires a stable operation identifier")
                prior = db.execute(
                    "SELECT * FROM artifact_operations WHERE environment=? AND operation_id=? FOR UPDATE",
                    (environment, operation_id),
                ).fetchone()
                if prior is not None:
                    if prior["kind"] != "get" or prior["object_key"] != artifact["id"]:
                        raise Conflict("hosted artifact read operation identifier was reused")
                    if prior["status"] == "failed":
                        raise Conflict("hosted artifact read operation is terminal")
                    if prior["status"] == "in_flight" and prior["attempt_until"] > time.time():
                        raise Conflict("hosted artifact read operation is still in progress")
                    attempt_number = prior["attempts"] + 1
                else:
                    attempt_number = 1
                self._budget_attempt(usage, budget, "get")
                reserve = budget.max_object_bytes + self._get_overread_bound()
                now = time.time()
                if prior is None:
                    db.execute(
                        "INSERT INTO artifact_operations "
                        "(environment,operation_id,kind,object_key,size,status,attempts,attempt_until,created,updated) "
                        "VALUES (?,?, 'get', ?,0,'in_flight',1,?,?,?)",
                        (environment, operation_id, artifact["id"], now + ARTIFACT_ATTEMPT_LEASE_SECONDS, now, now),
                    )
                else:
                    db.execute(
                        "UPDATE artifact_operations SET status='in_flight',attempts=attempts+1,attempt_until=?,updated=? "
                        "WHERE environment=? AND operation_id=?",
                        (now + ARTIFACT_ATTEMPT_LEASE_SECONDS, now, environment, operation_id),
                    )
                db.execute(
                    "UPDATE artifact_budget_usage SET get_attempts=get_attempts+1,egress_bytes=egress_bytes+? "
                    "WHERE environment=?",
                    (reserve, environment),
                )
        if budget is not None:
            try:
                data = bounded_get(
                    f"{environment}/{artifact['id']}", budget.max_object_bytes
                )
                if len(data) > budget.max_object_bytes:
                    raise Conflict("hosted artifact response exceeded its object allowance")
                if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                    raise Conflict("artifact integrity failure")
                if not self._finish_operation(
                    environment,
                    operation_id,
                    attempt_number,
                    "committed",
                    response_bytes=len(data),
                ):
                    raise Conflict("hosted artifact read response belongs to a stale attempt")
            except Exception:
                self._finish_operation(environment, operation_id, attempt_number, "unknown")
                raise
        if budget is None and hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise Conflict("artifact integrity failure")
        return data, artifact["media_type"]

    def _purge_budgeted_environment(self, environment, *, provider_quiescence_sha256):
        list_page = getattr(self.object_store, "list_page", None)
        delete_batch = getattr(self.object_store, "delete_batch", None)
        if not callable(list_page) or not callable(delete_batch):
            raise Conflict("hosted artifact adapter lacks bounded cleanup operations")
        list_response_limit = self._control_response_limit("max_list_response_bytes")
        delete_response_limit = self._control_response_limit("max_delete_response_bytes")
        prefix = environment + "/"
        with self.transaction() as db:
            row = self._environment_row(db, environment)
            if row is None:
                raise Conflict("hosted cleanup target disappeared")
            if row["lease_until"] > time.time():
                raise Conflict("active writer prevents hosted cleanup")
            if self._hosted_artifact_budget(db, row) is None:
                raise Conflict("hosted cleanup budget disappeared")
            self._ensure_artifact_usage(db, environment)
            db.execute(
                "UPDATE artifact_budget_usage SET purge_started=TRUE WHERE environment=?",
                (environment,),
            )
        with self.transaction() as db:
            self._environment_row(db, environment)
            usage = self._ensure_artifact_usage(db, environment)
            unresolved_puts = db.execute(
                "SELECT count(*) FROM artifact_operations WHERE environment=? AND kind='put' "
                "AND status IN ('in_flight','unknown')",
                (environment,),
            ).fetchone()[0]
            if unresolved_puts and usage["provider_quiescence_sha256"] is None and provider_quiescence_sha256 is None:
                raise Conflict("provider quiescence is required to erase unresolved artifact writes")
            if provider_quiescence_sha256 is not None:
                db.execute(
                    "UPDATE artifact_budget_usage SET provider_quiesced_at=?,provider_quiescence_sha256=? "
                    "WHERE environment=? AND provider_quiescence_sha256 IS NULL",
                    (time.time(), provider_quiescence_sha256, environment),
                )
        while True:
            with self.transaction() as db:
                row = self._environment_row(db, environment)
                if row is None:
                    raise Conflict("hosted cleanup target disappeared")
                budget = self._hosted_artifact_budget(db, row)
                if budget is None:
                    raise Conflict("hosted cleanup budget disappeared")
                self._check_deadline(budget, cleanup=True)
                usage = self._ensure_artifact_usage(db, environment)
                if usage["purge_complete"]:
                    return usage["purge_deleted_objects"]
                page_number = usage["purge_page"]
                list_id = f"purge-list:{page_number}"
                list_op = db.execute(
                    "SELECT * FROM artifact_operations WHERE environment=? AND operation_id=? FOR UPDATE",
                    (environment, list_id),
                ).fetchone()
                if list_op is None or list_op["status"] != "committed":
                    now = time.time()
                    if list_op and list_op["status"] == "failed":
                        raise Conflict("hosted cleanup listing is terminal")
                    if list_op and list_op["status"] == "in_flight" and list_op["attempt_until"] > now:
                        raise Conflict("hosted cleanup listing is still in progress")
                    self._budget_attempt(
                        usage,
                        budget,
                        "purge_list",
                        response_reserve=list_response_limit,
                    )
                    if list_op is None:
                        list_attempt_number = 1
                        db.execute(
                            "INSERT INTO artifact_operations "
                            "(environment,operation_id,kind,status,attempts,attempt_until,cursor,created,updated) "
                            "VALUES (?,?, 'purge_list','in_flight',1,?,?,?,?)",
                            (environment, list_id, now + ARTIFACT_ATTEMPT_LEASE_SECONDS,
                             usage["purge_cursor"], now, now),
                        )
                    else:
                        list_attempt_number = list_op["attempts"] + 1
                        db.execute(
                            "UPDATE artifact_operations SET status='in_flight',attempts=attempts+1,"
                            "attempt_until=?,updated=? WHERE environment=? AND operation_id=?",
                            (now + ARTIFACT_ATTEMPT_LEASE_SECONDS, now, environment, list_id),
                        )
                    db.execute(
                        "UPDATE artifact_budget_usage SET list_attempts=list_attempts+1,"
                        "cleanup_list_attempts=cleanup_list_attempts+1,"
                        "control_response_bytes=control_response_bytes+? WHERE environment=?",
                        (list_response_limit, environment),
                    )
                    cursor = usage["purge_cursor"]
                    action = "list"
                else:
                    page = json.loads(list_op["page_result"])
                    objects = page["objects"]
                    if not objects:
                        if page["truncated"]:
                            db.execute(
                                "UPDATE artifact_budget_usage SET purge_cursor=?,purge_page=purge_page+1 "
                                "WHERE environment=?",
                                (page["cursor"], environment),
                            )
                            continue
                        unresolved = db.execute(
                            "SELECT count(*) FROM artifact_operations WHERE environment=? AND kind='put' "
                            "AND status IN ('in_flight','unknown')",
                            (environment,),
                        ).fetchone()[0]
                        usage = self._ensure_artifact_usage(db, environment)
                        if unresolved and usage["provider_quiescence_sha256"] is None:
                            raise Conflict("unresolved artifact writes prevent cleanup completion")
                        db.execute(
                            "UPDATE artifact_budget_usage SET purge_complete=TRUE WHERE environment=?",
                            (environment,),
                        )
                        return usage["purge_deleted_objects"]
                    delete_id = f"purge-delete:{page_number}"
                    delete_op = db.execute(
                        "SELECT * FROM artifact_operations WHERE environment=? AND operation_id=? FOR UPDATE",
                        (environment, delete_id),
                    ).fetchone()
                    if delete_op is None:
                        db.execute(
                            "INSERT INTO artifact_operations "
                            "(environment,operation_id,kind,status,attempts,object_keys,cursor,created,updated) "
                            "VALUES (?,?, 'purge_delete','reserved',0,?,?,?,?)",
                            (environment, delete_id, encode(objects), list_id, time.time(), time.time()),
                        )
                        delete_op = db.execute(
                            "SELECT * FROM artifact_operations WHERE environment=? AND operation_id=? FOR UPDATE",
                            (environment, delete_id),
                        ).fetchone()
                    if delete_op["status"] == "committed":
                        raise Conflict("hosted cleanup page state is inconsistent")
                    now = time.time()
                    if delete_op["status"] == "failed":
                        raise Conflict("hosted cleanup deletion is terminal")
                    if delete_op["status"] == "in_flight" and delete_op["attempt_until"] > now:
                        raise Conflict("hosted cleanup deletion is still in progress")
                    delete_attempt_number = delete_op["attempts"] + 1
                    saved_objects = json.loads(delete_op["object_keys"])
                    if saved_objects != objects:
                        raise Conflict("hosted cleanup page changed before deletion")
                    delete_keys = [item["key"] for item in saved_objects]
                    if (
                        len(delete_keys) > PURGE_PAGE_OBJECTS
                        or any(not isinstance(key, str) or not key.startswith(prefix) for key in delete_keys)
                    ):
                        raise Conflict("hosted cleanup inventory escaped its exact prefix")
                    self._budget_attempt(
                        usage,
                        budget,
                        "purge_delete",
                        response_reserve=delete_response_limit,
                    )
                    if usage["delete_objects"] + len(delete_keys) > budget.max_delete_objects:
                        raise Conflict("hosted cleanup object allowance exhausted")
                    if usage["cleanup_delete_objects"] + len(delete_keys) > budget.cleanup_reserve.delete_objects:
                        raise Conflict("hosted cleanup object reserve exhausted")
                    db.execute(
                        "UPDATE artifact_operations SET status='in_flight',attempts=attempts+1,"
                        "attempt_until=?,updated=? WHERE environment=? AND operation_id=?",
                        (now + ARTIFACT_ATTEMPT_LEASE_SECONDS, now, environment, delete_id),
                    )
                    db.execute(
                        "UPDATE artifact_budget_usage SET delete_attempts=delete_attempts+1,"
                        "cleanup_delete_attempts=cleanup_delete_attempts+1,"
                        "delete_objects=delete_objects+?,cleanup_delete_objects=cleanup_delete_objects+?,"
                        "control_response_bytes=control_response_bytes+? WHERE environment=?",
                        (len(delete_keys), len(delete_keys), delete_response_limit, environment),
                    )
                    action = "delete"
                    cursor = page["cursor"]
                    truncated = page["truncated"]
            try:
                if action == "list":
                    page, response_bytes = list_page(prefix, cursor=cursor, limit=PURGE_PAGE_OBJECTS)
                    objects = page.get("objects")
                    if (
                        not isinstance(objects, list)
                        or len(objects) > PURGE_PAGE_OBJECTS
                        or not isinstance(page.get("truncated"), bool)
                        or (
                            page.get("cursor") is not None
                            and (
                                not isinstance(page.get("cursor"), str)
                                or len(page["cursor"].encode()) > 4096
                            )
                        )
                        or (page["truncated"] and not page.get("cursor"))
                        or (not page["truncated"] and page.get("cursor") is not None)
                        or any(
                            not isinstance(item, dict)
                            or set(item) != {"key", "size"}
                            or not isinstance(item.get("key"), str)
                            or not re.fullmatch(
                                re.escape(prefix) + r"[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._:-]+)*",
                                item["key"],
                            )
                            or len(item["key"].encode()) > 700
                            or type(item.get("size")) is not int
                            or not 0 <= item["size"] <= budget.max_object_bytes
                            for item in objects
                        )
                        or len({item["key"] for item in objects if isinstance(item, dict) and isinstance(item.get("key"), str)}) != len(objects)
                        or type(response_bytes) is not int
                        or not 0 <= response_bytes <= list_response_limit
                    ):
                        raise Conflict("hosted cleanup inventory response is invalid")
                    result = {"objects": objects, "cursor": page.get("cursor"), "truncated": page["truncated"]}
                    with self.transaction() as db:
                        self._environment_row(db, environment)
                        usage = self._ensure_artifact_usage(db, environment)
                        inventory_bytes = sum(item["size"] for item in objects)
                        if (
                            usage["purge_inventory_objects"] + len(objects) > budget.max_lifetime_objects
                            or usage["purge_inventory_bytes"] + inventory_bytes > budget.max_lifetime_uploaded_bytes
                        ):
                            raise Conflict("hosted cleanup inventory exceeds its frozen upload budget")
                        db.execute(
                            "UPDATE artifact_operations SET status='committed',page_result=?,response_bytes=?,"
                            "attempt_until=0,updated=? WHERE environment=? AND operation_id=? "
                            "AND attempts=? AND status='in_flight'",
                            (encode(result), response_bytes, time.time(), environment, list_id, list_attempt_number),
                        )
                        if db.execute(
                            "SELECT 1 FROM artifact_operations WHERE environment=? AND operation_id=? "
                            "AND attempts=? AND status='committed'",
                            (environment, list_id, list_attempt_number),
                        ).fetchone() is None:
                            raise Conflict("hosted inventory response belongs to a stale attempt")
                        db.execute(
                            "UPDATE artifact_budget_usage SET purge_inventory_objects=purge_inventory_objects+?,"
                            "purge_inventory_bytes=purge_inventory_bytes+? WHERE environment=?",
                            (len(objects), inventory_bytes, environment),
                        )
                else:
                    ack, response_bytes = delete_batch(prefix, delete_keys)
                    if (
                        not isinstance(ack, dict)
                        or ack.get("deleted") != len(delete_keys)
                        or type(response_bytes) is not int
                        or not 0 <= response_bytes <= delete_response_limit
                    ):
                        raise Conflict("hosted cleanup deletion acknowledgement is invalid")
                    with self.transaction() as db:
                        self._environment_row(db, environment)
                        db.execute(
                            "UPDATE artifact_operations SET status='committed',response_bytes=?,"
                            "attempt_until=0,updated=? WHERE environment=? AND operation_id=? "
                            "AND attempts=? AND status='in_flight'",
                            (response_bytes, time.time(), environment, delete_id, delete_attempt_number),
                        )
                        if db.execute(
                            "SELECT 1 FROM artifact_operations WHERE environment=? AND operation_id=? "
                            "AND attempts=? AND status='committed'",
                            (environment, delete_id, delete_attempt_number),
                        ).fetchone() is None:
                            raise Conflict("hosted deletion response belongs to a stale attempt")
                        db.execute(
                            "UPDATE artifact_budget_usage SET purge_cursor=?,purge_truncated=?,"
                            "purge_page=purge_page+1,purge_deleted_objects=purge_deleted_objects+? "
                            "WHERE environment=?",
                            (cursor, truncated, len(delete_keys), environment),
                        )
            except Exception:
                self._finish_operation(
                    environment,
                    list_id if action == "list" else delete_id,
                    list_attempt_number if action == "list" else delete_attempt_number,
                    "unknown",
                )
                raise


    def purge_tenant(self, tenant, *, confirm, provider_quiescence_sha256=None):
        """Trusted retention operation after the host proves every worker stopped.

        This method is not exposed by the session server. The caller must stop
        admission and all writers for the tenant before invoking it. Artifact
        deletion precedes metadata deletion; failure keeps metadata for recovery.
        """
        if not tenant or confirm != "permanently-delete:" + tenant:
            raise ValueError("explicit tenant erasure confirmation required")
        if provider_quiescence_sha256 is not None and not re.fullmatch(
            r"[a-f0-9]{64}", provider_quiescence_sha256
        ):
            raise ValueError("provider quiescence receipt digest must be SHA-256")
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id,lease_until FROM environments WHERE tenant=? ORDER BY id", (tenant,)
            ).fetchall()
            if any(row["lease_until"] > time.time() for row in rows):
                raise Conflict("active writer prevents tenant erasure")
            identities = [row["id"] for row in rows]
            if identities and db.execute(
                "SELECT 1 FROM hosted_branch_copies WHERE parent=ANY(?) AND status='pending' LIMIT 1",
                (identities,),
            ).fetchone():
                raise Conflict("pending hosted branch copy prevents source erasure")
        # Budgeted prefixes are drained one durably journaled provider operation
        # at a time. An exhausted or ambiguous cleanup leaves all metadata intact.
        budget_deleted = 0
        for identity in identities:
            with self.transaction() as db:
                row = db.execute("SELECT * FROM environments WHERE id=?", (identity,)).fetchone()
                if row is None:
                    raise Conflict("tenant erasure target changed")
                budget = self._hosted_artifact_budget(db, row)
            if budget is not None:
                budget_deleted += self._purge_budgeted_environment(
                    identity,
                    provider_quiescence_sha256=provider_quiescence_sha256,
                )
            elif self._budget_required(db, identity):
                raise Conflict("hosted session is missing its immutable artifact budget")
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id,lease_until FROM environments WHERE tenant=? ORDER BY id FOR UPDATE", (tenant,)
            ).fetchall()
            if any(row["lease_until"] > time.time() for row in rows):
                raise Conflict("active writer prevents tenant erasure")
            if [row["id"] for row in rows] != identities:
                raise Conflict("tenant erasure inventory changed during cleanup")
            for identity in identities:
                row = db.execute("SELECT * FROM environments WHERE id=?", (identity,)).fetchone()
                budget = self._hosted_artifact_budget(db, row)
                if budget is not None:
                    usage = db.execute(
                        "SELECT purge_complete,purge_deleted_objects FROM artifact_budget_usage WHERE environment=?",
                        (identity,),
                    ).fetchone()
                    if usage is None or not usage["purge_complete"]:
                        raise Conflict("hosted artifact cleanup remains incomplete")
            experiment_ids = [
                row["id"]
                for row in db.execute("SELECT id FROM experiments WHERE tenant=?", (tenant,)).fetchall()
            ]
            deleted = 0
            for identity in identities:
                row = db.execute("SELECT * FROM environments WHERE id=?", (identity,)).fetchone()
                if self._hosted_artifact_budget(db, row) is None:
                    deleted += self.object_store.purge_prefix(identity + "/")
            db.execute("SELECT set_config('environment_harness.erase_tenant',?,true)", (tenant,))
            source_ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM trajectory_sources WHERE tenant=?", (tenant,)
                ).fetchall()
            ]
            snapshot_ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM trajectory_snapshots WHERE tenant=?", (tenant,)
                ).fetchall()
            ]
            if snapshot_ids:
                db.execute("DELETE FROM trajectory_snapshot_records WHERE snapshot=ANY(?)", (snapshot_ids,))
            db.execute("DELETE FROM trajectory_snapshots WHERE tenant=?", (tenant,))
            if source_ids:
                db.execute("DELETE FROM trajectory_source_records WHERE source=ANY(?)", (source_ids,))
            db.execute("DELETE FROM trajectory_sources WHERE tenant=?", (tenant,))
            db.execute("DELETE FROM training_runs WHERE tenant=?", (tenant,))
            db.execute("DELETE FROM trajectory_datasets WHERE tenant=?", (tenant,))
            for table in (
                "agent_work",
                "transitions",
                "artifact_aliases",
                "reports",
                "artifacts",
                "operations",
                "checkpoints",
                "actions",
                "observations",
                "events",
            ):
                db.execute(f"DELETE FROM {table} WHERE environment=ANY(?)", (identities,))
            db.execute("DELETE FROM event_outbox WHERE tenant=?", (tenant,))
            db.execute("DELETE FROM session_runs WHERE tenant=?", (tenant,))
            if experiment_ids:
                db.execute("DELETE FROM scenario_snapshots WHERE experiment=ANY(?)", (experiment_ids,))
            db.execute("DELETE FROM experiments WHERE tenant=?", (tenant,))
            db.execute("DELETE FROM credentials WHERE tenant=?", (tenant,))
            db.execute("DELETE FROM splits WHERE tenant=?", (tenant,))
            if identities:
                db.execute("DELETE FROM hosted_branch_copies WHERE environment=ANY(?)", (identities,))
                db.execute("DELETE FROM artifact_operations WHERE environment=ANY(?)", (identities,))
                db.execute("DELETE FROM artifact_budget_usage WHERE environment=ANY(?)", (identities,))
                db.execute("DELETE FROM hosted_artifact_budgets WHERE environment=ANY(?)", (identities,))
            db.execute("DELETE FROM environments WHERE tenant=?", (tenant,))
        return {
            "schema_version": "tenant-erasure.v1",
            "tenant": tenant,
            "environments": len(identities),
            "environment_ids_sha256": digest(identities),
            "objects_deleted": deleted + budget_deleted,
            "status": "purged",
        }

    def _write_artifact(self, environment, key, data):
        self.object_store.put(f"{environment}/{key}", data)

    def _read_artifact(self, environment, key):
        return self.object_store.get(f"{environment}/{key}")


class S3Artifacts:
    put_request_units = 1
    max_put_response_bytes = PURGE_CONTROL_RESPONSE_CHARGE_BYTES
    max_get_overread_bytes = 1
    max_list_response_bytes = PURGE_CONTROL_RESPONSE_CHARGE_BYTES
    max_delete_response_bytes = PURGE_CONTROL_RESPONSE_CHARGE_BYTES

    def __init__(self, client, bucket, *, prefix="environment-harness", budget_client=None):
        self.client, self.bucket, self.prefix = client, bucket, prefix
        self.budget_client = budget_client

    def _single_attempt_client(self):
        client = self.budget_client or self.client
        retries = getattr(getattr(getattr(client, "meta", None), "config", None), "retries", None)
        if not isinstance(retries, dict):
            raise ValueError("hosted artifact operations require a single-attempt S3 client")
        total = retries.get("total_max_attempts")
        if total is None and isinstance(retries.get("max_attempts"), int):
            total = retries["max_attempts"] + 1
        if total != 1:
            raise ValueError("hosted artifact operations require S3 retries disabled")
        return client

    def put(self, key, data):
        self.client.put_object(
            Bucket=self.bucket,
            Key=f"{self.prefix}/{key}",
            Body=data,
            IfNoneMatch="*",
            ContentType="application/octet-stream",
        )

    def put_once(self, key, data):
        client = self._single_attempt_client()
        client.put_object(
            Bucket=self.bucket,
            Key=f"{self.prefix}/{key}",
            Body=data,
            ContentType="application/octet-stream",
        )

    def get(self, key):
        response = self.client.get_object(Bucket=self.bucket, Key=f"{self.prefix}/{key}")
        try:
            return response["Body"].read(16777217)
        finally:
            response["Body"].close()

    def get_bounded(self, key, max_bytes):
        client = self._single_attempt_client()
        response = client.get_object(Bucket=self.bucket, Key=f"{self.prefix}/{key}")
        try:
            if response.get("ContentLength", max_bytes + 1) > max_bytes:
                raise ValueError("artifact size limit exceeded")
            data = response["Body"].read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("artifact size limit exceeded")
            return data
        finally:
            response["Body"].close()

    def list_page(self, prefix, *, cursor=None, limit=PURGE_PAGE_OBJECTS):
        if (
            not re.fullmatch(r"[a-f0-9]{32}/", prefix)
            or type(limit) is not int
            or not 1 <= limit <= PURGE_PAGE_OBJECTS
            or (cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor.encode()) > 4096))
        ):
            raise ValueError("bounded exact environment prefix required")
        client = self._single_attempt_client()
        full = f"{self.prefix}/{prefix}"
        args = {"Bucket": self.bucket, "Prefix": full, "MaxKeys": limit}
        if cursor is not None:
            args["ContinuationToken"] = cursor
        page = client.list_objects_v2(**args)
        objects = page.get("Contents", [])
        if not isinstance(objects, list) or len(objects) > limit:
            raise ValueError("invalid storage inventory response")
        values = [{"key": item["Key"][len(f"{self.prefix}/"):], "size": item["Size"]} for item in objects]
        if any(
            not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._:-]+)*", value["key"])
            or len(value["key"].encode()) > 700
            or type(value["size"]) is not int
            or value["size"] < 0
            for value in values
        ):
            raise ValueError("storage inventory escaped exact environment prefix")
        truncated = page.get("IsTruncated")
        next_cursor = page.get("NextContinuationToken")
        if (
            type(truncated) is not bool
            or (truncated and (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor.encode()) > 4096))
            or (not truncated and next_cursor is not None)
        ):
            raise ValueError("invalid storage inventory cursor")
        result = {"objects": values, "cursor": next_cursor, "truncated": truncated}
        response_bytes = len(encode(result).encode())
        if response_bytes > PURGE_LIST_RESPONSE_MAX_BYTES:
            raise ValueError("storage inventory response exceeds its bound")
        return result, response_bytes

    def delete_batch(self, prefix, keys):
        if (
            not re.fullmatch(r"[a-f0-9]{32}/", prefix)
            or not keys
            or len(keys) > PURGE_PAGE_OBJECTS
            or any(not isinstance(key, str) or not key.startswith(prefix) for key in keys)
        ):
            raise ValueError("bounded exact environment object batch required")
        client = self._single_attempt_client()
        full_keys = [f"{self.prefix}/{key}" for key in keys]
        response = client.delete_objects(
            Bucket=self.bucket,
            Delete={"Objects": [{"Key": key} for key in full_keys], "Quiet": False},
        )
        deleted = {item["Key"] for item in response.get("Deleted", [])}
        if response.get("Errors") or deleted != set(full_keys):
            raise ValueError("storage deletion acknowledgement is incomplete")
        summary = {"deleted": len(deleted)}
        response_bytes = len(encode(summary).encode())
        if response_bytes > PURGE_DELETE_RESPONSE_MAX_BYTES:
            raise ValueError("storage deletion response exceeds its bound")
        return summary, response_bytes

    def purge_prefix(self, prefix):
        """Delete an exact environment prefix in an unversioned runtime bucket."""
        if not re.fullmatch(r"[a-f0-9]{32}/", prefix):
            raise ValueError("exact environment prefix required")
        if self.client.get_bucket_versioning(Bucket=self.bucket).get("Status") in ("Enabled", "Suspended"):
            raise ValueError("versioned runtime buckets require a version-aware retention adapter")
        full, removed = f"{self.prefix}/{prefix}", 0
        while True:
            page = self.client.list_objects_v2(Bucket=self.bucket, Prefix=full, MaxKeys=1000)
            keys = [entry["Key"] for entry in page.get("Contents", [])]
            if not keys:
                if page.get("IsTruncated"):
                    raise ValueError("incomplete storage deletion inventory")
                return removed
            if any(not key.startswith(full) for key in keys):
                raise ValueError("storage inventory escaped environment prefix")
            result = self.client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": key} for key in keys], "Quiet": False}
            )
            if result.get("Errors") or {entry["Key"] for entry in result.get("Deleted", [])} != set(keys):
                raise ValueError("storage deletion is not confirmed")
            removed += len(keys)

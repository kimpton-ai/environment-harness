"""Optional PostgreSQL metadata and S3-compatible artifacts.

Initialize only in a dedicated harness schema. Platform migrations remain owned
by their platform. Constructing the store does not change a database schema.
"""

import re
import time
from contextlib import contextmanager
from typing import cast

from .errors import Conflict
from .store import EvidenceStore, digest, encode


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

    def __iter__(self):
        return (Row(row) for row in self.cursor)


class Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        # DB-API parameter markers only. SQL semantics live in explicit storage operations.
        return Cursor(self.connection.execute(sql.replace("?", "%s"), params))


class PostgresEvidenceStore(EvidenceStore):
    def __init__(self, dsn, object_store, *, schema="environment_harness", max_retained_bytes=None):
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("invalid harness schema")
        if max_retained_bytes is not None and (
            not isinstance(max_retained_bytes, int) or max_retained_bytes < 1
        ):
            raise ValueError("positive retained storage allowance required")
        self.dsn, self.object_store, self.schema = dsn, object_store, schema
        self.max_retained_bytes = max_retained_bytes

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

    def _event_page(self, db, environment, after, who, limit):
        return db.execute(
            """SELECT * FROM events WHERE environment=? AND seq>? AND
            (? IN ('researcher','scorer','worker') OR audience='["*"]' OR
             EXISTS(SELECT 1 FROM jsonb_array_elements_text(audience::jsonb)
                    AS membership(value) WHERE value=?)) ORDER BY seq LIMIT ?""",
            (environment, after, who.role, who.participant, limit),
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
        }
        for table, columns in fields.items():
            expression = "+".join(f"octet_length(coalesce({column},''))" for column in columns)
            key = "id" if table == "environments" else "environment"
            total += db.execute(
                f"SELECT coalesce(sum({expression}),0) FROM {table} WHERE {key}=?", (environment,)
            ).fetchone()[0]
        return (
            total
            + db.execute(
                "SELECT coalesce(sum(size),0) FROM artifacts WHERE environment=?", (environment,)
            ).fetchone()[0]
        )

    def _check_artifact_budget(self, db, environment, size):
        if (
            self.max_retained_bytes is not None
            and self.retained_bytes(db, environment) + size > self.max_retained_bytes
        ):
            raise Conflict("retained storage allowance exceeded")

    def append(self, db, environment, revision, kind, payload, audience=(), event_time=None):
        self._check_artifact_budget(db, environment, len(encode(payload).encode()))
        return super().append(db, environment, revision, kind, payload, audience, event_time)

    def purge_tenant(self, tenant, *, confirm):
        """Trusted retention operation after the host proves every worker stopped.

        This method is not exposed by the session server. The caller must stop
        admission and all writers for the tenant before invoking it. Artifact
        deletion precedes metadata deletion; failure keeps metadata for recovery.
        """
        if not tenant or confirm != "permanently-delete:" + tenant:
            raise ValueError("explicit tenant erasure confirmation required")
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id,lease_until FROM environments WHERE tenant=? ORDER BY id FOR UPDATE", (tenant,)
            ).fetchall()
            if any(row["lease_until"] > time.time() for row in rows):
                raise Conflict("active writer prevents tenant erasure")
            identities = [row["id"] for row in rows]
            deleted = sum(self.object_store.purge_prefix(identity + "/") for identity in identities)
            db.execute("SELECT set_config('environment_harness.erase_tenant',?,true)", (tenant,))
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
            db.execute("DELETE FROM credentials WHERE principal::jsonb->>'tenant'=?", (tenant,))
            db.execute("DELETE FROM splits WHERE tenant=?", (tenant,))
            db.execute("DELETE FROM environments WHERE tenant=?", (tenant,))
        return {
            "schema_version": "tenant-erasure.v1",
            "tenant": tenant,
            "environments": len(identities),
            "environment_ids_sha256": digest(identities),
            "objects_deleted": deleted,
            "status": "purged",
        }

    def _write_artifact(self, environment, key, data):
        self.object_store.put(f"{environment}/{key}", data)

    def _read_artifact(self, environment, key):
        return self.object_store.get(f"{environment}/{key}")


class S3Artifacts:
    def __init__(self, client, bucket, *, prefix="environment-harness"):
        self.client, self.bucket, self.prefix = client, bucket, prefix

    def put(self, key, data):
        self.client.put_object(
            Bucket=self.bucket,
            Key=f"{self.prefix}/{key}",
            Body=data,
            IfNoneMatch="*",
            ContentType="application/octet-stream",
        )

    def get(self, key):
        response = self.client.get_object(Bucket=self.bucket, Key=f"{self.prefix}/{key}")
        try:
            return response["Body"].read(16777217)
        finally:
            response["Body"].close()

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

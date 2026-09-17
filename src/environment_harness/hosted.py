"""Optional PostgreSQL metadata and S3-compatible artifacts.

Initialize only in a dedicated harness schema. Platform migrations remain owned
by their platform. Constructing the store does not change a database schema.
"""

import re
from contextlib import contextmanager
from typing import cast

from .store import EvidenceStore


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
    def __init__(self, dsn, object_store, *, schema="environment_harness"):
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("invalid harness schema")
        self.dsn, self.object_store, self.schema = dsn, object_store, schema

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

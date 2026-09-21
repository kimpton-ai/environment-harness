import os
from uuid import uuid4

import pytest


@pytest.mark.skipif(
    not os.environ.get("ENVIRONMENT_HARNESS_POSTGRES_URL"),
    reason="ephemeral PostgreSQL service is not configured",
)
def test_postgres_schema_migrations_are_idempotent():
    import psycopg
    from psycopg import sql

    from environment_harness.hosted import PostgresEvidenceStore

    class Objects:
        def put(self, _key, _data):
            return None

        def get(self, _key):
            return b""

    dsn = os.environ["ENVIRONMENT_HARNESS_POSTGRES_URL"]
    schema = "environment_harness_test_" + uuid4().hex
    store = PostgresEvidenceStore(dsn, Objects(), schema=schema)
    try:
        store.initialize()
        store.initialize()
        with store.transaction() as database:
            migration = database.execute("SELECT version,sha256 FROM schema_migrations").fetchone()
        assert migration["version"] == "001_durable_sessions.sql"
        assert len(migration["sha256"]) == 64
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

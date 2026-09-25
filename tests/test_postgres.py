import os
from uuid import uuid4

import pytest

from environment_harness.access import _AccessContext
from environment_harness.runtime import _SessionRuntime


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


@pytest.mark.skipif(
    not os.environ.get("ENVIRONMENT_HARNESS_POSTGRES_URL"),
    reason="ephemeral PostgreSQL service is not configured",
)
def test_postgres_persists_and_lists_trajectory_snapshot_boundaries():
    import psycopg
    from psycopg import sql

    from environment_harness import AgentSpec, ExperimentSpec
    from environment_harness.fixtures import SyntheticEnvironment
    from environment_harness.hosted import PostgresEvidenceStore
    from environment_harness.trajectories import TrajectoryRepository

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
        who = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
        environment = SyntheticEnvironment()
        session = _SessionRuntime(store, environment)
        environment_id = session.create(
            ExperimentSpec(
                environment=environment.spec,
                participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
            ),
            who,
        )["id"]
        session.observe(environment_id, who, "alice")
        repository = TrajectoryRepository(store)

        frozen = repository.freeze(environment_id, who)

        assert repository.list_snapshots(environment_id, who) == [frozen]
        assert list(repository.export_snapshot(frozen.metadata.id, who))
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.skipif(
    not os.environ.get("ENVIRONMENT_HARNESS_POSTGRES_URL"),
    reason="ephemeral PostgreSQL service is not configured",
)
def test_explicit_tenant_erasure_preserves_other_tenants_and_refuses_active_writer():
    import psycopg
    from psycopg import sql

    from environment_harness.contracts import AgentSpec, ExperimentSpec
    from environment_harness.errors import Conflict
    from environment_harness.fixtures import SyntheticEnvironment
    from environment_harness.hosted import PostgresEvidenceStore
    from environment_harness.trajectories import SourceRegistration, TrajectoryRepository

    class Objects:
        def __init__(self):
            self.values = {}

        def put(self, key, data):
            self.values[key] = data

        def purge_prefix(self, prefix):
            keys = [key for key in self.values if key.startswith(prefix)]
            for key in keys:
                del self.values[key]
            return len(keys)

    dsn = os.environ["ENVIRONMENT_HARNESS_POSTGRES_URL"]
    schema = "environment_harness_test_" + uuid4().hex
    objects = Objects()
    store = PostgresEvidenceStore(dsn, objects, schema=schema)
    try:
        store.initialize()
        env = SyntheticEnvironment()
        session = _SessionRuntime(store, env)
        experiment = ExperimentSpec(
            environment=env.spec,
            participants=(AgentSpec(id="a", implementation="external", policy_version="1"),),
        )
        first = _AccessContext(tenant="first", subject="owner", policy="trusted-local")
        other = _AccessContext(tenant="other", subject="owner", policy="trusted-local")
        a, b = session.create(experiment, first)["id"], session.create(experiment, other)["id"]
        trajectories = TrajectoryRepository(store)
        trajectories.freeze(a, first)
        trajectories.register_source(
            SourceRegistration(
                namespace="com.example.retention",
                run_id="first-source",
                schema_version="retention.v1",
                environment={"id": "synthetic"},
                participants=("a",),
                purpose="evaluation",
            ),
            first,
        )
        with store.transaction() as db:
            db.execute(
                "INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("experiment-first", "first", "first", "completed", 1, 1, 1, 1, 0, 0, 0, "{}", None, 1, 1),
            )
            db.execute(
                "INSERT INTO scenario_snapshots VALUES (?,?,?,?)",
                ("experiment-first", "scenario", 0, "{}"),
            )
            db.execute(
                "INSERT INTO session_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    a,
                    "first",
                    "experiment-first",
                    "scenario",
                    0,
                    1,
                    "completed",
                    None,
                    1,
                    1,
                    "Done",
                    "{}",
                    1,
                    1,
                ),
            )
            db.execute(
                "INSERT INTO event_outbox (tenant,topic,experiment,environment,kind,body,created) "
                "VALUES (?,?,?,?,?,?,?)",
                ("first", "environment", "experiment-first", a, "completed", "{}", 1),
            )
        store.artifact(a, first, b"private-first")
        store.artifact(b, other, b"private-other")
        with pytest.raises(psycopg.Error, match="immutable"):
            with store.transaction() as db:
                db.execute("DELETE FROM events WHERE environment=?", (a,))
        with pytest.raises(ValueError):
            store.purge_tenant("first", confirm="wrong")
        lease = session.lease(a, first, "writer")
        with pytest.raises(Conflict, match="active writer"):
            store.purge_tenant("first", confirm="permanently-delete:first")
        session.release(a, first, lease)
        with store.transaction() as db:
            retained = store.retained_bytes(db, a)
        store.max_retained_bytes = retained + 1
        with pytest.raises(Conflict, match="storage allowance"):
            store.artifact(a, first, b"not-written-over-budget")
        assert len(objects.values) == 2
        store.max_retained_bytes = None
        result = store.purge_tenant("first", confirm="permanently-delete:first")
        assert result["environments"] == result["objects_deleted"] == 1
        assert not any(key.startswith(a) for key in objects.values)
        assert any(key.startswith(b) for key in objects.values)
        assert store.events(b, other)
        with store.transaction() as db:
            for table in ("experiments", "scenario_snapshots", "session_runs", "event_outbox"):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert store.purge_tenant("first", confirm="permanently-delete:first")["environments"] == 0
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.skipif(
    not os.environ.get("ENVIRONMENT_HARNESS_POSTGRES_URL"),
    reason="ephemeral PostgreSQL service is not configured",
)
def test_postgres_credential_and_scheduler_migrations_apply_and_force_reissue():
    """The 0.3.0rc1 migrations drop legacy credentials and add the reference columns."""

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
        with store.transaction() as database:
            database.execute(
                "INSERT INTO credentials (hash,tenant,subject,policy,expires) VALUES (?,?,?,?,?)",
                ("a" * 64, "tenant", "ops", "management", 9.9e9),
            )
            columns = {
                row["column_name"]
                for row in database.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=? AND table_name='credentials'",
                    (schema,),
                ).fetchall()
            }
            assert "principal" not in columns
            assert {"policy", "session", "participant", "generation"} <= columns
            sessions = {
                row["column_name"]
                for row in database.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=? AND table_name='session_runs'",
                    (schema,),
                ).fetchall()
            }
            assert {
                "environment_id",
                "environment_version",
                "spec_digest",
                "blocked_reason",
            } <= sessions
            applied = {
                row["version"] for row in database.execute("SELECT version FROM schema_migrations").fetchall()
            }
        assert "005_credential_policies.sql" in applied
        assert "006_scheduler_recovery.sql" in applied
        # Reapplying is a no-op and does not delete the reissued credential.
        store.initialize()
        with store.transaction() as database:
            assert database.execute("SELECT count(*) FROM credentials").fetchone()[0] == 1
        assert store.authenticate  # the policy-based resolver is the only reader
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

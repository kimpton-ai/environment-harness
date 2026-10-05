import hashlib
import json
import time
from uuid import uuid4

import pytest

from environment_harness.access import _AccessContext, trusted_local
from environment_harness.adapters.programs import InstrumentedModel
from environment_harness.contracts import (
    AgentSpec,
    ArtifactCleanupReserve,
    ExperimentSpec,
    HostedArtifactBudget,
    RunPolicy,
)
from environment_harness.errors import Conflict
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.hosted import PostgresEvidenceStore
from environment_harness.runner import inference_context
from environment_harness.runtime import _SessionRuntime


class MemoryArtifacts:
    put_request_units = 1
    max_put_response_bytes = 1_052_672
    max_list_response_bytes = 1_048_576
    max_delete_response_bytes = 1_048_576
    max_get_overread_bytes = 65_536

    def __init__(self):
        self.values = {}
        self.calls = []
        self.fail_after_put_once = False

    def put(self, key, data):
        self.values[key] = data

    def put_once(self, key, data):
        self.calls.append(("put", key))
        existing = self.values.get(key)
        if existing is not None and existing != data:
            raise ValueError("key content mismatch")
        self.values[key] = data
        if self.fail_after_put_once:
            self.fail_after_put_once = False
            raise TimeoutError("lost response")
        return len(data)

    def get(self, key):
        return self.values[key]

    def get_bounded(self, key, max_bytes):
        self.calls.append(("get", key))
        data = self.values[key]
        if len(data) > max_bytes:
            raise ValueError("too large")
        return data

    def list_page(self, prefix, *, cursor=None, limit=1000):
        self.calls.append(("list", cursor))
        keys = sorted(key for key in self.values if key.startswith(prefix))
        offset = int(cursor or 0)
        selected = keys[offset : offset + limit]
        next_offset = offset + len(selected)
        truncated = next_offset < len(keys)
        next_cursor = str(next_offset) if truncated else None
        page = {
            "objects": [{"key": key, "size": len(self.values[key])} for key in selected],
            "cursor": next_cursor,
            "truncated": truncated,
        }
        return page, len(str(page).encode())

    def delete_batch(self, prefix, keys):
        self.calls.append(("delete", tuple(keys)))
        for key in keys:
            if not key.startswith(prefix):
                raise ValueError("prefix mismatch")
            self.values.pop(key, None)
        return {"deleted": len(keys)}, len(str(len(keys)).encode())

    def purge_prefix(self, prefix):
        keys = [key for key in self.values if key.startswith(prefix)]
        for key in keys:
            del self.values[key]
        return len(keys)


def _budget(
    *,
    max_live_bytes=64,
    max_lifetime_objects=2,
    max_put_attempts=2,
    put_units=1,
    max_get_attempts=1,
    max_object_bytes=64,
):
    cleanup_lists = (max_lifetime_objects + 999) // 1000 + 1
    cleanup_deletes = max(1, (max_lifetime_objects + 999) // 1000)
    now = int(time.time())
    return HostedArtifactBudget(
        max_live_bytes=max_live_bytes,
        max_lifetime_uploaded_bytes=max_live_bytes,
        max_lifetime_objects=max_lifetime_objects,
        max_object_bytes=max_object_bytes,
        max_put_attempts=max_put_attempts,
        max_get_attempts=max_get_attempts,
        max_list_attempts=cleanup_lists,
        max_delete_attempts=cleanup_deletes,
        max_delete_objects=max_lifetime_objects,
        max_egress_bytes=max_get_attempts * (max_object_bytes + MemoryArtifacts.max_get_overread_bytes),
        max_control_response_bytes=(max_put_attempts // put_units + cleanup_lists + cleanup_deletes) * 1_052_672,
        cleanup_reserve=ArtifactCleanupReserve(
            list_attempts=cleanup_lists,
            delete_attempts=cleanup_deletes,
            delete_objects=max_lifetime_objects,
        ),
        expires_at=now + 300,
        retention_deadline=now + 600,
        cleanup_deadline=now + 900,
    )


def _new_session(store, budget=None, *, policy=None):
    access = trusted_local("budget-test")
    env = SyntheticEnvironment()
    spec = ExperimentSpec(
        environment=env.spec,
        participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
        purpose="training" if policy and policy.inference_capture == "training" else "evaluation",
        split="training" if policy and policy.inference_capture == "training" else "heldout",
        policy=policy or RunPolicy(),
    )
    session = _SessionRuntime(store, env)
    environment_id = session.create(spec, access)["id"]
    with store.transaction() as db:
        row = store._environment_row(db, environment_id)
        manifest_sha = hashlib.sha256(row["manifest"].encode()).hexdigest()
    store.install_hosted_artifact_budget(
        environment_id,
        budget or _budget(),
        expected_manifest_sha256=manifest_sha,
        authorized_expires_at=int(time.time()) + 300,
        authorized_retention_deadline=int(time.time()) + 600,
        authorized_cleanup_deadline=int(time.time()) + 900,
    )
    return environment_id, access


@pytest.fixture
def hosted_store(monkeypatch):
    dsn = __import__("os").environ.get("ENVIRONMENT_HARNESS_POSTGRES_URL")
    if not dsn:
        pytest.skip("isolated PostgreSQL URL is required")
    objects = MemoryArtifacts()
    schema = "artifact_budget_" + uuid4().hex
    store = PostgresEvidenceStore(dsn, objects, schema=schema)
    store.initialize()
    try:
        yield store, objects
    finally:
        import psycopg
        from psycopg import sql

        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_hosted_put_charges_before_io_and_recovers_same_key_after_lost_reply(hosted_store):
    store, objects = hosted_store
    environment, access = _new_session(store)
    objects.fail_after_put_once = True
    with pytest.raises(TimeoutError):
        store.artifact(environment, access, b"payload", operation_id="artifact-one")
    first_key = next(iter(objects.values))

    saved = store.artifact(environment, access, b"payload", operation_id="artifact-one")
    assert saved["id"] == first_key.rsplit("/", 1)[1]
    assert [kind for kind, _ in objects.calls] == ["put", "put"]
    with store.transaction() as db:
        store.max_retained_bytes = store.retained_bytes(db, environment)
    replay = store.artifact(environment, access, b"payload", operation_id="artifact-one")
    assert replay == saved
    assert [kind for kind, _ in objects.calls] == ["put", "put"]
    with store.transaction() as db:
        usage = db.execute("SELECT * FROM artifact_budget_usage WHERE environment=?", (environment,)).fetchone()
        operation = db.execute(
            "SELECT status,attempts FROM artifact_operations WHERE environment=? AND operation_id=?",
            (environment, "artifact-one"),
        ).fetchone()
    assert (usage["lifetime_objects"], usage["lifetime_uploaded_bytes"], usage["put_attempts"]) == (1, 7, 2)
    assert (operation["status"], operation["attempts"]) == ("committed", 2)
    assert not store._finish_operation(environment, "artifact-one", 1, "unknown")
    with store.transaction() as db:
        assert db.execute(
            "SELECT status FROM artifact_operations WHERE environment=? AND operation_id=?",
            (environment, "artifact-one"),
        ).fetchone()["status"] == "committed"


def test_hosted_budget_denial_happens_before_provider_request(hosted_store):
    store, objects = hosted_store
    environment, access = _new_session(store)
    with pytest.raises(Conflict, match="object allowance"):
        store.artifact(environment, access, b"x" * 65, operation_id="too-large")
    assert objects.calls == []


def test_host_required_marker_survives_restart_and_create_retry_fails_closed(hosted_store):
    store, objects = hosted_store
    environment = uuid4().hex
    access = trusted_local("budget-test")
    env = SyntheticEnvironment()
    spec = ExperimentSpec(
        environment=env.spec,
        participants=(AgentSpec(id="alice", implementation="synthetic", policy_version="1"),),
    )
    store.require_hosted_artifact_budget_for(environment)
    session = _SessionRuntime(store, env)
    session.create(spec, access, environment_id=environment)

    # The host can replay its stable create request after a crash and then
    # finish installing the budget. No artifact call can use the legacy path.
    session.create(spec, access, environment_id=environment)
    restarted = PostgresEvidenceStore(store.dsn, objects, schema=store.schema)
    with pytest.raises(Conflict, match="missing its immutable artifact budget"):
        restarted.artifact(environment, access, b"payload", operation_id="unbudgeted")
    assert objects.calls == []

    with restarted.transaction() as db:
        row = restarted._environment_row(db, environment)
        manifest_sha = hashlib.sha256(row["manifest"].encode()).hexdigest()
    restarted.install_hosted_artifact_budget(
        environment,
        _budget(),
        expected_manifest_sha256=manifest_sha,
        authorized_expires_at=int(time.time()) + 300,
        authorized_retention_deadline=int(time.time()) + 600,
        authorized_cleanup_deadline=int(time.time()) + 900,
    )
    saved = restarted.artifact(environment, access, b"payload", operation_id="budgeted")
    assert saved["size"] == 7
    assert [kind for kind, _ in objects.calls] == ["put"]


def test_hosted_put_charges_fixed_provider_request_units_before_io(hosted_store):
    store, objects = hosted_store
    objects.put_request_units = 3
    environment, access = _new_session(store, _budget(max_put_attempts=6, put_units=3))

    store.artifact(environment, access, b"payload", operation_id="three-request-put")

    with store.transaction() as db:
        usage = db.execute("SELECT put_attempts FROM artifact_budget_usage WHERE environment=?", (environment,)).fetchone()
    assert usage["put_attempts"] == 3


def test_hosted_inference_spills_use_stable_budgeted_artifact_operations(hosted_store):
    store, objects = hosted_store
    policy = RunPolicy(
        max_event_bytes=4096,
        max_artifact_bytes=200_000,
        inference_capture="training",
        max_inference_artifact_bytes=200_000,
    )
    budget = _budget(
        max_live_bytes=300_000,
        max_lifetime_objects=2,
        max_put_attempts=2,
        max_object_bytes=200_000,
    )
    environment, _ = _new_session(store, budget, policy=policy)
    principal = _AccessContext(
        tenant="budget-test",
        subject="alice",
        policy="participant",
        session=environment,
        participant="alice",
    )

    def generate(_request):
        return {
            "text": "response " * 1500,
            "token_ids": [1] * 3000,
            "logprobs": [-0.1] * 3000,
            "usage": {"output_tokens": 3000},
            "finish_reason": "length",
        }

    model = InstrumentedModel(store, environment, principal, generate, capture_content=True)
    for _ in range(2):
        with inference_context({
            "agent_operation_id": "durable-agent-work-1",
            "observation_id": "observation-1",
            "participant": "alice",
            "generation": 0,
            "revision": 0,
            "_inference_call_sequence": [0],
        }):
            model.call({"prompt": "request " * 1500})
    with store.transaction() as db:
        operations = db.execute(
            "SELECT operation_id,status FROM artifact_operations WHERE environment=? ORDER BY operation_id",
            (environment,),
        ).fetchall()
        call_ids = [
            json.loads(event["body"])["call_id"]
            for event in db.execute(
                "SELECT body FROM events WHERE environment=? AND kind='model.request' ORDER BY seq",
                (environment,),
            ).fetchall()
        ]
    assert len(operations) == 2
    assert all(row["status"] == "committed" for row in operations)
    assert all(row["operation_id"].startswith("inference:") for row in operations)
    assert [kind for kind, _ in objects.calls].count("put") == 2
    assert len(call_ids) == 2 and call_ids[0] == call_ids[1]


def test_hosted_purge_uses_durable_exact_prefix_pages(hosted_store):
    store, objects = hosted_store
    environment, access = _new_session(store)
    store.artifact(environment, access, b"one", operation_id="artifact-one")
    store.artifact(environment, access, b"two", operation_id="artifact-two")

    result = store.purge_tenant("budget-test", confirm="permanently-delete:budget-test")

    assert result["status"] == "purged"
    assert result["objects_deleted"] == 2
    assert not objects.values
    assert [kind for kind, _ in objects.calls].count("list") == 2
    assert [kind for kind, _ in objects.calls].count("delete") == 1


def test_hosted_purge_fences_unknown_writes_until_provider_quiescence(hosted_store):
    store, objects = hosted_store
    environment, access = _new_session(store)
    objects.fail_after_put_once = True
    with pytest.raises(TimeoutError):
        store.artifact(environment, access, b"payload", operation_id="unknown-write")

    with pytest.raises(Conflict, match="provider quiescence"):
        store.purge_tenant("budget-test", confirm="permanently-delete:budget-test")
    provider_calls = list(objects.calls)
    with pytest.raises(Conflict, match="fenced"):
        store.artifact(environment, access, b"payload", operation_id="unknown-write")
    assert objects.calls == provider_calls

    result = store.purge_tenant(
        "budget-test",
        confirm="permanently-delete:budget-test",
        provider_quiescence_sha256="a" * 64,
    )
    assert result["objects_deleted"] == 1
    assert not objects.values


def test_hosted_branch_installs_child_budget_and_recovers_stable_artifact_copy(hosted_store):
    store, objects = hosted_store
    environment, access = _new_session(
        store,
        _budget(max_lifetime_objects=3, max_put_attempts=3, max_get_attempts=2),
    )
    source = store.artifact(environment, access, b"branch-payload", operation_id="source-artifact")
    runtime = _SessionRuntime(store, SyntheticEnvironment())
    lease = runtime.lease(environment, access, "checkpoint", ttl=30)
    checkpoint = runtime.checkpoint(environment, access, lease)["id"]
    runtime.release(environment, access, lease)

    child = uuid4().hex
    objects.fail_after_put_once = True
    child_budget = _budget(max_lifetime_objects=2, max_put_attempts=2, max_get_attempts=1)
    deadlines = {
        "authorized_expires_at": child_budget.expires_at,
        "authorized_retention_deadline": child_budget.retention_deadline,
        "authorized_cleanup_deadline": child_budget.cleanup_deadline,
    }
    with pytest.raises(TimeoutError):
        runtime.branch(
            environment,
            access,
            checkpoint,
            {},
            new_environment=child,
            hosted_artifact_budget=child_budget,
            **deadlines,
        )
    with pytest.raises(Conflict, match="not yet exposed"):
        runtime.get(child, access)

    result = runtime.branch(
        environment,
        access,
        checkpoint,
        {},
        new_environment=child,
        hosted_artifact_budget=child_budget,
        **deadlines,
    )
    assert result["id"] == child
    assert store.read_artifact(child, access, source["id"])[0] == b"branch-payload"
    with store.transaction() as db:
        intent = db.execute(
            "SELECT status FROM hosted_branch_copies WHERE environment=?", (child,)
        ).fetchone()
        child_usage = db.execute(
            "SELECT put_attempts,lifetime_uploaded_bytes,lifetime_objects,control_response_bytes "
            "FROM artifact_budget_usage WHERE environment=?",
            (child,),
        ).fetchone()
        parent_usage = db.execute(
            "SELECT get_attempts,egress_bytes FROM artifact_budget_usage WHERE environment=?",
            (environment,),
        ).fetchone()
        source_row = db.execute(
            "SELECT sha256,size,media_type,audience FROM artifacts WHERE environment=? AND id=?",
            (environment, source["id"]),
        ).fetchone()
        child_row = db.execute(
            "SELECT sha256,size,media_type,audience FROM artifacts WHERE environment=?",
            (child,),
        ).fetchone()
        parent_get = db.execute(
            "SELECT attempts,status FROM artifact_operations "
            "WHERE environment=? AND kind='get'",
            (environment,),
        ).fetchone()
        child_put = db.execute(
            "SELECT attempts,status,sha256,size FROM artifact_operations "
            "WHERE environment=? AND kind='put'",
            (child,),
        ).fetchone()
    assert intent["status"] == "completed"
    assert (child_usage["put_attempts"], child_usage["lifetime_uploaded_bytes"], child_usage["lifetime_objects"]) == (
        2,
        len(b"branch-payload"),
        1,
    )
    assert parent_usage["get_attempts"] == 2
    assert parent_usage["egress_bytes"] == 2 * (64 + MemoryArtifacts.max_get_overread_bytes)
    assert (parent_get["attempts"], parent_get["status"]) == (2, "committed")
    assert (child_put["attempts"], child_put["status"]) == (2, "committed")
    assert (child_row["sha256"], child_row["size"], child_row["media_type"], child_row["audience"]) == (
        source_row["sha256"],
        source_row["size"],
        source_row["media_type"],
        source_row["audience"],
    )
    assert (child_put["sha256"], child_put["size"]) == (source_row["sha256"], source_row["size"])
    assert child_usage["control_response_bytes"] == 2 * MemoryArtifacts.max_put_response_bytes


def test_hosted_branch_quota_denial_happens_before_parent_read(hosted_store):
    store, objects = hosted_store
    parent_budget = _budget(
        max_lifetime_objects=3,
        max_put_attempts=3,
        max_get_attempts=0,
    )
    environment, access = _new_session(store, parent_budget)
    store.artifact(environment, access, b"source", operation_id="source-artifact")
    runtime = _SessionRuntime(store, SyntheticEnvironment())
    lease = runtime.lease(environment, access, "checkpoint", ttl=30)
    checkpoint = runtime.checkpoint(environment, access, lease)["id"]
    runtime.release(environment, access, lease)
    prior_provider_calls = list(objects.calls)
    child = uuid4().hex
    child_budget = _budget(max_lifetime_objects=2, max_put_attempts=2, max_get_attempts=1)
    with pytest.raises(Conflict, match="GET allowance exhausted"):
        runtime.branch(
            environment,
            access,
            checkpoint,
            {},
            new_environment=child,
            hosted_artifact_budget=child_budget,
            authorized_expires_at=child_budget.expires_at,
            authorized_retention_deadline=child_budget.retention_deadline,
            authorized_cleanup_deadline=child_budget.cleanup_deadline,
        )
    assert objects.calls == prior_provider_calls
    with pytest.raises(Conflict, match="not yet exposed"):
        runtime.get(child, access)

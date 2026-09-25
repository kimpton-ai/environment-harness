"""Authorization boundary: server-owned policies, private contexts, public shape."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import time

import pytest
from _credentials import bearer
from fastapi.testclient import TestClient

import environment_harness
from environment_harness import (
    AgentSpec,
    BranchRequest,
    EnvironmentHarness,
    EnvironmentSession,
    EvidenceStore,
    ExperimentSpec,
    Scenario,
)
from environment_harness import access as access_module
from environment_harness.access import (
    ACTIONS,
    ISSUABLE_POLICIES,
    POLICY_ACTIONS,
    _AccessContext,
    registry_fingerprint,
    trusted_local,
)
from environment_harness.errors import Conflict, Forbidden, Unauthenticated
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.runtime import _SessionRuntime
from environment_harness.server import create_app


def session_fixture(tmp_path):
    """Create one session plus a context for each server-owned policy."""

    environment = SyntheticEnvironment()
    store = EvidenceStore(tmp_path)
    runtime = _SessionRuntime(store, environment)
    local = trusted_local("tenant", "fixture")
    spec = ExperimentSpec(
        environment=environment.spec,
        participants=(
            AgentSpec(id="alice", implementation="synthetic-agent@1", policy_version="1"),
            AgentSpec(id="bob", implementation="synthetic-agent@1", policy_version="1"),
        ),
    )
    identity = runtime.create(spec, local)["id"]
    contexts = {
        "trusted-local": local,
        "management": _AccessContext(tenant="tenant", subject="ops", policy="management"),
        "viewer": _AccessContext(tenant="tenant", subject="viewer", policy="viewer"),
        "participant": runtime.participant_context(identity, local, "alice"),
    }
    return store, runtime, identity, spec, contexts


def test_every_registered_action_belongs_to_at_least_one_policy():
    assert len(ACTIONS) == len(set(ACTIONS))
    for policy, allowed in POLICY_ACTIONS.items():
        assert allowed <= set(ACTIONS), policy
    covered = set().union(*POLICY_ACTIONS.values())
    assert covered == set(ACTIONS)
    # trusted-local is the in-process facade and is never issued to a caller.
    assert "trusted-local" not in ISSUABLE_POLICIES
    assert set(ISSUABLE_POLICIES) < set(POLICY_ACTIONS)


def test_registry_fingerprint_is_stable_and_covers_policy_changes(monkeypatch):
    first = registry_fingerprint()
    assert first == registry_fingerprint()
    monkeypatch.setitem(access_module.POLICY_ACTIONS, "viewer", POLICY_ACTIONS["viewer"] | {"session.create"})
    assert registry_fingerprint() != first


def test_access_contexts_fail_closed_on_unknown_policies_and_actions():
    with pytest.raises(ValueError, match="unknown access policy"):
        _AccessContext(tenant="t", subject="s", policy="superuser")
    with pytest.raises(ValueError, match="require a session and participant"):
        _AccessContext(tenant="t", subject="s", policy="participant")
    with pytest.raises(ValueError, match="require a session and participant"):
        _AccessContext(tenant="t", subject="s", policy="participant", session="a" * 32)

    local = trusted_local("t")
    with pytest.raises(Forbidden, match="unregistered access action"):
        local.require("session.destroy")
    viewer = _AccessContext(tenant="t", subject="v", policy="viewer")
    with pytest.raises(Forbidden, match="policy denies"):
        viewer.require("session.create")
    assert viewer.full_evidence and viewer.scoped_to("anything")
    scoped = _AccessContext(tenant="t", subject="a", policy="participant", session="a" * 32, participant="a")
    assert not scoped.full_evidence
    assert scoped.scoped_to("a" * 32) and not scoped.scoped_to("b" * 32)
    assert scoped.replace(participant="b").participant == "b"


@pytest.mark.parametrize(
    ("policy", "create", "write", "control", "act", "dataset"),
    [
        # Acting as a participant always requires a participant-scoped context,
        # even for the trusted in-process facade.
        ("trusted-local", True, True, True, False, True),
        ("management", True, True, True, False, True),
        ("viewer", False, False, False, False, True),
        ("participant", False, False, False, True, False),
    ],
)
def test_one_private_runtime_enforces_every_entry_point(
    tmp_path, policy, create, write, control, act, dataset
):
    """Trusted local, management, viewer, and participant share one runtime."""

    store, runtime, identity, spec, contexts = session_fixture(tmp_path)
    access = contexts[policy]

    def allowed(call):
        try:
            call()
        except Forbidden:
            return False
        return True

    assert allowed(lambda: runtime.create(spec, access, environment_id="c" * 32)) is create
    assert allowed(lambda: runtime.lease(identity, access, "owner")) is write
    assert allowed(lambda: runtime.cancel(identity, access)) is control
    assert allowed(lambda: runtime.participant_context(identity, access, "alice")) is not False

    observation = runtime.observe(identity, contexts["trusted-local"], "alice")
    from environment_harness.contracts import Action

    action = Action(
        operation_id="act-" + policy,
        participant="alice",
        observation_id=observation["id"],
        revision=observation["revision"],
        payload={"value": 1},
    )
    assert allowed(lambda: runtime.submit(identity, access, action)) is act

    from environment_harness.training import TrainingRepository

    assert allowed(lambda: TrainingRepository(store).list_datasets(access)) is dataset


def test_participant_contexts_cannot_cross_sessions_participants_or_generations(tmp_path):
    store, runtime, identity, spec, contexts = session_fixture(tmp_path)
    local = contexts["trusted-local"]
    alice = contexts["participant"]
    second = runtime.create(spec, local, environment_id="b" * 32)["id"]

    with pytest.raises(Forbidden, match="unavailable"):
        runtime.get(second, alice)
    with pytest.raises(Forbidden, match="private observation"):
        runtime.observe(identity, alice, "bob")

    stale = alice.replace(generation=alice.generation + 1)
    with pytest.raises(Forbidden, match="participant authority expired"):
        runtime.get(identity, stale)

    impostor = alice.replace(subject="attacker")
    with pytest.raises(Forbidden, match="participant authority expired"):
        runtime.get(identity, impostor)

    outsider = local.replace(tenant="other")
    with pytest.raises(Forbidden, match="unavailable"):
        runtime.get(identity, outsider)


def test_public_api_exposes_no_principal_access_context_or_permission(tmp_path):
    forbidden = ("principal", "access_context", "accesscontext", "role", "roles", "permission")
    for name in environment_harness.__all__:
        exported = getattr(environment_harness, name)
        assert "_AccessContext" != name
        assert not any(token in name.lower() for token in ("principal", "permission"))
        if not callable(exported):
            continue
        members = [(name, exported)]
        if isinstance(exported, type):
            members = [
                (attribute, getattr(exported, attribute))
                for attribute in dir(exported)
                if not attribute.startswith("_") and callable(getattr(exported, attribute, None))
            ]
        for attribute, member in members:
            try:
                signature = inspect.signature(member)
            except (TypeError, ValueError):
                continue
            for parameter in signature.parameters:
                assert not any(token in parameter.lower() for token in forbidden), (name, attribute)

    # The authorization-aware runtime is private and is not re-exported.
    assert "_SessionRuntime" not in environment_harness.__all__
    assert not issubclass(EnvironmentSession, _SessionRuntime)
    # Only the facade starts or resumes local execution.
    assert not hasattr(environment_harness, "run")
    assert callable(EnvironmentHarness.start) and callable(EnvironmentHarness.run)


def test_trusted_local_sdk_requires_no_credential_and_issues_scoped_ones(tmp_path):
    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=SyntheticEnvironment,
        agent_factories={"alice": SyntheticAgent},
    )
    session = harness.run(Scenario(id="local", input={}), turns=2)
    assert session.status == "succeeded"
    assert harness.hierarchy()["summary"] == {"running": 0, "queued": 0, "failed": 0}
    assert session.verify()["events"] > 0
    assert session.records(limit=5).records

    management = harness.management_credential()
    viewer = harness.viewer_credential()
    participant = session.participant_credential("alice")
    resolved = {token: harness.store.authenticate(token) for token in (management, viewer, participant)}
    assert [context.policy for context in resolved.values()] == ["management", "viewer", "participant"]
    assert resolved[participant].session == session.id
    assert resolved[participant].participant == "alice"

    with pytest.raises(Conflict, match="unknown participant"):
        session.participant_credential("nobody")


def test_participant_credentials_are_bound_and_do_not_enumerate(tmp_path):
    store, runtime, identity, spec, contexts = session_fixture(tmp_path)
    client = TestClient(create_app(runtime), base_url="http://testserver")
    scoped = {"Authorization": "Bearer " + bearer(store, contexts["participant"])}
    management = {"Authorization": "Bearer " + bearer(store, contexts["management"])}

    assert client.get(f"/v1/environments/{identity}", headers=scoped).status_code == 200
    other = runtime.create(spec, contexts["trusted-local"], environment_id="d" * 32)["id"]
    denied = client.get(f"/v1/environments/{other}", headers=scoped)
    missing = client.get(f"/v1/environments/{'e' * 32}", headers=scoped)
    # An out-of-scope session and an absent session are indistinguishable.
    assert denied.status_code == missing.status_code == 403
    assert denied.json()["error"]["message"] == missing.json()["error"]["message"]

    # A participant credential cannot mint another credential.
    assert (
        client.post(
            f"/v1/environments/{identity}/credentials", headers=scoped, json={"participant": "bob"}
        ).status_code
        == 403
    )
    issued = client.post(
        f"/v1/environments/{identity}/credentials", headers=management, json={"participant": "bob"}
    )
    assert issued.status_code == 200
    resolved = store.authenticate(issued.json()["token"])
    assert (resolved.policy, resolved.session, resolved.participant) == ("participant", identity, "bob")


def test_requests_cannot_assert_a_policy_or_permission(tmp_path):
    store, runtime, identity, _spec, contexts = session_fixture(tmp_path)
    client = TestClient(create_app(runtime), base_url="http://testserver")
    scoped = {"Authorization": "Bearer " + bearer(store, contexts["participant"])}
    for attempt in (
        {"participant": "bob", "policy": "management"},
        {"participant": "bob", "permissions": ["session.create"]},
        {"participant": "bob", "role": "researcher"},
    ):
        response = client.post(f"/v1/environments/{identity}/credentials", headers=scoped, json=attempt)
        # Unknown request fields are rejected before authorization is consulted.
        assert response.status_code in (403, 422)
    assert client.get("/v1/environments?policy=management", headers=scoped).status_code == 200


def test_credential_store_never_trusts_a_client_supplied_policy(tmp_path):
    store = EvidenceStore(tmp_path)
    with pytest.raises(Forbidden, match="cannot be issued"):
        store._issue(trusted_local("tenant"), 60)
    with pytest.raises(ValueError, match="token lifetime"):
        store.issue_management("tenant", ttl=0)
    token = store.issue_management("tenant", "ops")
    with store.transaction() as db:
        row = db.execute(
            "SELECT * FROM credentials WHERE hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
        ).fetchone()
    assert row["policy"] == "management" and row["session"] is None
    with pytest.raises(Unauthenticated, match="expired or invalid"):
        store.authenticate("not-a-credential")


def test_numbered_credential_migration_deletes_legacy_rows_and_forces_reissue(tmp_path):
    """The 0.3.0rc1 migration discards every pre-change principal row."""

    root = tmp_path / "legacy"
    root.mkdir()
    database = root / "evidence.sqlite"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE credentials (hash TEXT PRIMARY KEY, principal TEXT NOT NULL,"
            " expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute("CREATE TABLE events (environment TEXT, seq INTEGER, hash TEXT)")
        db.execute("INSERT INTO events VALUES ('env',1,'preserved')")
        db.execute(
            "INSERT INTO credentials VALUES (?,?,?,0)",
            (
                hashlib.sha256(b"legacy-token").hexdigest(),
                json.dumps({"tenant": "local", "subject": "old", "role": "researcher"}),
                time.time() + 86400,
            ),
        )

    store = EvidenceStore(root)
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM credentials").fetchone()[0] == 0
        assert {row["name"] for row in db.execute("PRAGMA table_info(credentials)")} == {
            "hash",
            "tenant",
            "subject",
            "policy",
            "session",
            "participant",
            "generation",
            "expires",
            "revoked",
        }
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT count(*) FROM schema_migrations WHERE version='005_credential_policies'"
            ).fetchone()[0]
            == 1
        )

    with pytest.raises(Unauthenticated):
        store.authenticate("legacy-token")
    reissued = store.issue_management("local")
    assert store.authenticate(reissued).policy == "management"

    # Reconciliation is idempotent: a second open applies nothing further.
    EvidenceStore(root)
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1


def test_postgres_credential_migration_is_numbered_and_transactional():
    from importlib.resources import files

    migration = files("environment_harness").joinpath("migrations/005_credential_policies.sql")
    body = migration.read_text()
    assert "DROP TABLE IF EXISTS credentials" in body
    assert "policy TEXT NOT NULL" in body and "session TEXT" in body
    assert "principal" not in body.split("--")[-1]


def test_branch_request_creates_a_child_session_with_lineage(tmp_path):
    from environment_harness.runner import run

    def three(session, environment, access, agents, *, turns):
        return run(session, environment, access, agents, turns=1)

    harness = EnvironmentHarness(
        tmp_path,
        environment_factory=SyntheticEnvironment,
        agent_factories={"alice": SyntheticAgent},
        session_runner=three,
    )
    parent = harness.run(Scenario(id="lineage", input={}), turns=4)
    checkpoint = parent.checkpoint(exact_agents=True)
    assert [item["id"] for item in parent.checkpoints()] == [checkpoint["id"]]

    request = BranchRequest(checkpoint=checkpoint["id"], interventions={"total": 7}, idempotency_key="once")
    child = parent.branch(request)
    assert parent.branch(request).id == child.id
    record = child.record()
    assert record["parent"] == parent.id and record["checkpoint"] == checkpoint["id"]
    assert child.experiment_id == parent.experiment_id
    assert child.status == "interrupted"

    conflicting = BranchRequest(
        checkpoint=checkpoint["id"], interventions={"total": 9}, idempotency_key="once"
    )
    with pytest.raises(Conflict, match="branch ID reused"):
        parent.branch(conflicting)

    child.advance(turns=1)
    assert child.observation("alice")["payload"]["total"] == 8

    # No Branch resource exists in the public contract surface.
    assert not any(
        "branch" in name.lower() for name in environment_harness.__all__ if name != "BranchRequest"
    )

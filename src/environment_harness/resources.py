"""Portable authoring and run resources projected from authoritative evidence.

This module holds the read side of the contract family: `ScenarioSet`,
`Experiment`, `Session`, and `Checkpoint`. They use the same
`environmentharness.dev/v1alpha1` envelope, registry, canonical encoder, and
unknown-field rules as `Trajectory`.

These are **resources**, not handles. The execution facade in
`environment_harness` (``EnvironmentHarness``, its ``Experiment`` builder, and
its ``EnvironmentSession`` handle) drives local work; the classes here are the
immutable projections a downstream consumer reads, serializes, and digests. A
Checkpoint is not a `TrajectorySnapshot`: the former may contain private opaque
continuation state and exists to resume or branch execution, while the latter is
an authorized evidence projection for repeatable inspection or export.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import Json
from .trajectories import (
    API_VERSION,
    LifecycleStatus,
    PortableRecord,
    ResourceFeatures,
    ResourceMetadata,
    TerminationStatus,
    VerifiedOutcome,
)

__all__ = [
    "API_VERSION",
    "Checkpoint",
    "CheckpointSpec",
    "CheckpointStatus",
    "CredentialRequirement",
    "EnvironmentReferenceModel",
    "Experiment",
    "ExperimentPlan",
    "ExperimentStatus",
    "MetricDeclaration",
    "ParticipantBinding",
    "ResourceReference",
    "ScenarioReference",
    "ScenarioSet",
    "ScenarioSetSpec",
    "ScenarioSetStatus",
    "ScorerReference",
    "Session",
    "SessionSpec",
    "SessionStatus",
]


class ResourceReference(PortableRecord):
    """Enough identity to avoid mutable-name ambiguity across resources."""

    kind: str = Field(min_length=1, max_length=100)
    id: str = Field(min_length=1, max_length=200)
    api_version: str = Field(alias="apiVersion", default=API_VERSION, min_length=1, max_length=200)
    digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision: int | None = Field(default=None, ge=0)
    #: A human-readable label never participates as authority.
    label: str | None = Field(default=None, max_length=500)


class EnvironmentReferenceModel(PortableRecord):
    """The portable environment identity that is serialized in every resource.

    Executable code is resolved from typed factories supplied to one harness;
    no resource carries an import path, callable, or provider object.
    """

    id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=200)
    spec_digest: str = Field(alias="specDigest", pattern=r"^[0-9a-f]{64}$")


class CredentialRequirement(PortableRecord):
    """A named credential requirement. Values never appear in portable JSON."""

    name: str = Field(min_length=1, max_length=200)
    consumer: str = Field(min_length=1, max_length=100)
    provider: str | None = Field(default=None, max_length=200)
    required: bool = True
    injection: str = Field(default="process-local", min_length=1, max_length=100)
    preflight: bool = False


class ScenarioReference(PortableRecord):
    """One immutable scenario selected into a scenario set."""

    id: str = Field(min_length=1, max_length=200)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: str | None = Field(default=None, max_length=100)
    labels: dict[str, str] = Field(default_factory=dict)


class MetricDeclaration(PortableRecord):
    """A declared aggregate metric. Executable scoring code stays installed."""

    id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=200)
    unit: str = Field(min_length=1, max_length=200)


class ScenarioSetSpec(PortableRecord):
    name: str = Field(min_length=1, max_length=200)
    scenarios: tuple[ScenarioReference, ...] = Field(min_length=1)
    schema_version: str = Field(alias="schemaVersion", min_length=1, max_length=200)
    provenance: Json = Field(default_factory=dict)
    metrics: tuple[MetricDeclaration, ...] = ()

    @model_validator(mode="after")
    def unique_scenarios(self):
        identities = [scenario.id for scenario in self.scenarios]
        if len(identities) != len(set(identities)):
            raise ValueError("scenario IDs must be unique within a scenario set")
        return self


class ScenarioSetStatus(PortableRecord):
    scenario_count: int = Field(alias="scenarioCount", ge=1)
    set_digest: str = Field(alias="setDigest", pattern=r"^[0-9a-f]{64}$")


class ScenarioSet(BaseModel):
    """An ordered, digest-bound collection of scenarios used as experiment input.

    `ScenarioSet` names the *input* collection. `TrajectoryDataset` keeps its own
    meaning as a dataset of completed trajectory snapshots; the two are
    deliberately different types.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    api_version: Literal["environmentharness.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["ScenarioSet"]
    metadata: ResourceMetadata
    features: ResourceFeatures
    spec: ScenarioSetSpec
    status: ScenarioSetStatus
    extensions: Json

    @model_validator(mode="after")
    def counts_are_consistent(self):
        if self.status.scenario_count != len(self.spec.scenarios):
            raise ValueError("scenario count does not match the selection")
        json.dumps(self.model_dump(mode="json", by_alias=True), allow_nan=False)
        return self


class ParticipantBinding(PortableRecord):
    """One actor in an experiment, bound to a policy or adapter reference."""

    id: str = Field(min_length=1, max_length=200)
    implementation: str = Field(min_length=1, max_length=500)
    policy_version: str = Field(alias="policyVersion", min_length=1, max_length=200)
    config_digest: str = Field(alias="configDigest", pattern=r"^[0-9a-f]{64}$")
    checkpoint: bool = False
    credentials: tuple[CredentialRequirement, ...] = ()


class ScorerReference(PortableRecord):
    id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=200)


class ExperimentPlan(PortableRecord):
    """The frozen plan. ``EnvironmentSpec`` is embedded, not a separate resource."""

    name: str = Field(min_length=1, max_length=500)
    environment: Json
    environment_reference: EnvironmentReferenceModel = Field(alias="environmentReference")
    scenario_set: ResourceReference = Field(alias="scenarioSet")
    participants: tuple[ParticipantBinding, ...] = Field(min_length=1)
    scorers: tuple[ScorerReference, ...] = ()
    trials: int = Field(ge=1)
    seed: int
    turns: int = Field(ge=1)
    purpose: str = Field(min_length=1, max_length=100)
    split: str = Field(min_length=1, max_length=100)
    limits: Json = Field(default_factory=dict)
    credentials: tuple[CredentialRequirement, ...] = ()


class ExperimentProgress(PortableRecord):
    planned: int = Field(ge=0)
    created: int = Field(ge=0)
    pending: int = Field(ge=0)
    running: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    uncertain: int = Field(ge=0)


class ExperimentStatus(PortableRecord):
    """A derived aggregate projection, never an execution authority."""

    state: str = Field(min_length=1, max_length=100)
    progress: ExperimentProgress
    started_at: str | None = Field(alias="startedAt", default=None)
    last_activity_at: str | None = Field(alias="lastActivityAt", default=None)
    lock_digest: str = Field(alias="lockDigest", pattern=r"^[0-9a-f]{64}$")
    scores: Json = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()


class Experiment(BaseModel):
    """A frozen plan spanning an environment, scenarios, participants, and trials.

    This is the portable read resource. The local execution builder returned by
    ``EnvironmentHarness.experiment(...)`` is a different object.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    api_version: Literal["environmentharness.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["Experiment"]
    metadata: ResourceMetadata
    features: ResourceFeatures
    spec: ExperimentPlan
    status: ExperimentStatus
    extensions: Json

    @model_validator(mode="after")
    def json_serializable(self):
        json.dumps(self.model_dump(mode="json", by_alias=True), allow_nan=False)
        return self


class SessionLineage(PortableRecord):
    """Parent, Checkpoint, and intervention references for a branched child."""

    root: str = Field(min_length=1, max_length=200)
    parent: str | None = Field(default=None, max_length=200)
    checkpoint: str | None = Field(default=None, max_length=200)
    interventions: Json = Field(default_factory=dict)


class SessionSpec(PortableRecord):
    experiment: ResourceReference
    scenario: ScenarioReference
    trial: int = Field(ge=0)
    seed: int
    turns: int = Field(ge=1)
    environment: Json
    environment_reference: EnvironmentReferenceModel = Field(alias="environmentReference")
    participants: tuple[ParticipantBinding, ...] = Field(min_length=1)
    lineage: SessionLineage
    purpose: str = Field(min_length=1, max_length=100)
    split: str = Field(min_length=1, max_length=100)


class SessionStatus(PortableRecord):
    """Independent status dimensions. None of them implies another.

    A finished process, an accepted command, or a completed ingestion batch never
    proves task success.
    """

    collection: LifecycleStatus
    execution: LifecycleStatus
    termination: TerminationStatus
    verified_outcome: VerifiedOutcome = Field(alias="verifiedOutcome")
    revision: int = Field(ge=0)
    evidence_sequence: int = Field(alias="evidenceSequence", ge=0)
    evidence_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    score_readiness: str = Field(alias="scoreReadiness", min_length=1, max_length=100)
    reports: tuple[ResourceReference, ...] = ()
    artifacts: tuple[ResourceReference, ...] = ()
    trajectory: ResourceReference | None = None
    reconciliation: str | None = Field(default=None, max_length=500)


class Session(BaseModel):
    """One concrete attempt for one scenario and trial within an Experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    api_version: Literal["environmentharness.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["Session"]
    metadata: ResourceMetadata
    features: ResourceFeatures
    spec: SessionSpec
    status: SessionStatus
    extensions: Json

    @model_validator(mode="after")
    def json_serializable(self):
        json.dumps(self.model_dump(mode="json", by_alias=True), allow_nan=False)
        return self


class ParticipantContinuation(PortableRecord):
    """Whether one participant can continue exactly from this Checkpoint."""

    id: str = Field(min_length=1, max_length=200)
    implementation: str = Field(min_length=1, max_length=500)
    policy_version: str = Field(alias="policyVersion", min_length=1, max_length=200)
    exact: bool
    unavailable: tuple[str, ...] = ()


class CheckpointSpec(PortableRecord):
    session: ResourceReference
    revision: int = Field(ge=0)
    environment_reference: EnvironmentReferenceModel = Field(alias="environmentReference")
    participants: tuple[ParticipantContinuation, ...] = Field(min_length=1)
    #: Opaque continuation state is referenced, never inlined into the envelope.
    #: Required and digest-shaped, which is what guarantees a resumable or
    #: branchable Checkpoint always carries continuation state.
    state_reference: str = Field(alias="stateReference", pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[ResourceReference, ...] = ()
    evidence_sequence: int = Field(alias="evidenceSequence", ge=0)
    evidence_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CheckpointStatus(PortableRecord):
    exact: bool
    resumable: bool
    branchable: bool
    limitations: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    checkpoint_digest: str = Field(alias="checkpointDigest", pattern=r"^[0-9a-f]{64}$")


class Checkpoint(BaseModel):
    """Immutable resumable state for one Session revision.

    A Checkpoint exists to resume or branch execution and may reference private
    opaque continuation state. It is not an authorized evidence projection; use
    `TrajectorySnapshot` for repeatable inspection and export.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    api_version: Literal["environmentharness.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["Checkpoint"]
    metadata: ResourceMetadata
    features: ResourceFeatures
    spec: CheckpointSpec
    status: CheckpointStatus
    extensions: Json

    @model_validator(mode="after")
    def continuation_is_consistent(self):
        if self.status.exact and not all(item.exact for item in self.spec.participants):
            raise ValueError("an exact checkpoint requires exact participant continuation")
        json.dumps(self.model_dump(mode="json", by_alias=True), allow_nan=False)
        return self


def reference(kind: str, identity: str, **fields: Any) -> ResourceReference:
    """Build one cross-resource reference with explicit identity."""

    return ResourceReference.model_validate({"kind": kind, "id": identity, **fields})


def _timestamp(value: float | None) -> str | None:
    from datetime import UTC, datetime

    if value is None:
        return None
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


#: Durable local scheduler states projected onto the portable execution
#: dimension. These are documented, evolvable strings: an unknown value is
#: preserved rather than coerced.
_EXECUTION_STATES = {
    "queued": "pending",
    "running": "active",
    "succeeded": "completed",
    "failed": "failed",
    "stopped": "cancelled",
    "interrupted": "interrupted",
    "blocked": "blocked",
}


class ResourceProjection:
    """Read portable experiment resources from the authoritative store.

    Projections never rewrite a stored manifest, evidence hash, or scenario
    snapshot. Missing historical information stays explicitly unavailable.
    """

    def __init__(self, store):
        self.store = store

    def _index_scope(self, access) -> str | None:
        """Return the owning experiment a session-scoped context may index.

        A credential bound to one Session cannot enumerate its tenant's other
        experiments, scenario sets, or sessions.
        """

        if access.session is None:
            return None
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT experiment FROM session_runs WHERE environment=? AND tenant=?",
                (access.session, access.tenant),
            ).fetchone()
        if row is None or row["experiment"] is None:
            from .errors import Forbidden

            raise Forbidden("credential policy denies this operation")
        return row["experiment"]

    # -- ScenarioSet --------------------------------------------------------
    def _scenario_references(self, db, experiment: str) -> tuple[ScenarioReference, ...]:
        from .store import digest

        rows = db.execute(
            "SELECT scenario,body FROM scenario_snapshots WHERE experiment=? ORDER BY position",
            (experiment,),
        ).fetchall()
        return tuple(
            ScenarioReference.model_validate(
                {"id": row["scenario"], "digest": digest(json.loads(row["body"]))}
            )
            for row in rows
        )

    def _scenario_set(self, db, experiment: str, config: Json) -> ScenarioSet:
        from .store import digest

        scenarios = self._scenario_references(db, experiment)
        if not scenarios:
            raise ValueError("experiment has no frozen scenario snapshots")
        environment = config.get("environment", {})
        spec = {
            "name": config.get("name") or experiment,
            "scenarios": [item.model_dump(mode="json", by_alias=True) for item in scenarios],
            "schemaVersion": environment.get("protocol", "environment-session.v1"),
            "provenance": {"experiment": experiment, "source": "environment-harness.local-store"},
            "metrics": [],
        }
        set_digest = digest(spec)
        return ScenarioSet.model_validate(
            {
                "apiVersion": API_VERSION,
                "kind": "ScenarioSet",
                "metadata": {
                    "id": "scenario-set-" + set_digest[:32],
                    "createdAt": _timestamp(0.0),
                    "labels": {"experiment": experiment},
                },
                "features": {"required": [], "optional": []},
                "spec": spec,
                "status": {"scenarioCount": len(scenarios), "setDigest": set_digest},
                "extensions": {},
            }
        )

    def scenario_sets(self, access, *, limit: int = 100) -> tuple[ScenarioSet, ...]:
        access.require("session.read")
        if not 1 <= limit <= 1000:
            raise ValueError("invalid scenario-set page size")
        scope = self._index_scope(access)
        found: dict[str, ScenarioSet] = {}
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT id,name,config,created FROM experiments WHERE tenant=? "
                "AND (CAST(? AS TEXT) IS NULL OR id=?) ORDER BY created DESC",
                (access.tenant, scope, scope),
            ).fetchall()
            for row in rows:
                try:
                    projected = self._scenario_set(
                        db, row["id"], json.loads(row["config"]) | {"name": row["name"]}
                    )
                except ValueError:
                    continue
                found.setdefault(projected.metadata.id, projected)
                if len(found) >= limit:
                    break
        return tuple(found.values())

    def scenario_set(self, identity: str, access) -> ScenarioSet:
        from .errors import Forbidden

        for projected in self.scenario_sets(access, limit=1000):
            if projected.metadata.id == identity:
                return projected
        raise Forbidden("scenario set unavailable")

    # -- Experiment ---------------------------------------------------------
    def experiment(self, identity: str, access) -> Experiment:
        from .errors import Forbidden
        from .store import digest

        access.require("session.read")
        scope = self._index_scope(access)
        if scope is not None and scope != identity:
            raise Forbidden("experiment unavailable")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM experiments WHERE id=? AND tenant=?", (identity, access.tenant)
            ).fetchone()
            if row is None:
                raise Forbidden("experiment unavailable")
            config = json.loads(row["config"])
            scenario_set = self._scenario_set(db, identity, config | {"name": row["name"]})
            counts = {
                item["status"]: item["count"]
                for item in db.execute(
                    "SELECT status,count(*) AS count FROM session_runs WHERE experiment=? GROUP BY status",
                    (identity,),
                ).fetchall()
            }
            latest = db.execute(
                "SELECT max(updated) AS updated FROM session_runs WHERE experiment=?", (identity,)
            ).fetchone()["updated"]
            metrics = self._score_summary(db, identity)
        environment = config["environment"]
        declared = config.get("environment_reference") or {
            "id": environment["id"],
            "version": environment["version"],
            "spec_digest": digest(environment),
        }
        plan = {
            "name": row["name"],
            "environment": environment,
            "environmentReference": {
                "id": declared["id"],
                "version": declared["version"],
                "specDigest": declared["spec_digest"],
            },
            "scenarioSet": {
                "kind": "ScenarioSet",
                "id": scenario_set.metadata.id,
                "digest": scenario_set.status.set_digest,
            },
            "participants": [
                {
                    "id": participant["id"],
                    "implementation": participant["implementation"],
                    "policyVersion": participant["policy_version"],
                    "configDigest": digest(participant.get("config", {})),
                    "checkpoint": bool(participant.get("checkpoint")),
                }
                for participant in config["participants"]
            ],
            "scorers": [
                {"id": item.rsplit("@", 1)[0], "version": item.rsplit("@", 1)[-1]}
                for item in config.get("scoring_versions", [])
                if "@" in item
            ],
            "trials": row["trials"],
            "seed": row["seed"],
            "turns": config["execution"]["turns"],
            # An Experiment's purpose and split are frozen per Session; the
            # default below is replaced from the first Session's manifest.
            "purpose": "evaluation",
            "split": "heldout",
            "limits": config["policy"],
            "credentials": [],
        }
        # Purpose and split are frozen per Session; project the experiment's own
        # declaration when a Session already recorded one.
        with self.store.transaction() as db:
            sample = db.execute(
                "SELECT environment FROM session_runs WHERE experiment=? ORDER BY created LIMIT 1",
                (identity,),
            ).fetchone()
            if sample is not None:
                manifest = db.execute(
                    "SELECT manifest FROM environments WHERE id=?", (sample["environment"],)
                ).fetchone()
                if manifest is not None:
                    frozen = json.loads(manifest["manifest"])
                    plan["purpose"] = frozen["purpose"]
                    plan["split"] = frozen["split"]
        total = row["total"]
        created = sum(counts.values())
        progress = {
            "planned": total,
            "created": created,
            "pending": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "completed": counts.get("succeeded", 0),
            "failed": counts.get("failed", 0),
            "cancelled": counts.get("stopped", 0),
            "uncertain": counts.get("interrupted", 0) + counts.get("blocked", 0),
        }
        limitations = []
        if counts.get("blocked"):
            limitations.append("one or more sessions are blocked on an unavailable environment factory")
        if counts.get("interrupted"):
            limitations.append("one or more sessions were interrupted and require explicit resume")
        return Experiment.model_validate(
            {
                "apiVersion": API_VERSION,
                "kind": "Experiment",
                "metadata": {
                    "id": identity,
                    "createdAt": _timestamp(row["created"]),
                    "labels": {},
                },
                "features": {"required": [], "optional": []},
                "spec": plan,
                "status": {
                    "state": row["status"],
                    "progress": progress,
                    "startedAt": _timestamp(row["created"]),
                    "lastActivityAt": _timestamp(latest),
                    "lockDigest": digest(plan),
                    "scores": metrics,
                    "limitations": limitations,
                },
                "extensions": {},
            }
        )

    def experiments(self, access, *, limit: int = 100) -> tuple[Experiment, ...]:
        access.require("session.read")
        if not 1 <= limit <= 1000:
            raise ValueError("invalid experiment page size")
        scope = self._index_scope(access)
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT id FROM experiments WHERE tenant=? AND (CAST(? AS TEXT) IS NULL OR id=?) "
                "ORDER BY created DESC LIMIT ?",
                (access.tenant, scope, scope, limit),
            ).fetchall()
        return tuple(self.experiment(row["id"], access) for row in rows)

    @staticmethod
    def _score_summary(db, experiment: str) -> Json:
        values: dict[str, list[float]] = {}
        for row in db.execute(
            "SELECT environment FROM session_runs WHERE experiment=?", (experiment,)
        ).fetchall():
            latest = db.execute(
                "SELECT body FROM reports WHERE environment=? ORDER BY revision DESC LIMIT 1",
                (row["environment"],),
            ).fetchone()
            if latest is None:
                continue
            for key, value in json.loads(latest["body"]).get("metrics", {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values.setdefault(key, []).append(float(value))
        return {key: sum(items) / len(items) for key, items in sorted(values.items())}

    # -- Session ------------------------------------------------------------
    def session(self, identity: str, access) -> Session:
        from .errors import Forbidden
        from .store import digest

        with self.store.transaction() as db:
            run = db.execute(
                "SELECT * FROM session_runs WHERE environment=? AND tenant=?",
                (identity, access.tenant),
            ).fetchone()
            if run is None:
                # The same message as the store so a missing and an
                # inaccessible session stay indistinguishable.
                raise Forbidden("environment unavailable")
            row = self.store.environment(db, identity, access, "session.read")
            manifest = json.loads(row["manifest"])
            evidence = db.execute(
                "SELECT seq,hash FROM events WHERE environment=? ORDER BY seq DESC LIMIT 1",
                (identity,),
            ).fetchone()
            reports = db.execute(
                "SELECT revision,hash FROM reports WHERE environment=? ORDER BY revision", (identity,)
            ).fetchall()
            artifacts = db.execute(
                "SELECT id,sha256 FROM artifacts WHERE environment=? ORDER BY id", (identity,)
            ).fetchall()
            scenario_body = json.loads(run["scenario_body"])
            transition = db.execute(
                "SELECT body FROM events WHERE environment=? AND kind='transition.committed' "
                "ORDER BY seq DESC LIMIT 1",
                (identity,),
            ).fetchone()
        outcome = json.loads(transition["body"]) if transition is not None else {}
        environment = manifest["environment"]
        execution = _EXECUTION_STATES.get(run["status"], run["status"])
        terminal = bool(outcome.get("terminated") or outcome.get("truncated"))
        spec = {
            "experiment": {
                "kind": "Experiment",
                "id": run["experiment"] or "experiment-of-one:" + identity,
            },
            "scenario": {"id": run["scenario"], "digest": digest(scenario_body)},
            "trial": run["trial"],
            "seed": run["seed"],
            "turns": run["target_turns"],
            "environment": environment,
            "environmentReference": {
                "id": run["environment_id"] or environment["id"],
                "version": run["environment_version"] or environment["version"],
                "specDigest": run["spec_digest"] or digest(environment),
            },
            "participants": [
                {
                    "id": participant["id"],
                    "implementation": participant["implementation"],
                    "policyVersion": participant["policy_version"],
                    "configDigest": digest(participant.get("config", {})),
                    "checkpoint": bool(participant.get("checkpoint")),
                }
                for participant in manifest["participants"]
            ],
            "lineage": {
                "root": row["lineage"],
                "parent": row["parent"],
                "checkpoint": row["checkpoint"],
                "interventions": manifest.get("interventions", {}),
            },
            "purpose": manifest["purpose"],
            "split": manifest["split"],
        }
        return Session.model_validate(
            {
                "apiVersion": API_VERSION,
                "kind": "Session",
                "metadata": {
                    "id": identity,
                    "createdAt": _timestamp(run["created"]),
                    "labels": {},
                },
                "features": {"required": [], "optional": []},
                "spec": spec,
                "status": {
                    "collection": {"state": "complete" if terminal else "current"},
                    "execution": {"state": execution},
                    "termination": {
                        "terminated": bool(outcome.get("terminated")),
                        "truncated": bool(outcome.get("truncated")),
                        "reason": outcome.get("reason")
                        or (run["blocked_reason"] if run["status"] == "blocked" else None)
                        or ("completed" if terminal else "not_terminated"),
                    },
                    "verifiedOutcome": {
                        "state": "pending" if row["status"] == "outcomes_pending" else "unavailable",
                        "evidence": [],
                    },
                    "revision": row["revision"],
                    "evidenceSequence": evidence["seq"] if evidence is not None else 0,
                    "evidenceHead": evidence["hash"] if evidence is not None else None,
                    "scoreReadiness": "reported" if reports else "unreported",
                    "reports": [
                        {"kind": "ScoreReport", "id": str(item["revision"]), "digest": item["hash"]}
                        for item in reports
                    ],
                    "artifacts": [
                        {"kind": "Artifact", "id": item["id"], "digest": item["sha256"]} for item in artifacts
                    ],
                    "trajectory": {"kind": "Trajectory", "id": "trajectory-" + identity},
                    "reconciliation": run["blocked_reason"],
                },
                "extensions": {},
            }
        )

    def sessions(self, access, *, experiment: str | None = None, limit: int = 100) -> tuple[Session, ...]:
        access.require("session.read")
        if not 1 <= limit <= 1000:
            raise ValueError("invalid session page size")
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT environment FROM session_runs WHERE tenant=? "
                "AND (CAST(? AS TEXT) IS NULL OR experiment=?) "
                "AND (CAST(? AS TEXT) IS NULL OR environment=?) ORDER BY created DESC LIMIT ?",
                (access.tenant, experiment, experiment, access.session, access.session, limit),
            ).fetchall()
        return tuple(self.session(row["environment"], access) for row in rows)

    # -- Checkpoint ---------------------------------------------------------
    def checkpoint(self, session: str, identity: str, access) -> Checkpoint:
        from .errors import Forbidden
        from .store import digest

        with self.store.transaction() as db:
            row = self.store.environment(db, session, access, "session.read")
            saved = db.execute(
                "SELECT * FROM checkpoints WHERE environment=? AND id=?", (session, identity)
            ).fetchone()
            if saved is None:
                raise Forbidden("checkpoint unavailable")
            body = json.loads(saved["body"])
        manifest = json.loads(body["manifest"])
        environment = manifest["environment"]
        frozen = json.loads(row["manifest"])["environment"]
        members = json.loads(body["participants"])
        participants = []
        unavailable: list[str] = []
        for participant in manifest["participants"]:
            member = members.get(participant["id"], {})
            exact = member.get("agent_state") is not None
            missing = () if exact else ("participant continuation state",)
            if not exact:
                unavailable.append(f"{participant['id']}: continuation state unavailable")
            participants.append(
                {
                    "id": participant["id"],
                    "implementation": participant["implementation"],
                    "policyVersion": participant["policy_version"],
                    "exact": exact,
                    "unavailable": list(missing),
                }
            )
        capabilities = environment["capabilities"]
        spec = {
            "session": {"kind": "Session", "id": session, "revision": body["revision"]},
            "revision": body["revision"],
            "environmentReference": {
                "id": environment["id"],
                "version": environment["version"],
                "specDigest": digest(frozen),
            },
            "participants": participants,
            "stateReference": digest(json.loads(body["state"])),
            "artifacts": [
                {"kind": "Artifact", "id": item["id"], "digest": item["sha256"]}
                for item in body.get("artifacts", [])
            ],
            "evidenceSequence": body.get("evidence_cursor") or 0,
            "evidenceHead": body.get("evidence_head"),
        }
        limitations = []
        if body.get("operations"):
            limitations.append("unsettled external operations remain in the checkpoint")
        return Checkpoint.model_validate(
            {
                "apiVersion": API_VERSION,
                "kind": "Checkpoint",
                "metadata": {
                    "id": identity,
                    "createdAt": _timestamp(0.0),
                    "labels": {"session": session},
                },
                "features": {"required": [], "optional": []},
                "spec": spec,
                "status": {
                    "exact": bool(body.get("exact_agents")),
                    "resumable": bool(capabilities["resume"]),
                    "branchable": bool(capabilities["branch"]) and not body.get("operations"),
                    "limitations": limitations,
                    "unavailable": unavailable,
                    "checkpointDigest": saved["hash"],
                },
                "extensions": {},
            }
        )

    def checkpoints(self, session: str, access, *, limit: int = 100) -> tuple[Checkpoint, ...]:
        with self.store.transaction() as db:
            self.store.environment(db, session, access, "session.read")
            rows = db.execute(
                "SELECT id FROM checkpoints WHERE environment=? ORDER BY revision,id LIMIT ?",
                (session, limit),
            ).fetchall()
        return tuple(self.checkpoint(session, row["id"], access) for row in rows)


class PolicyProjection:
    """Derive `Policy` resources from frozen participant specifications.

    Existing stores synthesize a policy reference at read time; no manifest or
    evidence row is rewritten. ``AgentSpec.policy_version`` keeps its persisted
    meaning and supplies the default version.
    """

    def __init__(self, store):
        self.store = store

    def _policies(self, access, *, session: str | None = None):
        from .store import digest
        from .trajectories import API_VERSION as VERSION
        from .trajectories import Policy

        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT id,manifest FROM environments WHERE tenant=? "
                "AND (CAST(? AS TEXT) IS NULL OR id=?) ORDER BY id",
                (access.tenant, session or access.session, session or access.session),
            ).fetchall()
        found: dict[str, Any] = {}
        for row in rows:
            manifest = json.loads(row["manifest"])
            for participant in manifest.get("participants", []):
                body = {
                    "implementation": participant["implementation"],
                    "version": participant.get("policy_version"),
                    "lineage": [],
                    "artifact": None,
                }
                identity = "policy-" + digest(body)[:32]
                found.setdefault(
                    identity,
                    Policy.model_validate(
                        {
                            "apiVersion": VERSION,
                            "kind": "Policy",
                            "metadata": {
                                "id": identity,
                                "createdAt": _timestamp(0.0),
                                "labels": {"participant": participant["id"]},
                            },
                            "features": {"required": [], "optional": []},
                            "spec": body,
                            "status": {"digest": digest(body)},
                            "extensions": {},
                        }
                    ),
                )
        return found

    def list(self, access, *, limit: int = 100):
        access.require("session.read")
        if not 1 <= limit <= 1000:
            raise ValueError("invalid policy page size")
        return tuple(self._policies(access).values())[:limit]

    def get(self, identity: str, access):
        from .errors import Forbidden

        access.require("session.read")
        found = self._policies(access)
        if identity not in found:
            raise Forbidden("policy unavailable")
        return found[identity]

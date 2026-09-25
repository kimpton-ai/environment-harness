"""Immutable trajectory datasets and local training-integration receipts."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import Json, Record
from .errors import Conflict, Forbidden
from .store import digest, encode
from .trajectories import (
    API_VERSION,
    Policy,
    PortableRecord,
    ResourceFeatures,
    ResourceMetadata,
    TrajectoryRepository,
)


class DatasetMember(PortableRecord):
    trajectory_id: str = Field(alias="trajectoryId", min_length=1, max_length=200)
    snapshot_id: str = Field(alias="snapshotId", min_length=1, max_length=200)
    trajectory_digest: str = Field(alias="trajectoryDigest", pattern=r"^[0-9a-f]{64}$")
    snapshot_digest: str = Field(alias="snapshotDigest", pattern=r"^[0-9a-f]{64}$")
    schema_version: str = Field(alias="schemaVersion", min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=100)


class TrajectoryDatasetSpec(PortableRecord):
    name: str = Field(min_length=1, max_length=200)
    members: tuple[DatasetMember, ...] = Field(min_length=1)


class TrajectoryDatasetStatus(PortableRecord):
    record_count: int = Field(alias="recordCount", ge=1)
    dataset_digest: str = Field(alias="datasetDigest", pattern=r"^[0-9a-f]{64}$")
    reward_state: str = Field(alias="rewardState", min_length=1, max_length=100)


class TrajectoryDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    api_version: Literal["environmentharness.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["TrajectoryDataset"]
    metadata: ResourceMetadata
    features: ResourceFeatures
    spec: TrajectoryDatasetSpec
    status: TrajectoryDatasetStatus
    extensions: Json

    @model_validator(mode="after")
    def json_serializable(self):
        json.dumps(self.model_dump(mode="json", by_alias=True), allow_nan=False)
        return self


class TrainingOutput(Record):
    policy: Policy
    metrics: Json
    artifacts: tuple[Json, ...] = ()
    limitations: tuple[str, ...] = ()


class DatasetCreate(Record):
    name: str = Field(min_length=1, max_length=200)
    trajectories: tuple[str, ...] = Field(min_length=1, max_length=1000)


class TrainingIntegration(Protocol):
    identity: str
    version: str

    def validate(self, dataset: TrajectoryDataset) -> None: ...

    def train(self, dataset: TrajectoryDataset, config: Json) -> TrainingOutput: ...


class TrainingRunSpec(PortableRecord):
    dataset_id: str = Field(alias="datasetId", min_length=1, max_length=200)
    dataset_digest: str = Field(alias="datasetDigest", pattern=r"^[0-9a-f]{64}$")
    integration: str = Field(min_length=1, max_length=200)
    integration_version: str = Field(alias="integrationVersion", min_length=1, max_length=100)
    config_digest: str = Field(alias="configDigest", pattern=r"^[0-9a-f]{64}$")


class TrainingRunStatus(PortableRecord):
    state: str = Field(min_length=1, max_length=100)
    policy: Policy
    metrics: Json
    artifacts: tuple[Json, ...]
    limitations: tuple[str, ...]
    completed_at: str = Field(alias="completedAt", min_length=1)


class TrainingRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    api_version: Literal["environmentharness.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["TrainingRun"]
    metadata: ResourceMetadata
    features: ResourceFeatures
    spec: TrainingRunSpec
    status: TrainingRunStatus
    extensions: Json


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class TrainingRepository:
    """Create immutable local training resources; never execute integrations through HTTP."""

    def __init__(self, store):
        self.store = store
        self.trajectories = TrajectoryRepository(store)

    def freeze_dataset(self, name: str, trajectories: tuple[str, ...], access) -> TrajectoryDataset:
        access.require("dataset.create")
        if not name or not trajectories:
            raise ValueError("dataset name and trajectories are required")
        members = []
        record_count = 0
        any_reward = False
        schema_version = None
        for identity in trajectories:
            trajectory = self.trajectories.get(identity, access)
            manifest = trajectory.spec.manifest
            experiment = (manifest.model_extra or {}).get("experiment")
            source_registration = (manifest.model_extra or {}).get("sourceRegistration")
            split = None
            if isinstance(experiment, dict):
                split = experiment.get("split")
            elif isinstance(source_registration, dict):
                split = source_registration.get("split")
            if manifest.purpose != "training" or split != "training":
                raise Forbidden("dataset requires training-entitled trajectory evidence")
            if trajectory.status.collection.state != "complete":
                raise Conflict("dataset requires complete evidence collection")
            if trajectory.status.execution.state != "completed":
                raise Conflict("dataset requires completed execution")
            if not (trajectory.status.termination.terminated or trajectory.status.termination.truncated):
                raise Conflict("dataset requires a terminal trajectory")
            if trajectory.status.verified_outcome.state in ("pending", "uncertain"):
                raise Conflict("dataset requires resolved outcomes")
            any_reward = self._has_resolved_reward(trajectory) or any_reward
            if schema_version is None:
                schema_version = manifest.source.schema_version
            elif schema_version != manifest.source.schema_version:
                raise Conflict("dataset contains incompatible trajectory schemas")
            snapshot = self.trajectories.freeze(identity, access)
            members.append(
                {
                    "trajectoryId": trajectory.metadata.id,
                    "snapshotId": snapshot.metadata.id,
                    "trajectoryDigest": trajectory.status.trajectory_digest,
                    "snapshotDigest": snapshot.status.snapshot_digest,
                    "schemaVersion": manifest.source.schema_version,
                    "purpose": manifest.purpose,
                }
            )
            record_count += snapshot.status.record_count
        canonical_spec = {"name": name, "members": members}
        dataset_digest = digest(canonical_spec)
        dataset = TrajectoryDataset.model_validate(
            {
                "apiVersion": API_VERSION,
                "kind": "TrajectoryDataset",
                "metadata": {
                    "id": "dataset-" + dataset_digest[:32],
                    "createdAt": _now(),
                    "labels": {},
                },
                "features": {"required": [], "optional": []},
                "spec": canonical_spec,
                "status": {
                    "recordCount": record_count,
                    "datasetDigest": dataset_digest,
                    "rewardState": "ready" if any_reward else "unavailable",
                },
                "extensions": {},
            }
        )
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT body FROM trajectory_datasets WHERE id=? AND tenant=?",
                (dataset.metadata.id, access.tenant),
            ).fetchone()
            body = encode(dataset.model_dump(mode="json", by_alias=True))
            if existing is not None and existing["body"] != body:
                raise Conflict("dataset identity conflicts with stored content")
            if existing is None:
                db.execute(
                    "INSERT INTO trajectory_datasets VALUES (?,?,?,?,?)",
                    (
                        dataset.metadata.id,
                        access.tenant,
                        body,
                        dataset.status.dataset_digest,
                        datetime.now(UTC).timestamp(),
                    ),
                )
        return dataset

    @staticmethod
    def _has_resolved_reward(trajectory) -> bool:
        rewards = {
            record.id: record for record in trajectory.status.records if record.type == "environment.reward"
        }
        superseders: dict[str, str] = {}
        for record in rewards.values():
            if not isinstance(record.data, dict):
                raise Conflict("dataset contains a malformed reward chain")
            predecessor = record.data.get("supersedes")
            if predecessor is not None:
                if not isinstance(predecessor, str) or predecessor not in rewards:
                    raise Conflict("dataset contains an incomplete reward chain")
                if predecessor in superseders:
                    raise Conflict("dataset contains an ambiguous reward chain")
                superseders[predecessor] = record.id

        for origin in rewards:
            seen = set()
            current = origin
            while current in superseders:
                if current in seen:
                    raise Conflict("dataset contains a cyclic reward chain")
                seen.add(current)
                current = superseders[current]

        ready = False
        for record in trajectory.status.records:
            if record.type == "action.executed" and isinstance(record.data, dict):
                value = record.data.get("reward")
                if value is not None:
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise Conflict("dataset contains a non-finite reward")
                    ready = True
            if record.id not in rewards or record.id in superseders:
                continue
            state = record.data.get("state")
            if state in ("retracted", "revoked"):
                raise Conflict("dataset contains an unresolved reward chain")
            value = record.data.get("value", record.data.get("reward"))
            if value is not None:
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise Conflict("dataset contains a non-finite reward")
                ready = True
        return ready

    def get_dataset(self, dataset: str, access) -> TrajectoryDataset:
        access.require("dataset.read")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT body FROM trajectory_datasets WHERE id=? AND tenant=?",
                (dataset, access.tenant),
            ).fetchone()
        if row is None:
            raise Forbidden("dataset unavailable")
        return TrajectoryDataset.model_validate_json(row["body"])

    def list_datasets(self, access, *, limit=100) -> tuple[TrajectoryDataset, ...]:
        access.require("dataset.read")
        if not 1 <= limit <= 1000:
            raise ValueError("invalid dataset page size")
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT body FROM trajectory_datasets WHERE tenant=? ORDER BY created DESC LIMIT ?",
                (access.tenant, limit),
            ).fetchall()
        return tuple(TrajectoryDataset.model_validate_json(row["body"]) for row in rows)

    def export_dataset(self, dataset: str, access):
        selected = self.get_dataset(dataset, access)
        yield {"dataset": selected.model_dump(mode="json", by_alias=True)}
        for member in selected.spec.members:
            yield from self.trajectories.export_snapshot(member.snapshot_id, access)

    def run(self, dataset: str, integration: TrainingIntegration, config: Json, access) -> TrainingRun:
        access.require("training.execute")
        selected = self.get_dataset(dataset, access)
        json.dumps(config, allow_nan=False)
        integration.validate(selected)
        output = integration.train(selected, config)
        if not isinstance(output, TrainingOutput):
            output = TrainingOutput.model_validate(output)
        completed_at = _now()
        spec = {
            "datasetId": selected.metadata.id,
            "datasetDigest": selected.status.dataset_digest,
            "integration": integration.identity,
            "integrationVersion": integration.version,
            "configDigest": digest(config),
        }
        status = {
            "state": "completed",
            "policy": output.policy.model_dump(mode="json", by_alias=True),
            "metrics": output.metrics,
            "artifacts": list(output.artifacts),
            "limitations": list(output.limitations),
            "completedAt": completed_at,
        }
        run_digest = digest({"spec": spec, "status": status})
        recorded = TrainingRun.model_validate(
            {
                "apiVersion": API_VERSION,
                "kind": "TrainingRun",
                "metadata": {
                    "id": "training-" + run_digest[:32],
                    "createdAt": completed_at,
                    "labels": {},
                },
                "features": {"required": [], "optional": []},
                "spec": spec,
                "status": status,
                "extensions": {},
            }
        )
        body = encode(recorded.model_dump(mode="json", by_alias=True))
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT body FROM training_runs WHERE id=? AND tenant=?",
                (recorded.metadata.id, access.tenant),
            ).fetchone()
            if existing is not None and existing["body"] != body:
                raise Conflict("training-run identity conflicts with stored content")
            if existing is None:
                db.execute(
                    "INSERT INTO training_runs VALUES (?,?,?,?,?)",
                    (
                        recorded.metadata.id,
                        access.tenant,
                        selected.metadata.id,
                        body,
                        datetime.now(UTC).timestamp(),
                    ),
                )
        return recorded

    def get_run(self, training_run: str, access) -> TrainingRun:
        access.require("training.read")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT body FROM training_runs WHERE id=? AND tenant=?",
                (training_run, access.tenant),
            ).fetchone()
        if row is None:
            raise Forbidden("training run unavailable")
        return TrainingRun.model_validate_json(row["body"])

    def list_runs(self, access, *, dataset=None, limit=100) -> tuple[TrainingRun, ...]:
        access.require("training.read")
        if not 1 <= limit <= 1000:
            raise ValueError("invalid training-run page size")
        query = "SELECT body FROM training_runs WHERE tenant=?"
        values = [access.tenant]
        if dataset is not None:
            query += " AND dataset=?"
            values.append(dataset)
        query += " ORDER BY created DESC LIMIT ?"
        values.append(limit)
        with self.store.transaction() as db:
            rows = db.execute(query, tuple(values)).fetchall()
        return tuple(TrainingRun.model_validate_json(row["body"]) for row in rows)

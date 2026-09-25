"""Portable trajectory resources derived from authoritative evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .contracts import Json, Record
from .errors import Conflict, Forbidden, Unsupported
from .store import digest, encode

API_VERSION = "environmentharness.dev/v1alpha1"

#: The canonical record vocabulary this contract owns. Third parties add
#: reverse-domain namespaced types instead of extending this set.
CORE_RECORD_TYPES = (
    "environment.observation",
    "decision.requested",
    "decision.selected",
    "agent.action",
    "inference.generation",
    "environment.reward",
    "environment.operation",
    "environment.outcome",
    "evaluation.report",
    "artifact.reference",
)

#: First-party native lifecycle records. They are core types rather than
#: extensions, but they are not part of the canonical causal vocabulary above.
NATIVE_RECORD_TYPES = (
    "session.created",
    "session.paused",
    "session.cancelled",
    "session.resumed",
    "session.branched",
    "agent.dispatched",
    "agent.reconciled",
    "environment.transition",
    "environment.event",
    "phase.closed",
    "feed.ingested",
    "participant.memory",
    "authority.transferred",
    "checkpoint.committed",
    "outcomes.finalized",
    "operation.intent",
    "operation.dispatched",
    "operation.cancelled",
)

#: Map the durable native event kind onto its canonical record type. Kinds that
#: are absent keep their own first-party type.
NATIVE_RECORD_MAPPING = {
    "observation.delivered": "environment.observation",
    "action.attempted": "agent.action",
    "action.executed": "environment.outcome",
    "transition.committed": "environment.transition",
    "operation.receipt": "environment.operation",
    "report": "evaluation.report",
    "artifact": "artifact.reference",
    "model.request": "inference.generation",
    "model.response": "inference.generation",
    "model.failure": "inference.generation",
}


class SourceRegistration(Record):
    namespace: str = Field(
        min_length=3,
        max_length=200,
        pattern=r"^[a-z][a-z0-9]*(?:[.-][a-z0-9][a-z0-9-]*)+$",
    )
    run_id: str = Field(min_length=1, max_length=200)
    schema_version: str = Field(min_length=1, max_length=200)
    environment: Json
    participants: tuple[str, ...]
    purpose: Literal["evaluation", "training"]
    split: Literal["training", "heldout"] = "heldout"

    @model_validator(mode="after")
    def json_serializable(self):
        if self.purpose == "training" and self.split != "training":
            raise ValueError("training sources require the training split")
        if self.purpose == "evaluation" and self.split != "heldout":
            raise ValueError("evaluation sources require the heldout split")
        json.dumps(self.model_dump(mode="json"), allow_nan=False)
        return self


class SourceRegistrationReceipt(Record):
    id: str
    namespace: str
    run_id: str
    registration_hash: str
    collection_state: str


class TrajectorySummary(Record):
    id: str
    trajectory_id: str
    origin: Literal["native", "imported"]
    collection_state: str
    execution_state: str
    namespace: str
    run_id: str


class PortableRecord(BaseModel):
    """Frozen evidence payload that preserves additive optional fields.

    Serialization omits optional fields a writer never set, so a document from
    another writer survives a parse–serialize cycle byte-for-byte under the
    canonical encoder. Preserved unknown data participates in the digest.
    """

    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)

    @model_serializer(mode="wrap")
    def _omit_unset_optionals(self, handler):
        data = handler(self)
        fields = type(self).model_fields
        omitted = {
            key
            for name, field in fields.items()
            if name not in self.model_fields_set
            for key in (name, field.alias)
            if key is not None
        }
        # Preserved unknown fields always survive; only declared fields the
        # writer never supplied are absent from the serialized document.
        return {key: value for key, value in data.items() if key not in omitted}

    @model_validator(mode="after")
    def json_serializable(self):
        json.dumps(self.model_dump(mode="json", by_alias=True), allow_nan=False)
        return self


class ResourceMetadata(PortableRecord):
    id: str = Field(min_length=1, max_length=200)
    created_at: str = Field(alias="createdAt", min_length=1)
    labels: dict[str, str]


class ResourceFeatures(PortableRecord):
    required: tuple[str, ...]
    optional: tuple[str, ...]


class PolicySpec(PortableRecord):
    implementation: str = Field(min_length=1, max_length=500)
    version: str | None = Field(default=None, max_length=200)
    lineage: tuple[str, ...] = ()
    artifact: Json | None = None


class PolicyStatus(PortableRecord):
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class Policy(BaseModel):
    """Portable identity for an agent policy without serializing live framework state."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    api_version: Literal["environmentharness.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["Policy"]
    metadata: ResourceMetadata
    features: ResourceFeatures
    spec: PolicySpec
    status: PolicyStatus
    extensions: Json


class PolicyReference(PortableRecord):
    id: str = Field(min_length=1, max_length=200)
    participant: str = Field(min_length=1, max_length=200)
    implementation: str = Field(min_length=1, max_length=500)
    version: str | None = Field(default=None, max_length=200)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceIdentity(PortableRecord):
    namespace: str = Field(min_length=1, max_length=200)
    run_id: str = Field(alias="runId", min_length=1, max_length=200)
    schema_version: str = Field(alias="schemaVersion", min_length=1, max_length=200)


class TrajectoryManifest(PortableRecord):
    environment: Json
    source: SourceIdentity
    participants: tuple[str, ...]
    purpose: str = Field(min_length=1, max_length=100)
    policies: tuple[PolicyReference, ...] = Field(default=(), exclude_if=lambda value: not value)


class TrajectorySpec(PortableRecord):
    manifest: TrajectoryManifest


class LifecycleStatus(PortableRecord):
    state: str = Field(min_length=1, max_length=100)


class NativeTimeCoordinate(PortableRecord):
    clock: str = Field(min_length=1, max_length=200)
    value: Any


class RecordTime(PortableRecord):
    """Clocks are preserved, never coerced into one scalar.

    Ordering comes from the durable record sequence and causal links; a
    timestamp alone never establishes causality.
    """

    wall_time: str | None = Field(alias="wallTime", default=None, min_length=1)
    monotonic_offset_ns: int | None = Field(alias="monotonicOffsetNs", default=None)
    monotonic_origin: str | None = Field(alias="monotonicOrigin", default=None, max_length=200)
    native: tuple[NativeTimeCoordinate, ...] = ()

    @model_validator(mode="after")
    def some_coordinate_is_present(self):
        if self.wall_time is None and self.monotonic_offset_ns is None and not self.native:
            raise ValueError("a record time requires a wall, monotonic, or native coordinate")
        if self.monotonic_offset_ns is not None and self.monotonic_origin is None:
            raise ValueError("a process-local duration requires its declared origin")
        return self


class SourceRecord(Record):
    id: str = Field(min_length=1, max_length=200)
    position: str = Field(min_length=1, max_length=500)
    previous_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    type: str = Field(min_length=1, max_length=200)
    segment: str = Field(min_length=1, max_length=200)
    participant: str | None
    revision: int = Field(ge=0)
    time: RecordTime
    data: Json
    audience: tuple[str, ...]

    def canonical_source_body(self):
        return {
            "id": self.id,
            "position": self.position,
            "previous_hash": self.previous_hash,
            "type": self.type,
            "segment": self.segment,
            "participant": self.participant,
            "revision": self.revision,
            "time": self.time.model_dump(mode="json", by_alias=True),
            "data": self.data,
            "audience": list(self.audience),
        }

    @classmethod
    def create(cls, **values):
        unhashed = cls.model_validate(values | {"source_hash": "0" * 64})
        return unhashed.model_copy(update={"source_hash": digest(unhashed.canonical_source_body())})


class SourceAcknowledgement(Record):
    source: str
    accepted: int = Field(ge=0)
    position: str
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_state: str


class SourceIngestionBatch(Record):
    records: tuple[SourceRecord, ...] = Field(min_length=1, max_length=1000)


class TrajectorySegment(PortableRecord):
    """One addressable execution interval inside a trajectory.

    Even a single uninterrupted run receives one segment, so there is never a
    second record shape with a missing segment. A pause, process restart,
    checkpoint continuation, recovery attempt, or counterfactual branch starts a
    new segment instead of pretending the episode was one sequential run.
    """

    id: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=100)
    sequence_start: int = Field(alias="sequenceStart", ge=1)
    sequence_end: int = Field(alias="sequenceEnd", ge=1)
    collection: LifecycleStatus
    execution: LifecycleStatus
    #: The segment this one continues from, when it is a continuation.
    continues: str | None = Field(default=None, max_length=200)
    #: Why the preceding segment ended, when that is known.
    interruption: str | None = Field(default=None, max_length=500)
    native_start: tuple[NativeTimeCoordinate, ...] = Field(alias="nativeStart", default=())
    native_end: tuple[NativeTimeCoordinate, ...] = Field(alias="nativeEnd", default=())

    @model_validator(mode="after")
    def ordered_sequence(self):
        if self.sequence_end < self.sequence_start:
            raise ValueError("segment sequence end precedes its start")
        return self


class TrajectoryRecord(PortableRecord):
    type: str = Field(min_length=1, max_length=200)
    id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1)
    segment: str = Field(min_length=1, max_length=200)
    participant: str | None
    revision: int = Field(ge=0)
    causes: tuple[str, ...]
    time: RecordTime
    data: Json
    extensions: Json


class ExtensionRecord(TrajectoryRecord):
    """An inert, lossless record whose namespaced type is owned by an extension."""


class TrajectoryRecordPage(Record):
    records: tuple[TrajectoryRecord, ...]
    cursor: int = Field(ge=0)
    has_more: bool


class TerminationStatus(PortableRecord):
    terminated: bool
    truncated: bool
    reason: str


class VerifiedOutcome(PortableRecord):
    state: str = Field(min_length=1, max_length=100)
    evidence: tuple[str, ...]


class SourceStatusUpdate(Record):
    collection_state: str = Field(min_length=1, max_length=100)
    execution_state: str = Field(min_length=1, max_length=100)
    termination: TerminationStatus
    verified_outcome: VerifiedOutcome
    terminal_position: str = Field(min_length=1, max_length=500)
    terminal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    backlog: int | None = Field(default=None, ge=0)
    gaps: tuple[str, ...] = ()
    capture_failures: tuple[str, ...] = ()


class SourceStatus(Record):
    source: str
    namespace: str
    run_id: str
    registration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_state: str
    execution_state: str
    acknowledged_position: str | None
    acknowledged_hash: str | None
    backlog: int | None
    gaps: tuple[str, ...]
    capture_failures: tuple[str, ...]
    termination: TerminationStatus
    verified_outcome: VerifiedOutcome


class SourceCollection(PortableRecord):
    """Collection health for the trajectory's delivery backlog."""

    state: str = Field(min_length=1, max_length=100)
    acknowledged_position: str | None = Field(alias="acknowledgedPosition", default=None)
    acknowledged_hash: str | None = Field(alias="acknowledgedHash", default=None)
    backlog: int | None = Field(default=None, ge=0)
    gaps: tuple[str, ...] = ()
    capture_failures: tuple[str, ...] = Field(alias="captureFailures", default=())

    @model_validator(mode="after")
    def completion_requires_a_closed_boundary(self):
        if self.state == "complete" and (self.gaps or self.capture_failures or self.backlog):
            raise ValueError("collection cannot be complete with a declared gap, failure, or backlog")
        return self


class TrajectoryStatus(PortableRecord):
    """The detail projection. Records are a separately paged stream.

    ``Trajectory.status`` never materializes a record tuple: its digest binds
    the typed manifest and the authoritative evidence or source head, and record
    pages remain independently verifiable against those boundaries.
    """

    segments: tuple[TrajectorySegment, ...] = Field(min_length=1)
    record_count: int = Field(alias="recordCount", ge=0)
    sequence_start: int = Field(alias="sequenceStart", ge=0)
    sequence_end: int = Field(alias="sequenceEnd", ge=0)
    collection: SourceCollection
    execution: LifecycleStatus
    termination: TerminationStatus
    verified_outcome: VerifiedOutcome = Field(alias="verifiedOutcome")
    evidence_head: str = Field(alias="evidenceHead", pattern=r"^[0-9a-f]{64}$")
    trajectory_digest: str = Field(alias="trajectoryDigest", pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def segments_are_consistent(self):
        identities = [segment.id for segment in self.segments]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate trajectory segment")
        boundary = 0
        for segment in self.segments:
            if segment.sequence_start <= boundary:
                raise ValueError("trajectory segments must cover increasing sequence ranges")
            if segment.continues is not None and segment.continues not in identities:
                raise ValueError("a continuation segment must reference a known predecessor")
            boundary = segment.sequence_end
        if self.sequence_end < self.sequence_start:
            raise ValueError("trajectory sequence end precedes its start")
        if self.record_count and self.sequence_end != self.segments[-1].sequence_end:
            raise ValueError("trajectory sequence end must match its final segment")
        return self


class Trajectory(BaseModel):
    """Immutable, portable projection of one native or imported trace."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    api_version: Literal["environmentharness.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["Trajectory"]
    metadata: ResourceMetadata
    features: ResourceFeatures
    spec: TrajectorySpec
    status: TrajectoryStatus
    extensions: Json

    @model_validator(mode="after")
    def json_serializable(self):
        json.dumps(self.model_dump(mode="json", by_alias=True), allow_nan=False)
        return self


class SourceBoundary(PortableRecord):
    position: str = Field(min_length=1, max_length=500)
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ScoreReportBoundary(PortableRecord):
    scorer: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=0)
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactBoundary(PortableRecord):
    id: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TrajectorySnapshotSpec(PortableRecord):
    trajectory_id: str = Field(alias="trajectoryId", min_length=1, max_length=200)
    trajectory_digest: str = Field(alias="trajectoryDigest", pattern=r"^[0-9a-f]{64}$")
    source_boundary: SourceBoundary = Field(alias="sourceBoundary")
    evidence_head: str = Field(alias="evidenceHead", pattern=r"^[0-9a-f]{64}$")
    sequence_start: int = Field(alias="sequenceStart", ge=1)
    sequence_end: int = Field(alias="sequenceEnd", ge=1)
    score_reports: tuple[ScoreReportBoundary, ...] = Field(alias="scoreReports")
    artifacts: tuple[ArtifactBoundary, ...]
    audience: tuple[str, ...]

    @model_validator(mode="after")
    def ordered_sequence(self):
        if self.sequence_end < self.sequence_start:
            raise ValueError("snapshot sequence end precedes its start")
        return self


class TrajectorySnapshotStatus(PortableRecord):
    record_count: int = Field(alias="recordCount", ge=0)
    artifact_count: int = Field(alias="artifactCount", ge=0)
    manifest_digest: str = Field(alias="manifestDigest", pattern=r"^[0-9a-f]{64}$")
    records_digest: str = Field(alias="recordsDigest", pattern=r"^[0-9a-f]{64}$")
    snapshot_digest: str = Field(alias="snapshotDigest", pattern=r"^[0-9a-f]{64}$")
    complete: bool


class TrajectorySnapshot(BaseModel):
    """Immutable export boundary for an authorized trajectory projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    api_version: Literal["environmentharness.dev/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["TrajectorySnapshot"]
    metadata: ResourceMetadata
    features: ResourceFeatures
    spec: TrajectorySnapshotSpec
    status: TrajectorySnapshotStatus
    extensions: Json

    @model_validator(mode="after")
    def counts_are_consistent(self):
        if self.status.artifact_count != len(self.spec.artifacts):
            raise ValueError("snapshot artifact count does not match its boundary")
        json.dumps(self.model_dump(mode="json", by_alias=True), allow_nan=False)
        return self


class ResourceRegistry:
    """Resolve portable resources by their explicit version and kind."""

    def __init__(self, *, supported_features=()):
        from .resources import Checkpoint, Experiment, ScenarioSet, Session
        from .training import TrainingRun, TrajectoryDataset

        self.supported_features = frozenset(supported_features)
        self._models: dict[tuple[str, str], type[BaseModel]] = {
            (API_VERSION, "ScenarioSet"): ScenarioSet,
            (API_VERSION, "Experiment"): Experiment,
            (API_VERSION, "Session"): Session,
            (API_VERSION, "Checkpoint"): Checkpoint,
            (API_VERSION, "Policy"): Policy,
            (API_VERSION, "Trajectory"): Trajectory,
            (API_VERSION, "TrajectorySnapshot"): TrajectorySnapshot,
            (API_VERSION, "TrajectoryDataset"): TrajectoryDataset,
            (API_VERSION, "TrainingRun"): TrainingRun,
        }
        self._record_models: dict[str, type[TrajectoryRecord]] = {}
        self._built_in_record_types = frozenset(CORE_RECORD_TYPES) | frozenset(NATIVE_RECORD_TYPES)

    def register(self, api_version: str, kind: str, model: type[BaseModel]):
        key = (api_version, kind)
        if key in self._models:
            raise Conflict(f"resource contract already registered: {api_version} {kind}")
        self._models[key] = model

    def decode(self, value: Json):
        api_version = value.get("apiVersion")
        kind = value.get("kind")
        if not isinstance(api_version, str) or not isinstance(kind, str):
            raise Unsupported("portable resource requires apiVersion and kind")
        model = self._models.get((api_version, kind))
        if model is None:
            raise Unsupported(f"unsupported portable resource: {api_version} {kind}")
        features = value.get("features", {})
        required = features.get("required", []) if isinstance(features, dict) else []
        unsupported = sorted(
            feature
            for feature in required
            if not isinstance(feature, str) or feature not in self.supported_features
        )
        if unsupported:
            raise Unsupported("unsupported required resource features: " + ", ".join(unsupported))
        return model.model_validate(value)

    def register_record(self, record_type: str, model: type[TrajectoryRecord]):
        if record_type in self._record_models or record_type in self._built_in_record_types:
            raise Conflict(f"record contract already registered: {record_type}")
        if not _extension_record_type(record_type):
            raise ValueError("extension record type must use a reverse-domain namespace")
        self._record_models[record_type] = model

    def decode_record(self, value: Json) -> TrajectoryRecord:
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise Unsupported("trajectory record requires a type")
        record_type = value["type"]
        model = self._record_models.get(record_type)
        if model is not None:
            return model.model_validate(value)
        if record_type in self._built_in_record_types:
            return TrajectoryRecord.model_validate(value)
        if not _extension_record_type(record_type):
            raise Unsupported("unknown record type must use a reverse-domain namespaced identifier")
        return ExtensionRecord.model_validate(value)


ALL_RECORD_TYPES = frozenset(CORE_RECORD_TYPES) | frozenset(NATIVE_RECORD_TYPES)


def _extension_record_type(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
            r"(?:\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*){2,}",
            value,
        )
    )


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


class TrajectoryRepository:
    """Read portable resources from the existing authoritative evidence store."""

    def __init__(self, store):
        self.store = store

    def list_page(self, access, limit=100, cursor=None):
        access.require("trajectory.read")
        if not 1 <= limit <= 1000:
            raise ValueError("invalid trajectory page size")
        with self.store.transaction() as db:
            summaries = [
                TrajectorySummary(
                    id=row["id"],
                    trajectory_id="trajectory-" + row["id"],
                    origin="native",
                    collection_state=(
                        "complete" if row["status"] in ("completed", "succeeded") else "current"
                    ),
                    execution_state=row["status"],
                    namespace="environment-harness",
                    run_id=row["id"],
                )
                for row in db.execute(
                    "SELECT id,status FROM environments WHERE tenant=?", (access.tenant,)
                ).fetchall()
            ]
            summaries.extend(
                TrajectorySummary(
                    id=row["id"],
                    trajectory_id="trajectory-" + row["id"],
                    origin="imported",
                    collection_state=row["collection_state"],
                    execution_state=row["execution_state"],
                    namespace=row["namespace"],
                    run_id=row["run_id"],
                )
                for row in db.execute(
                    "SELECT id,namespace,run_id,collection_state,execution_state "
                    "FROM trajectory_sources WHERE tenant=?",
                    (access.tenant,),
                ).fetchall()
            )
        summaries.sort(key=lambda summary: summary.id, reverse=True)
        start = 0
        if cursor is not None:
            try:
                start = next(index for index, summary in enumerate(summaries) if summary.id == cursor) + 1
            except StopIteration:
                raise ValueError("invalid trajectory cursor") from None
        page = tuple(summaries[start : start + limit])
        next_cursor = page[-1].id if start + len(page) < len(summaries) else None
        return page, next_cursor

    def register_source(self, registration: SourceRegistration, access) -> SourceRegistrationReceipt:
        access.require("source.register")
        body = registration.model_dump(mode="json")
        registration_hash = digest(body)
        source_id = (
            "source-"
            + digest(
                {
                    "authority": access.tenant,
                    "namespace": registration.namespace,
                    "run_id": registration.run_id,
                }
            )[:32]
        )
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT * FROM trajectory_sources WHERE tenant=? AND namespace=? AND run_id=?",
                (access.tenant, registration.namespace, registration.run_id),
            ).fetchone()
            if existing is not None:
                if existing["registration_hash"] != registration_hash:
                    raise Conflict("source identity reused with different metadata")
                return SourceRegistrationReceipt(
                    id=existing["id"],
                    namespace=existing["namespace"],
                    run_id=existing["run_id"],
                    registration_hash=existing["registration_hash"],
                    collection_state=existing["collection_state"],
                )
            db.execute(
                "INSERT INTO trajectory_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    access.tenant,
                    registration.namespace,
                    registration.run_id,
                    encode(body),
                    registration_hash,
                    "registered",
                    "unknown",
                    encode({"terminated": False, "truncated": False, "reason": "unavailable"}),
                    encode({"state": "unavailable", "evidence": []}),
                    None,
                    None,
                    None,
                    encode([]),
                    encode([]),
                    datetime.now(UTC).timestamp(),
                ),
            )
        return SourceRegistrationReceipt(
            id=source_id,
            namespace=registration.namespace,
            run_id=registration.run_id,
            registration_hash=registration_hash,
            collection_state="registered",
        )

    def records_page(self, trajectory: str, access, *, after=0, limit=200) -> TrajectoryRecordPage:
        access.require("trajectory.read")
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid trajectory record page")
        with self.store.transaction() as db:
            native = db.execute(
                "SELECT id FROM environments WHERE id=? AND tenant=?", (trajectory, access.tenant)
            ).fetchone()
        if native is not None:
            events = self.store.events(trajectory, access, after=after, limit=limit)
            previous_id = None
            segment_index = 1
            if events and events[0]["seq"] > 1:
                with self.store.transaction() as db:
                    previous = db.execute(
                        "SELECT hash FROM events WHERE environment=? AND seq=?",
                        (trajectory, events[0]["seq"] - 1),
                    ).fetchone()
                    segment_index += db.execute(
                        "SELECT count(*) FROM events WHERE environment=? AND kind='session.resumed' "
                        "AND seq<?",
                        (trajectory, events[0]["seq"]),
                    ).fetchone()[0]
                previous_id = "event-" + previous["hash"] if previous is not None else None
            records = []
            for event in events:
                if event["kind"] == "session.resumed":
                    segment_index += 1
                record = self._native_record(event, previous_id, f"segment-{segment_index}")
                records.append(record)
                previous_id = record.id
            cursor = records[-1].sequence if records else after
            with self.store.transaction() as db:
                has_more = (
                    db.execute(
                        "SELECT 1 FROM events WHERE environment=? AND seq>? LIMIT 1",
                        (trajectory, cursor),
                    ).fetchone()
                    is not None
                )
            return TrajectoryRecordPage(records=tuple(records), cursor=cursor, has_more=has_more)

        with self.store.transaction() as db:
            source = db.execute(
                "SELECT id FROM trajectory_sources WHERE id=? AND tenant=?", (trajectory, access.tenant)
            ).fetchone()
            if source is None:
                raise Forbidden("trajectory unavailable")
            rows = db.execute(
                "SELECT * FROM trajectory_source_records WHERE source=? AND ordinal>? "
                "ORDER BY ordinal LIMIT ?",
                (trajectory, after, limit),
            ).fetchall()
            previous = None
            if rows and rows[0]["ordinal"] > 1:
                previous = db.execute(
                    "SELECT record_id FROM trajectory_source_records WHERE source=? AND ordinal=?",
                    (trajectory, rows[0]["ordinal"] - 1),
                ).fetchone()
            cursor = rows[-1]["ordinal"] if rows else after
            has_more = (
                db.execute(
                    "SELECT 1 FROM trajectory_source_records WHERE source=? AND ordinal>? LIMIT 1",
                    (trajectory, cursor),
                ).fetchone()
                is not None
            )
        previous_id = previous["record_id"] if previous is not None else None
        records = []
        for row in rows:
            record = self._imported_record(row, previous_id)
            records.append(record)
            previous_id = record.id
        return TrajectoryRecordPage(records=tuple(records), cursor=cursor, has_more=has_more)

    @staticmethod
    def _native_record(event, previous_id, segment="segment-1") -> TrajectoryRecord:
        payload = event["payload"]
        participant = payload.get("participant")
        if participant is None and len(event["audience"]) == 1:
            participant = event["audience"][0]
        wall_time = event["event_time"] if event["event_time"] is not None else event["ingested"]
        record_type = NATIVE_RECORD_MAPPING.get(event["kind"], event["kind"])
        if record_type not in ALL_RECORD_TYPES:
            # An environment-declared event keeps its kind inside a first-party
            # container rather than pretending to be a canonical record.
            record_type = "environment.event"
        return TrajectoryRecord.model_validate(
            {
                "type": record_type,
                "id": "event-" + event["hash"],
                "sequence": event["seq"],
                "segment": segment,
                "participant": participant,
                "revision": event["revision"],
                "causes": [] if previous_id is None else [previous_id],
                "time": {
                    "wallTime": _timestamp(wall_time),
                    "native": [{"clock": "environment.revision", "value": event["revision"]}],
                },
                "data": payload,
                "extensions": {
                    "environmentharness.dev/sourceHash": event["hash"],
                    "environmentharness.dev/previousHash": event["previous"],
                    "environmentharness.dev/audience": event["audience"],
                    "environmentharness.dev/eventKind": event["kind"],
                },
            }
        )

    @staticmethod
    def _imported_record(row, previous_id) -> TrajectoryRecord:
        source_record = SourceRecord.model_validate_json(row["body"])
        return TrajectoryRecord.model_validate(
            {
                "type": source_record.type,
                "id": source_record.id,
                "sequence": row["ordinal"],
                "segment": source_record.segment,
                "participant": source_record.participant,
                "revision": source_record.revision,
                "causes": [] if previous_id is None else [previous_id],
                "time": source_record.time.model_dump(mode="json", by_alias=True),
                "data": source_record.data,
                "extensions": {
                    "environmentharness.dev/sourcePosition": source_record.position,
                    "environmentharness.dev/sourceHash": source_record.source_hash,
                    "environmentharness.dev/previousHash": source_record.previous_hash,
                    "environmentharness.dev/audience": list(source_record.audience),
                },
            }
        )

    def ingest(self, source: str, records: tuple[SourceRecord, ...], access) -> SourceAcknowledgement:
        access.require("source.ingest")
        if not 1 <= len(records) <= 1000:
            raise ValueError("ingestion batch must contain 1 to 1000 records")
        accepted = 0
        with self.store.transaction() as db:
            source_row = db.execute(
                "SELECT * FROM trajectory_sources WHERE id=? AND tenant=?", (source, access.tenant)
            ).fetchone()
            if source_row is None:
                raise Forbidden("trajectory source unavailable")
            cursor_position = source_row["acknowledged_position"]
            cursor_hash = source_row["acknowledged_hash"] or "0" * 64
            for record in records:
                body = record.model_dump(mode="json", by_alias=True)
                existing = db.execute(
                    "SELECT * FROM trajectory_source_records WHERE source=? AND (record_id=? OR position=?)",
                    (source, record.id, record.position),
                ).fetchone()
                if existing is not None:
                    if existing["body"] != encode(body):
                        raise Conflict("source record identity reused with different content")
                    accepted += 1
                    continue
                if digest(record.canonical_source_body()) != record.source_hash:
                    raise Conflict("source record hash does not match its content")
                if record.previous_hash != cursor_hash:
                    raise Conflict("source chain does not continue from the acknowledged hash")
                ordinal = db.execute(
                    "SELECT coalesce(max(ordinal),0)+1 FROM trajectory_source_records WHERE source=?",
                    (source,),
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO trajectory_source_records VALUES (?,?,?,?,?,?,?,?)",
                    (
                        source,
                        ordinal,
                        record.id,
                        record.position,
                        record.source_hash,
                        record.previous_hash,
                        encode(body),
                        datetime.now(UTC).timestamp(),
                    ),
                )
                cursor_position = record.position
                cursor_hash = record.source_hash
                accepted += 1
            db.execute(
                "UPDATE trajectory_sources SET acknowledged_position=?,acknowledged_hash=?,"
                "collection_state='current' WHERE id=?",
                (cursor_position, cursor_hash, source),
            )
        return SourceAcknowledgement(
            source=source,
            accepted=accepted,
            position=cursor_position,
            hash=cursor_hash,
            collection_state="current",
        )

    def update_source_status(self, source: str, update: SourceStatusUpdate, access):
        access.require("source.status.write")
        with self.store.transaction() as db:
            source_row = db.execute(
                "SELECT * FROM trajectory_sources WHERE id=? AND tenant=?", (source, access.tenant)
            ).fetchone()
            if source_row is None:
                raise Forbidden("trajectory source unavailable")
            if update.collection_state == "complete":
                if update.gaps:
                    raise Conflict("collection has unresolved gaps")
                if update.capture_failures:
                    raise Conflict("collection has unresolved capture failures")
                if update.backlog not in (None, 0):
                    raise Conflict("collection has an unacknowledged backlog")
                if (
                    update.terminal_position != source_row["acknowledged_position"]
                    or update.terminal_hash != source_row["acknowledged_hash"]
                ):
                    raise Conflict("terminal source boundary has not been acknowledged")
            for reference in update.verified_outcome.evidence:
                if (
                    db.execute(
                        "SELECT 1 FROM trajectory_source_records WHERE source=? AND record_id=?",
                        (source, reference),
                    ).fetchone()
                    is None
                ):
                    raise Conflict("verified outcome references unavailable source evidence")
            db.execute(
                "UPDATE trajectory_sources SET collection_state=?,execution_state=?,termination=?,"
                "verified_outcome=?,backlog=?,gaps=?,capture_failures=? WHERE id=?",
                (
                    update.collection_state,
                    update.execution_state,
                    encode(update.termination.model_dump(mode="json", by_alias=True)),
                    encode(update.verified_outcome.model_dump(mode="json", by_alias=True)),
                    update.backlog,
                    encode(list(update.gaps)),
                    encode(list(update.capture_failures)),
                    source,
                ),
            )
        return update

    def source_status(self, source: str, access) -> SourceStatus:
        access.require("trajectory.read")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM trajectory_sources WHERE id=? AND tenant=?", (source, access.tenant)
            ).fetchone()
        if row is None:
            raise Forbidden("trajectory source unavailable")
        return SourceStatus(
            source=row["id"],
            namespace=row["namespace"],
            run_id=row["run_id"],
            registration_hash=row["registration_hash"],
            collection_state=row["collection_state"],
            execution_state=row["execution_state"],
            acknowledged_position=row["acknowledged_position"],
            acknowledged_hash=row["acknowledged_hash"],
            backlog=row["backlog"],
            gaps=tuple(json.loads(row["gaps"])),
            capture_failures=tuple(json.loads(row["capture_failures"])),
            termination=TerminationStatus.model_validate_json(row["termination"]),
            verified_outcome=VerifiedOutcome.model_validate_json(row["verified_outcome"]),
        )

    def list_sources(self, access, *, limit: int = 100) -> tuple[SourceStatus, ...]:
        """List every registered source's collection and execution status."""

        access.require("trajectory.read")
        if not 1 <= limit <= 1000:
            raise ValueError("invalid source page size")
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT id FROM trajectory_sources WHERE tenant=? ORDER BY created DESC,id LIMIT ?",
                (access.tenant, limit),
            ).fetchall()
        return tuple(self.source_status(row["id"], access) for row in rows)

    def snapshot_records_page(
        self, snapshot: str, access, *, after: int = 0, limit: int = 200
    ) -> TrajectoryRecordPage:
        """Page through one frozen snapshot's ordered records."""

        access.require("trajectory.read")
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid snapshot record page")
        body = self._snapshot_body(snapshot, access)
        if "records" in body:
            # Read prerelease snapshots written before normalized record storage.
            selected = [
                record
                for record in body["records"]
                if isinstance(record.get("sequence"), int) and record["sequence"] > after
            ][: limit + 1]
            records = tuple(TrajectoryRecord.model_validate(item) for item in selected[:limit])
            cursor = records[-1].sequence if records else after
            return TrajectoryRecordPage(records=records, cursor=cursor, has_more=len(selected) > limit)
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT sequence,body FROM trajectory_snapshot_records WHERE snapshot=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (snapshot, after, limit + 1),
            ).fetchall()
        records = tuple(TrajectoryRecord.model_validate_json(row["body"]) for row in rows[:limit])
        cursor = records[-1].sequence if records else after
        return TrajectoryRecordPage(records=records, cursor=cursor, has_more=len(rows) > limit)

    def get(self, environment: str, access) -> Trajectory:
        with self.store.transaction() as db:
            native = db.execute("SELECT 1 FROM environments WHERE id=?", (environment,)).fetchone()
        if native is None:
            return self._get_imported(environment, access)
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "evidence.read.full")
            manifest = json.loads(row["manifest"])
            participants = tuple(json.loads(row["participants"]))
            execution_state = row["status"]
        # Stream the evidence: a large campaign is never loaded into memory to
        # build the detail projection.
        segments: list[dict] = []
        record_count = 0
        first_ingested = None
        source_head = None
        transition: Json = {}
        interruption = None
        for event in self.store.replay(environment, access):
            record_count += 1
            if first_ingested is None:
                first_ingested = event["ingested"]
            if event["kind"] == "session.resumed" or not segments:
                previous = segments[-1] if segments else None
                segments.append(
                    {
                        "id": f"segment-{len(segments) + 1}",
                        "kind": "execution" if previous is None else "continuation",
                        "sequenceStart": event["seq"],
                        "sequenceEnd": event["seq"],
                        "continues": None if previous is None else previous["id"],
                        "interruption": interruption,
                        "nativeStart": [{"clock": "environment.revision", "value": event["revision"]}],
                        "nativeEnd": [{"clock": "environment.revision", "value": event["revision"]}],
                    }
                )
                interruption = None
            segments[-1]["sequenceEnd"] = event["seq"]
            segments[-1]["nativeEnd"] = [{"clock": "environment.revision", "value": event["revision"]}]
            if event["kind"] in ("session.paused", "session.cancelled"):
                interruption = event["kind"]
            if event["kind"] == "transition.committed":
                transition = event["payload"]
            source_head = event["hash"]
        if source_head is None:
            raise ValueError("trajectory has no evidence")

        trajectory_digest = digest(
            {
                "environment": environment,
                "manifest": manifest,
                "evidenceHead": source_head,
                "recordCount": record_count,
                "segments": segments,
            }
        )
        completed = execution_state in ("completed", "succeeded")
        terminated = bool(transition.get("terminated"))
        truncated = bool(transition.get("truncated"))
        policies = []
        for participant in manifest.get("participants", []):
            policy_body = {
                "participant": participant["id"],
                "implementation": participant["implementation"],
                "version": participant.get("policy_version"),
            }
            policies.append(
                policy_body
                | {
                    "id": "policy-" + digest(policy_body)[:32],
                    "digest": digest(policy_body),
                }
            )
        return Trajectory.model_validate(
            {
                "apiVersion": API_VERSION,
                "kind": "Trajectory",
                "metadata": {
                    "id": "trajectory-" + environment,
                    "createdAt": _timestamp(first_ingested or 0.0),
                    "labels": {"source": "native"},
                },
                "features": {"required": [], "optional": []},
                "spec": {
                    "manifest": {
                        "environment": manifest["environment"],
                        "source": {
                            "namespace": "environment-harness",
                            "runId": environment,
                            "schemaVersion": manifest["environment"]["protocol"],
                        },
                        "participants": participants,
                        "purpose": manifest["purpose"],
                        "policies": policies,
                        "experiment": manifest,
                    }
                },
                "status": {
                    "segments": [
                        segment
                        | {
                            "collection": {"state": "complete" if completed else "current"},
                            "execution": {
                                "state": execution_state if index == len(segments) - 1 else "paused"
                            },
                        }
                        for index, segment in enumerate(segments)
                    ],
                    "recordCount": record_count,
                    "sequenceStart": segments[0]["sequenceStart"],
                    "sequenceEnd": segments[-1]["sequenceEnd"],
                    "collection": {"state": "complete" if completed else "current"},
                    "execution": {"state": execution_state},
                    "termination": {
                        "terminated": terminated,
                        "truncated": truncated,
                        "reason": transition.get("reason")
                        or ("completed" if completed else "not_terminated"),
                    },
                    "verifiedOutcome": {
                        "state": "pending" if execution_state == "outcomes_pending" else "unavailable",
                        "evidence": [],
                    },
                    "evidenceHead": source_head,
                    "trajectoryDigest": trajectory_digest,
                },
                "extensions": {},
            }
        )

    def _get_imported(self, source: str, access) -> Trajectory:
        access.require("trajectory.read")
        with self.store.transaction() as db:
            source_row = db.execute(
                "SELECT * FROM trajectory_sources WHERE id=? AND tenant=?", (source, access.tenant)
            ).fetchone()
            if source_row is None:
                raise Forbidden("trajectory source unavailable")
            # Stream the imported journal rather than materializing every record.
            cursor = db.execute(
                "SELECT ordinal,body,source_hash FROM trajectory_source_records WHERE source=? "
                "ORDER BY ordinal",
                (source,),
            )
            segments: list[dict] = []
            record_count = 0
            created_at = None
            for row in cursor:
                record_count += 1
                source_record = SourceRecord.model_validate_json(row["body"])
                if created_at is None:
                    created_at = source_record.time.wall_time
                if not segments or segments[-1]["id"] != source_record.segment:
                    previous = segments[-1] if segments else None
                    segments.append(
                        {
                            "id": source_record.segment,
                            "kind": "execution" if previous is None else "continuation",
                            "sequenceStart": row["ordinal"],
                            "sequenceEnd": row["ordinal"],
                            "continues": None if previous is None else previous["id"],
                            "nativeStart": [
                                item.model_dump(mode="json", by_alias=True)
                                for item in source_record.time.native
                            ],
                        }
                    )
                segments[-1]["sequenceEnd"] = row["ordinal"]
                segments[-1]["nativeEnd"] = [
                    item.model_dump(mode="json", by_alias=True) for item in source_record.time.native
                ]
        if not record_count:
            raise ValueError("trajectory has no evidence")

        registration = json.loads(source_row["registration"])
        collection_state = source_row["collection_state"]
        execution_state = source_row["execution_state"]
        termination = json.loads(source_row["termination"])
        verified_outcome = json.loads(source_row["verified_outcome"])
        collection = {
            "state": collection_state,
            "acknowledgedPosition": source_row["acknowledged_position"],
            "acknowledgedHash": source_row["acknowledged_hash"],
            "backlog": source_row["backlog"],
            "gaps": json.loads(source_row["gaps"]),
            "captureFailures": json.loads(source_row["capture_failures"]),
        }
        trajectory_digest = digest(
            {
                "source": source,
                "registration": registration,
                "evidenceHead": source_row["acknowledged_hash"],
                "recordCount": record_count,
                "segments": segments,
            }
        )
        return Trajectory.model_validate(
            {
                "apiVersion": API_VERSION,
                "kind": "Trajectory",
                "metadata": {
                    "id": "trajectory-" + source,
                    "createdAt": created_at or _timestamp(source_row["created"]),
                    "labels": {"source": "imported"},
                },
                "features": {"required": [], "optional": []},
                "spec": {
                    "manifest": {
                        "environment": registration["environment"],
                        "source": {
                            "namespace": registration["namespace"],
                            "runId": registration["run_id"],
                            "schemaVersion": registration["schema_version"],
                        },
                        "participants": registration["participants"],
                        "purpose": registration["purpose"],
                        "sourceRegistration": registration,
                    }
                },
                "status": {
                    "segments": [
                        segment | {"collection": collection, "execution": {"state": execution_state}}
                        for segment in segments
                    ],
                    "recordCount": record_count,
                    "sequenceStart": segments[0]["sequenceStart"],
                    "sequenceEnd": segments[-1]["sequenceEnd"],
                    "collection": collection,
                    "execution": {"state": execution_state},
                    "termination": termination,
                    "verifiedOutcome": verified_outcome,
                    "evidenceHead": source_row["acknowledged_hash"],
                    "trajectoryDigest": trajectory_digest,
                },
                "extensions": {},
            }
        )

    def stream_records(self, trajectory: str, access, *, page_size: int = 200):
        """Yield every authorized record for one trajectory, page by page.

        Pagination is the only read path: a large trajectory is never
        materialized. Restarting from the returned cursor neither omits nor
        duplicates a record.
        """

        if not 1 <= page_size <= 1000:
            raise ValueError("invalid trajectory record page size")
        cursor = 0
        while True:
            page = self.records_page(trajectory, access, after=cursor, limit=page_size)
            for record in page.records:
                yield record
            if not page.has_more or page.cursor == cursor:
                return
            cursor = page.cursor

    def freeze(self, environment: str, access) -> TrajectorySnapshot:
        access.require("snapshot.create")
        trajectory = self.get(environment, access)
        with self.store.transaction() as db:
            native = db.execute(
                "SELECT tenant FROM environments WHERE id=? AND tenant=?", (environment, access.tenant)
            ).fetchone()
            if native is not None:
                reports = db.execute(
                    "SELECT revision,body,hash FROM reports WHERE environment=? ORDER BY revision",
                    (environment,),
                ).fetchall()
                artifacts = db.execute(
                    "SELECT id,sha256 FROM artifacts WHERE environment=? ORDER BY id", (environment,)
                ).fetchall()
                tenant = native["tenant"]
            else:
                source = db.execute(
                    "SELECT tenant FROM trajectory_sources WHERE id=? AND tenant=?",
                    (environment, access.tenant),
                ).fetchone()
                if source is None:
                    raise Forbidden("trajectory unavailable")
                reports, artifacts, tenant = (), (), source["tenant"]

        # Compute the ordered record digest incrementally while streaming, so a
        # frozen snapshot never depends on materializing its records.
        ordered = hashlib.sha256()
        record_count = 0
        source_position = str(trajectory.status.sequence_end)
        bodies: list[tuple[int, str]] = []
        for record in self.stream_records(environment, access):
            body = record.model_dump(mode="json", by_alias=True)
            ordered.update(digest(body).encode())
            record_count += 1
            bodies.append((record.sequence, encode(body)))
            if isinstance(record.extensions, dict):
                source_position = str(
                    record.extensions.get("environmentharness.dev/sourcePosition", source_position)
                )
        if not record_count:
            raise ValueError("trajectory has no evidence")

        spec = {
            "trajectoryId": trajectory.metadata.id,
            "trajectoryDigest": trajectory.status.trajectory_digest,
            "sourceBoundary": {
                "position": source_position,
                "hash": trajectory.status.evidence_head,
            },
            "evidenceHead": trajectory.status.evidence_head,
            "sequenceStart": trajectory.status.sequence_start,
            "sequenceEnd": trajectory.status.sequence_end,
            "scoreReports": [
                {
                    "scorer": json.loads(report["body"])["scorer"],
                    "revision": report["revision"],
                    "hash": report["hash"],
                }
                for report in reports
            ],
            "artifacts": [{"id": artifact["id"], "sha256": artifact["sha256"]} for artifact in artifacts],
            "audience": ["*"] if access.full_evidence else [access.participant],
        }
        manifest_dump = trajectory.spec.manifest.model_dump(mode="json", by_alias=True)
        status_without_digest = {
            "recordCount": record_count,
            "artifactCount": len(artifacts),
            "manifestDigest": digest(manifest_dump),
            "recordsDigest": ordered.hexdigest(),
            "complete": trajectory.status.collection.state == "complete",
        }
        snapshot_digest = digest({"spec": spec, "status": status_without_digest})
        snapshot = TrajectorySnapshot.model_validate(
            {
                "apiVersion": API_VERSION,
                "kind": "TrajectorySnapshot",
                "metadata": {
                    "id": "snapshot-" + snapshot_digest[:32],
                    "createdAt": trajectory.metadata.created_at,
                    "labels": {"trajectory": trajectory.metadata.id},
                },
                "features": {"required": [], "optional": []},
                "spec": spec,
                "status": status_without_digest | {"snapshotDigest": snapshot_digest},
                "extensions": {},
            }
        )
        stored = {"snapshot": snapshot.model_dump(mode="json", by_alias=True)}
        with self.store.transaction() as db:
            inserted = db.execute(
                "INSERT INTO trajectory_snapshots VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (
                    snapshot.metadata.id,
                    tenant,
                    environment,
                    encode(stored),
                    snapshot.status.snapshot_digest,
                    datetime.now(UTC).timestamp(),
                ),
            )
            if inserted.cursor.rowcount if hasattr(inserted, "cursor") else inserted.rowcount:
                for sequence, body in bodies:
                    db.execute(
                        "INSERT INTO trajectory_snapshot_records VALUES (?,?,?)",
                        (snapshot.metadata.id, sequence, body),
                    )
        return snapshot

    def _snapshot_body(self, snapshot: str, access):
        access.require("trajectory.read")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT body FROM trajectory_snapshots WHERE id=? AND tenant=?", (snapshot, access.tenant)
            ).fetchone()
        if row is None:
            raise Forbidden("snapshot unavailable")
        return json.loads(row["body"])

    def get_snapshot(self, snapshot: str, access) -> TrajectorySnapshot:
        return TrajectorySnapshot.model_validate(self._snapshot_body(snapshot, access)["snapshot"])

    def list_snapshots(self, environment: str, access, *, limit: int = 100) -> list[TrajectorySnapshot]:
        access.require("trajectory.read")
        with self.store.transaction() as db:
            rows = db.execute(
                """SELECT body FROM trajectory_snapshots
                   WHERE tenant=? AND environment=? ORDER BY created,id LIMIT ?""",
                (access.tenant, environment, limit),
            ).fetchall()
        return [TrajectorySnapshot.model_validate(json.loads(row["body"])["snapshot"]) for row in rows]

    def list_all_snapshots(self, access, *, trajectory=None, limit: int = 100):
        """List immutable snapshots across the authority, newest first."""

        access.require("trajectory.read")
        if not 1 <= limit <= 1000:
            raise ValueError("invalid snapshot page size")
        environment = trajectory
        if environment is not None and environment.startswith("trajectory-"):
            environment = environment[len("trajectory-") :]
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT body FROM trajectory_snapshots WHERE tenant=? "
                "AND (CAST(? AS TEXT) IS NULL OR environment=?) ORDER BY created DESC,id LIMIT ?",
                (access.tenant, environment, environment, limit),
            ).fetchall()
        return tuple(TrajectorySnapshot.model_validate(json.loads(row["body"])["snapshot"]) for row in rows)

    def export_snapshot(self, snapshot: str, access):
        body = self._snapshot_body(snapshot, access)
        yield {"snapshot": body["snapshot"]}
        if "records" in body:  # Read prerelease snapshots written before normalized record storage.
            yield from body["records"]
            return
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT body FROM trajectory_snapshot_records WHERE snapshot=? ORDER BY sequence",
                (snapshot,),
            )
            for row in rows:
                yield json.loads(row["body"])

# Trajectories and historical evidence

EnvironmentHarness keeps two concepts separate:

- A **trace** is the authoritative append-only journal produced by an environment session or an
  imported source.
- A **trajectory** is a portable, immutable projection of that trace. It is safe to inspect,
  snapshot, export, and hand to an authorized downstream consumer without changing the source.

Native evidence remains in the existing EnvironmentHarness journal. Imported evidence remains in
the source journal and its acknowledged local copy. A trajectory never becomes a second writer.

## Resource family

Portable resources use `environmentharness.dev/v1alpha1` and a strict top-level envelope:

- `Policy` identifies recorded policy implementation and lineage without serializing a live model.
- `Trajectory` contains its manifest, segments, causal records, and independent lifecycle states.
- `TrajectorySnapshot` freezes an authorized trajectory at explicit source, score, and artifact
  boundaries.
- `TrajectoryDataset` freezes an ordered, training-entitled set of snapshots.
- `TrainingRun` records the immutable receipt returned by an explicitly injected local integration.

The Python package version and resource API version are independent. Additive optional fields,
resource kinds, and namespaced record types do not require a new API version. See
[Compatibility](COMPATIBILITY.md).

## Episodes, segments, and records

One trajectory represents one complete environment session or imported execution. It can contain
multiple execution segments. A native `session.resumed` event opens a continuation segment while
the first record of that continuation still names the last record of the previous segment as its
cause. A pause or process restart therefore does not pretend that execution was uninterrupted.

Each record has a durable sequence, segment, wall time, zero or more native clock coordinates, and
causal links to earlier records. Sequence and causal links determine ordering; a simulator tick,
market timestamp, or other native clock is preserved without being treated as a global clock.

Built-in evidence can use `environment.observation`, `agent.action`, `inference.generation`,
`environment.reward`, `environment.operation`, `environment.outcome`, `evaluation.report`, and
`artifact.reference`. Existing native event kinds remain readable as recorded. Extensions use a
reverse-domain type such as `com.example.drone.telemetry`; an unknown namespaced record is inert
and round-trips without executing extension code.

The separately owned **Pluggable Decision-Selection Seam** may add typed `decision.requested` and
`decision.selected` records against this envelope. It does not own the journal, trajectory,
snapshot, ingestion, or dataset contracts described here.

## Independent lifecycle states

Do not infer task success from ingestion or process completion:

- `collection` says whether the available evidence is registered, current, lagging, or complete.
- `execution` says whether the source process is unknown, active, paused, completed, or failed.
- `termination` records terminated and truncated independently, with a reason.
- `verifiedOutcome` says whether an authorized outcome is unavailable, pending, successful, or
  another environment-defined state and links to its evidence.

Imported collection health also preserves the acknowledged position and hash, backlog, declared
gaps, and capture failures. A source cannot become complete while any gap, capture failure,
backlog, or unacknowledged terminal boundary remains.

## Register and import a historical source

Historical ingestion is a management capability. It is disabled in `create_app()` unless the
embedding application explicitly passes `trajectory_ingestion=True`; read-only inspection stays
available. Source namespaces are stable namespaced identifiers, and `(tenant, namespace, run_id)`
is immutable.

```python
from environment_harness.trajectories import SourceRecord, SourceRegistration

sources = harness.sources()

source = sources.register(
    SourceRegistration(
        namespace="com.example.simulator",
        run_id="run-42",
        schema_version="simulator.trace.v1",
        environment={"id": "simulator", "version": "1"},
        participants=("alice",),
        purpose="evaluation",
    )
)

record = SourceRecord.create(
    id="frame-1",
    position="1",
    previous_hash="0" * 64,
    type="com.example.simulator.observation",
    segment="segment-1",
    participant="alice",
    revision=0,
    time={
        "wallTime": "2026-09-22T15:00:00Z",
        "native": [{"clock": "simulator.frame", "value": 1}],
    },
    data={"available": True},
    audience=("alice",),
)
acknowledgement = sources.ingest(source.id, (record,))
print(acknowledgement.position, acknowledgement.hash)
```

`SourceRecord.create()` hashes the canonical source body. Retry the identical registration or
batch safely. Reusing an identity with different content, breaking the previous-hash chain, or
skipping the acknowledged boundary fails. The domain runtime should continue writing its local
journal during an ingestion outage and resume from the last acknowledged position and hash.
Importers are read-only: they must never rerun an action, model, simulator, or uncertain side
effect to fill missing history.

Run the complete synthetic walkthrough to create a native multi-segment session, import a separate
historical source, finalize its independent states, freeze it, and stream the snapshot:

```sh
python examples/trajectory_walkthrough.py --store .local/trajectory-walkthrough
environment-harness --store .local/trajectory-walkthrough serve --open
```

Inspect status before or after records arrive:

```sh
environment-harness --store .local/evidence source-status SOURCE_ID
```

## Inspect, page, snapshot, and export

```python
trajectory = repository.trajectory(source.id)
page = repository.records(source.id, after=0, limit=200)
snapshot = repository.freeze(source.id)

for row in repository.export_snapshot(snapshot.metadata.id):
    process(row)  # one bounded manifest or record at a time
```

`Trajectory.status` never materializes a record tuple. The detail resource carries segment
summaries, a record count, the sequence boundary, typed collection health, the independent
lifecycle states, the evidence head, and the digest; records are read only through the paged
stream. `TrajectoryRepository.stream_records` iterates that stream page by page, and freezing a
snapshot computes its ordered record digest incrementally while streaming.

Over HTTP the records live at `GET /v1/trajectories/{id}/records`, and a frozen snapshot's records
at `GET /v1/snapshots/{id}/records` — a typed JSON cursor page by default, or NDJSON when the
request sends `Accept: application/x-ndjson`. The remote Python and TypeScript clients expose both
the paged reader and an explicit streaming iterator.
Snapshot identity covers its manifest, ordered records, source/evidence boundary, selected score
report revisions, artifact digests, schema, and audience projection. Appending evidence, regrading,
or later disclosure creates new records or a new snapshot; it never changes an existing one.

Snapshot export is evaluation inspection and does not require training entitlement. Artifacts are
content-addressed references; copying authorized artifact bytes into an archive is a separate
packaging concern and must preserve their digests.

## Privacy and unavailable evidence

Participant observations are immutable evidence. Later disclosure is a new record, not a rewrite.
Audience filtering applies before export, and extensions cannot grant authority or relax limits.
Missing history, a pending outcome, a source gap, or an unavailable field remains explicit. The
viewer renders record summaries and collection health but suppresses raw extension payloads,
token arrays, and captured inference content by default.

## Standalone SDK boundary

These primitives belong in EnvironmentHarness because environment authors need them without a
hosted product: the evidence originates at the environment boundary and must remain portable. A
downstream product can add workspace storage, retention policy, comparison UX, billing, routing,
or managed training. EnvironmentHarness does not import those product services or schemas.

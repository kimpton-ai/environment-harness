# Data models

EnvironmentHarness has a small, connected vocabulary. This page catalogs it once
so a consumer can tell a resource from a command, a handle, or a status
projection without reading the source.

Three layers stay separate:

```text
Portable resources           Python protocols and factories     Evidence and results
------------------           ------------------------------     --------------------
What should exist            How installed code behaves         What actually happened
JSON serializable            Process-local clients allowed      Immutable, digest-bound
No credential values         Credentials injected               Provider objects excluded
```

## Ownership hierarchy

```text
Experiment
└── Scenario (from a ScenarioSet)
    └── Session
        ├── Checkpoint        (resumable state; may branch to a child Session)
        └── Trajectory        (portable projection of the trace)
            ├── TrajectorySnapshot
            └── TrajectoryDataset → TrainingRun → Policy
```

- **Trace** — the authoritative append-only evidence written by a native session
  or an imported source journal.
- **Trajectory** — the portable, immutable, digest-bound projection of a trace.
  It is never a second journal.
- **Checkpoint** — immutable resumable state for one Session revision. It may
  reference private opaque continuation state and exists to resume or branch
  execution. It is *not* a `TrajectorySnapshot`.
- **TrajectorySnapshot** — an authorized evidence projection frozen at explicit
  source, evidence, score-revision, and artifact boundaries for repeatable
  inspection or export.
- **ScenarioSet** — the ordered, digest-bound *input* collection.
  **TrajectoryDataset** — the *output* collection of completed trajectory
  snapshots. The two are deliberately different types.

There is no `Environment` resource and no `Branch` resource. An
`EnvironmentSpec` is embedded and frozen inside each Experiment and inherited by
its Sessions; environment implementations are typed process dependencies.
Branching accepts a strict `BranchRequest` and returns a child `Session` whose
lineage references its parent and Checkpoint.

## Top-level portable resources

Every one uses the strict `environmentharness.dev/v1alpha1` envelope:
`apiVersion`, `kind`, `metadata`, `features`, `spec`, `status`, `extensions`.
`spec` holds frozen identity and inputs; `status` holds recorded results and
provenance. There is no `metadata.generation` — finalized resources are
immutable.

| Kind | Python import | Addressable at | Meaning |
| --- | --- | --- | --- |
| `ScenarioSet` | `environment_harness.resources.ScenarioSet` | `/v1/scenario-sets` | Ordered scenario references, digests, selection provenance, schema identity, declared aggregate metrics |
| `Experiment` | `environment_harness.resources.Experiment` | `/v1/experiments` | Frozen plan: embedded `EnvironmentSpec`, scenario set, participants, scorers, trials, seed, limits, purpose, split, lock digest |
| `Session` | `environment_harness.resources.Session` | `/v1/sessions` | One concrete attempt for one scenario and trial, with independent status dimensions and lineage |
| `Checkpoint` | `environment_harness.resources.Checkpoint` | `/v1/sessions/{id}/checkpoints` | Immutable resumable state for one Session revision |
| `Policy` | `environment_harness.Policy` | `/v1/policies` | Policy identity, implementation lineage, optional artifact reference, digest |
| `Trajectory` | `environment_harness.Trajectory` | `/v1/trajectories` | Portable projection of one native session or imported execution |
| `TrajectorySnapshot` | `environment_harness.TrajectorySnapshot` | `/v1/snapshots` | Immutable authorized trajectory view at frozen boundaries |
| `TrajectoryDataset` | `environment_harness.TrajectoryDataset` | `/v1/datasets` | Immutable ordered trajectory selection, schema, split, digest, lineage |
| `TrainingRun` | `environment_harness.TrainingRun` | `/v1/training-runs` | Integration request, receipt, resulting policy, metrics, artifacts, limitations |

## Embedded values and references

These are not resources. They appear inside resources and never get their own
collection, database owner, page, or breadcrumb.

| Model | Purpose |
| --- | --- |
| `contracts.EnvironmentSpec` | The environment contract: schemas, scheduling, capabilities, operations, purposes |
| `contracts.Capabilities` | Declared domain capabilities |
| `contracts.OperationSpec` | Frozen identity and public configuration for one environment-supplied operation |
| `contracts.RunPolicy` | Turn, cost, event, artifact, and state limits |
| `contracts.Scenario` | One immutable, serializable input case |
| `contracts.AgentSpec` | Frozen participant registration |
| `contracts.MetricDefinition` | Metric identity, version, unit |
| `resources.ResourceReference` | Kind, ID, API version, digest or revision, and a non-authoritative label |
| `resources.EnvironmentReferenceModel` | The only environment identity that is serialized: `(id, version, specDigest)` |
| `resources.ParticipantBinding` | Participant identity plus policy and config digest |
| `resources.CredentialRequirement` | A named requirement. Credential *values* never appear in portable JSON |
| `trajectories.TrajectoryManifest` | Environment, experiment, scenario, trial, participant, policy, source, schema, lineage, purpose, authority |
| `trajectories.TrajectorySegment` | One addressable execution interval with sequence and lifecycle boundaries |
| `trajectories.TrajectoryRecord` | One discriminated causal evidence record |
| `trajectories.RecordTime` | Optional wall time, process-local duration, and namespaced native clocks |

## Strict commands

Mutation inputs stay frozen and reject unknown fields. A command never accepts a
Python path, callable, pickle, live client, or credential value.

| Command | Python import | Used by |
| --- | --- | --- |
| `ExperimentSpec` | `environment_harness.ExperimentSpec` | Freezing one Session's manifest |
| `Action` | `environment_harness.contracts.Action` | Participant action submission |
| `BranchRequest` | `environment_harness.BranchRequest` | Creating a child Session from a Checkpoint |
| `ScoreReport` | `environment_harness.contracts.ScoreReport` | Publishing a versioned score report |
| `SourceRegistration` | `environment_harness.trajectories.SourceRegistration` | Registering an external source run |
| `SourceIngestionBatch` | `environment_harness.trajectories.SourceIngestionBatch` | Bounded evidence ingestion |
| `SourceStatusUpdate` | `environment_harness.trajectories.SourceStatusUpdate` | Declaring collection health |
| `DatasetCreate` | `environment_harness.training.DatasetCreate` | Freezing a training-entitled dataset |

## Behavior protocols and factories

These are installed Python objects, never serialized.

| Protocol | Responsibility |
| --- | --- |
| `contracts.Environment` | Owns state, valid actions, transitions, rules, and native outcomes |
| `contracts.AgentProgram` | Proposes one action for a delivered observation |
| `contracts.Scorer` | Evaluates immutable evidence and returns a score report |
| `operations.EnvironmentOperation` | One atomic externally visible effect with its own receipt |
| `training.TrainingIntegration` | Consumes a qualified dataset and returns a receipt |
| `harness.SessionRunner` | Advances one durable session; an advanced TDD seam |

Environment implementations are selected by object or class identity through the
typed factories supplied to one `EnvironmentHarness`. Only the portable
`(id, version, specDigest)` reference is serialized; nothing resolves code from
persisted text.

## Execution facade versus portable resource

The execution facade and the portable resources deliberately share nouns. They
are different layers, and the import path disambiguates them.

| Name | Import | What it is |
| --- | --- | --- |
| `EnvironmentHarness` | `environment_harness` | The only public local-execution facade |
| `Experiment` | `environment_harness` | A mutable local builder and handle (`start`, `wait`, `stop`, `resume`, `result`) |
| `Experiment` | `environment_harness.resources` | The immutable portable resource |
| `EnvironmentSession` | `environment_harness` | A typed domain handle for one session |
| `Session` | `environment_harness.resources` | The immutable portable resource |
| `ExperimentResult` | `environment_harness` | A local aggregate snapshot of a handle's sessions |

Call `handle.resource()` to project a handle into its portable resource:

```python
experiment = harness.experiment("study", scenarios, trials=2, turns=5)
result = experiment.run()
portable = experiment.resource()          # resources.Experiment
scenarios = experiment.scenario_set()     # resources.ScenarioSet
session = result.sessions[0].resource()   # resources.Session
```

## Status is a projection, not an authority

`Session.status` keeps its dimensions independent. None of them implies another,
and a finished process, an accepted command, or a completed ingestion batch
never proves task success.

| Dimension | Question it answers |
| --- | --- |
| `collection` | Is evidence capture current, lagging, gapped, failed, or complete? |
| `execution` | Is the native runner pending, active, paused, recovering, completed, cancelled, failed, blocked, or uncertain? |
| `termination` | What is the environment-native terminated/truncated state and reason? |
| `verifiedOutcome` | What did domain verification conclude, with which evidence? |
| `scoreReadiness` | Are score reports available for this session? |
| `reconciliation` | Is an explicit operator correction outstanding? |

`Experiment.status` is a derived aggregate: planned, created, pending, running,
completed, failed, cancelled, and uncertain counts plus timestamps, the frozen
lock digest, score references, and limitations. Status projections can be
rebuilt from authoritative records and must not become a competing journal.

Status values are documented, evolvable strings with unknown-value
preservation, not closed `Literal` unions.

## Generated artifacts

Each resource and command above publishes a standalone JSON Schema under
[`contracts/`](../contracts) named after its Python class — for example
`contracts/Session.schema.json`. The authenticated HTTP surface is described by
`contracts/openapi.json`. Regenerate both with:

```sh
uv run python scripts/build_contracts.py
uv run python scripts/build_openapi.py
```

Python is the canonical digest authority for `v1alpha1`. The TypeScript client
consumes and displays server-supplied digests and never recomputes them.

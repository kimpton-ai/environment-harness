# Changelog

## Unreleased

### Breaking changes

- **The public four-role authorization model is removed.** `Principal`,
  `Principal.role`, and the `researcher`/`agent`/`scorer`/`worker` vocabulary are gone from the
  SDK, HTTP API, OpenAPI (`x-roles`), generated artifacts, and examples. A remote caller sends only
  an opaque bearer credential; the server resolves it to an `admin`, `viewer`, or `participant`
  policy it owns. Requests can never assert a policy, role, or permission.
- **Every pre-`0.3.0rc1` credential is deleted.** Numbered migration `005_credential_policies`
  drops all legacy principal rows transactionally. Old bearer tokens return `401`; reissue through
  `environment-harness token`, the embedding API, or the participant-credential operation. See
  [Authentication](docs/AUTHENTICATION.md).
- **The `/v1/environments` surface is replaced** by the canonical short resource hierarchy
  (`/v1/scenario-sets`, `/v1/experiments`, `/v1/sessions`, `/v1/policies`, `/v1/trajectories`,
  `/v1/snapshots`, `/v1/datasets`, `/v1/sources`, `/v1/training-runs`, `/v1/comparisons`,
  `/v1/capabilities`, `/v1/activity`). Old paths return `404`. `GET /v1/environment` is removed:
  `EnvironmentSpec` is frozen inside each Experiment and inherited by its Sessions. Every 0.2
  operation is classified exactly once in the enforced
  [HTTP migration manifest](docs/HTTP-MIGRATION.md).
- **`Trajectory.status.records` is removed.** Records are a cursor-paged stream read through
  `GET /v1/trajectories/{id}/records`, `TrajectoryRepository.records_page`, or
  `TrajectoryRepository.stream_records`.
- **`ActivitySnapshot` is renamed `ActivityHierarchy`** and `GET /v1/activity/snapshot` becomes
  `GET /v1/activity/hierarchy`, so "snapshot" names only the immutable `TrajectorySnapshot`.
- **Management collections return one typed envelope** (`items`, `nextCursor`, `links`) with an
  opaque cursor. Durable evidence and activity feeds keep their integer cursors.
- **Snapshot and dataset export uses content negotiation** on `/records` instead of an `/export`
  verb path.
- Viewer routes are plural: `/overview`, `/experiments/{id}`, `/sessions/{id}`,
  `/trajectories/{id}`, `/comparisons`. `/home`, `/compare`, `/experiment/{id}`,
  `/session/{id}`, and `/trajectory/{id}` are gone, a detail root is equivalent to its `Overview`
  tab, and `serve --open` now opens `/overview`. `create_synthetic_showcase` returns
  `review.overview` in place of `review.home`.
- **A custom `SessionRunner` is called with a typed `SessionControl`**, not the private session
  runtime, environment-session ID, and access context. Replace
  `runner(session, environment, access, agents, turns=...)` with
  `runner(control, agents, turns=...)` and use `control.advance`, `control.observation`,
  `control.lease`, `control.release`, `control.prepare_operation`, and
  `control.dispatch_operation`. The default runner is the exported `run_session`. See
  [Environment authoring](docs/AUTHORING.md).
- **`EnvironmentHarness` constructor arguments are renamed and one is removed.**
  `environment_factory=` becomes `environment=`, `agent_factories=` becomes `agents=`, and the
  separate `environments=` sequence parameter is gone — `environment=` accepts one class or a
  sequence of them. Passing both previously dropped `environment_factory` silently. Passing an
  instance now explains that the harness builds a fresh environment per session. The durable
  blocked reason `environment_factory_unavailable` becomes `environment_not_configured`.

  ```python
  # before
  EnvironmentHarness(store, environment_factory=MyEnvironment, agent_factories={"alice": MyAgent})
  # after
  EnvironmentHarness(store, environment=MyEnvironment, agents={"alice": MyAgent})
  ```
- **`GET /v1/sessions/{id}/invocations` returns `items`**, replacing the `work` key on the removed
  `/v1/environments/{environment}/agent-work` route.
- **Token-faithful inference capture is now entitled and budgeted.** `RunPolicy.inference_capture`
  defaults to `summary`; `training` requires `purpose`/`split` of `training` and a non-zero
  `RunPolicy.max_inference_artifact_bytes`. Experiments that previously recorded rendered requests,
  responses, token IDs, or log probabilities by default now record summaries only until they opt in.

### Added

- `EnvironmentHarness` is the only public local-execution facade, returning a typed
  `EnvironmentSession` handle that neither inherits from nor exposes the private session runtime.
  The handle adds `advance`, `checkpoint`, `branch`, `resource`, `records`, `snapshot`, `report`,
  `artifact`, and `participant_credential`, and `harness.sources()` exposes the trusted local
  trajectory, source, and dataset surface.
- Restart-safe local scheduling: startup reconciliation, durable submission-failure recording, and
  typed environment factories selected by object identity with only
  `(id, version, spec_digest)` serialized. A missing or mismatched factory leaves a Session
  durably `blocked`.
- Portable `ScenarioSet`, `Experiment`, `Session`, and `Checkpoint` resources in
  `environment_harness.resources`, with a strict `BranchRequest` that returns a child Session.
- `GET /v1/capabilities`, `x-capability` annotations, and `501 capability_unavailable`.
- One stable error taxonomy shared by Python, HTTP, OpenAPI, and TypeScript.
- `docs/AUTHENTICATION.md`, `docs/DATA-MODELS.md`, `docs/HTTP-MIGRATION.md`, and
  `docs/DECISION-RUNTIME.md`.
- Inference capture levels `none`, `summary`, and `training`, a cumulative per-session artifact
  budget that fails closed, and representative storage estimates in
  [Training](docs/TRAINING.md).
- The accepted viewer information architecture: four global destinations
  (`Overview | Experiments | Sessions | Trajectories`), one contextual left navigation per selected
  resource, ownership-ancestry breadcrumbs that stop at the parent, a reserved-height context bar
  with same-height loading skeletons, narrow-screen ancestry collapse, and contextual training
  provenance. See [Viewer maintenance](docs/VIEWER-MAINTENANCE.md).
- `SessionControl` and `run_session` are exported, so a custom session runner composes domain
  operations without receiving an authorization value.

### Fixed

- Three unreachable guards are removed rather than left to read as protection they cannot provide:
  a `Checkpoint` continuation-state check that its own required field already enforced, a duplicate
  16 MiB artifact bound the request middleware applies first, and a third copy of
  "trajectory has no evidence" inside `freeze`, which `get` raises before it is reached.
- `403 cross_origin_denied` is a distinct error code again. The taxonomy rewrite had collapsed it
  into `forbidden`, which contradicts the documented meaning of that code: a cross-origin rejection
  happens before any credential is consulted, so it is a browser-boundary rejection rather than an
  authorization decision.
- A background catalog refresh no longer drops keyboard focus out of an open viewer listbox.
- Switching viewer destinations while the catalog request is in flight no longer lets the older
  request repaint over the newer destination.

EnvironmentHarness now projects native and imported traces into additive `v1alpha1` Policy,
Trajectory, TrajectorySnapshot, TrajectoryDataset, and TrainingRun resources. Historical sources use
immutable namespaced registrations, bounded hash-chained ingestion, idempotent retries, explicit
acknowledgement cursors, and independently inspectable collection, execution, termination, and
verified-outcome state. Native pause/resume histories become causally linked continuation segments.

Python, TypeScript, HTTP, and CLI surfaces now page trajectory records and stream reproducible
snapshot/dataset JSONL. Training datasets require complete terminal training evidence, compatible
schemas, and resolved finite reward supersession chains. `InstrumentedModel` correlates model calls
to durable agent work and spills oversized detail to participant-scoped artifacts. Trainer plugins
remain explicit local Python/CLI integrations; the server only reads immutable receipts.

The viewer adds imported trajectory health/segment inspection and conditional experiment Training
details without exposing raw extension or inference payloads. The Verifiers bridge now matches the
declared `>=0.3.1,<0.4` extra and derives authorized rows from canonical trajectories. Optional
integration changes have a path-routed CI job. RLlib, TRL, live OpenEnv training, Parquet, and the
separately owned decision-selection seam are not claimed by this change.

Typed scenarios now expand into bounded concurrent experiment trials with durable
status, deterministic seeds and resumable activity feeds. The evidence viewer adds
experiment grouping, filtering, unequal-length comparison, progression and report
history. The HTTP service now checks in generated OpenAPI, validates command and
operation request shapes, and returns one traceable error envelope across routes and
both SDK clients.

The viewer now expands experiment rows directly into environment sessions instead of adding a
label-only scenario layer. Session rows show scenario and trial identity, status, turn progress,
participants and latest activity; experiment names open stable detail routes. Activity pages and
snapshots now have checked-in JSON Schemas and concrete OpenAPI response models.
Experiment detail routes now separate shared frozen configuration, clickable scenario snapshots,
and environment sessions. Empty/default scenarios do not add navigation; meaningful scenarios
show structured input, reference, metadata, progress, and a filtered path to their sessions.

Environment packages can now expose typed `EnvironmentOperation` classes for
engine-specific or imperative capabilities. Environment and experiment manifests
freeze portable `OperationSpec` identities and JSON configuration, while runtime
clients, credentials and process-local handles remain on the environment instance.
Specialized motor APIs have been removed from the prerelease SDK; integrations use
the environment-owned operation seam without adding domain-specific types to core.

`EnvironmentHarness` now accepts an optional Python `session_runner`, allowing grouped experiments
to interleave ordinary turns with environment operations without replacing scheduling or durable
status. A runnable custom-environment experiment records operation receipts, comparable scores and
evidence-linked findings, and the CLI/browser timelines describe operation activity directly.
The public `SessionRunner` protocol documents this extension boundary. Experiment startup now
validates environment operations before queueing work, runner results must identify the current
environment-session record, and conformance reports how many operation classes it checked.
An external-simulator example keeps the domain client and operation outside core while exercising
a real subprocess connection, explicit external-write policy, idempotent receipts, scoring,
findings and the same multi-session viewer workflow.

Native implementations can run behind authenticated HTTP workers while a trusted
supervisor retains the evidence store. External participants can advance ready
phases through the fenced coordinator. A versioned legacy adapter preserves old
records, and an ORS HTTP/SSE client retains original task receipts without implicit
write retries. See [remote workers](docs/REMOTE-WORKERS.md).

Hosted PostgreSQL/S3 storage adds aggregate environment payload admission and
explicit tenant erasure with confirmed object deletion. Erasure includes experiment,
scenario, environment-session and activity metadata introduced by this release.

Production PyPI publishing now uses short-lived Trusted Publishing credentials and publishes only the reviewed Python wheel and source distribution. Releases support PEP 440 alpha, beta, and release-candidate versions while mapping those versions to npm-compatible SemVer prereleases. Package metadata now includes the rendered README, classifiers, project links, and the `py.typed` marker.

Deployment, release, and viewer-maintenance guides now define the supported local topology, hosted-service qualification boundary, prerelease workflow, generated browser assets, required checks, and documentation triggers for future contributors.

## 0.2.2 - 2026-09-18

Release publishing now extracts immutable-ID artifacts directly into the verified distribution directory before checking attestations and explicitly disables dependency caching during the tag build. Routine Dependabot version updates are grouped into one monthly pull request per ecosystem, while security updates remain immediate.

## 0.2.1 - 2026-09-17

The public SDK now includes coordinated vulnerability reporting, governance and support files, dependency cooldowns, immutable workflow and container pins, credential-free security scans, cross-platform CI, positive distribution manifests, installed-wheel smoke tests, SBOM and tag-triggered artifact-attestation release automation, and protected-release setup guidance. Release maintainers create the reviewed protected tag manually; Actions stores no long-lived release credential. Python and TypeScript coverage gates require at least 90% statements/lines and 80% branches; security-critical Python modules meet those floors independently.

Session recovery now rejects stale worker completion and protects reconciled responses. Cancellation works while a writer is active, terminates managed command process groups and reports unresolved effects. The runner validates registered agent implementations, and conformance supports coordinator closure and explicit event inputs.

Comparisons keep incompatible metric definitions and units in separate groups. Legacy reports remain readable as raw values. Large inherited records use bounded chunks and can be branched repeatedly without adding encoding layers. Existing stores require no migration. See [compatibility and protocol details](docs/COMPATIBILITY.md).

The command line gains `list`, `show` and `timeline`. `timeline` groups recorded events by the state revision an action was taken from and prints one line per participant with the observation, the attempted action and the executed outcome; `--participant`, `--kind` and `--verbose` narrow or expand it. `compare` now prints a readable summary by default, and `--json` on these commands prints the underlying records, so scripts that parsed `compare` output should pass `--json`. Comparison records include `participants` and `revision`.

The browser viewer is read-only. It groups the timeline by revision, names environments by their participants and role, collapses inherited history into one line, renders scores as cards, and uses a sandstone palette that follows the system light or dark preference. Checkpoint, pause, cancel and branch controls moved out of the viewer; they remain command-line and SDK operations.

The README's local startup now uses `serve --open` to open and connect the viewer automatically. A single-use loopback connection ticket avoids copying credentials, and tab-scoped storage keeps the local viewer connected across refreshes. Supplier API authentication remains required.

## 0.1.0

EnvironmentHarness provides persistent shared environments, participant-specific observations, checkpoints, isolated branches and recorded evidence. The local viewer supports inspection, comparison and JSONL export.

The release includes Python and TypeScript clients, an authenticated HTTP service, and runnable synthetic examples. The examples require no account or model API key. Their results demonstrate the SDK workflow and do not measure model intelligence or safety.

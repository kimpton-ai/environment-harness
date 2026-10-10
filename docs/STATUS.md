# Release scope

EnvironmentHarness 0.4.0rc1 builds on the local persistent sessions and recorded evidence in 0.3.0. The SDK also supports typed scenario snapshots, bounded local experiment concurrency, deterministic scenario/trial seeds, durable experiment status and resumable authenticated activity feeds. The synthetic examples exercise shared state, participant-specific observations, changing rewards, versioned score history, an attributed malformed-action finding, artifacts, explicit checkpoints, isolated branches, agent execution and JSONL export. The command line lists, shows and renders turn-grouped timelines of recorded environments; the read-only viewer presents the same evidence in a browser. Both run locally without a model account.

The package includes an authenticated supplier HTTP service, typed Python/TypeScript clients, generated JSON schemas and optional adapters. The [adapter table](ADAPTERS.md) records their boundaries. A packaged integration is not proof that its upstream service or cloud backend has been qualified.

## 0.4.0rc1 scope

This candidate adds opt-in, durable hosted artifact operation budgets for PostgreSQL-backed
environment sessions. A host can require an immutable budget before session execution; provider
attempts, transfer bytes, branch copies, and exact-prefix cleanup are journaled against it.
Migrations `007` and `008` add the required storage. Legacy sessions retain their existing artifact
behavior unless the host marks them as budget-required. See [Deployment](DEPLOYMENT.md) for the
host integration contract and cleanup limits. This candidate does not qualify a production hosted
service.

## 0.3.0 scope

`0.3.0` finalizes the published `0.3.0rc1` contract. The package set and public feature surface are
unchanged. The release checks include an installed-wheel walkthrough of the public journey.

The release includes:

- **Authorization boundary.** `Principal` and the public four-role model are removed. A remote
  caller sends only an opaque bearer credential and the server resolves it to one of three fixed
  policies. A private `_SessionRuntime` requires an access context on every observation and
  mutation, `EnvironmentHarness` is the only public local-execution facade, and numbered migration
  `005_credential_policies` deletes every legacy credential row and forces reissue. See
  [Authentication](AUTHENTICATION.md).
- **Restart-safe local scheduling.** Persisted Experiment and Session rows are the durable queue.
  Startup reconciliation reconstructs queued work, marks orphaned running work interrupted behind
  explicit resume, and leaves terminal rows untouched. Typed environment factories are configured
  once per harness; only `(id, version, spec_digest)` is serialized and a missing or mismatched
  factory leaves a Session durably blocked.
- **Portable experiment resources.** `ScenarioSet`, `Experiment`, `Session`, and `Checkpoint` join
  the resource family with a shared experiment fixture and an enforced contract-compatibility
  suite. See [Data models](DATA-MODELS.md).
- **Paged trajectory records.** `Trajectory.status` no longer materializes records; both native and
  imported projections stream. The bounded-allocation gate proves a 100,000-record export reads
  through pages of at most 1,000 records within 32 MiB of tracemalloc-reported allocation.
- **Corrected HTTP hierarchy.** The `/v1/environments` surface is replaced by the canonical short
  hierarchy, with a typed management-list envelope, ETags, `Location` headers, deployment
  capabilities, and one stable error taxonomy. Every 0.2 operation is classified exactly once in
  the enforced [HTTP migration](HTTP-MIGRATION.md) manifest.
- **Inference capture levels.** `none`, `summary`, and `training` are explicit, token-faithful
  capture requires a training entitlement and a bounded budget, and a cumulative per-Session
  artifact budget fails closed. See [Training](TRAINING.md).
- **Viewer information architecture.** The viewer has exactly four global destinations —
  `Overview | Experiments | Sessions | Trajectories` — one contextual left navigation per selected
  resource, ownership-ancestry breadcrumbs that stop at the parent, a reserved-height context bar
  with same-height loading skeletons, and narrow-screen ancestry collapse. Training provenance
  stays contextual. See [Viewer maintenance](VIEWER-MAINTENANCE.md).
- **Documentation.** Every guide named in the plan's documentation section has been rewritten for
  this contract, including the new [Decision runtime](DECISION-RUNTIME.md) guide, the
  [downstream-impact appendix](RELEASING.md#downstream-impact-appendix) with its last compatible
  pin and per-change migration, the version-range and digest-guarantee sections of
  [Compatibility](COMPATIBILITY.md), and the source-journal responsibilities in
  [Adapters](ADAPTERS.md).

- **Fresh-environment acceptance.** `scripts/check_release.py` installs the built wheel outside the
  checkout and runs the documentation walkthrough against it: a multi-segment native trajectory, a
  historical import resumed from its acknowledged cursor in a second process, the independent
  status dimensions, a byte-identical snapshot re-export, and evaluation export without a training
  entitlement. It is a required check, not a manual procedure. See
  [Release process](RELEASING.md#the-fresh-environment-walkthrough).

The [frozen candidate manifest](RELEASING.md#frozen-030rc1-manifest) defines the coordinated
artifacts, contracts, and exclusions. The [downstream-impact appendix](RELEASING.md#downstream-impact-appendix)
lists every breaking change and its migration. See [Trajectories](TRAJECTORIES.md),
[Training](TRAINING.md), and [Compatibility](COMPATIBILITY.md) for the supported boundaries.

## Verified release workflow

The release checks cover the documented synthetic experiment, parent and branch totals, lineage reporting, participant-private event filtering, custom command execution, core runtime tests, schema drift, TypeScript compilation, package contents and clean installation. Release artifacts include a separate verification record identifying the exact build and checks performed.

## Limits

- The example's synthetic total is a protocol illustration, not a measure of model intelligence, safety or economic performance.
- Branches share lineage. One parent and its descendants cannot establish uncertainty across independent environments.
- Agent-state recovery requires explicit serialization hooks. Arbitrary process memory, sockets and external effects are not restored by a checkpoint.
- Management credentials authorize all evidence in their scope. Viewer perspective selection is a display filter; participant credentials enforce the actual HTTP boundary.
- The trusted local Python API and command subprocess do not isolate hostile code. Container boundaries and scoped network access are separate requirements.
- Resume marks committed state ready for execution. Advancing turns still requires the runner and matching agent implementations.
- Hosted capacity, extended lifecycle, backup/restore operations and external-provider integrations require their own acceptance evidence. This local release makes no availability or throughput guarantee.
- Hosted environment admission, managed training and supplier payouts are not supplied by this SDK release.
- Source registration or execution completion does not prove task success. Collection, execution,
  termination, and verified outcome must be inspected independently.

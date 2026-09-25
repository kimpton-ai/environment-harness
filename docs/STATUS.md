# Release scope

EnvironmentHarness 0.2.4rc2 focuses on local persistent sessions and recorded evidence. The SDK also supports typed scenario snapshots, bounded local experiment concurrency, deterministic scenario/trial seeds, durable experiment status and resumable authenticated activity feeds. The synthetic examples exercise shared state, participant-specific observations, changing rewards, versioned score history, an attributed malformed-action finding, artifacts, explicit checkpoints, isolated branches, agent execution and JSONL export. The command line lists, shows and renders turn-grouped timelines of recorded environments; the read-only viewer presents the same evidence in a browser. Both run locally without a model account.

The package includes an authenticated supplier HTTP service, typed Python/TypeScript clients, generated JSON schemas and optional adapters. The [adapter table](ADAPTERS.md) records their boundaries. A packaged integration is not proof that its upstream service or cloud backend has been qualified.

## Unreleased 0.3.0rc1 scope

Commit `43346e7` is the landed implementation baseline for the trajectory program, not proof that
the contract is qualified. The following corrections have landed on top of it and are tracked as
implemented; the release candidate is not cut until the remaining items below are complete.

Landed since the baseline:

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

Remaining before the candidate:

- the fresh-environment documentation walkthrough that installs the built wheel, records a
  multi-segment synthetic trajectory, imports a synthetic historical source with a restart-safe
  cursor, inspects collection/execution/outcome state in the viewer, freezes and re-exports a
  snapshot, and verifies evaluation export without training entitlement; and
- the coordinated manifest freeze and the `0.3.0rc1` version bump itself.

Explicitly outside the candidate manifest: Parquet export, RLlib conversion and external-environment
support, TRL integration, live OpenEnv training, remote training workers, and the separately owned
**Pluggable Decision-Selection Seam**. The trajectory foundation has no dependency on any of them.

## Unreleased trajectory scope

The next candidate adds the `environmentharness.dev/v1alpha1` portable resource family. Native and
namespaced historical journals share one trajectory listing, segmented causal record view,
immutable snapshot/export path, and read-only viewer. Historical ingestion is explicitly configured
and source health keeps acknowledgement, backlog, gaps, and capture failures distinct from
execution, termination, and verified outcome.

Training-entitled terminal trajectories can be frozen into immutable datasets. Reward
supersession chains are validated, inference detail spills to restricted artifacts when necessary,
and explicitly injected local training integrations record immutable receipts. The HTTP service
never executes trainer code. The bounded Verifiers legacy bridge is qualified separately against
`>=0.3.1,<0.4` and consumes canonical trajectories.

The candidate manifest does not include RLlib, TRL, live OpenEnv training, Parquet, managed jobs,
remote training workers, or the independently owned **Pluggable Decision-Selection Seam** unless
those complete before manifest freeze. The trajectory foundation has no dependency on those
packages. See [Trajectories](TRAJECTORIES.md), [Training](TRAINING.md), and
[Compatibility](COMPATIBILITY.md).

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

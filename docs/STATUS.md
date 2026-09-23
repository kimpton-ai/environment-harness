# Release scope

EnvironmentHarness 0.2.4rc2 focuses on local persistent sessions and recorded evidence. The SDK also supports typed scenario snapshots, bounded local experiment concurrency, deterministic scenario/trial seeds, durable experiment status and resumable authenticated activity feeds. The synthetic examples exercise shared state, participant-specific observations, changing rewards, versioned score history, an attributed malformed-action finding, artifacts, explicit checkpoints, isolated branches, agent execution and JSONL export. The command line lists, shows and renders turn-grouped timelines of recorded environments; the read-only viewer presents the same evidence in a browser. Both run locally without a model account.

The package includes an authenticated supplier HTTP service, typed Python/TypeScript clients, generated JSON schemas and optional adapters. The [adapter table](ADAPTERS.md) records their boundaries. A packaged integration is not proof that its upstream service or cloud backend has been qualified.

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
- Researcher credentials authorize all evidence in their scope. Viewer perspective selection is a display filter; participant credentials enforce the actual HTTP boundary.
- The trusted local Python API and command subprocess do not isolate hostile code. Container boundaries and scoped network access are separate requirements.
- Resume marks committed state ready for execution. Advancing turns still requires the runner and matching agent implementations.
- Hosted capacity, extended lifecycle, backup/restore operations and external-provider integrations require their own acceptance evidence. This local release makes no availability or throughput guarantee.
- Hosted environment admission, managed training and supplier payouts are not supplied by this SDK release.
- Source registration or execution completion does not prove task success. Collection, execution,
  termination, and verified outcome must be inspected independently.

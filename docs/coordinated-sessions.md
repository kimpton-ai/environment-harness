# Coordinated persistent sessions

## Trajectory continuity

An environment session is the complete execution identity. Pausing and resuming it creates a new
trajectory continuation segment rather than a new environment session. The resume record begins
that segment and remains causally linked to the final record of the prior segment. A branch is a new
environment session with parent/checkpoint lineage; it does not overwrite the parent's trajectory.

Concurrent participants and overlapping work use durable operation IDs and causal links. A final
decision may authorize zero, one, or many operations, and fan-out/fan-in must preserve those exact
links; no consumer should reconstruct causality from timestamps. The decision payload itself is
owned by **Pluggable Decision-Selection Seam**, while the journal and trajectory envelope remain
core EnvironmentHarness contracts.

`EnvironmentSpec.phase_deadline` defaults to `wall`. An environment may declare `coordinator` when only explicit phase closure advances execution. For that mode, a trusted researcher or worker with the current writer lease calls `close_phase(environment, principal, lease, revision=...)` before `resolve`. Closure is durable and idempotent. Actions submitted after closure are rejected. Waiting for inference or reconnecting does not change simulation time.

`AgentJournal` stores serializable, revision-scoped work through the participant checkpoint hook. A program can preserve tool responses and final decisions before submitting an action. State writes check participant authority and the expected environment revision. External operations still use the operation journal and receipt reconciliation; agent memory does not authorize redispatch of an ambiguous effect.

Checkpoints capture an evidence cursor and artifact inventory. A branch inherits events through `history.inherited` envelopes or bounded `history.inherited.chunk` records, which preserve the original event row and audience. Child-owned artifact copies and aliases allow handles embedded in checkpointed memory to resolve inside the child. This does not grant access to parent artifacts or make parent credentials valid in the child. Newly delivered observations and decisions use new branch-local identities. Scorers should distinguish inherited history from new decisions.

Findings can identify omissions with a null `action_id`, a delivered observation, and an `opportunity_event`. The opportunity and outcome must identify the same participant and fall within the report cursor. `status="omitted"` describes a recorded omission; uncertainty can instead make the finding inconclusive. `action_item` optionally identifies an item within a composite decision without imposing a domain action schema on the harness.

These additions are generic. Supplier mechanics, private datasets, domain graders, and visualization plugins remain outside this repository.

## Dispatch and transition recovery

The built-in runner records prepared agent work before dispatch, then persists
responses and explicit continuation state before submitting a stable action ID.
A restarted runner reuses accepted actions and saved responses. Ambiguous work
stops with a reconciliation error instead of repeating an agent call.

Researchers and scoped agents can inspect `/v1/environments/{environment}/agent-work`.
An authorized researcher can recover a known completed result using the
`reconcile_agent` command with its operation ID, response, continuation state
and lookup or operator evidence. Recovery is audited. It does not make another
model call. Checkpointing rejects unresolved work and responses awaiting action
submission so a branch cannot silently lose a decision.

Writer leases renew during inference and environment computation. Coordinated
phases close before resolution. `run(..., phase_timeout=300)` bounds how long
the coordinator waits for agent responses. Arbitrary Python threads remain
cooperative and may finish later. Use bounded command or isolated process
backends when the agent itself must be terminated at its deadline.

Environment initialization, observation and transition computation run outside
metadata transactions. A transition intent freezes its inputs and RNG; the
commit rechecks revision, inputs, status and writer lease. Cached computations
can be reused after recovery. Pure environment computation may be repeated
after a crash before its result is recorded. External effects must use the
operation journal. Branch artifact copying still runs inside its transaction.

PostgreSQL uses explicit environment-lock and private-event queries plus checked
schema migrations in `src/environment_harness/migrations`. Parameter-marker adaptation
does not rewrite ordering, conflict handling or JSON membership semantics.
Run `PostgresEvidenceStore.initialize()` as the schema owner before starting
workers. The application constructor does not run migrations.

[Compatibility details](COMPATIBILITY.md) describe cancellation, stale response guards and reconstruction of inherited records.

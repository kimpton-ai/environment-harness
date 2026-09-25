# Session reliability and compatibility

## Portable-resource evolution

Trajectory resources begin at `environmentharness.dev/v1alpha1`. The Python package version is
independent. Additive optional fields, resource kinds, and namespaced record types stay in this
family. A new API version is reserved for an incompatible removal, restructure, validation change,
or semantic change and requires an explicit converter while both forms are served.

Top-level envelopes and mutation inputs remain strict. Nested portable evidence preserves unknown
optional fields recursively; unknown required features fail. Unknown namespaced records decode as
inert extension records and survive parse/serialize. Preserved fields participate in canonical
digests. Python's sorted compact finite JSON encoding is the `v1alpha1` digest authority;
TypeScript consumes server-supplied digests and does not recompute them.

Existing environment manifests, events, reports, checkpoints, actions, and evidence hashes are not
rewritten. Policy resources are synthesized from existing participant implementation and
`policy_version`. The earlier action-row rollout export remains readable but is not the portable
trajectory authority; consumers migrate to trajectory snapshots and datasets.

EnvironmentHarness is the upstream authority for the public environment-boundary contracts. A
downstream product may need a version pin, adapter, or intentional breaking migration when adopting
them; product storage and APIs do not become SDK contracts merely to avoid that migration. No
downstream product code or private schema is included in this repository.

## What upgrading to `0.3.0rc1` does and does not change

Existing SQLite and PostgreSQL stores stay readable, and no stored manifest, event, report,
checkpoint, action, artifact, or evidence hash is rewritten. Two numbered migrations do run:

| Migration | Effect | Rewrites evidence? |
| --- | --- | --- |
| `005_credential_policies` | Deletes every legacy credential row inside one transaction and recreates `credentials` with `tenant`, `subject`, `policy`, `session`, `participant`, `generation`, `expires`, `revoked` | No |
| `006_scheduler_recovery` | Adds `environment_id`, `environment_version`, `spec_digest`, and `blocked_reason` to `session_runs` | No |

Both are idempotent. SQLite applies them when the store is opened and records them in
`schema_migrations`; PostgreSQL applies them through `PostgresEvidenceStore.initialize()` run by the
schema owner. Legacy `session_runs` rows with null reference columns project read-only into the
portable resources without being rewritten, which is covered by a contract fixture.

**Every pre-`0.3.0rc1` bearer credential stops working.** The `credentials` table stored the whole
`Principal` as JSON and revalidated it against a strict model, so every legacy row fails to parse
once the field is gone. The project treats those rows as disposable and accepts a forced reissue
rather than carrying the discarded role taxonomy into a compatibility mapper. Reissue through
`environment-harness token`, the embedding API, or the participant-credential operation.

Client calls do **not** keep their existing arguments in this release. The public API surface,
HTTP routes, and list envelopes changed; see the per-item table in
[Release process](RELEASING.md#downstream-impact-appendix) and the machine-readable
[HTTP migration](HTTP-MIGRATION.md) manifest.

## Version ranges and what a version promises

| Surface | Versioning | What it promises |
| --- | --- | --- |
| Python package | PEP 440 (`environment-harness`) | Ordinary SemVer-style intent for the Python API. Independent of the resource API version. |
| TypeScript client | SemVer, same coordinated number | Same remote contract as the Python package of that release. |
| Portable resource API | `environmentharness.dev/v1alpha1` | Additive evolution only. An incompatible change requires a new API version served alongside the old one with an explicit converter. |
| Worker protocol | `environment-worker.v1` | Private transport with its own shared secret, size boundary, and no OpenAPI document. Not part of the public HTTP contract. |
| Legacy environment bridge | `world-session.v1` | Readable as a distinct native execution identity. Historical manifests still need their original reader. |
| Verifiers bridge | `>=0.3.1,<0.4` | A bounded legacy rollout invocation, installed and checked in a path-routed job. |

A **package prerelease is not contract graduation.** `0.3.0rc1` publishes `v1alpha1` resources; the
package leaving prerelease does not promote the resource API out of `v1alpha1`, and graduating the
resource API is a separate, explicitly announced decision with its own converter obligations. Do not
read a stable package version as a stability claim about `v1alpha1`.

Enforced fixtures, not prose, are what make this evolution policy real. `tests/test_contract_fixtures.py`
is a required CI gate covering strict first-party writes, lenient compatibility reads, legacy-store
projection, unknown optional versus unknown required features, unknown namespaced record types,
digest participation for preserved data, and the minimum decision payloads. Schema drift is checked
separately by `scripts/build_contracts.py --check`; path classification or schema drift alone is not
compatibility qualification.

## Digest guarantees and non-guarantees

Canonical digests **do** provide reproducible identity, change detection, and evidence-chain
integrity when the verifier trusts its copy or its source boundary. Repeated export of a frozen
snapshot preserves canonical manifest and JSONL contents and artifact digests, and digests are
stable across supported Python versions.

They **do not** authenticate data against an attacker who controls the store. An unsigned digest is
not proof of provenance or tamper resistance against the store operator. Authenticity requires the
separately specified signature or attestation boundary in [Protocol](PROTOCOL.md). Do not describe
an unsigned digest as malicious-store tamper detection.

Python is the sole digest authority for `v1alpha1`. TypeScript treats a server-supplied digest as
opaque and never recomputes it, which is asserted by a client test.

## Downstream breaking-change policy

EnvironmentHarness is the upstream authority for the public environment-boundary contracts, so a
downstream product may need a pin, adapter, or intentional breaking migration when adopting them.
The rules are:

1. **A downstream migration is never a reason to pick the wrong contract.** Choose the correct
   upstream contract, then record the downstream cost.
2. **Every breaking change gets an appendix row.** The change, the affected API or stored
   representation, the last compatible pin, and the required adapter, converter, or migration. See
   [Release process](RELEASING.md#downstream-impact-appendix).
3. **Every persisted-shape change gets its own numbered migration.** A stored-row change is not a
   wire-only change and may not be smuggled in behind one.
4. **An unqualified downstream deployment must not upgrade past its compatible pin.** For consumers
   that have not migrated, that pin is `environment-harness==0.2.4rc2`.
5. **Product storage and APIs do not become SDK contracts** merely to avoid a migration, and no
   downstream product code or private schema is included in this repository.

## Cancellation and recovery

Use `session.cancel()` on the `EnvironmentSession` handle in Python, or `client.cancel(environment)` through either client. The HTTP command is:

```json
{"operation": "cancel", "arguments": {}}
```

Cancellation requires trusted-local or management authority over the target environment. It does not require the execution writer's lease. It atomically marks the session cancelled, invalidates that lease, stops new action/state commits and releases reservations for known-undispatched operations. Repeating cancellation is safe. Completed sessions remain terminal and reject cancellation. The existing `control` command with `command="cancel"` still works and delegates to the same operation.

The response contains `status`, `unresolved_agent_work` and `unresolved_operations`. Cancellation does not prove that an already-dispatched external effect did not happen. Unknown operations retain their reservation. Resolve those outcomes through the existing receipt or agent reconciliation interface. Recovery after cancellation records the recovered response without changing the cancelled environment's participant continuation state.

The runner checks for cancellation while preparing and waiting for agent work. `CommandAgent` supports `act_cancellable(observation, cancel_event)` and stops its process group, including descendants. It sends SIGTERM, escalates to SIGKILL after one second when necessary, reaps its direct process and cleans its temporary workspace. The same cleanup runs on ordinary completion and timeout. The original `act(observation)` interface is unchanged.

Other agents can implement the optional cancellable method and cooperate with the per-invocation `threading.Event`. Arbitrary Python code and HTTP agents may continue executing after the local runner stops waiting. Their unresolved work stays visible. The SDK does not invent a remote cancellation endpoint or claim that it can stop an externally owned process.

A completed response may update its journal and continuation only while it still owns the running session's current lease, revision, participant generation and dispatch. A late response or failure cannot overwrite an authorized reconciliation. Heartbeats extend existing leases and cannot reacquire an expired or cancelled lease. CLI checkpoint and resume operations release their leases on both success and failure.

## Agent registration and conformance

Before invoking a dispatch set, the runner verifies each program's `implementation` against its frozen `AgentSpec`. Checkpointable programs also need callable `checkpoint` and `restore` hooks. New `agent.dispatched` evidence records the implementation, policy version, participant generation, operation ID and frozen configuration hash.

A mismatched historical registration remains readable, but executing a new call requires a matching implementation. Historical manifests are never silently relabeled. Implementation strings remain declarations by trusted embedding code; they are not executable attestations.

The conformance helper first verifies that every advertised environment operation has one matching `EnvironmentOperation` runtime class with the same name and version and a non-empty endpoint. It closes coordinator phases and tests checkpoints only when advertised. It always releases its lease. Event-driven environments with wall deadlines require explicit input events:

```python
check(store, environment, experiment, action_factory, events=[{
    "source": "synthetic-feed",
    "cursor": 1,
    "event_time": 0.0,
    "payload": {"tick": 1},
}])
```

The helper passes each event through `external_event`. It does not wait for a real feed or invent supplier events. A passing check establishes compatibility for the supplied transition, not environment validity or deployment health.

## Score comparisons

New scorers should declare `ScoreReport.metric_definitions`. Each numeric metric maps to an immutable definition ID, version and unit. For example:

```python
metric_definitions={
    "accuracy": {"id": "example.accuracy", "version": "1", "unit": "fraction"}
}
```

Changing a metric's meaning or scale requires a new definition or version. Comparison selects the latest report for each scorer/version/kind separately. It groups numeric values by definition and unit, scorer identity, environment contract, participant configuration, execution policy, purpose and split. Seeds, scenarios and interventions remain declared experiment dimensions. Statistics still weight independent lineages rather than counting related branches as independent samples. Units are never converted implicitly.

Comparison responses add `metric_groups`, `warnings`, cohort identifiers and per-environment `selected_reports`. Each group includes its selected report revisions/hashes, values, summary and counts for selected, reported, missing and incomplete environments. Incomplete means the environment is not `completed`; visible scores from those environments remain included and explicitly counted.

The existing `environments`, `latest_report`, `metrics`, `uncertainty` and `design` fields remain. A metric appears in the flat `metrics` object only when exactly one compatible, defined group has that name. The CLI and viewer show incompatible groups separately. They do not produce a combined average.

Old reports without definitions remain visible as raw values with a warning. They have no pooled statistic. To qualify their statistics, submit a new immutable report revision with the correct definitions. Do not edit old evidence. An empty flat `metrics` object can therefore mean that scores exist but cannot safely share a summary.

## Large inherited records

Small inherited records continue using `history.inherited`. If the original row would exceed the frozen event limit, the branch writes ordered `history.inherited.chunk` records instead. Their payloads contain the original environment, sequence and hash, a SHA-256 digest of the complete encoded row, the `base64-json-v1` encoding identifier, zero-based `part`, total `parts` and Base64 `data`.

Each chunk fits the original event-size policy and carries the original audience. Branching again copies existing inherited payloads without wrapping them again. Existing inline history, including older nested envelopes, remains readable. Artifact copies and aliases retain their existing access rules.

JSONL exports contain every chunk needed to reconstruct the original row:

```python
from environment_harness.history import reconstruct_inherited

records = list(reconstruct_inherited(session.replay()))
```

Each result has `complete=True` and the original database row in `record`. Missing chunks, conflicting metadata and an incorrect record digest fail explicitly. For partial inspection pages, `strict=False` returns entries with `complete=False` and the source identity. The CLI and viewer count each original record once and expose reconstructed payloads. The viewer reconstructs bytes and checks identity; full integrity verification uses the Python reconstruction helper and `store.verify` for the enclosing chain.

Event timestamps are normalized to their stored floating-point representation before hashing. Older integral timestamps are recognized using their existing hash, preserving verification without rewriting the evidence.

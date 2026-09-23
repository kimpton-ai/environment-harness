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

These changes keep existing SQLite and PostgreSQL stores readable. They require no schema migration and do not rewrite stored manifests, reports, checkpoints or evidence hashes. Python and TypeScript client calls keep their existing arguments. Response additions are described below.

## Cancellation and recovery

Use `session.cancel(environment, researcher)` in Python or `client.cancel(environment)` through either client. The HTTP command is:

```json
{"operation": "cancel", "arguments": {}}
```

Cancellation requires researcher authority for the target environment. It does not require the execution writer's lease. It atomically marks the session cancelled, invalidates that lease, stops new action/state commits and releases reservations for known-undispatched operations. Repeating cancellation is safe. Completed sessions remain terminal and reject cancellation. The existing `control` command with `command="cancel"` still works and delegates to the same operation.

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

records = list(reconstruct_inherited(store.replay(environment_id, researcher)))
```

Each result has `complete=True` and the original database row in `record`. Missing chunks, conflicting metadata and an incorrect record digest fail explicitly. For partial inspection pages, `strict=False` returns entries with `complete=False` and the source identity. The CLI and viewer count each original record once and expose reconstructed payloads. The viewer reconstructs bytes and checks identity; full integrity verification uses the Python reconstruction helper and `store.verify` for the enclosing chain.

Event timestamps are normalized to their stored floating-point representation before hashing. Older integral timestamps are recognized using their existing hash, preserving verification without rewriting the evidence.

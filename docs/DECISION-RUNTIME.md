# Decision runtime

## What this repository owns

EnvironmentHarness owns the **evidence contract** for decisions: two record types in the canonical
causal vocabulary, their minimum payloads, and how they link to environment operations. It does
**not** implement a decision runtime.

The runtime — `DecisionSelector`, the candidate registry, the operation-expansion engine, the
deterministic reference selector, and the optional TypeSafe/Jev adapter — is the separately
versioned **Pluggable Decision-Selection Seam**, published as `environment-harness-decisions`
from [`packages/decision-runtime`](../packages/decision-runtime/README.md). It keeps its own
version and release gate and is **not part of the frozen `0.3.0rc1` manifest**: the companion
imports this SDK, the SDK never imports the companion, and the core distribution neither ships
nor degrades without it.

This split is deliberate. A trajectory must remain readable, diffable, and trainable whether the
decision that produced an action came from a selector runtime, a hand-written agent, or an imported
historical journal. Putting the selection mechanism in the evidence contract would have made the
contract depend on one producer's implementation.

## Core decision contracts

`decision.requested` and `decision.selected` are core record types, not extensions. Both ride the
generic `TrajectoryRecord` container documented in [Trajectories](TRAJECTORIES.md), so they share
its identity, sequence, segment, participant, revision, causal links, multi-clock time, audience,
and digest rules.

```json
{
  "type": "decision.requested",
  "id": "decision-request-1",
  "sequence": 3,
  "segment": "segment-1",
  "participant": "alice",
  "revision": 0,
  "causes": ["record-observation-1"],
  "time": {"wallTime": "2026-09-22T15:00:00Z", "native": []},
  "data": {
    "decision": "decision-1",
    "inputs": ["record-observation-1"],
    "candidateSetDigest": "0000000000000000000000000000000000000000000000000000000000000000",
    "candidates": [
      {"id": "candidate-a", "summary": "hold"},
      {"id": "candidate-b", "summary": "escalate"}
    ],
    "constraints": {"maxOperations": 1},
    "group": "decision-group-1"
  },
  "extensions": {}
}
```

```json
{
  "type": "decision.selected",
  "id": "decision-result-1",
  "sequence": 4,
  "segment": "segment-1",
  "participant": "alice",
  "revision": 0,
  "causes": ["decision-request-1"],
  "time": {"wallTime": "2026-09-22T15:00:01Z", "native": []},
  "data": {
    "decision": "decision-1",
    "selected": "candidate-b",
    "abstained": false,
    "selector": {"id": "reference-selector", "version": "1"},
    "requestDigest": "1111111111111111111111111111111111111111111111111111111111111111",
    "operations": ["operation-1"]
  },
  "extensions": {}
}
```

These two payloads are the **minimum** this repository owns. They are enforced as contract fixtures
in `tests/test_contract_fixtures.py`, which is a required CI gate. The decision-seam workstream may
add optional fields and namespaced extensions; it may not redefine, rename, or narrow the minimum,
and it may not introduce a competing record type or a second journal.

## Candidate bindings and selector-safe projection

`candidates` carries a **bounded, selector-safe projection**, not the runtime's internal candidate
objects. Each entry is an identity plus whatever summary the producer chose to disclose. The
complete candidate set is identified by `candidateSetDigest`, so a selector's input is verifiable
without the evidence having to carry unbounded payloads or private mechanics.

`decision.selected.selected` references a candidate identity from that set. A selector that
declines to act sets `abstained: true`; abstention is recorded evidence, not a missing record.
`requestDigest` binds the result to the exact request it answered, which is how a reader
distinguishes the **full intent** recorded in the request from the narrower **selector request**
the runtime actually evaluated.

Observation privacy still applies: a candidate projection may not disclose information the
participant could not observe, and later disclosure is new evidence rather than a rewrite. See
[Protocol](PROTOCOL.md).

## Zero, one, or many operations

`decision.selected.data.operations` links a final decision to the environment operations it
authorized. The cardinality is explicitly **zero, one, or many**:

| Links | Meaning |
| --- | --- |
| `[]` | The decision authorized no environment operation — a pure choice, or an abstention. |
| `["operation-1"]` | One decision, one durable operation. |
| `["operation-1", "operation-2"]` | One decision fanned out to several operations. |

Fan-in works the same way in reverse: several decisions may reference one operation identity. Every
link is a durable operation ID from the operation journal, so a consumer reconstructs causality from
the recorded links and never from timestamps or arrival order. This cardinality is covered by a
parameterized contract fixture.

## Deterministic reference selector

The seam's deterministic reference selector lives in the companion, not in the SDK. When the
companion is selected for a release manifest, it is published in the same candidate/final release
event and exercised by the installed-wheel walkthrough. Until then, the evidence contract records
`selector.id` and `selector.version` for whatever producer actually made the choice — including an
ordinary agent that never used a selector runtime at all.

Do not read `selector` as proof that a pluggable runtime was installed. It is a producer
declaration, like `AgentSpec.implementation`, not an executable attestation.

## Optional TypeSafe/Jev setup

TypeSafe and Jev are optional adapters owned by the companion, documented in
[TypeSafe Jev provider](../packages/decision-runtime/docs/TYPESAFE-JEV.md). The SDK declares no
dependency, extra, or entry point for them, and the core distribution does not install them.
`scripts/check_decision_distribution.py` installs the core wheel alone, then the companion wheel,
and asserts that neither acquires that dependency graph transitively.

A provider that wants to record its own adapter detail puts it in `data` as an additive optional
field or under a namespaced key in `extensions`. Both survive parse–serialize losslessly and
participate in canonical digests, so an unknown provider's trace changes the record digest exactly
as first-party data would.

## Failure and reconciliation behavior

Decision records are evidence, so they inherit the existing durability rules rather than adding a
second recovery path:

- **A decision without its operation is not a completed effect.** Authorizing an operation and
  dispatching it are distinct steps. `Operations.prepare` records intent and reserves cost;
  `Operations.dispatch` fences on the writer lease. An ambiguous dispatch stays blocked for explicit
  recovery instead of being repeated.
- **Restart reconstructs decisions from durable evidence.** There is no global candidate registry to
  rebuild and no provider installation to re-resolve. A restarted process reads the recorded
  `decision.requested`/`decision.selected` pair and the operation journal.
- **A queue acknowledgement is not a receipt.** See [Remote workers](REMOTE-WORKERS.md).
- **An invalid candidate reference fails.** A `selected` identity that is not in the recorded
  candidate set, a `requestDigest` that does not match, or a cross-trajectory cause is rejected
  rather than coerced.
- **Absent runtime, the projection still holds.** The trajectory projection treats decision records
  as one producer's evidence. Trajectories, snapshots, datasets, export, and the viewer all work
  when no decision record was ever written.

## Related documentation

- [Trajectories](TRAJECTORIES.md) — the record container, segments, causality, and snapshots
- [Protocol](PROTOCOL.md) — authority, audience, decision and operation cardinality
- [Authoring](AUTHORING.md) — environment operations and the `SessionControl` runner seam
- [Agent integration](AGENT-INTEGRATION.md) — correlated inference evidence
- [Coordinated sessions](coordinated-sessions.md) — simultaneous decisions and phase closure
- [Compatibility](COMPATIBILITY.md) — `v1alpha1` evolution and enforced fixtures
- [EnvironmentHarness Decisions](../packages/decision-runtime/README.md) — the separately
  versioned companion that implements the seam

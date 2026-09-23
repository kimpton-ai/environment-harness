# Environment-session v1

Environment-session v1 remains the native execution protocol. Portable resources use the separate,
additively evolving `environmentharness.dev/v1alpha1` family. A trace is the authoritative native
or imported journal; a trajectory is a digest-bound projection of that trace. Neither resource
family replaces or rewrites environment-session evidence.

## Authority and transport

The supplier service owns the environment. HTTPS commands use bearer credentials. Loopback HTTP is an explicit development option. Credentials bind tenant, role, environment, participant and authority generation. Researcher, worker, scorer and participant permissions are separate. Participant credential issuance is researcher-only. Expired credentials fail closed; researchers may issue replacements without changing participant generation. Authority transfer increments generation and invalidates the old controller.

Administrative Python methods are trusted embedding APIs. They must not be exposed directly to untrusted agents. Store directories are private to the operating-system account. SQL credentials, signing keys, model credentials and resource handles belong to the server or worker scope.

The loopback CLI's `serve` command configures automatic local viewer access independently of browser launch. The viewer reads its non-secret authentication mode from `GET /viewer/config` and exchanges local access through `POST /local/connect`, which requires the exact loopback origin and a loopback peer. The endpoint supports refreshes and new tabs, is absent from ordinary supplier applications, and never places its researcher credential in a URL, HTML, browser storage or a token file. `serve --open` only opens the plain viewer URL. The CLI disables proxy-header trust. Manual supplier connections retain credentials only in page memory, and every supplier API request still requires its bearer credential.

## API

| Operation | Route |
| --- | --- |
| Inspect contract | `GET /v1/environment` |
| Create/list environments | `POST/GET /v1/environments` |
| Session state | `GET /v1/environments/{id}` |
| Authorized observation | `GET /v1/environments/{id}/observation` |
| Submit decision | `POST /v1/environments/{id}/actions` |
| Events | `GET /v1/environments/{id}/events?after=CURSOR` |
| Activity snapshot | `GET /v1/activity/snapshot` |
| Global activity | `GET /v1/activity/events?after=CURSOR` |
| Experiment activity | `GET /v1/experiments/{id}/events?after=CURSOR` |
| Environment-session activity | `GET /v1/environments/{id}/activity?after=CURSOR` |
| Lifecycle | `POST /v1/environments/{id}/commands` |
| Participant token | `POST /v1/environments/{id}/credentials` |
| Journal external intent | `POST /v1/environments/{id}/operations` |
| Upload/download artifact | `POST /v1/environments/{id}/artifacts`, `GET .../artifacts/{key}` |
| Versioned scoring | `POST/GET /v1/environments/{id}/reports` |
| Evidence/training export | `GET /v1/environments/{id}/export?format=evidence` or `training` |
| Comparison | `POST /v1/compare` |

Every HTTP error uses one traceable envelope. `request_id` also appears in the `X-Request-ID` response header; operators may use it to correlate safe server-side logs without recording credentials or request bodies. Validation details identify fields but omit submitted values.

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Request validation failed",
    "status": 422,
    "request_id": "32-character request identifier",
    "timestamp": "2026-09-21T12:00:00.000Z",
    "details": [
      {
        "field": "query.limit",
        "message": "Input should be greater than or equal to 1",
        "type": "greater_than_equal"
      }
    ]
  }
}
```

`details` is optional. Authentication failures use `401`; authorization and intentionally hidden resource-existence failures use `403`; unsupported HTTP methods use `405`; state conflicts use `409`; schema and request validation failures use `422`; unavailable suppliers use `503`. Budget exhaustion retains `402` so callers can distinguish a frozen session budget from rate limiting. Unexpected failures return a generic `500 internal_error`; response bodies never contain stack traces.

Create requires `X-Operation-ID`, a durable caller-generated 32-character lowercase hex ID. Reusing it with a different experiment fails. Action IDs are stable, participant-bound operations. Accepted and committed receipt retries return the existing result. Do not generate a new ID after an ambiguous timeout.

Environment-session listing is bounded to 1–1,000 records and uses a stable keyset cursor. When another page exists, `X-Next-Cursor` and the `Link` header's `rel="next"` URL carry the opaque cursor. Event and activity feeds retain their separate monotonic event cursors.

Commands have shape `{"operation":"checkpoint","arguments":{"lease":{"owner":"worker","epoch":1}}}`. Available commands include advance, lease, release, cancel, resolve, close_phase, checkpoint, reconcile_agent, resume, branch, control, memory, transfer, external_event and finalize_outcomes. `advance` lets a trusted coordinator resolve at most one ready phase for externally controlled participants; it does not execute models. Branch takes a checkpoint ID, declared interventions and an optional durable new-environment ID. Lifecycle commands other than create, branch, cancellation and action submission do not promise general HTTP idempotency. Clients never retry writes implicitly.

Events support JSON pages and finite server-sent-event pages. Reconnect with `Last-Event-ID`; an empty page means caught up. Cursors expose ordering gaps but never hidden event payloads. A viewer can disconnect without blocking evidence writes. Artifact access is authorized against its environment and audience before retrieving any bytes.

Activity feeds use a transactional outbox and global cursor. They cover experiment status and environment-session evidence without exposing the scheduler queue as an authority. The recovery snapshot includes the experiment's frozen shared configuration and each scenario's immutable input, reference, and metadata. SSE pages include a reconnect delay and heartbeat; clients tolerate duplicate IDs and recover from the activity snapshot after reconnecting. Global, experiment and environment-session scopes all require an authenticated tenant principal.

The checked-in `ActivityPage` and `ActivitySnapshot` JSON Schemas are the durable JSON response
contracts for those feeds. The generated OpenAPI document references the same response models and
records `text/event-stream` as the alternate representation for event pages.

## Scheduling

Sequential environments select one actor. Simultaneous environments keep the committed state and observation revision fixed until all required decisions arrive, or until the declared deadline/missing-action rule resolves the phase. Decisions are passed to the environment in participant order, never arrival order. Event environments resolve only when an external event or deadline is present. Stale actions are explicitly rejected.

One logical writer holds a fenced lease with a monotonic epoch. Environment changes, action receipts, randomness, scheduler state and evidence commit in one database transaction. Lease expiry is checked before and after transition computation. Supplier resolution must be pure with respect to external services. Imperative native adapters preserve only the explicitly declared guarantees.

The supplied runner drives sequential and simultaneous phases. Event-driven suppliers ingest their external events through the worker API and call resolve at their declared cutoffs. Runtime membership supports activation, departure, replacement and rejoining within the frozen participant roster. Dynamically extending that roster requires an environment-specific versioned contract.

## Recovery and effects

Checkpoints include the environment, RNG, participants and agent checkpoint data, scheduler, feed cursors, budget state and pending journals. They commit atomically in the evidence database. Arbitrary external handles are not presumed durable. Exact-agent checkpoint requests fail if a participant lacks a hook. External opaque agent state has to be supplied by the agent integration.

The latest committed environment state is the recovery authority. Resume does not roll the outside environment back to an old checkpoint. Pending ambiguous external dispatches must be reconciled before resume or checkpoint. The operations journal persists intent and reservation before dispatch. An environment advertises operation names and versions, supplies their runtime classes, and freezes each selected class's JSON configuration in `ExperimentSpec.operations`. Session creation requires the selected specification to match the runtime class. Runtime classes receive a stable environment/operation key, a maximum cost and a live fenced-authority callback. Receipt settlement is idempotent; unknown outcomes stay blocked if lookup cannot prove what happened.

Budget limits cover operations routed through the journal. A backend must enforce the maximum passed to it. Arbitrary externally managed programs cannot acquire spending authority through this SDK. Reservations for known-unsent operations can be released on cancellation; ambiguous dispatches retain their reservation until settlement.

Branches copy checkpoint state into a new environment and retain lineage. Parent credentials and artifact references do not grant child access. Branching with pending actions or operations, or inheriting a live-write policy, is rejected. Private suppliers can provide different counterfactual capabilities only under a contract that implements them.

## Evidence and limitations

Local evidence uses sorted, compact ASCII JSON with finite numbers and a SHA-256 hash chain. This is an explicitly specified encoding, not a claim of RFC 8785 conformance. Events, checkpoints and report revisions are append-only. Participant projections cannot verify hidden portions of a hash chain; researcher/scorer authority can verify the complete chain. Optional Ed25519 receipts establish supplier provenance, not independent reproduction of hidden mechanics.

Findings validate participant/action/observation links and existence of referenced outcome/consequence events. The runtime does not adjudicate the scientific truth of a grader's judgment. Comparisons aggregate lineage means and do not treat turns or related branches as independent experiments. Unknown uncertainty remains explicit.

PostgreSQL and S3 support lives in `hosted.py`. Initialize its dedicated schema explicitly. It is not a migration authority for a platform database. Object access stays server-mediated. Production deployment additionally requires resource admission, backup/restore, rotation, transport hardening and the omitted live acceptance checks.

[Session reliability and compatibility](COMPATIBILITY.md) specifies lease-independent cancellation, guarded response recovery, metric grouping, inherited chunks and legacy-store behavior.

## Portable trajectory resources

`Policy`, `Trajectory`, `TrajectorySnapshot`, `TrajectoryDataset`, and `TrainingRun` use a strict
top-level envelope containing `apiVersion`, `kind`, `metadata`, `features`, `spec`, `status`, and
`extensions`. Commands and mutations reject unknown fields. Nested portable evidence preserves
unknown optional fields; required feature names must be understood before a reader accepts the
resource. Python's canonical sorted compact JSON encoding is the digest authority for `v1alpha1`.

A trajectory manifest freezes environment, participant, purpose, policy, and source identity. One
trajectory can contain multiple segments and an ordered record stream. Durable sequence and
explicit `causes` links determine order. Wall time and native clocks remain coordinates, not
authority. Unknown record types must be reverse-domain namespaced and are inert.

Collection, execution, termination/truncation, and verified outcome are independent. Ingestion
completion cannot imply task success. A source registration is immutable within its tenant,
namespace, and run ID. Each accepted batch continues a canonical hash chain and returns the
acknowledged native position plus hash. Identical retries are idempotent; conflicting identities or
broken chains fail. Gaps, capture failures, backlog, or an unacknowledged terminal boundary prevent
a complete collection state.

Snapshots freeze source/evidence cursors, segment and record ranges, score revisions, artifact
digests, schema/features, and audience projection. Re-export is ordered JSONL and does not change
after later appends or regrading. Reward supersession chains must be complete, acyclic, unambiguous,
finite, and unretracted before they enter a training dataset. See [Trajectories](TRAJECTORIES.md).

The provider-neutral decision payloads described by **Pluggable Decision-Selection Seam** attach to
this record envelope. They remain separate from environment execution authorization and do not
introduce another journal or registry.

# Environment-session v1

## Authority and transport

The supplier service owns the environment. HTTPS commands use bearer credentials. Loopback HTTP is an explicit development option. Credentials bind tenant, role, environment, participant and authority generation. Researcher, worker, scorer and participant permissions are separate. Participant credential issuance is researcher-only. Expired credentials fail closed; researchers may issue replacements without changing participant generation. Authority transfer increments generation and invalidates the old controller.

Administrative Python methods are trusted embedding APIs. They must not be exposed directly to untrusted agents. Store directories are private to the operating-system account. SQL credentials, signing keys, model credentials and resource handles belong to the server or worker scope.

## API

| Operation | Route |
| --- | --- |
| Inspect contract | `GET /v1/environment` |
| Create/list environments | `POST/GET /v1/environments` |
| Session state | `GET /v1/environments/{id}` |
| Authorized observation | `GET /v1/environments/{id}/observation` |
| Submit decision | `POST /v1/environments/{id}/actions` |
| Events | `GET /v1/environments/{id}/events?after=CURSOR` |
| Lifecycle | `POST /v1/environments/{id}/commands` |
| Participant token | `POST /v1/environments/{id}/credentials` |
| Journal external intent | `POST /v1/environments/{id}/operations` |
| Upload/download artifact | `POST /v1/environments/{id}/artifacts`, `GET .../artifacts/{key}` |
| Versioned scoring | `POST/GET /v1/environments/{id}/reports` |
| Evidence/training export | `GET /v1/environments/{id}/export?format=evidence` or `training` |
| Comparison | `POST /v1/compare` |

Create requires `X-Operation-ID`, a durable caller-generated 32-character lowercase hex ID. Reusing it with a different experiment fails. Action IDs are stable, participant-bound operations. Accepted and committed receipt retries return the existing result. Do not generate a new ID after an ambiguous timeout.

Commands have shape `{"operation":"checkpoint","arguments":{"lease":{"owner":"worker","epoch":1}}}`. Available commands include lease, release, resolve, checkpoint, resume, branch, control, memory, transfer external_event and finalize_outcomes. Branch takes a checkpoint ID, declared interventions and an optional durable new-environment ID. Lifecycle commands other than create, branch and action submission do not promise general HTTP idempotency. Clients never retry writes implicitly.

Events support JSON pages and finite server-sent-event pages. Reconnect with `Last-Event-ID`; an empty page means caught up. Cursors expose ordering gaps but never hidden event payloads. A viewer can disconnect without blocking evidence writes. Artifact access is authorized against its environment and audience before retrieving any bytes.

## Scheduling

Sequential environments select one actor. Simultaneous environments keep the committed state and observation revision fixed until all required decisions arrive, or until the declared deadline/missing-action rule resolves the phase. Decisions are passed to the environment in participant order, never arrival order. Event environments resolve only when an external event or deadline is present. Stale actions are explicitly rejected.

One logical writer holds a fenced lease with a monotonic epoch. Environment changes, action receipts, randomness, scheduler state and evidence commit in one database transaction. Lease expiry is checked before and after transition computation. Supplier resolution must be pure with respect to external services. Imperative native adapters preserve only the explicitly declared guarantees.

The supplied runner drives sequential and simultaneous phases. Event-driven suppliers ingest their external events through the worker API and call resolve at their declared cutoffs. Runtime membership supports activation, departure, replacement and rejoining within the frozen participant roster. Dynamically extending that roster requires an environment-specific versioned contract.

## Recovery and effects

Checkpoints include the environment, RNG, participants and agent checkpoint data, scheduler, feed cursors, budget state and pending journals. They commit atomically in the evidence database. Arbitrary external handles are not presumed durable. Exact-agent checkpoint requests fail if a participant lacks a hook. External opaque agent state has to be supplied by the agent integration.

The latest committed environment state is the recovery authority. Resume does not roll the outside environment back to an old checkpoint. Pending ambiguous external dispatches must be reconciled before resume or checkpoint. The operations journal persists intent and reservation before dispatch. Providers receive a stable environment/operation key and a maximum cost. Receipt settlement is idempotent; unknown outcomes stay blocked if lookup cannot prove what happened.

Budget limits cover operations routed through the journal. A backend must enforce the maximum passed to it. Arbitrary externally managed programs cannot acquire spending authority through this SDK. Reservations for known-unsent operations can be released on cancellation; ambiguous dispatches retain their reservation until settlement.

Branches copy checkpoint state into a new environment and retain lineage. Parent credentials and artifact references do not grant child access. Branching with pending actions or operations, or inheriting a live-write policy, is rejected. Private suppliers can provide different counterfactual capabilities only under a contract that implements them.

## Evidence and limitations

Local evidence uses sorted, compact ASCII JSON with finite numbers and a SHA-256 hash chain. This is an explicitly specified encoding, not a claim of RFC 8785 conformance. Events, checkpoints and report revisions are append-only. Participant projections cannot verify hidden portions of a hash chain; researcher/scorer authority can verify the complete chain. Optional Ed25519 receipts establish supplier provenance, not independent reproduction of hidden mechanics.

Findings validate participant/action/observation links and existence of referenced outcome/consequence events. The runtime does not adjudicate the scientific truth of a grader's judgment. Comparisons aggregate lineage means and do not treat turns or related branches as independent experiments. Unknown uncertainty remains explicit.

PostgreSQL and S3 support lives in `hosted.py`. Initialize its dedicated schema explicitly. It is not a migration authority for a platform database. Object access stays server-mediated. Production deployment additionally requires resource admission, backup/restore, rotation, transport hardening and the omitted live acceptance checks.

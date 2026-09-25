# EnvironmentHarness HTTP API reference

The EnvironmentHarness HTTP API exposes environment sessions, evidence, activity, artifacts, and evaluation results. API version 1 uses the `/v1` path prefix and the `environment-session.v1` protocol.

The canonical machine-readable contract is [`contracts/openapi.json`](../contracts/openapi.json). A running service exposes the same document at `/openapi.json` and interactive Swagger UI at `/docs`. Standalone JSON Schemas under `contracts/`, including `ActivityPage.schema.json` and `ActivityHierarchy.schema.json`, record the corresponding durable response shapes. [`PROTOCOL.md`](PROTOCOL.md) defines authority, lifecycle, recovery, and evidence guarantees that cannot be expressed completely in OpenAPI.

## Base URL and authentication

The local CLI listens on `http://127.0.0.1:8765` by default. Remote services must use HTTPS. Generate a local management credential from the same evidence store that the service uses:

```sh
export EH_TOKEN="$(environment-harness --store .local/demo token)"
export EH_URL="http://127.0.0.1:8765"
```

Every `/v1` route requires an opaque bearer credential. `GET /health`, the OpenAPI pages, and the
packaged viewer shell are public; the viewer obtains its API credential through the local or
supplier authentication flow described in [Deployment](DEPLOYMENT.md).

```http
Authorization: Bearer <credential>
```

Callers send only an opaque bearer credential. The server resolves it to an identity plus one of
three fixed access policies — `management`, `viewer`, or `participant` — and never accepts a policy,
role, or permission from a request. A participant credential is additionally constrained to one
environment session, participant, and authority generation. See [Authentication](AUTHENTICATION.md).

| Credential policy | Server-assigned authority |
| --- | --- |
| `management` | Create and inspect environment sessions, run lifecycle commands, read full evidence, publish score reports, register and ingest sources, freeze snapshots and datasets, and issue participant credentials. |
| `viewer` | Read-only inspection. It cannot mutate anything, ingest evidence, create a dataset, or issue a credential. |
| `participant` | Observe and act only as the credential's bound session, participant, and authority generation. |

OpenAPI publishes only its standard HTTP bearer security scheme; there is no `x-roles`,
`x-principal-kinds`, or other custom caller extension. An authenticated caller can still receive
`403` when its server-owned policy, tenant, environment-session scope, participant, audience, or
authority generation does not authorize the specific resource. An invalid, expired, or revoked
credential returns `401` instead.

## Common conventions

- JSON request bodies use `Content-Type: application/json`.
- JSON responses use `application/json`; exports use `application/x-ndjson`; event streams use `text/event-stream`.
- Every response includes `X-Request-ID`, `Cache-Control: no-store`, and security headers.
- Request bodies are limited to 16 MiB. Frozen run policies can impose smaller event, artifact, or state limits.
- Collection limits range from 1 to 1,000. The default is 100 for environment sessions and agent work, and 200 for event pages. Environment-session listing uses a stable keyset cursor; event and activity streams use their monotonic event cursors.
- The service has no built-in request-rate quota. A supplier deployment may add one at its gateway. `402 budget_exhausted` is a frozen environment-session budget failure, not rate limiting.
- There are no webhook callbacks. Use the resumable activity or evidence event streams for change notification.
- Write clients must not retry ambiguous writes with a new operation ID. The Python and TypeScript clients never retry writes implicitly.

The examples below use these placeholders:

```sh
export SESSION_ID="env_example"
export EXPERIMENT_ID="experiment_example"
export ARTIFACT_KEY="artifact_example"
export SNAPSHOT_ID="snapshot_example"
```

## Endpoint summary

| Method and path | Credential policies | Purpose |
| --- | --- | --- |
| `GET /health` | Public | Check service and protocol health. |
| `GET /v1/capabilities` | Any valid credential | Discover which conditionally available capabilities are enabled. |
| `GET /v1/scenario-sets` | Management, viewer | List frozen scenario sets. |
| `GET /v1/scenario-sets/{scenario_set_id}` | Management, viewer | Get a frozen scenario set. |
| `POST /v1/experiments` | Management | Create an experiment and queue its sessions. |
| `GET /v1/experiments` | Management, viewer | List experiments. |
| `GET /v1/experiments/{experiment_id}` | Management, viewer | Get an experiment. |
| `GET /v1/experiments/{experiment_id}/sessions` | Management, viewer | List the sessions an experiment derived. |
| `GET /v1/experiments/{experiment_id}/scores` | Management, viewer | List score reports across an experiment. |
| `GET /v1/experiments/{experiment_id}/activity` | Management, viewer | Read activity for one experiment. |
| `GET /v1/sessions` | Any valid credential | List environment sessions. |
| `GET /v1/sessions/{session_id}` | Any valid credential | Get one authorized environment session. |
| `GET /v1/sessions/{session_id}/participants/{participant_id}/observation` | Any valid credential | Read a participant-specific observation. |
| `POST /v1/sessions/{session_id}/participants/{participant_id}/actions` | Participant | Submit a participant action. |
| `POST /v1/sessions/{session_id}/participants/{participant_id}/credentials` | Management | Issue a scoped participant credential. |
| `GET /v1/sessions/{session_id}/checkpoints` | Any valid credential | List immutable checkpoints. |
| `POST /v1/sessions/{session_id}/checkpoints` | Management | Freeze an immutable checkpoint. |
| `GET /v1/sessions/{session_id}/checkpoints/{checkpoint_id}` | Any valid credential | Get an immutable checkpoint. |
| `POST /v1/sessions/{session_id}/branches` | Management | Create a child session from a checkpoint. |
| `POST /v1/sessions/{session_id}/commands` | Management | Run a lifecycle command; returns `202` and a typed receipt. |
| `GET /v1/sessions/{session_id}/invocations` | Any valid credential | List durable agent invocations. |
| `POST /v1/sessions/{session_id}/operations` | Participant | Persist an authorized external-operation intent. |
| `GET /v1/sessions/{session_id}/evidence` | Any valid credential | Read authorized evidence as JSON or SSE. |
| `POST /v1/sessions/{session_id}/artifacts` | Management, participant | Store an authorized artifact. |
| `GET /v1/sessions/{session_id}/artifacts/{artifact_id}` | Any valid credential | Download an authorized artifact. |
| `GET /v1/sessions/{session_id}/scores` | Management, viewer | List versioned score reports. |
| `POST /v1/sessions/{session_id}/scores` | Management | Publish a versioned score report. |
| `GET /v1/sessions/{session_id}/turn-series` | Management, viewer | Read bounded turn-level evidence series. |
| `GET /v1/sessions/{session_id}/activity` | Management, viewer | Read activity for one environment session. |
| `GET /v1/policies` | Any valid credential | List derived policy resources. |
| `GET /v1/policies/{policy_id}` | Any valid credential | Get a derived policy resource. |
| `GET /v1/trajectories` | Management, viewer | List native and imported trajectories. |
| `GET /v1/trajectories/{trajectory_id}` | Management, viewer | Get a portable trajectory. |
| `GET /v1/trajectories/{trajectory_id}/records` | Management, viewer | Page through trajectory records. |
| `GET /v1/trajectories/{trajectory_id}/scores` | Management, viewer | List score reports a trajectory references. |
| `GET /v1/trajectories/{trajectory_id}/snapshots` | Management, viewer | List snapshots frozen for one trajectory. |
| `POST /v1/trajectories/{trajectory_id}/snapshots` | Management, viewer | Freeze an authorized trajectory snapshot. |
| `GET /v1/snapshots` | Management, viewer | List immutable trajectory snapshots. |
| `GET /v1/snapshots/{snapshot_id}` | Management, viewer | Get an immutable trajectory snapshot. |
| `GET /v1/snapshots/{snapshot_id}/records` | Management, viewer | Read snapshot records as a JSON page or streamed NDJSON. |
| `POST /v1/datasets` | Management | Freeze a training-entitled trajectory dataset. |
| `GET /v1/datasets` | Management, viewer | List immutable trajectory datasets. |
| `GET /v1/datasets/{dataset_id}` | Management, viewer | Get an immutable trajectory dataset. |
| `GET /v1/datasets/{dataset_id}/records` | Management, viewer | Read dataset records as a JSON page or streamed NDJSON. |
| `GET /v1/training-runs` | Management, viewer | List recorded local training results. |
| `GET /v1/training-runs/{training_run_id}` | Management, viewer | Get a recorded local training result. |
| `POST /v1/sources` | Management | Register an external trajectory source. |
| `GET /v1/sources` | Management, viewer | List registered external sources. |
| `GET /v1/sources/{source_id}` | Management, viewer | Get a registered external source. |
| `GET /v1/sources/{source_id}/status` | Management, viewer | Inspect source collection and execution status. |
| `POST /v1/sources/{source_id}/status-reports` | Management | Declare source collection and execution status. |
| `GET /v1/sources/{source_id}/records` | Management, viewer | Page through ingested source records. |
| `POST /v1/sources/{source_id}/records` | Management | Ingest a bounded source batch. |
| `POST /v1/comparisons` | Management, viewer | Compare 1 to 100 environment sessions. |
| `GET /v1/activity` | Management, viewer | Read tenant activity as JSON or SSE. |
| `GET /v1/activity/hierarchy` | Management, viewer | Read the current experiment, scenario, and session hierarchy. |

Hyphenation is limited to `scenario-sets`, `training-runs`, `turn-series`, and `status-reports`.
Snapshots, datasets, and sources are top-level because they are independently addressable; creating
a snapshot stays nested under its owning trajectory.

### Management collections versus durable feeds

Management collection indexes share one typed envelope with an opaque cursor and RFC `Link` headers:

```json
{"items": [], "nextCursor": null, "links": {"self": "/v1/sessions", "next": null}}
```

Ordered evidence and activity feeds are a different contract. `TrajectoryRecordPage` keeps its
durable integer `sequence` cursor, and `ActivityPage` keeps its durable event cursor for JSON and
SSE resume. Those source positions are never made opaque just to reuse the management envelope.

### Content negotiation

`GET /v1/snapshots/{id}/records` and `GET /v1/datasets/{id}/records` return the typed JSON cursor
page by default and stream NDJSON when the request sends `Accept: application/x-ndjson`. There is no
`/export` verb path. OpenAPI documents both media types; generated clients decode the JSON page,
while streaming uses the explicit hand-written iterator (`stream_snapshot_records` in Python,
`streamSnapshotRecords` in TypeScript).

### Deployment capabilities

`GET /v1/capabilities` returns only non-secret capability names with `enabled` and an optional
public reason. Every conditionally available operation declares its capability through
`x-capability`. Calling a disabled capability returns `501 capability_unavailable` with the same
name; a transient failure of an enabled capability returns `503 service_unavailable`.

Historical ingestion is disabled by default and must be enabled explicitly with
`create_app(harness, trajectory_ingestion=True)`.

### Migrating from 0.2

Every operation published by `0.2.4rc2` is classified exactly once in
[`contracts/migrations/http-0.2-to-0.3.json`](../contracts/migrations/http-0.2-to-0.3.json). The
generated human tables are in [HTTP migration](HTTP-MIGRATION.md). CI regenerates both from the
frozen inventory and the internal authorization registry, so this documentation cannot drift from
enforcement.



## Service and contract

### Check health

```sh
curl --fail-with-body "$EH_URL/health"
```

```json
{"status":"ok","protocol":"environment-session.v1"}
```

### Read the active environment contract

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/capabilities"
```

The response is an `EnvironmentSpec`. It declares the environment implementation, observation and action JSON Schemas, scheduling mode, modalities, capabilities, purposes, environment-supplied operation identities, and phase behavior. The versioned schema is [`EnvironmentSpec.schema.json`](../contracts/EnvironmentSpec.schema.json); reusable operation identity and JSON configuration use [`OperationSpec.schema.json`](../contracts/OperationSpec.schema.json).

## Environment sessions

### Create an environment session

`X-Operation-ID` is required. Use a durable caller-generated 32-character lowercase hexadecimal ID and retain it across an ambiguous retry. Reusing an ID with a different request returns `409 conflict`.

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Operation-ID: 00000000000000000000000000000000" \
  --data @experiment.json \
  "$EH_URL/v1/experiments"
```

The body is an [`ExperimentSpec`](../contracts/ExperimentSpec.schema.json). A successful response is the created environment-session record:

```json
{
  "id": "00000000000000000000000000000000",
  "status": "running",
  "revision": 0,
  "participants": ["alice"]
}
```

### List environment sessions

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/sessions?limit=100"
```

The response is an array of tenant-visible environment-session records in descending ID order. When another page exists, the response includes both `X-Next-Cursor` and a relative `Link` header with `rel="next"`:

```http
X-Next-Cursor: 0123456789abcdef0123456789abcdef
Link: </v1/sessions?limit=100&cursor=0123456789abcdef0123456789abcdef>; rel="next"
```

Pass that cursor unchanged; clients must not construct or interpret it. The final page omits both headers. Page size is bounded to 1–1,000, and keyset pagination avoids increasingly expensive offsets.

### Get one environment session

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/sessions/$SESSION_ID"
```

The response includes the current status, revision, frozen manifest, participants, lineage, budget state, and timestamps visible to the caller.

### Read an observation

A participant credential is already bound to its participant. A management or viewer credential can select a participant explicitly.

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/sessions/$SESSION_ID/participants/alice/observation"
```

```json
{
  "id": "observation_example",
  "environment": "env_example",
  "participant": "alice",
  "revision": 2,
  "generation": 0,
  "payload": {"total": 2},
  "memory": {},
  "may_act": true,
  "deadline": null
}
```

### Submit an action

The caller must use the participant credential bound to the action. `operation_id` is stable for retries, and `observation_id` plus `revision` prevents a stale decision from being applied.

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "operation_id":"action_01",
    "participant":"alice",
    "observation_id":"observation_example",
    "revision":2,
    "payload":{"value":1}
  }' \
  "$EH_URL/v1/sessions/$SESSION_ID/participants/alice/actions"
```

The response is the accepted action receipt or the previously committed receipt for an identical retry.

### Read agent work

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/sessions/$SESSION_ID/invocations?limit=100"
```

```json
{
  "items": [
    {"id":"work_01","revision":2,"participant":"alice","generation":0,"status":"pending"}
  ]
}
```

A participant credential sees only its own participant's invocations.

### Execute a lifecycle command

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"operation":"cancel","arguments":{}}' \
  "$EH_URL/v1/sessions/$SESSION_ID/commands"
```

The command envelope is always:

```json
{"operation":"checkpoint","arguments":{"lease":{"owner":"worker","epoch":1}}}
```

Supported operation names are `advance`, `lease`, `release`, `cancel`, `resolve`, `close_phase`, `checkpoint`, `reconcile_agent`, `resume`, `branch`, `control`, `memory`, `transfer`, `external_event`, and `finalize_outcomes`. `advance` resolves at most one ready externally controlled phase under the normal fenced writer lease; it returns `waiting` while required actions or events are missing and never executes a model. Other arguments are the corresponding `EnvironmentSession` method arguments after `environment` and `who`. Unknown operations or incompatible arguments return the documented `422 invalid_request` envelope. See [`PROTOCOL.md`](PROTOCOL.md), [`coordinated-sessions.md`](coordinated-sessions.md), and [`REMOTE-WORKERS.md`](REMOTE-WORKERS.md) before building recovery, remote-worker, or branching automation.

| Operation | Required policy | Required arguments | Success result | Common errors |
| --- | --- | --- | --- | --- |
| `advance` | Management, viewer | None; optional `owner` | One ready phase result, or `{"status":"waiting","revision":N,"deadline_exceeded":false}` | `403` scope, `409` lost authority or invalid phase, `503` supplier unavailable |
| `lease` | Management, viewer | `owner`; optional `ttl` (1–300 seconds) | Fenced lease with `owner`, `epoch`, and `expires` | `409` another writer is active, `422` invalid owner/TTL |
| `release` | Management, viewer | `lease` | `{"released":true}` | `403` scope, `409` stale/expired lease |
| `cancel` | Management | None | Cancelled status plus unresolved agent-work and operation IDs | `403` policy/scope, `409` already terminal |
| `resolve` | Management, viewer | `lease` | Committed revision, status, and evidence event | `409` incomplete phase, stale lease, or invalid transition; `503` supplier unavailable |
| `close_phase` | Management, viewer | `lease`, `revision`; optional `reason` | Closed revision receipt | `409` stale phase/lease, `422` wall-clock phase |
| `checkpoint` | Management | `lease`; optional `exact_agents` | Checkpoint ID, revision, hash, and exactness | `409` unsettled work, `422` unsupported capability |
| `reconcile_agent` | Management, viewer | `lease`, `operation_id`, `response`, `evidence`; optional `agent_state` | Responded operation receipt | `409` conflicting/stale work, `422` invalid evidence |
| `resume` | Management, viewer | `lease`; optional `implementations` | Updated environment session | `409` unsettled effects or changed implementations, `422` unsupported capability |
| `branch` | Management | `checkpoint`; optional `interventions`, `new_environment` | New environment session | `403` unavailable checkpoint, `409` integrity/version conflict, `422` unsupported pending/live-write state |
| `control` | Management | `lease`, `command` (`pause` or `cancel`) | Updated lifecycle status | `409` stale lease/terminal session, `422` unknown command |
| `memory` | Participant | `memory`; optional `agent_state`, `expected_revision` | `null` after the update commits | `403` participant authority, `409` stale revision/size, `422` missing checkpoint hook |
| `transfer` | Management | `lease`, `participant`, `controller`; optional `active` | New scoped participant credential | `409` decision boundary/last participant, `422` undeclared participant |
| `external_event` | Management, viewer | `lease`, `source`, `cursor`, `event_time`, `payload`; optional `gap` | Evidence event receipt | `409` stale cursor/queue limit/lease |
| `finalize_outcomes` | Management, viewer | `lease`, `report_revision` | Completed outcome receipt | `409` missing report or unsettled operations |

All command errors use the shared envelope below. A `403` can intentionally hide whether an environment session exists; a `409` means the caller should refresh state or reconcile authority rather than retry blindly; a `422` means the operation name, arguments, or frozen capability does not permit the request.

### Issue a participant credential

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"participant":"alice","ttl":3600}' \
  "$EH_URL/v1/sessions/$SESSION_ID/participants/alice/credentials"
```

```json
{"token":"opaque-environment-harness-credential"}
```

`ttl` defaults to 3,600 seconds and is capped at 86,400 seconds. Treat the response as a secret; do not put it in a URL, log, repository, or public browser bundle.

### Prepare an external operation

This endpoint journals intent and reserves its maximum cost before a worker dispatches it. The endpoint and operation must be allowed by the frozen run policy.

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "operation_id":"lookup_01",
    "endpoint":"https://synthetic.invalid",
    "operation":"lookup",
    "payload":{"query":"example"},
    "maximum_cost_micros":1000,
    "write":false
  }' \
  "$EH_URL/v1/sessions/$SESSION_ID/operations"
```

```json
{"id":"lookup_01","status":"prepared"}
```

## Evidence and activity

### Read evidence events

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/sessions/$SESSION_ID/evidence?after=0&limit=200"
```

```json
{"events":[{"seq":1,"revision":0,"kind":"environment.created"}],"cursor":1}
```

Evidence is filtered by caller authority before the page limit is applied. Continue with the returned cursor until `events` is empty.

For a finite server-sent-event page, request `text/event-stream`. Reconnect with the last received ID:

```sh
curl --no-buffer \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Accept: text/event-stream" \
  -H "Last-Event-ID: 42" \
  "$EH_URL/v1/sessions/$SESSION_ID/evidence"
```

### Read activity

The global, experiment, and environment-session feeds share `after`, `limit`, `Accept`, and `Last-Event-ID` behavior:

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/activity?after=0&limit=200"

curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/experiments/$EXPERIMENT_ID/activity?after=0&limit=200"

curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/sessions/$SESSION_ID/activity?after=0&limit=200"
```

```json
{
  "events": [
    {
      "id": 17,
      "topic": "environment_session",
      "experiment": "experiment_example",
      "environment": "env_example",
      "kind": "action.committed",
      "payload": {},
      "created": 1790000000.0
    }
  ],
  "cursor": 17
}
```

Activity SSE pages include `retry: 2000` and a heartbeat. Consumers must tolerate duplicate IDs and recover from a snapshot after reconnecting.

The JSON responses for all three activity feeds conform to
[`ActivityPage.schema.json`](../contracts/ActivityPage.schema.json). OpenAPI records both the typed
JSON response and the alternate `text/event-stream` representation.

### Read the activity snapshot

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/activity/hierarchy"
```

The response contains current experiment, scenario, and environment-session records plus the current global activity cursor. Experiment records include the frozen environment, participant, execution, policy, operation, and scoring configuration shared by their sessions. Scenario records preserve their immutable input, reference, and metadata snapshots. It is the recovery source for clients that miss activity events.
Its machine-readable response contract is
[`ActivitySnapshot.schema.json`](../contracts/ActivitySnapshot.schema.json).

### Store and download an artifact

Upload bytes with their real media type:

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @result.json \
  "$EH_URL/v1/sessions/$SESSION_ID/artifacts"
```

The response identifies the stored artifact:

```json
{
  "id":"artifact_example",
  "sha256":"64-character SHA-256 digest",
  "size":128,
  "media_type":"application/json"
}
```

Download authorized bytes with:

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  --output result.json \
  "$EH_URL/v1/sessions/$SESSION_ID/artifacts/$ARTIFACT_KEY"
```

The download response uses `application/octet-stream` and a `Content-Disposition` filename. Authorization is checked against the environment session and artifact audience before any bytes are returned.

## Evaluation

### List score reports

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/sessions/$SESSION_ID/scores"
```

The response is an array of versioned report envelopes ordered by report revision.

### Publish a score report

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Content-Type: application/json" \
  --data @score-report.json \
  "$EH_URL/v1/sessions/$SESSION_ID/scores"
```

The body follows [`ScoreReport.schema.json`](../contracts/ScoreReport.schema.json). It identifies the scorer and version, the evidence cursor, metrics and their definitions, findings, rewards, uncertainty, and provenance. The response is the stored report envelope with its revision.

### Read bounded turn series

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/sessions/$SESSION_ID/turn-series?start_turn=1&end_turn=5000&max_points=300"
```

The response projects public numeric signals, cumulative reward, and cumulative executed actions onto environment-session turns. `start_turn` and `end_turn` select a window; `max_points` is bounded from 20 to 1000 per series. When a series exceeds that limit, the projection retains its endpoints and bucket extrema so long sessions remain readable without hiding spikes.

### Export evidence or training rows

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/snapshots/$SNAPSHOT_ID/records" \
  --output evidence.ndjson
```

Use `format=training` only when the frozen environment purpose and split grant training entitlement. Both formats stream one JSON object per line. Unknown formats return `422`.

### Compare environment sessions

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"environments":["env_original","env_branch"]}' \
  "$EH_URL/v1/comparisons"
```

The request accepts 1 to 100 authorized environment-session IDs. The response contains session lineage, selected report revisions, metric groups, warnings, raw values, and aggregate summaries. Related turns and branches are not treated as independent experiments.

## Trajectories, sources, snapshots, and training records

| Method and path | Availability | Purpose |
| --- | --- | --- |
| `GET /v1/trajectories` | Always authenticated | Page native and imported trajectory summaries. |
| `GET /v1/trajectories/{id}` | Always authenticated | Read one portable trajectory projection. |
| `GET /v1/trajectories/{id}/records` | Always authenticated | Page records with `after` and bounded `limit`. |
| `POST /v1/trajectories/{id}/snapshots` | Always authenticated | Freeze the caller's authorized trajectory projection. |
| `GET /v1/trajectories/{id}/snapshots` | Always authenticated | List the trajectory's immutable snapshot boundaries for inspection. |
| `GET /v1/snapshots/{id}` | Always authenticated | Read immutable snapshot boundaries and digests. |
| `GET /v1/snapshots/{id}/records` | Always authenticated | Read snapshot records as a JSON page, or stream NDJSON with `Accept: application/x-ndjson`. |
| `GET /v1/sources/{id}/status` | Always authenticated | Inspect acknowledgement and collection/execution/outcome health. |
| `POST /v1/sources` | Configured ingestion only | Register an immutable namespaced source/run identity. |
| `POST /v1/sources/{id}/records` | Configured ingestion only | Ingest 1–1000 hash-chained records. |
| `PUT /v1/sources/{id}/status` | Configured ingestion only | Record source health; it never executes the source. |
| `POST /v1/datasets` | Always authenticated | Freeze complete training-entitled trajectories. |
| `GET /v1/datasets` | Always authenticated | List immutable datasets. |
| `GET /v1/datasets/{id}` | Always authenticated | Read one dataset. |
| `GET /v1/datasets/{id}/records` | Always authenticated | Stream its snapshots and records as NDJSON. |
| `GET /v1/training-runs` | Always authenticated | List recorded local integration receipts. |
| `GET /v1/training-runs/{id}` | Always authenticated | Read one recorded receipt. |

The server has no training-execution endpoint. Source mutation routes exist only when the embedding
application calls `create_app(session, trajectory_ingestion=True)`. Supplier/read-only mode omits
them. Every route still requires a bearer credential and authority check.

Training sources must declare both `purpose="training"` and `split="training"`; evaluation defaults
to heldout. A source cannot become complete until its terminal position/hash matches the
acknowledged boundary and backlog, gaps, and capture failures are clear. Remote clients should use
cursor paging and their incremental JSONL helpers, and treat server digests as opaque. See
[Trajectories](TRAJECTORIES.md) and [Training](TRAINING.md).

## Errors

Every HTTP error uses the same JSON envelope:

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

`details` is optional. The same `request_id` appears in the `X-Request-ID` response header.

| Status | Typical code | Meaning |
| --- | --- | --- |
| `400` | `invalid_request` | The HTTP request is malformed. |
| `401` | `unauthorized` | A bearer credential is missing. |
| `402` | `budget_exhausted` | The frozen environment-session budget is exhausted. |
| `401` | `unauthorized` | The credential is missing, malformed, expired, revoked, or otherwise invalid. No policy was consulted. |
| `403` | `forbidden` | The credential is valid but its server-owned policy, scope, audience, or authority generation does not authorize the operation. Resource existence is intentionally hidden. |
| `404` | `not_found` | An unprotected route or asset does not exist. Protected resources commonly use `403` to avoid disclosure. |
| `405` | `method_not_allowed` | The route exists but does not support the requested HTTP method. |
| `409` | `conflict` | Current session state conflicts with the requested operation. |
| `413` | `request_too_large` | The request exceeds the service or frozen artifact limit. |
| `422` | `invalid_request` or `unsupported` | Schema validation failed or the requested operation is unsupported. |
| `500` | `internal_error` | An unexpected server failure occurred. The response never includes a stack trace. |
| `503` | `unavailable` | The environment supplier is unavailable. |

The Python and TypeScript clients raise a typed `ServiceError` only after strictly validating a bounded error envelope. Otherwise they return a generic service failure without trusting arbitrary response content.

## SDK examples

### Python

```python
import os

from environment_harness.client import EnvironmentClient
from environment_harness.errors import ServiceError

client = EnvironmentClient(
    "http://127.0.0.1:8765",
    os.environ["EH_TOKEN"],
    allow_loopback=True,
)
for environment_session in client.list(limit=100):
    print(environment_session["id"], environment_session["status"])

try:
    result = client.advance("environment-session-id")
except ServiceError as error:
    print(error.status, error.code, error.request_id)
```

### TypeScript

```ts
import { EnvironmentClient, ServiceError } from "@environment-harness/client";

const client = new EnvironmentClient(
  "http://127.0.0.1:8765",
  process.env.EH_TOKEN!,
  true,
);

for (const environmentSession of await client.list({limit: 100})) {
  console.log(environmentSession.id, environmentSession.status);
}

try {
  await client.advance("environment-session-id");
} catch (error) {
  if (error instanceof ServiceError) {
    console.error(error.status, error.code, error.requestId);
  }
}
```

## Contract maintenance and compatibility

- `contracts/openapi.json` is generated from `environment_harness.server:create_app` by `scripts/build_openapi.py`.
- CI fails when the checked-in OpenAPI document differs from the application.
- Pydantic contract schemas in [`contracts/`](../contracts/) are generated separately and checked for drift.
- Additive response fields are allowed within v1. Clients should ignore fields they do not recognize.
- Breaking transport changes require a new API path version. Behavioral compatibility and recovery limits are tracked in [`COMPATIBILITY.md`](COMPATIBILITY.md), with release notes in [`CHANGELOG.md`](../CHANGELOG.md).

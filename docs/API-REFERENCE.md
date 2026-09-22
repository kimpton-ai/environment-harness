# EnvironmentHarness HTTP API reference

The EnvironmentHarness HTTP API exposes environment sessions, evidence, activity, artifacts, and evaluation results. API version 1 uses the `/v1` path prefix and the `environment-session.v1` protocol.

The canonical machine-readable contract is [`contracts/openapi.json`](../contracts/openapi.json). A running service exposes the same document at `/openapi.json` and interactive Swagger UI at `/docs`. Standalone JSON Schemas under `contracts/`, including `ActivityPage.schema.json` and `ActivitySnapshot.schema.json`, record the corresponding durable response shapes. [`PROTOCOL.md`](PROTOCOL.md) defines authority, lifecycle, recovery, and evidence guarantees that cannot be expressed completely in OpenAPI.

## Base URL and authentication

The local CLI listens on `http://127.0.0.1:8765` by default. Remote services must use HTTPS. Generate a local researcher credential from the same evidence store that the service uses:

```sh
export EH_TOKEN="$(environment-harness --store .local/demo token)"
export EH_URL="http://127.0.0.1:8765"
```

Every route except `GET /health` requires an opaque bearer credential:

```http
Authorization: Bearer <credential>
```

Credentials are scoped to a tenant and role. Agent credentials are additionally scoped to one environment session, participant, and authority generation.

| Role | Intended authority |
| --- | --- |
| `researcher` | Create and inspect environment sessions, issue participant credentials, control lifecycle, and evaluate results. |
| `worker` | Operate and recover authorized environment sessions without researcher-only creation or scoring authority. |
| `scorer` | Read authorized evidence and publish or inspect score reports. |
| `agent` | Observe and act only as the credential's bound participant. |

The `x-roles` field on each OpenAPI operation lists the accepted roles. A listed role can still receive `403` when its tenant, environment-session scope, participant, audience, or authority generation does not authorize the specific resource.

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
```

## Endpoint summary

| Method and path | Roles | Purpose |
| --- | --- | --- |
| `GET /health` | Public | Check service and protocol health. |
| `GET /v1/environment` | All authenticated roles | Read the active environment contract. |
| `POST /v1/environments` | Researcher | Create an environment session idempotently. |
| `GET /v1/environments` | Researcher | List environment sessions. |
| `GET /v1/environments/{environment}` | All authenticated roles | Read one authorized environment session. |
| `GET /v1/environments/{environment}/observation` | All authenticated roles | Read a participant-specific observation. |
| `POST /v1/environments/{environment}/actions` | Agent | Submit a participant action. |
| `GET /v1/environments/{environment}/events` | All authenticated roles | Read authorized evidence as JSON or SSE. |
| `GET /v1/activity/events` | Researcher, worker | Read tenant activity as JSON or SSE. |
| `GET /v1/activity/snapshot` | Researcher, worker | Read the current experiment, scenario, and environment-session hierarchy. |
| `GET /v1/experiments/{experiment}/events` | Researcher, worker | Read activity for one experiment. |
| `GET /v1/environments/{environment}/activity` | Researcher, worker | Read activity for one environment session. |
| `GET /v1/environments/{environment}/agent-work` | Researcher, worker, agent | Read agent-work records. |
| `POST /v1/environments/{environment}/commands` | Researcher, worker, or agent; command-specific | Execute a lifecycle command. |
| `POST /v1/environments/{environment}/credentials` | Researcher | Issue a scoped participant credential. |
| `POST /v1/environments/{environment}/operations` | Agent | Persist an authorized external-operation intent. |
| `POST /v1/environments/{environment}/artifacts` | All authenticated roles | Store an authorized artifact. |
| `GET /v1/environments/{environment}/artifacts/{key}` | All authenticated roles | Download an authorized artifact. |
| `GET /v1/environments/{environment}/reports` | Researcher, scorer | List versioned score reports. |
| `POST /v1/environments/{environment}/reports` | Researcher, scorer | Publish a versioned score report. |
| `GET /v1/environments/{environment}/turn-series` | Researcher, scorer | Read bounded turn-level evidence series for analysis and visualization. |
| `GET /v1/environments/{environment}/export` | All authenticated roles | Stream evidence or entitled training rows as NDJSON. |
| `POST /v1/compare` | Researcher, scorer | Compare 1 to 100 environment sessions. |

`{environment}` is always an environment-session ID, despite the historical plural route name.

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
  "$EH_URL/v1/environment"
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
  "$EH_URL/v1/environments"
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
  "$EH_URL/v1/environments?limit=100"
```

The response is an array of tenant-visible environment-session records in descending ID order. When another page exists, the response includes both `X-Next-Cursor` and a relative `Link` header with `rel="next"`:

```http
X-Next-Cursor: 0123456789abcdef0123456789abcdef
Link: </v1/environments?limit=100&cursor=0123456789abcdef0123456789abcdef>; rel="next"
```

Pass that cursor unchanged; clients must not construct or interpret it. The final page omits both headers. Page size is bounded to 1–1,000, and keyset pagination avoids increasingly expensive offsets.

### Get one environment session

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/environments/$SESSION_ID"
```

The response includes the current status, revision, frozen manifest, participants, lineage, budget state, and timestamps visible to the caller.

### Read an observation

An agent credential is already bound to its participant. A researcher, worker, or scorer can select a participant explicitly.

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/environments/$SESSION_ID/observation?participant=alice"
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
  "$EH_URL/v1/environments/$SESSION_ID/actions"
```

The response is the accepted action receipt or the previously committed receipt for an identical retry.

### Read agent work

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/environments/$SESSION_ID/agent-work?limit=100"
```

```json
{
  "work": [
    {"id":"work_01","revision":2,"participant":"alice","generation":0,"status":"pending"}
  ]
}
```

Agent callers see only their own participant's work.

### Execute a lifecycle command

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"operation":"cancel","arguments":{}}' \
  "$EH_URL/v1/environments/$SESSION_ID/commands"
```

The command envelope is always:

```json
{"operation":"checkpoint","arguments":{"lease":{"owner":"worker","epoch":1}}}
```

Supported operation names are `advance`, `lease`, `release`, `cancel`, `resolve`, `close_phase`, `checkpoint`, `reconcile_agent`, `resume`, `branch`, `control`, `memory`, `transfer`, `external_event`, and `finalize_outcomes`. `advance` resolves at most one ready externally controlled phase under the normal fenced writer lease; it returns `waiting` while required actions or events are missing and never executes a model. Other arguments are the corresponding `EnvironmentSession` method arguments after `environment` and `who`. Unknown operations or incompatible arguments return the documented `422 invalid_request` envelope. See [`PROTOCOL.md`](PROTOCOL.md), [`coordinated-sessions.md`](coordinated-sessions.md), and [`REMOTE-WORKERS.md`](REMOTE-WORKERS.md) before building recovery, remote-worker, or branching automation.

| Operation | Authorized role | Required arguments | Success result | Common errors |
| --- | --- | --- | --- | --- |
| `advance` | Researcher, worker | None; optional `owner` | One ready phase result, or `{"status":"waiting","revision":N,"deadline_exceeded":false}` | `403` scope, `409` lost authority or invalid phase, `503` supplier unavailable |
| `lease` | Researcher, worker | `owner`; optional `ttl` (1–300 seconds) | Fenced lease with `owner`, `epoch`, and `expires` | `409` another writer is active, `422` invalid owner/TTL |
| `release` | Researcher, worker | `lease` | `{"released":true}` | `403` scope, `409` stale/expired lease |
| `cancel` | Researcher | None | Cancelled status plus unresolved agent-work and operation IDs | `403` role/scope, `409` already terminal |
| `resolve` | Researcher, worker | `lease` | Committed revision, status, and evidence event | `409` incomplete phase, stale lease, or invalid transition; `503` supplier unavailable |
| `close_phase` | Researcher, worker | `lease`, `revision`; optional `reason` | Closed revision receipt | `409` stale phase/lease, `422` wall-clock phase |
| `checkpoint` | Researcher | `lease`; optional `exact_agents` | Checkpoint ID, revision, hash, and exactness | `409` unsettled work, `422` unsupported capability |
| `reconcile_agent` | Researcher, worker | `lease`, `operation_id`, `response`, `evidence`; optional `agent_state` | Responded operation receipt | `409` conflicting/stale work, `422` invalid evidence |
| `resume` | Researcher, worker | `lease`; optional `implementations` | Updated environment session | `409` unsettled effects or changed implementations, `422` unsupported capability |
| `branch` | Researcher | `checkpoint`; optional `interventions`, `new_environment` | New environment session | `403` unavailable checkpoint, `409` integrity/version conflict, `422` unsupported pending/live-write state |
| `control` | Researcher | `lease`, `command` (`pause` or `cancel`) | Updated lifecycle status | `409` stale lease/terminal session, `422` unknown command |
| `memory` | Agent | `memory`; optional `agent_state`, `expected_revision` | `null` after the update commits | `403` participant authority, `409` stale revision/size, `422` missing checkpoint hook |
| `transfer` | Researcher | `lease`, `participant`, `controller`; optional `active` | New scoped participant principal | `409` decision boundary/last participant, `422` undeclared participant |
| `external_event` | Researcher, worker | `lease`, `source`, `cursor`, `event_time`, `payload`; optional `gap` | Evidence event receipt | `409` stale cursor/queue limit/lease |
| `finalize_outcomes` | Researcher, worker | `lease`, `report_revision` | Completed outcome receipt | `409` missing report or unsettled operations |

All command errors use the shared envelope below. A `403` can intentionally hide whether an environment session exists; a `409` means the caller should refresh state or reconcile authority rather than retry blindly; a `422` means the operation name, arguments, or frozen capability does not permit the request.

### Issue a participant credential

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"participant":"alice","ttl":3600}' \
  "$EH_URL/v1/environments/$SESSION_ID/credentials"
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
  "$EH_URL/v1/environments/$SESSION_ID/operations"
```

```json
{"id":"lookup_01","status":"prepared"}
```

## Evidence and activity

### Read evidence events

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/environments/$SESSION_ID/events?after=0&limit=200"
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
  "$EH_URL/v1/environments/$SESSION_ID/events"
```

### Read activity

The global, experiment, and environment-session feeds share `after`, `limit`, `Accept`, and `Last-Event-ID` behavior:

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/activity/events?after=0&limit=200"

curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/experiments/$EXPERIMENT_ID/events?after=0&limit=200"

curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/environments/$SESSION_ID/activity?after=0&limit=200"
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
  "$EH_URL/v1/activity/snapshot"
```

The response contains current experiment, scenario, and environment-session records plus the current global activity cursor. It is the recovery source for clients that miss activity events.
Its machine-readable response contract is
[`ActivitySnapshot.schema.json`](../contracts/ActivitySnapshot.schema.json).

### Store and download an artifact

Upload bytes with their real media type:

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @result.json \
  "$EH_URL/v1/environments/$SESSION_ID/artifacts"
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
  "$EH_URL/v1/environments/$SESSION_ID/artifacts/$ARTIFACT_KEY"
```

The download response uses `application/octet-stream` and a `Content-Disposition` filename. Authorization is checked against the environment session and artifact audience before any bytes are returned.

## Evaluation

### List score reports

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/environments/$SESSION_ID/reports"
```

The response is an array of versioned report envelopes ordered by report revision.

### Publish a score report

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Content-Type: application/json" \
  --data @score-report.json \
  "$EH_URL/v1/environments/$SESSION_ID/reports"
```

The body follows [`ScoreReport.schema.json`](../contracts/ScoreReport.schema.json). It identifies the scorer and version, the evidence cursor, metrics and their definitions, findings, rewards, uncertainty, and provenance. The response is the stored report envelope with its revision.

### Read bounded turn series

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/environments/$SESSION_ID/turn-series?start_turn=1&end_turn=5000&max_points=300"
```

The response projects public numeric signals, cumulative reward, and cumulative executed actions onto environment-session turns. `start_turn` and `end_turn` select a window; `max_points` is bounded from 20 to 1000 per series. When a series exceeds that limit, the projection retains its endpoints and bucket extrema so long sessions remain readable without hiding spikes.

### Export evidence or training rows

```sh
curl --fail-with-body -H "Authorization: Bearer $EH_TOKEN" \
  "$EH_URL/v1/environments/$SESSION_ID/export?format=evidence" \
  --output evidence.ndjson
```

Use `format=training` only when the frozen environment purpose and split grant training entitlement. Both formats stream one JSON object per line. Unknown formats return `422`.

### Compare environment sessions

```sh
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $EH_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"environments":["env_original","env_branch"]}' \
  "$EH_URL/v1/compare"
```

The request accepts 1 to 100 authorized environment-session IDs. The response contains session lineage, selected report revisions, metric groups, warnings, raw values, and aggregate summaries. Related turns and branches are not treated as independent experiments.

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
| `403` | `forbidden` | The credential, role, scope, audience, or authority generation does not authorize the operation. Resource existence can be intentionally hidden. |
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

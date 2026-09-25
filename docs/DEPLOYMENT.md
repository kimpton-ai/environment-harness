# How to run EnvironmentHarness

EnvironmentHarness currently qualifies a local Python SDK, a persistent SQLite store, and a
same-origin read-only viewer. This guide explains that supported path, the supplier-service seam,
and the work that remains before claiming a production hosted deployment.

## Supported deployment matrix

| Topology | Status | Storage | Browser access |
| --- | --- | --- | --- |
| Python SDK in one process | Supported locally | SQLite store directory | Start `serve` against the same store |
| Loopback CLI server | Supported locally | SQLite store directory | Automatic local viewer access on `127.0.0.1` |
| Embedded supplier application | Integration seam only | SQLite or optional PostgreSQL/S3 adapters | Same-origin viewer with a manually issued bearer credential |
| Production hosted service | Not yet qualified | Operator-owned | Requires deployment-specific acceptance evidence |
| Separately hosted UI and API | Not supported | Operator-owned | The current server intentionally denies cross-origin browser requests |

The presence of a server or storage adapter does not establish availability, throughput,
backup, recovery, or security for a particular hosted environment. See [Release scope](STATUS.md)
for the current qualification limits.

## Run the supported local deployment

### Prerequisites

- Python 3.12 or later
- A private local directory for environment-session evidence
- The `server` optional dependency

### Install and create sample data

Install the latest stable release:

```sh
python -m pip install "environment-harness[server]"
```

Create synthetic environment sessions in a named store:

```sh
environment-harness --store ./environment-sessions quickstart --turns 3
```

The store directory is the persistence boundary. Restarting Python or the viewer does not remove
progress as long as the same directory remains available.

### Start the viewer and API

```sh
environment-harness --store ./environment-sessions serve --open
```

`serve` binds to `127.0.0.1` on port `8765`. It never listens on an external interface. Omit
`--open` when you want to open `http://127.0.0.1:8765/overview` yourself. The flag changes only browser
launch behavior, not authentication.

Use another port when needed:

```sh
environment-harness --store ./environment-sessions serve --port 9876
```

The loopback viewer obtains an in-memory read-only viewer credential on every page load. Refreshes, deep
links, and new tabs work without a login form or browser storage. Every `/v1` API route still
requires an explicit bearer credential.

### Call the local API

Issue a credential from the same store:

```sh
ENVIRONMENT_HARNESS_TOKEN="$(
  environment-harness --store ./environment-sessions token
)"
export ENVIRONMENT_HARNESS_TOKEN
```

Pass the value only in the `Authorization` header:

```sh
curl \
  --header "Authorization: Bearer $ENVIRONMENT_HARNESS_TOKEN" \
  http://127.0.0.1:8765/v1/sessions
```

Do not put credentials in URLs, committed configuration, shell history, browser bundles, or logs.
The Python and TypeScript clients require HTTPS unless the caller explicitly permits loopback HTTP.

### Verify the service

```sh
curl http://127.0.0.1:8765/health
```

The response is:

```json
{"status":"ok","protocol":"environment-session.v1"}
```

OpenAPI is available at `/openapi.json`, and the interactive API reference is available at
`/docs`. The viewer routes are `/overview`, `/comparisons`, `/sessions/{id}`, and the supported session
subroutes.

## Understand local persistence

`EvidenceStore` creates `evidence.sqlite` inside the directory passed to `--store`. The directory
and database use owner-only permissions where the operating system supports them. SQLite uses a
write-ahead log and full synchronous commits.

Use the same `--store` and `--tenant` values for the SDK, CLI, and viewer when they should see the
same environment sessions. A different directory is a different local deployment.

EnvironmentHarness does not currently provide a local backup command. For a cold copy, stop the
server and every process writing to the store, then copy the complete store directory. A copied
database is not a qualified production backup until its restore has been tested in the target
environment.

## Embed a supplier application

## Durable local scheduling

The database, not the process-local thread pool, is the source of requested Session work. Creating
an Experiment and all of its Session rows is one transaction; only after it commits does the bounded
local scheduler claim work. Submitting a job to the thread pool is a post-commit delivery attempt —
if submission fails, the Session stays durably `queued`, the failure is recorded as scheduler
activity, and a later `harness.reconcile()` or process restart retries it without creating another
Session.

Opening a store runs startup reconciliation before the process accepts new work:

- every eligible `queued` Session is reconstructed from its frozen Experiment, Scenario, seed,
  trial, turn limit, participant, policy, and environment references;
- a Session left `running` by a lost process becomes `interrupted` with a `process_loss` reason and
  requires the existing explicit resume; it is never executed twice automatically; and
- terminal Sessions remain byte-for-byte unchanged.

Reconciliation is idempotent, runs under the store's normal writer-concurrency mechanism, and
produces durable activity evidence for every state correction. It assumes no other live process is
executing the same store's sessions. This is a local durable scheduler, not a hosted queue: it adds
no Redis, worker service, deployment orchestration, cross-machine lease, or remote execution.

### Typed environment factories

Recovered work resolves executable code through typed factories configured once on one
`EnvironmentHarness`:

```python
harness = EnvironmentHarness(
    store,
    environments=(WarehouseEnvironment, BrowserEnvironment),
    agent_factories={"alice": MyAgent},
)
experiment = harness.experiment("study", scenarios, environment=WarehouseEnvironment)
```

Python callers supply implementation classes or typed factory objects, never import-path strings.
Only the portable reference `(id, version, spec_digest)` is serialized. Recovery matches that frozen
reference against the specs produced by the currently supplied factories and verifies the
schema/capability digest before scheduling; it never dynamically imports persisted text. A missing,
duplicate, or mismatched factory leaves the Session `blocked` with a typed, inspectable reason and
never substitutes another implementation. Supplying the correct factory later makes the same frozen
Session schedulable through `resume()` without rewriting the Experiment or Session.

`create_app()` is the application seam for a supplier-owned service. Without `local_access`, it
uses credential mode: `/local/connect` is absent, the viewer shows a credential form, and every API
request requires a bearer credential.

The following synthetic application is suitable for integration testing, not production hosting:

```python
from environment_harness import EnvironmentHarness, EvidenceStore
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.server import create_app

harness = EnvironmentHarness(
    EvidenceStore("./environment-sessions"),
    environment_factory=SyntheticEnvironment,
    agent_factories={"alice": SyntheticAgent},
)
app = create_app(harness)
```

Issue admin credentials with `harness.admin_credential()`. The server never accepts a
policy, role, or permission from a request. See [Authentication](AUTHENTICATION.md).

Save that module as `app.py`, then run it behind Uvicorn during development:

```sh
python -m pip install "environment-harness[server]"
uvicorn app:app --host 127.0.0.1 --port 8765
```

For a remote service, terminate HTTPS outside the application and preserve the original same
origin. Do not construct `LocalViewerAccess` for a remote deployment. The local exchange trusts the
loopback machine and is deliberately separate from supplier authentication.

The current browser bundle must be served by the same EnvironmentHarness application as its API.
The server rejects browser requests whose `Origin` differs from its own base URL. A UI hosted on a
separate domain therefore requires a future, reviewed authentication and cross-origin design.

### Historical trajectory ingestion

`create_app(session)` is read-only for historical sources. It exposes authenticated trajectory,
source-status, snapshot, dataset, and recorded-training-result reads but omits source registration,
record ingestion, and status mutation. An explicitly managed ingestion service may use:

```python
app = create_app(session, trajectory_ingestion=True)
```

That flag adds only bearer-authenticated admin routes. It does not make ingestion
public, authorize environment actions, run selectors, or execute trainer plugins. Keep a source's
domain journal as the delivery backlog during service outages and resume from its last acknowledged
position and hash. Do not configure ingestion on a supplier/viewer process that is intended to be
read-only.

The server never executes `TrainingIntegration` code. Trusted local Python or the CLI may invoke an
explicitly installed integration and record its immutable receipt; HTTP can only read that receipt.

## Optional PostgreSQL and object-storage seam

Install the PostgreSQL dependency with:

```sh
python -m pip install "environment-harness[server,postgres]"
```

`PostgresEvidenceStore` expects a dedicated schema and an object-store implementation. A schema
owner must call `PostgresEvidenceStore.initialize()` before application workers start; constructing
the store does not run migrations. Applied migration checksums are immutable.

This adapter is not a complete deployment product. Before treating it as production-ready, an
operator must qualify at least:

- database and object-store backup and restore;
- schema-upgrade ordering and rollback behavior;
- HTTPS, network boundaries, identity, and credential rotation;
- admission limits, monitoring, capacity, and failure recovery;
- worker shutdown before tenant erasure; and
- live acceptance tests for the chosen infrastructure.

See [Protocol](PROTOCOL.md), [Coordinated persistent sessions](coordinated-sessions.md), and
[Adapters](ADAPTERS.md) for the existing contracts.

## Troubleshooting

### A refreshed route returns 404

Run the packaged EnvironmentHarness server. It serves the viewer shell for `/overview`,
`/comparisons`, `/experiments/{id}`, `/experiments/{id}/scenarios`,
`/experiments/{id}/scenarios/{scenario}`, `/experiments/{id}/sessions`, and supported
`/sessions/{id}/{section}` deep links. A generic static-file server does not know those routes.

`/experiments/{id}` restores the frozen experiment overview. Its Scenarios tab appears only for
meaningful scenario snapshots, and its Sessions tab retains a flat, filterable list. Each child
session links to its canonical `/sessions/{id}/{section}` inspection route.

`/experiments/{id}/training` is refresh-safe even when its conditional tab is hidden, and
`/trajectories/{id}` restores imported-trajectory inspection. Both remain authenticated read-only
viewer routes.

### The local viewer shows “Local viewer unavailable”

Confirm that the URL uses the exact origin printed by `serve`, including port and `127.0.0.1`.
Local connection rejects a different host spelling, origin, or non-loopback peer. Restart the CLI
server and retry from the banner.

### The supplier viewer asks for a credential

That is expected when the application was created without local viewer access. Issue a scoped
credential through a trusted administrative path and paste it into the viewer. The viewer retains
it only in page memory.

### An API request returns `401`

The viewer's automatic local exchange does not make `/v1` routes public. Supply a credential from
`environment-harness token` or the trusted participant-credential endpoint.

## Maintenance triggers

Update this guide in the same pull request when any of these change:

- `serve` flags, bind address, authentication, routes, or health response;
- store location, migration, backup, or restoration behavior;
- historical-ingestion or training-result boundaries;
- supported Python version or installation extras;
- remote browser topology or cross-origin policy; or
- hosted-deployment qualification in [Release scope](STATUS.md).

Release maintainers should also follow [How to release EnvironmentHarness](RELEASING.md). Viewer
contributors should follow [How to maintain the viewer](VIEWER-MAINTENANCE.md).

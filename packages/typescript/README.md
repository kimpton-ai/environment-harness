# EnvironmentHarness client

Typed HTTP client for an EnvironmentHarness service, built on the same versioned contracts as the
Python SDK.

## Install

Download the `environment-harness-client-0.3.0-rc.1.tgz` asset from the GitHub release, then:

```sh
npm install ./environment-harness-client-0.3.0-rc.1.tgz
```

There is no registry publication.

## Use

```ts
import { EnvironmentClient } from '@environment-harness/client';

const client = new EnvironmentClient(
  'http://127.0.0.1:8765',
  process.env.ENVIRONMENT_HARNESS_TOKEN!,
  true,                       // explicitly permit a loopback HTTP endpoint
);

const sessions = await client.sessions({ limit: 100 });
const session = await client.session(sessions.items[0].metadata.id);
```

Use HTTPS for anything remote. Keep credentials out of source control and browser bundles; the
server enforces credential scope regardless.

## Surface

| Area | Methods |
| --- | --- |
| Resources | `experiments()`, `experiment()`, `scenarioSets()`, `sessions()`, `session()`, `policies()` |
| Participants | `observe()`, `submit()`, `credentials()` — issues a credential scoped to one session, participant, and generation |
| Lifecycle | `advance()` resolves at most one ready phase and returns `waiting` while required actions or events are missing; `cancel()` cancels without holding the writer lease and reports unresolved agent work and operations; `createCheckpoint()`, `branch()` |
| Evidence | `evidence()`, `scores()`, `turnSeries()`, `invocations()`, `activity()`, `activityHierarchy()` |
| Trajectories | `trajectories()`, `trajectory()`, cursor-paged `trajectoryRecords()`, `trajectorySnapshots()`, `freezeTrajectory()` |
| Sources | `sourceStatus()` in any deployment; registration, ingestion, and status mutation need a server with ingestion explicitly enabled |
| Comparison | `compare()` returns typed `metric_groups` with selected report revisions; incompatible units or definitions stay in separate groups |

Collections return `{ items, nextCursor, links }` with an opaque cursor. Snapshot and dataset
exports are incremental async generators:

```ts
for await (const row of client.streamSnapshotRecords(snapshotId)) {
  process(row);
}
```

## Contract notes

- Digests are server-authoritative and opaque here; the client never recomputes them.
- Every method rejects with a typed `ServiceError` only after validating the service's bounded
  error envelope. Malformed or non-JSON upstream errors stay generic.
- There is no trainer-execution method. The server reads training receipts and does not run
  integrations.
- The client does not restore agent processes or qualify hosted execution.

`npm test` compiles the package and exercises request shapes and inherited-record reconstruction.

Source and documentation: https://github.com/kimpton-ai/environment-harness ·
[Compatibility](../../docs/COMPATIBILITY.md) · MIT licensed, see LICENSE.

# EnvironmentHarness client

Typed HTTP client for an EnvironmentHarness supplier service. This package uses the same versioned contracts as the Python SDK.

Install the `environment-harness-client-0.2.4-rc.1.tgz` asset from the GitHub release with `npm install ./environment-harness-client-0.2.4-rc.1.tgz`. A registry publication is not required.

```ts
import { EnvironmentClient } from '@environment-harness/client';

const client = new EnvironmentClient('http://127.0.0.1:8765', process.env.ENVIRONMENT_HARNESS_TOKEN!, true);
const environments = await client.list({limit: 100});
```

The third argument explicitly permits a local HTTP endpoint. Use HTTPS for remote services. Keep credentials outside source control and public browser bundles. The service enforces credential scope. The client does not restore arbitrary agent processes or qualify hosted execution.

Source and documentation: https://github.com/kimpton-ai/environment-harness

MIT licensed. See LICENSE.

`client.advance(environmentId)` resolves at most one ready externally controlled phase and returns `waiting` while required actions or events are missing. `client.credentials(environmentId, participant, ttl)` issues a scoped participant credential. `client.cancel(environmentId)` cancels a running session without its writer lease and returns unresolved agent work and external operations. `client.compare(ids)` retains the existing response fields and adds typed `metric_groups`, `warnings` and selected report revisions. Incompatible units or definitions appear in separate groups. Legacy reports without definitions remain visible as raw values. Every method rejects with a typed `ServiceError` only after validating the service's bounded standard error envelope; malformed or non-JSON upstream errors remain generic.

Run `npm test` to compile and exercise client request shapes and inherited-record reconstruction. See [SDK compatibility](../../docs/COMPATIBILITY.md) for details.

# EnvironmentHarness client

Typed HTTP client for a EnvironmentHarness supplier service. This package uses the same versioned contracts as the Python SDK.

Install the `environment-harness-client-0.1.0.tgz` asset from the GitHub release with `npm install ./environment-harness-client-0.1.0.tgz`. A registry publication is not required.

```ts
import { EnvironmentClient } from '@environment-harness/client';

const client = new EnvironmentClient('http://127.0.0.1:8765', process.env.ENVIRONMENT_HARNESS_TOKEN!, true);
const environments = await client.list();
```

The third argument explicitly permits a local HTTP endpoint. Use HTTPS for remote services. Keep credentials outside source control and public browser bundles. The service enforces credential scope. The client does not restore arbitrary agent processes or qualify hosted execution.

Source and documentation: https://github.com/pollice-verso/environment-harness

MIT licensed. See LICENSE.

# Adapter capabilities

| Adapter | Implemented boundary | Persistence limits |
| --- | --- | --- |
| Python client | Authenticated commands, observations, actions, event replay | Supplier capabilities are authoritative. No automatic write retry. |
| TypeScript client | Typed session reads, commands, replay and live polling | Same remote contract. Token held in memory. |
| JSON environment process | Separate supplier process; typed state, observations and transitions | Supplier serializes state. Process handles are disposable. |
| Command agent | Bounded JSON stdin/stdout, timeout, clean environment and workspace | No process-memory recovery. Trusted local execution only. |
| HTTP agent | Authenticated `/act` request | Opaque internals; no implied checkpoint hook. |
| Instrumented model | Rendered requests, responses and compaction metadata | Supplied generation callback owns provider transport and spending controls. |
| MCP | Tool allowlist and recorded intent/result around `ClientSession.call_tool` | No exactly-once claim for tools without receipt lookup. MCP 1.30.0 is the locked optional dependency. |
| Inspect | Async agent bridge wrapper | Requires upstream Inspect and its sandbox/instrumentation configuration. Not installed or executed here. |
| OpenEnv | A synchronous native session mapped to one participant | No checkpoint, resume or branch claim; remote session loss is explicit. Not installed here. |
| PettingZoo AEC | Native selected actor and dead-step behavior | One native session per adapter instance. No process recovery or branching. |
| PettingZoo parallel | One simultaneous upstream step for a complete decision set | One cached transition receipt tolerates a local database commit retry. Restart cannot restore native state. |
| Verifiers | Existing 0.1.14 rollout invocation and authorized training projection | The bridge accepts 0.1.14, while the locked optional dependency is 0.3.1, so it currently fails closed as unsupported. No v1 taskset assumptions or Prime package are claimed. |
| Docker | Digest-pinned isolated workers with no network, non-root user and resource bounds | Container handles can be inspected/stopped. No arbitrary process checkpoint. |
| Modal | Direct Sandbox SDK start/status/stop with network blocked | Requires an operator-supplied app/image. No cloud execution was performed. |
| PostgreSQL/S3 | Dedicated metadata schema and protected object backend | Implementation supplied; hosted recovery and durability qualification remain open. |

Adapters are optional. Their existence does not mean their upstream dependencies are installed, that a private simulator exists, or that hosted execution is qualified. Browser and terminal agents are programs running inside isolated workers. The harness does not itself implement a browser engine or terminal emulator.

Technical references used for boundaries: [PettingZoo parallel API](https://pettingzoo.farama.org/api/parallel/), [OpenEnv core API](https://huggingface.github.io/OpenEnv/reference/core.html), [Inspect agent bridge](https://inspect.aisi.org.uk/agent-bridge.html), and [Modal Sandbox API](https://modal.com/docs/sdk/py/latest/Sandbox). Upstream APIs can change; admitted deployments must pin and qualify their actual package versions.

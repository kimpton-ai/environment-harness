# Adapter capabilities

| Adapter | Implemented boundary | Persistence limits |
| --- | --- | --- |
| Python client | Authenticated commands, observations, actions, event replay | Supplier capabilities are authoritative. No automatic write retry. |
| TypeScript client | Typed session reads, commands, replay and live polling | Same remote contract. Token held in memory. |
| JSON environment process | Separate supplier process; typed state, observations and transitions | Supplier serializes state. Process handles are disposable. |
| HTTP environment worker | Authenticated remote execution of the same serialized environment contract | Supervisor retains evidence and credentials. No implicit write retry; supplier methods must be pure over serialized inputs. |
| ORS HTTP/SSE | Explicit session, task discovery, tool calls and original task receipts | Session handles are not checkpoints. Interrupted writes remain outcome-unknown until reconciled with the recorded task ID. |
| Legacy environment | Adapts a `world-session.v1` implementation into a distinct native version | Creates new environment sessions only; historical manifests and evidence still require their original reader. |
| Command agent | Bounded JSON stdin/stdout, timeout, clean environment and workspace | No process-memory recovery. Trusted local execution only. |
| HTTP agent | Authenticated `/act` request | Opaque internals; no implied checkpoint hook. |
| Instrumented model | Rendered requests, responses and compaction metadata | Supplied generation callback owns provider transport and spending controls. |
| MCP | Tool allowlist and recorded intent/result around `ClientSession.call_tool` | No exactly-once claim for tools without receipt lookup. MCP 1.30.0 is the locked optional dependency. |
| Inspect | Async agent bridge wrapper | Requires upstream Inspect and its sandbox/instrumentation configuration. Not installed or executed here. |
| OpenEnv | A synchronous native session mapped to one participant | No checkpoint, resume or branch claim; remote session loss is explicit. Not installed here. |
| PettingZoo AEC | Native selected actor and dead-step behavior | One native session per adapter instance. No process recovery or branching. |
| PettingZoo parallel | One simultaneous upstream step for a complete decision set | One cached transition receipt tolerates a local database commit retry. Restart cannot restore native state. |
| Verifiers | Bounded `>=0.3.1,<0.4` legacy rollout invocation and authorized rows derived from canonical `Trajectory` resources | Installed and checked in a path-routed integration job. No native Verifiers v1 `Episode`, taskset, harness, runtime, or Prime integration is claimed. |
| Trajectory source | Read-only import of a durable journal with namespaced run identity and cursor-plus-hash acknowledgement | The source owns execution and its local backlog. Import never replays a model, action, simulator, or uncertain effect. |
| Training integration | Explicitly injected Python/CLI consumer of an immutable trajectory dataset | Live clients and credentials are not serialized. HTTP can read receipts but cannot execute integrations. |
| Docker | Digest-pinned isolated workers with no network, non-root user and resource bounds | Container handles can be inspected/stopped. No arbitrary process checkpoint. |
| Modal | Direct Sandbox SDK start/status/stop with network blocked | Requires an operator-supplied app/image. No cloud execution was performed. |
| PostgreSQL/S3 | Dedicated metadata schema, aggregate environment payload admission and receipt-checked tenant erasure | Production workload, backup/restore and durability qualification remain open. Erasure requires stopped workers and an unversioned runtime bucket. |

Adapters are optional. Their existence does not mean their upstream dependencies are installed, that a private simulator exists, or that hosted execution is qualified. Browser and terminal agents are programs running inside isolated workers. The harness does not itself implement a browser engine or terminal emulator.

Technical references used for boundaries: [PettingZoo parallel API](https://pettingzoo.farama.org/api/parallel/), [OpenEnv core API](https://huggingface.github.io/OpenEnv/reference/core.html), [Inspect agent bridge](https://inspect.aisi.org.uk/agent-bridge.html), [Verifiers](https://github.com/PrimeIntellect-ai/verifiers), and [Modal Sandbox API](https://modal.com/docs/sdk/py/latest/Sandbox). Upstream APIs can change; admitted deployments must pin and qualify their actual package versions. RLlib and TRL converters are not in the current frozen manifest; see [Training](TRAINING.md).

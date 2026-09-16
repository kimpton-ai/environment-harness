# Connect an agent

Run the included custom JSON program:

```sh
uv run python examples/custom_agent.py --store .local/custom-agent
uv run environment-harness --store .local/custom-agent serve
```

The example runs four turns and prints an environment ID and revision 4. Its synthetic policy adds to the shared total until it reaches 2, then subtracts. It uses no model service.

`CommandAgent` sends one complete observation as JSON on stdin. Your program writes one JSON action object to stdout. Read `observation["payload"]` for environment-specific information. Diagnostic output belongs on stderr. The action must satisfy the environment's action schema. [command_agent.py](../examples/command_agent.py) is the complete program; [custom_agent.py](../examples/custom_agent.py) registers and executes it.

Use an absolute executable and script path. Commands run in a temporary working directory with a minimal environment, bounded output and a timeout. Model credentials are not inherited automatically. A local subprocess is not an operating-system security sandbox; run only trusted code through this local example. Untrusted programs need an isolated backend and explicitly scoped network access.

For Python agents, implement `act(observation) -> dict`. Declare an immutable implementation identifier and a policy version in `AgentSpec`. To support agent-state checkpoints, also implement `checkpoint() -> JSON` and `restore(state)`, and set `checkpoint=True` in its registration. The runner persists that explicit state. It cannot serialize arbitrary Python objects, sockets or external process memory.

The supplied command example deliberately leaves agent-state checkpointing disabled. Environment-state branching does not prove that a native agent process can be resumed. To run a model, your agent owns its provider integration and error handling; the environment contract stays the same. Model requests can be recorded through `InstrumentedModel`, whose supplied callback owns transport and spending limits.

The in-process Python API is a trusted embedding boundary. For independent untrusted participants, use scoped participant credentials through the HTTP API. See [protocol authority](PROTOCOL.md) and [adapter capabilities](ADAPTERS.md).

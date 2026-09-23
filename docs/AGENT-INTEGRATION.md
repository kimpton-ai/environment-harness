# Connect an agent

## Correlated model evidence

Wrap an in-process generation callback with `InstrumentedModel` when a trajectory needs attributable
model evidence. During runner invocation, request, response, and failure events share a unique call
ID and the durable agent-work operation, observation, participant, generation, and revision. Calls
outside runner context remain valid diagnostics but are explicitly uncorrelated.

Token IDs must be non-negative integers. Log probabilities must be finite and non-positive, and
paired token/logprob arrays must have equal lengths. Rendered content is opt-in. Detail that exceeds
the event limit spills to a participant-scoped JSON artifact; the event keeps identity, usage,
finish reason, validation state, request digest, and the artifact reference. The artifact limit
still applies.

`CommandAgent` and remote HTTP participants submit the existing strict action contract and cannot
attach inference evidence. Do not place tokens or model responses in an action. A future
transport-neutral inference endpoint requires a separate protocol and security review.

Run the included custom JSON program:

```sh
uv run python examples/custom_agent.py --store .local/custom-agent
uv run environment-harness --store .local/custom-agent serve
```

The example runs four turns and prints an environment ID and revision 4. Its synthetic policy adds to the shared total until it reaches 2, then subtracts. It uses no model service.

`CommandAgent` sends one complete observation as JSON on stdin. Your program writes one JSON action object to stdout. Read `observation["payload"]` for environment-specific information. Diagnostic output belongs on stderr. The action must satisfy the environment's action schema. [command_agent.py](../examples/command_agent.py) is the complete program; [custom_agent.py](../examples/custom_agent.py) registers and executes it.

Use an absolute executable and script path. Commands run in a temporary working directory with a minimal environment, bounded output and a timeout. Model credentials are not inherited automatically. A local subprocess is not an operating-system security sandbox; run only trusted code through this local example. Untrusted programs need an isolated backend and explicitly scoped network access.

For Python agents, implement `act(observation) -> dict`. Set the program's `implementation` attribute to the same immutable identifier declared in `AgentSpec`, and record its policy version there. To support agent-state checkpoints, also implement `checkpoint() -> JSON` and `restore(state)`, and set `checkpoint=True` in its registration. The runner persists that explicit state. It cannot serialize arbitrary Python objects, sockets or external process memory.

The supplied command example deliberately leaves agent-state checkpointing disabled. Environment-state branching does not prove that a native agent process can be resumed. To run a model, your agent owns its provider integration and error handling; the environment contract stays the same. Model requests can be recorded through `InstrumentedModel`, whose supplied callback owns transport and spending limits.

The in-process Python API is a trusted embedding boundary. For independent untrusted participants, use scoped participant credentials through the HTTP API. See [protocol authority](PROTOCOL.md) and [adapter capabilities](ADAPTERS.md).

Optional `act_cancellable(observation, cancel_event)` support lets managed programs stop when the session is cancelled or times out. `CommandAgent` implements it with process-group cleanup. See [cancellation and registration details](COMPATIBILITY.md).

# Implement an environment

An environment owns its rules and serializable environment state. EnvironmentHarness owns the session journal, participant delivery, checkpoint storage and execution coordination. The included [SyntheticEnvironment](../src/environment_harness/fixtures.py) is a complete, small reference implementation.

Implement these five members:

| Member | Purpose |
| --- | --- |
| `spec: EnvironmentSpec` | Stable identity/version, action schema, scheduling, supported purposes and capabilities. |
| `initialize(experiment)` | Return the initial JSON state. |
| `observe(state, participant)` | Return only the information that participant is allowed to receive. |
| `resolve(state, actions, random, events)` | Return a `Transition` with new state, per-participant outcomes/rewards and events. |
| `intervene(state, changes)` | Apply explicitly supported branch interventions; reject unsupported changes. |

Start by importing your class into the [Python experiment example](../examples/branch_comparison.py) and passing its instance to `EnvironmentSession`. Replace the agent's action program to satisfy your declared schema. Keep initialization and transitions reproducible under the recorded inputs and supplied random generator. Do not hide external effects inside a state transition; use the operation journal and receipt reconciliation.

For CLI or supplier-server discovery, package the class with a factory under the `environment_harness.environments` entry-point group:

```toml
[project.entry-points."environment_harness.environments"]
my-environment = "my_package.environment:load_environment"
```

The factory returns an environment instance. Install the package into the same Python environment as EnvironmentHarness. `environment-harness doctor` lists installed plugins. The supplier HTTP service can load a registered plugin through `environment-harness serve --environment my-environment`.

Only declare checkpoint, resume or branch support when the implementation can restore the actual state it uses. A remote session handle by itself is not a portable checkpoint. Concurrent participants must receive observations from the appropriate frozen revision, not state modified by another participant's uncommitted action.

Write independent controls for your mechanics, valid and invalid actions, privacy projections and supported interventions. The public `environment_harness.conformance.check` helper checks one transition and the advertised checkpoint path. It is a compatibility check, not evidence that a grader is valid or a hosted deployment is reliable.

Scoring is a separate implementation. Freeze scorer versions in `ExperimentSpec`, record a `ScoreReport` against an evidence cursor, and retain the distinction between deterministic checks, model judgments and human judgments. Preserve unresolved outcomes and explain what each metric establishes. [Protocol](PROTOCOL.md) and [coordinated sessions](coordinated-sessions.md) cover the complete semantics.

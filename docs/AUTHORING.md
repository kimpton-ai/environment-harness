# Implement an environment

## Design for portable trajectories

Keep environment-native action semantics, state, rewards, and verification in the environment.
EnvironmentHarness projects the frozen manifest and recorded evidence into a trajectory; authors do
not write a second journal. Give policy implementations stable identifiers and increment
`policy_version` when resuming with different behavior would be incompatible.

Domain evidence that has no core record type uses a reverse-domain namespace such as
`com.example.unreal.frame` or `com.example.drone.telemetry`. Extension payloads must be JSON,
bounded, and observational. They cannot grant authority, choose credentials, relax limits, or alter
core reward semantics. Put large binary or random-access detail in an artifact and record its digest.

For delayed rewards, emit stable reward identities and use `supersedes` to replace provisional
values. Do not mutate old evidence. Training datasets reject missing, cyclic, ambiguous, retracted,
or non-finite reward chains. Keep `terminated`, `truncated`, and verified outcome distinct.

The separately owned **Pluggable Decision-Selection Seam** defines candidate selection records. An
environment still owns candidate-to-action meaning and every externally visible operation remains
individually authorized, receipted, and reconcilable.

An environment owns its rules and serializable environment state. EnvironmentHarness owns the session journal, participant delivery, checkpoint storage and execution coordination. The included [SyntheticEnvironment](../src/environment_harness/fixtures.py) is a complete, small reference implementation.

Implement these members (`operations` is optional):

| Member | Purpose |
| --- | --- |
| `spec: EnvironmentSpec` | Stable identity/version, action schema, scheduling, supported purposes, capabilities and operation identities. |
| `operations` | Optional mapping of operation names to environment-owned `EnvironmentOperation` instances. |
| `initialize(experiment)` | Return the initial JSON state. |
| `observe(state, participant)` | Return only the information that participant is allowed to receive. |
| `resolve(state, actions, random, events)` | Return a `Transition` with new state, per-participant outcomes/rewards and events. |
| `intervene(state, changes)` | Apply explicitly supported branch interventions; reject unsupported changes. |

Start with the [typed experiment](../examples/typed_experiment.py) for the minimal high-level API or the [custom environment experiment](../examples/custom_environment_experiment.py) for operations, scoring, findings and viewer output. The [external simulator experiment](../examples/external_environment_experiment.py) applies the same operation contract to a long-lived process that could be replaced by Minecraft, Unreal Engine, a robotics service or another domain package. The [branch comparison](../examples/branch_comparison.py) demonstrates the explicit low-level session and checkpoint APIs. Replace the agent's action program to satisfy your declared schema. Keep initialization and transitions reproducible under the recorded inputs and supplied random generator. Do not hide external effects inside a state transition; use the operation journal and receipt reconciliation.

## Add environment operations

An environment operation is the extension seam for imperative work that does not belong in the pure `resolve` transition: controlling a game, driving an engine, reading a simulator, or invoking an environment-specific tool. Subclass `EnvironmentOperation`; do not add a new session type or a core `if` statement for each integration.

```python
from environment_harness import EnvironmentOperation, EnvironmentSpec, OperationSpec


class TeleportOperation(EnvironmentOperation):
    endpoint = "unreal"

    def __init__(self, client, *, level: str):
        self.client = client
        self.spec = OperationSpec(
            name="world.teleport",
            version="unreal-1",
            config={"level": level},
        )

    def execute(self, operation_id, request, maximum_cost_micros, *, authority):
        payload = request["payload"]
        authority(payload)  # recheck the live session fence before the effect
        location = self.client.teleport(**payload)
        return {"operation_id": operation_id, "cost_micros": 0, "location": location}

    def lookup(self, operation_id):
        return self.client.receipt(operation_id)


class UnrealEnvironment:
    def __init__(self, client):
        teleport = TeleportOperation(client, level="Arena")
        self.operations = {teleport.spec.name: teleport}
        self.spec = EnvironmentSpec(
            id="unreal-arena",
            version="1",
            implementation="my-unreal-environment@1",
            scheduling="event",
            operations=(
                OperationSpec(name=teleport.spec.name, version=teleport.spec.version),
            ),
        )
        # initialize, observe, resolve, and intervene omitted
```

`EnvironmentSpec.operations` advertises the stable names and versions the environment can supply. Each runtime instance exposes the corresponding class instances in `environment.operations`. `EnvironmentHarness` freezes their exact `OperationSpec` values into every experiment and adds their names and endpoints to the run policy automatically. The low-level API accepts the same values through `ExperimentSpec.operations` and requires the caller to set its explicit allowlist. Session creation rejects missing classes, version mismatches, or configuration that differs from the runtime instance. External writes remain off unless the environment advertises them and the application passes an explicit `RunPolicy(external_writes=True)` to `EnvironmentHarness`.

The operation name is the portable capability identity; `config` is a JSON snapshot, not a live client or credential. Keep engine handles, Java bridges, sockets and secrets on the class instance. `Operations.prepare` records intent and reserves cost before dispatch. `Operations.dispatch` finds the class through the environment mapping and supplies a fenced authority callback. Implement `lookup` when the external system can prove an ambiguous result; otherwise the operation remains blocked for explicit recovery.

For a standalone session, call `Operations.prepare` and `dispatch` while the session is running. For a grouped experiment, implement the public `SessionRunner` protocol and pass the callable as `session_runner` to `EnvironmentHarness`.

The harness calls the runner with a typed `SessionControl`, the agent instances, and the turn budget. The control carries the private session runtime and the trusted local access context internally, so a custom runner never receives, constructs, or forwards an authorization value:

```python
def run_with_inspection(control, agents, *, turns):
    result = control.advance(agents, turns=1)
    observation = control.observation("alice")
    # The control derives the participant scope; a caller never builds one.
    control.prepare_operation(
        "inspect-after-turn-1",
        participant="alice",
        endpoint="synthetic",
        operation="synthetic.inspect-total",
        payload={"total": observation["payload"]["total"]},
    )
    lease = control.lease("synthetic-inspection")
    try:
        control.dispatch_operation(lease, "inspect-after-turn-1")
    finally:
        control.release(lease)
    if turns > 1:
        result = control.advance(agents, turns=turns - 1)
    return result
```

`SessionControl` exposes exactly `id`, `status`, `advance`, `observation`, `lease`, `release`, `prepare_operation`, and `dispatch_operation`. `advance` runs the standard turn loop, so a runner interleaves environment operations without replacing experiment scheduling or persistence. The runner must return the current record — `control.advance(...)` or `control.status()`; the harness rejects stale, partial or unrelated results. The default runner, `run_session`, advances the whole budget and remains in place when this argument is omitted.

Scoring stays separate from execution. Freeze scorer IDs through `scoring_versions`, replay the authorized evidence after a session, create a `ScoreReport`, and save it with `EvidenceStore.report`. Metrics appear in Progression and Reports. A `Finding` must link to its supporting observation, action and outcome evidence; operation receipts can contribute metrics and provenance, but they do not bypass the finding evidence contract. The complete example applies the same scorer to every session in the experiment so compatible metrics can be aggregated.

Run the external example with `python examples/external_environment_experiment.py`. It starts [a dependency-free simulator process](../examples/external_simulator.py), connects over a synchronized JSON-lines client, and runs four environment sessions through the ordinary experiment API. Each external write is explicitly enabled in both environment capabilities and `RunPolicy`, fenced immediately before dispatch, and returned as a durable JSON receipt. The simulator keys effects by the harness operation ID, so a lookup can reconcile an ambiguous response without repeating the move. The process client and domain operation remain example code rather than new core SDK types.

For CLI or supplier-server discovery, package the class with a factory under the `environment_harness.environments` entry-point group:

```toml
[project.entry-points."environment_harness.environments"]
my-environment = "my_package.environment:load_environment"
```

The factory returns an environment instance. Install the package into the same Python environment as EnvironmentHarness. `environment-harness doctor` lists installed plugins. The supplier HTTP service can load a registered plugin through `environment-harness serve --environment my-environment`.

Only declare checkpoint, resume or branch support when the implementation can restore the actual state it uses. A remote session handle by itself is not a portable checkpoint. Concurrent participants must receive observations from the appropriate frozen revision, not state modified by another participant's uncommitted action.

Write independent controls for your mechanics, valid and invalid actions, privacy projections and supported interventions. The public `environment_harness.conformance.check` helper checks operation declarations against their runtime classes, then checks one transition and the advertised checkpoint path. It is a compatibility check, not evidence that a grader is valid or a hosted deployment is reliable.

Scoring is a separate implementation. Freeze scorer versions in `ExperimentSpec`, record a `ScoreReport` against an evidence cursor, and retain the distinction between deterministic checks, model judgments and human judgments. Preserve unresolved outcomes and explain what each metric establishes. [Protocol](PROTOCOL.md) and [coordinated sessions](coordinated-sessions.md) cover the complete semantics.

The conformance checker accepts explicit `events` for event-driven suppliers. Scorers should provide `metric_definitions` so comparison can distinguish meanings and units. See [conformance and score compatibility](COMPATIBILITY.md).

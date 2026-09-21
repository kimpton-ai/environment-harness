# Optional motor control

Motor execution is an optional application capability. Luna selects goals and bounded targets. Code executes the approved motor request through an injected driver and records the observation before and after it. Jev is an optional selector among candidates that code already produced. It cannot invent targets, steps, permissions or effects.

Direct deterministic execution is the default. Add a motor profile to the frozen experiment only when the environment and driver support it:

```python
from pathlib import Path
from environment_harness import MotorExecutor
from environment_harness.operations import Operations
from environment_harness.motor_contracts import MotorRequest

executor = MotorExecutor(adapter, profile, journal=Path(".local/motor-journal"))
request = MotorRequest(
    skill="move",
    target={"x": 10, "y": 64, "z": -2},
    expected={"position": {"x": 10, "y": 64, "z": -2}},
    observation_revision="obs-17",
    goal_revision=str(session.get(environment_id, worker)["revision"]),
    stop_epoch=executor.stop_epoch,
)
operations = Operations(session.store)
operations.prepare(
    environment_id, agent, "motor-17", endpoint="motor", operation="motor.execute",
    payload=request.model_dump(mode="json"), write=True,
)
receipt = operations.dispatch(session, environment_id, worker, lease, "motor-17", executor)
```

The session, principals, writer lease, adapter and profile above come from the application. The profile must be frozen in `ExperimentSpec.motor` before creating the session. `goal_revision` is the current harness session revision, while `observation_revision` comes from the native driver. A changed session revision or participant generation cancels the motor. Goal controllers must also call `stop()` when their external goal changes. Drivers must be injected. A driver implements `execute(operation, payload, *, operation_id, cancel, deadline)` and `observe()` returns a revisioned observation. The executor prepares a durable operation before an effect and records a receipt after it. Unknown effects stay blocked and are never replayed. Cancellation is cooperative and must call the native driver's stop operation before the controller claims control again.

Use the experiment's `motor` profile to freeze the adapter and mode. `motor_skills` on an environment declares the skills it can expose. A Jev profile must pin `mode="jev"` and `selector_model`; the selector endpoint must be allowlisted in the experiment policy. Jev may choose only among the bounded candidates returned by the adapter. Freeze assistance at the experiment level so replay and comparison retain the same configuration.

The motor endpoint is `motor` and the operation is `motor.execute`. An authorized agent prepares it through `Operations.prepare`; a fenced worker dispatches it with the request payload, `endpoint="motor"`, `operation="motor.execute"`, and `write=True` when the request can change the outside world. The endpoint and operation must be present in the frozen allowlist. The request payload is `MotorRequest.model_dump()`.

Motor control does not claim a measured speed, success rate or general capability. Receipts establish only the recorded operation, observations, postconditions and driver result for that run. Direct execution remains unchanged when no motor profile is configured.

Prepared successor execution is an opt-in native adapter capability. Callers must provide a `PreparedSuccessorIntent` with a candidate identity, predecessor operation ID, owner and revision metadata, and a bounded freshness window. `prepare_successor` requires the exact authorized `MotorRequest`, `MotorSelection`, and a live authority callback. The executor persists one pending intent and never applies controls during preparation. `admit_successor` rechecks the same authorization, predecessor receipt identity, ownership epoch, goal and observation revision, stop epoch, visible frame, camera and UI revisions, and native tick window. A completed or rejected admission is idempotent. Unknown preparation or admission is quarantined and can only be settled through `reconcile_successor`, which consults the native ledger without resending the input. Controller restart code should call `discard_prepared_successors()` before accepting new work. Browser and desktop adapters remain sequential and raise `UnsupportedPreparation`.


Drivers implement `observe`, `execute`, `stop` and `lookup`. Each step receives a stable operation ID, a cancellation event and a monotonic deadline. A confirmed driver receipt includes that exact `operation_id` and `status` (`completed`, `blocked` or `cancelled`). Exceptions, missing receipts and unknown outcomes retain the operation reservation. A driver must not report completion while it still holds native inputs. Deadline enforcement depends on its cooperative cancellation and stop implementation. The watchdog requests native stop even when a model call or driver call is waiting.

`MotorRequest.control_permissions` contains environment-issued, bounded control descriptors. A `MotorStep` may carry matching `controls` for intermediate movement, focus, aiming, or approach parameters, but its `target` must still equal the request's semantic target. This permits a Minecraft adapter to represent approach and interaction as one semantic mine or place step. The executor rejects controls that were not explicitly authorized and records stable `reason_code` values such as `abstention`, `invalid_candidate`, `unauthorized_control`, `target_changed`, and `outcome_unknown`. Selected candidates are revalidated by identity and immutable step sequence, so refreshes cannot reset step indexes or replay completed effects.

Use one stable journal path for each application across workers and restarts. The journal uses SQLite plus a POSIX file lock; this executor currently supports macOS and Linux. `progress(operation_id)` exposes recorded substeps. `lookup(operation_id)` returns only a durable terminal receipt. An interrupted operation without sufficient evidence stays blocked. After an explicit operator resume, `acknowledge_unknown()` stops native inputs, records a fresh observation, and permits new operation IDs. Old IDs and their cost reservations remain unknown and cannot replay. Changing journals to bypass this fence is unsafe.

`JevSelector` is opt-in and reads an explicit key or `TYPESAFE_API_KEY`. Add `motor.select` and the selector's HTTPS endpoint to the frozen allowlist. In Jev mode it is called even when an adapter produces one valid plan. The selector includes an explicit abstention choice, which causes no native effect. Missing credentials fail configuration instead of silently using deterministic execution. Adapters may supply a bounded `selection_observation(request, observation)` projection; the request and exact target remain part of the selector input. Usage and cost appear in the receipt. Its byte-based token reservation includes configurable overhead and is an estimate, not a provider-enforced spending cap. An unprovable charge preserves the reservation. Tests use injected transports and do not send paid requests.

Compare direct execution, deterministic motor execution, and the same motor with Jev using separate recorded experiment profiles. Report time, model calls, selector usage, confirmed completions, cancellations, and unknown effects. Synthetic fixture timings do not establish a Minecraft or browser speed improvement.

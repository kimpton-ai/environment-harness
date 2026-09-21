# Optional motor control

Motor execution is an optional application capability. Luna selects goals and bounded targets. Code executes the approved motor request through an injected driver and records the observation before and after it. Jev is an optional selector among candidates that code already produced. It cannot invent targets, steps, permissions or effects.

Direct deterministic execution is the default. Add a motor profile to the frozen experiment only when the environment and driver support it:

```python
from pathlib import Path
from environment_harness import MotorExecutor
from environment_harness.motor_contracts import MotorRequest

executor = MotorExecutor(adapter, profile, journal=Path(".local/motor-journal"))
request = MotorRequest(
    skill="move",
    target={"x": 10, "y": 64, "z": -2},
    expected={"position": {"x": 10, "y": 64, "z": -2}},
    observation_revision="obs-17",
    goal_revision="goal-3",
)
receipt = executor.execute(request, operation_id="motor-17")
```

The exact public constructor and adapter wiring are environment-specific. Drivers must be injected. A driver implements `execute(operation, payload, *, operation_id, cancel, deadline)` and `observe()` returns a revisioned observation. The executor prepares a durable operation before an effect and records a receipt after it. Unknown effects stay blocked and are never replayed. Cancellation is cooperative and must call the native driver's stop operation before the controller claims control again.

Use the experiment's `motor` profile to freeze the adapter and mode. `motor_skills` on an environment declares the skills it can expose. A Jev profile must pin `mode="jev"` and `selector_model`; the selector endpoint must be allowlisted in the experiment policy. Jev may choose only among the bounded candidates returned by the adapter. Freeze assistance at the experiment level so replay and comparison retain the same configuration.

The motor endpoint is `motor` and the operation is `motor.execute`. A worker prepares it through `Operations.prepare` with the request payload, `endpoint="motor"`, `operation="motor.execute"`, and `write=True` when the request can change the outside world. The endpoint and operation must be present in the frozen allowlist. The request payload is `MotorRequest.model_dump()`.

Motor control does not claim a measured speed, success rate or general capability. Receipts establish only the recorded operation, observations, postconditions and driver result for that run. Direct execution remains unchanged when no motor profile is configured.

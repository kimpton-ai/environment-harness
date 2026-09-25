"""How the companion plugs into the SDK's environment-operation contract.

The first test uses only the public local facade: an `EnvironmentHarness`, a
custom session runner, and the typed `SessionControl` it receives. The second
reaches for the private session runtime on purpose, because the property it
pins -- a queued native effect is fenced when dispatch authority expires --
has no public trigger: only a participant controller transfer bumps the
generation the SDK checks, and `SessionControl` does not expose one.
"""

from datetime import datetime, timedelta, timezone

from environment_harness import AgentSpec, EnvironmentHarness, EvidenceStore, ExperimentSpec, Scenario
from environment_harness.access import _AccessContext
from environment_harness.contracts import Capabilities, EnvironmentSpec, RunPolicy, Transition
from environment_harness.operations import Operations
from environment_harness.runtime import _SessionRuntime

from environment_harness_decisions import (
    Admission,
    AuthorityBinding,
    BoundedInvocation,
    ChoiceOption,
    CompiledCommand,
    DecisionOperation,
    DecisionPolicy,
    DecisionQuestion,
    DecisionSet,
    FakeSelector,
    InvocationLimits,
    NativeReceipt,
    Objective,
    Observation,
    Verification,
)

#: The decision operation binds itself to one environment identity, frozen into
#: its own `OperationSpec`, so it is fixed before a session exists.
ENVIRONMENT_IDENTITY = "a" * 32


class SDKControl:
    implementation = "sdk-browser.v1"

    def __init__(self):
        self.identity = ENVIRONMENT_IDENTITY
        self.revision = 0
        self.submissions = []
        self.queue_hook = lambda: None

    def observe(self, objective):
        return Observation(revision=str(self.revision), model_input={"revision": self.revision})

    def decisions(self, objective, observation):
        return DecisionSet(
            id="browser",
            observation_revision=observation.revision,
            questions=(
                DecisionQuestion(
                    id="button",
                    kind="choice",
                    prompt="button",
                    options=(ChoiceOption(id="save", label="save"),),
                ),
            ),
        )

    def compile(self, objective, observation, decisions, selection):
        return CompiledCommand(id="save", adapter_version=self.implementation, payload={"button": "save"})

    def admit(self, execution_id, command, binding):
        return Admission(execution_id=execution_id, binding=binding, accepted=True)

    def execute(self, execution_id, command, *, before_dispatch, cancel, deadline):
        self.queue_hook()
        before_dispatch()
        self.submissions.append(execution_id)
        self.revision += 1
        return NativeReceipt(execution_id=execution_id, outcome="applied")

    def verify(self, objective, before, after, receipt):
        return Verification(status="completed", reason="saved")

    def stop(self):
        pass

    def lookup(self, execution_id):
        return None


def decision_operation(control, journal):
    return DecisionOperation(
        control,
        FakeSelector(),
        journal=journal,
        policy=DecisionPolicy(
            profile="fixture",
            selector_model="fixture.v1",
            endpoint="fixture",
            adapter_version=control.implementation,
        ),
        endpoint="fixture",
        name="decision",
    )


def invocation_for(session_revision):
    return BoundedInvocation(
        objective=Objective(
            id="goal",
            revision="directive-7",
            authorized_scope=("control",),
            completion_conditions=("saved",),
            limits=InvocationLimits(max_steps=1, timeout_ms=5000, max_cost_micros=0),
        ),
        binding=AuthorityBinding(
            environment_id=ENVIRONMENT_IDENTITY,
            session_revision=str(session_revision),
            directive_revision="directive-7",
            observation_revision="0",
            recovery_generation=0,
            owner="agent",
            stop_epoch=0,
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=5),
    )


class DecisionAgent:
    implementation = "fixture"
    policy_version = "1"

    def act(self, observation):
        return {}


def test_public_harness_dispatches_a_bounded_decision_through_session_control(tmp_path):
    built = []

    class DecisionEnvironment:
        spec = EnvironmentSpec(
            id="decision-environment",
            version="1",
            implementation="decision-environment@1",
            scheduling="simultaneous",
            capabilities=Capabilities(),
            missing_action="noop",
        )

        def __init__(self):
            operation = decision_operation(SDKControl(), tmp_path / f"decision-{len(built)}.sqlite")
            self.operations = {operation.spec.name: operation}
            self.spec = DecisionEnvironment.spec.model_copy(
                update={"operations": (operation.spec,)},
            )
            built.append(operation)

        def initialize(self, experiment):
            return {"turn": 0}

        def observe(self, state, participant):
            return state

        def resolve(self, state, actions, random, events):
            return Transition(state={**state, "turn": state["turn"] + 1})

    def run_with_a_decision(control, agents, *, turns):
        result = control.advance(agents, turns=1)
        control.prepare_operation(
            "decision-1",
            participant="agent",
            endpoint="fixture",
            operation="decision",
            payload=invocation_for(control.status()["revision"]).model_dump(mode="json"),
        )
        lease = control.lease("decision-runner")
        try:
            control.dispatch_operation(lease, "decision-1")
        finally:
            control.release(lease)
        return control.advance(agents, turns=turns - 1) if turns > 1 else result

    harness = EnvironmentHarness(
        tmp_path / "evidence",
        environment=DecisionEnvironment,
        agents={"agent": DecisionAgent},
        session_runner=run_with_a_decision,
    )

    session = harness.run(Scenario(id="decision", input={}), turns=2)

    assert session.status == "succeeded"
    # The registry builds a fresh environment per session; the last one ran it.
    operation = built[-1]
    receipt = next(event for event in session.replay() if event["kind"] == "operation.receipt")["payload"][
        "receipt"
    ]
    assert receipt["status"] == "completed"
    assert operation.control.submissions == [f"{session.id}:decision-1:execution:0"]
    manifest = session.record()["experiment"]
    assert manifest["operations"] == [operation.spec.model_dump(mode="json")]
    assert manifest["policy"]["allowed_endpoints"] == ["fixture"]


def test_sdk_dispatch_keeps_revisions_separate_and_fences_queued_native_effect(tmp_path):
    store = EvidenceStore(tmp_path)
    control = SDKControl()
    operation = decision_operation(control, tmp_path / "decision.sqlite")

    class DecisionEnvironment:
        spec = EnvironmentSpec(
            id="decision-environment",
            version="1",
            implementation="decision-environment@1",
            scheduling="simultaneous",
            capabilities=Capabilities(),
            missing_action="noop",
            operations=(operation.spec,),
        )
        operations = {operation.spec.name: operation}

        def initialize(self, experiment):
            return {"turn": 0}

        def observe(self, state, participant):
            return state

        def resolve(self, state, actions, random, events):
            return Transition(state={**state, "turn": state["turn"] + 1})

    env = DecisionEnvironment()
    session = _SessionRuntime(store, env)
    researcher = _AccessContext(tenant="t", subject="researcher", policy="trusted-local")
    spec = ExperimentSpec(
        environment=env.spec,
        participants=(AgentSpec(id="agent", implementation="fixture", policy_version="1"),),
        operations=(operation.spec,),
        policy=RunPolicy(
            max_cost_micros=10, allowed_endpoints=("fixture",), allowed_operations=("decision",)
        ),
    )
    environment = session.create(spec, researcher, environment_id=ENVIRONMENT_IDENTITY)["id"]
    lease = session.lease(environment, researcher, "worker")
    revision = session.get(environment, researcher)["revision"]
    invocation = invocation_for(revision)
    operations = Operations(store)
    operations.prepare(
        environment,
        session.participant_context(environment, researcher, "agent"),
        "decision-1",
        endpoint="fixture",
        operation="decision",
        payload=invocation.model_dump(mode="json"),
        maximum_cost_micros=0,
        write=False,
    )
    # Retiring the participant's controller mid-flight expires dispatch
    # authority, so the queued native effect must never reach the control.
    control.queue_hook = lambda: session.transfer(environment, researcher, lease, "agent", "replacement")

    receipt = operations.dispatch(session, environment, researcher, lease, "decision-1", operation)

    assert receipt["status"] in {"cancelled", "rejected"}
    assert control.submissions == []
    assert invocation.binding.session_revision != invocation.objective.revision

from datetime import datetime, timedelta, timezone

import pytest

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.contracts import Capabilities, EnvironmentSpec, RunPolicy
from environment_harness.errors import Forbidden
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.operations import Operations
from environment_harness_decisions import (
    Admission, AuthorityBinding, BoundedInvocation, ChoiceOption, CompiledCommand, DecisionOperation,
    DecisionPolicy, DecisionQuestion, DecisionSet, FakeSelector, InvocationLimits, NativeReceipt,
    Objective, Observation, Verification,
)


class SDKControl:
    implementation = "sdk-browser.v1"

    def __init__(self):
        self.identity = "pending"
        self.revision = 0
        self.submissions = []
        self.queue_hook = lambda: None

    def observe(self, objective):
        return Observation(revision=str(self.revision), model_input={"revision": self.revision})

    def decisions(self, objective, observation):
        return DecisionSet(id="browser", observation_revision=observation.revision, questions=(
            DecisionQuestion(id="button", kind="choice", prompt="button", options=(ChoiceOption(id="save", label="save"),)),
        ))

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


def test_sdk_dispatch_keeps_revisions_separate_and_fences_queued_native_effect(tmp_path):
    store = EvidenceStore(tmp_path)
    env = SyntheticEnvironment()
    control = SDKControl()
    control.identity = "a" * 32
    policy = DecisionPolicy(profile="fixture", selector_model="fixture.v1", endpoint="fixture",
                            adapter_version=control.implementation)
    operation = DecisionOperation(control, FakeSelector(), journal=tmp_path / "decision.sqlite",
                                  policy=policy, endpoint="fixture", name="decision")
    env.spec = env.spec.model_copy(update={"operations": (operation.spec,)})
    env.operations = {"decision": operation}
    researcher = Principal(tenant="t", subject="researcher", role="researcher")
    spec = ExperimentSpec(environment=env.spec, participants=(AgentSpec(id="agent", implementation="fixture", policy_version="1"),),
                          operations=(operation.spec,), policy=RunPolicy(max_cost_micros=10,
                          allowed_endpoints=("fixture",), allowed_operations=("decision",)))
    session = EnvironmentSession(store, env)
    environment = session.create(spec, researcher, environment_id=control.identity)["id"]
    agent = Principal(tenant="t", subject="agent", role="agent", environment=environment, participant="agent")
    lease = session.lease(environment, researcher, "worker")
    revision = session.get(environment, researcher)["revision"]
    invocation = BoundedInvocation(
        objective=Objective(id="goal", revision="directive-7", authorized_scope=("control",),
                            completion_conditions=("saved",), limits=InvocationLimits(max_steps=1, timeout_ms=5000, max_cost_micros=0)),
        binding=AuthorityBinding(environment_id=environment, session_revision=str(revision), directive_revision="directive-7",
                                 observation_revision="0", recovery_generation=0, owner="agent", stop_epoch=0),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=5),
    )
    operations = Operations(store)
    operations.prepare(environment, agent, "decision-1", endpoint="fixture", operation="decision",
                        payload=invocation.model_dump(mode="json"), maximum_cost_micros=0, write=False)
    control.queue_hook = lambda: session.transfer(environment, researcher, lease, "agent", "replacement")
    receipt = operations.dispatch(session, environment, researcher, lease, "decision-1", operation)
    assert receipt["status"] in {"cancelled", "rejected"}
    assert control.submissions == []
    assert invocation.binding.session_revision != invocation.objective.revision

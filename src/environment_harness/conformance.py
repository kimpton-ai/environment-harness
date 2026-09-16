"""Small reusable supplier conformance check. Does not imply live qualification."""

from .contracts import Action, Principal
from .runtime import EnvironmentSession
from .store import uid


def check(store, environment, experiment, action_factory):
    who = Principal(tenant=uid(), subject="conformance", role="researcher")
    session = EnvironmentSession(store, environment)
    environment = session.create(experiment, who)["id"]
    receipts = []
    for participant in experiment.participants:
        agent = Principal(
            tenant=who.tenant, subject=participant.id, role="agent", environment=environment, participant=participant.id
        )
        observation = session.observe(environment, agent)
        if observation["may_act"]:
            action = Action(
                operation_id=uid(),
                participant=participant.id,
                observation_id=observation["id"],
                revision=observation["revision"],
                payload=action_factory(observation),
            )
            receipt = session.submit(environment, agent, action)
            if receipt["status"] != "accepted" or session.submit(environment, agent, action) != receipt:
                raise AssertionError("action conformance failed")
            receipts.append(receipt)
    lease = session.lease(environment, who, "conformance")
    session.resolve(environment, who, lease)
    evidence = store.verify(environment, who)
    checkpoint = session.checkpoint(environment, who, lease) if environment.spec.capabilities.checkpoint else None
    session.release(environment, who, lease)
    return {
        "environment": environment,
        "actions": len(receipts),
        "evidence": evidence,
        "checkpoint": checkpoint,
        "scope": "one supplied contract transition; not a live or stress qualification",
    }

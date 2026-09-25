"""Small supplier compatibility check, not a live or stress qualification."""

from contextlib import suppress

from .access import _AccessContext, trusted_local
from .contracts import Action
from .errors import Conflict, Unsupported
from .operations import environment_operations
from .runtime import _SessionRuntime
from .store import uid


def check(store, environment, experiment, action_factory, *, events=()):
    events = tuple(events)
    operations = environment_operations(environment)
    if environment.spec.scheduling == "event" and environment.spec.phase_deadline == "wall" and not events:
        raise Unsupported("event conformance requires explicit input events")
    who = trusted_local(uid(), "conformance")
    session = _SessionRuntime(store, environment)
    environment_id = session.create(experiment, who)["id"]
    receipts = []
    lease = session.lease(environment_id, who, "conformance")
    try:
        for event in events:
            session.external_event(environment_id, who, lease, **event)
        for participant in experiment.participants:
            agent = _AccessContext(
                tenant=who.tenant,
                subject=participant.id,
                policy="participant",
                session=environment_id,
                participant=participant.id,
            )
            observation = session.observe(environment_id, agent)
            if observation["may_act"]:
                action = Action(
                    operation_id=uid(),
                    participant=participant.id,
                    observation_id=observation["id"],
                    revision=observation["revision"],
                    payload=action_factory(observation),
                )
                receipt = session.submit(environment_id, agent, action)
                if (
                    receipt["status"] != "accepted"
                    or session.submit(environment_id, agent, action) != receipt
                ):
                    raise AssertionError("action conformance failed")
                receipts.append(receipt)
        if environment.spec.phase_deadline == "coordinator":
            session.close_phase(environment_id, who, lease, revision=0)
        session.resolve(environment_id, who, lease)
        evidence = store.verify(environment_id, who)
        checkpoint = (
            session.checkpoint(environment_id, who, lease)
            if environment.spec.capabilities.checkpoint
            else None
        )
        return {
            "environment": environment_id,
            "actions": len(receipts),
            "operations": len(operations),
            "evidence": evidence,
            "checkpoint": checkpoint,
            "scope": "one supplied contract transition; not a live or stress qualification",
        }
    finally:
        with suppress(Conflict):
            session.release(environment_id, who, lease)

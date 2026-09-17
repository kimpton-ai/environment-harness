"""Durable agent dispatch and concurrent inference with fenced environment commits."""

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, suppress

from .contracts import Action, Principal
from .errors import Conflict
from .store import digest, encode, uid


@contextmanager
def writer(session, environment, researcher, owner):
    lease = session.lease(environment, researcher, owner, ttl=90)
    stop = threading.Event()
    failures = []

    def renew():
        while not stop.wait(20):
            try:
                renewed = session.renew(environment, researcher, lease, ttl=90)
                if renewed["epoch"] != lease["epoch"]:
                    raise Conflict("writer lost its lease")
            except Exception as exc:
                failures.append(exc)
                return

    heartbeat = threading.Thread(target=renew, daemon=True)
    heartbeat.start()
    try:
        yield lease, failures
    finally:
        stop.set()
        heartbeat.join(timeout=2)
        with suppress(Conflict):
            session.release(environment, researcher, lease)


def _prepare(session, environment, principal, observation, lease):
    key = (environment, observation["revision"], principal.participant, principal.generation)
    with session.store.transaction() as db:
        row = session.store.environment(db, environment, principal, ("agent",))
        session._fence(row, lease)
        if row["status"] != "running":
            raise Conflict("session is not running")
        existing = db.execute(
            "SELECT * FROM agent_work WHERE environment=? AND revision=? AND participant=? AND generation=?",
            key,
        ).fetchone()
        if existing:
            if existing["status"] in ("dispatching", "unknown", "failed"):
                raise Conflict("agent work requires reconciliation; automatic redispatch is forbidden")
            return dict(existing)
        work = dict(id=uid(), observation=encode(observation), status="prepared", response=None)
        db.execute(
            "INSERT INTO agent_work VALUES (?,?,?,?,?,?,?,?,?)",
            (*key, work["id"], work["observation"], "prepared", None, None),
        )
        return work


def _invoke(session, environment, principal, observation, agent, work, lease, cancel_event=None):
    cancel_event = cancel_event or threading.Event()
    key = (environment, observation["revision"], principal.participant, principal.generation)
    with session.store.transaction() as db:
        row = session.store.environment(db, environment, principal, ("agent",))
        session._fence(row, lease)
        current = db.execute(
            "SELECT * FROM agent_work WHERE environment=? AND revision=? AND participant=? AND generation=?",
            key,
        ).fetchone()
        if (
            current is None
            or current["id"] != work["id"]
            or current["status"] != "prepared"
            or row["revision"] != observation["revision"]
            or row["status"] != "running"
            or cancel_event.is_set()
        ):
            raise Conflict("agent work already dispatched or phase changed")
        member = json.loads(row["participants"])[principal.participant]
        _validate_agent(agent, member)
        registration = next(
            p for p in json.loads(row["manifest"])["participants"] if p["id"] == principal.participant
        )
        session.store.append(
            db,
            environment,
            row["revision"],
            "agent.dispatched",
            {
                "operation_id": work["id"],
                "participant": principal.participant,
                "generation": principal.generation,
                "implementation": agent.implementation,
                "config_hash": digest(registration["config"]),
                "policy_version": member["policy_version"],
            },
            (principal.participant,),
        )
        db.execute(
            "UPDATE agent_work SET status='dispatching' "
            "WHERE environment=? AND revision=? AND participant=? AND generation=?",
            key,
        )
    try:
        if member["agent_state"] is not None:
            agent.restore(member["agent_state"])
        if cancel_event.is_set():
            raise Conflict("agent execution cancelled")
        cancellable = getattr(agent, "act_cancellable", None)
        payload = cancellable(observation, cancel_event) if callable(cancellable) else agent.act(observation)
        if not isinstance(payload, dict):
            raise ValueError("agent returned a non-object action")
        state = agent.checkpoint() if member["agent_state"] is not None else None
        response = encode(payload)
        with session.store.transaction() as db:
            row = session.store.environment(db, environment, principal, ("agent",))
            session._fence(row, lease)
            current = db.execute(
                "SELECT id,status FROM agent_work WHERE environment=? AND revision=? AND participant=? AND generation=?",
                key,
            ).fetchone()
            if (
                row["revision"] != observation["revision"]
                or row["status"] != "running"
                or current is None
                or current["id"] != work["id"]
                or current["status"] != "dispatching"
                or cancel_event.is_set()
            ):
                raise Conflict("agent response no longer owns its dispatch")
            members = json.loads(row["participants"])
            members[principal.participant]["agent_state"] = state
            limit = json.loads(row["manifest"])["policy"]["max_state_bytes"]
            if len(response) > limit or len(encode(members)) > limit:
                raise Conflict("agent response or continuation exceeds state limit")
            db.execute("UPDATE environments SET participants=? WHERE id=?", (encode(members), environment))
            db.execute(
                "UPDATE agent_work SET status='responded',response=?,agent_state=? "
                "WHERE environment=? AND revision=? AND participant=? AND generation=? AND id=? AND status='dispatching'",
                (response, encode(state), *key, work["id"]),
            )
        return response
    except BaseException:
        with session.store.transaction() as db:
            session.store._environment_row(db, environment)
            db.execute(
                "UPDATE agent_work SET status='unknown' "
                "WHERE environment=? AND revision=? AND participant=? AND generation=? AND id=? AND status='dispatching'",
                (*key, work["id"]),
            )
        raise


def _validate_agent(agent, member):
    if getattr(agent, "implementation", None) != member["implementation"]:
        raise Conflict("agent implementation does not match frozen experiment")
    if not callable(getattr(agent, "act", None)):
        raise Conflict("agent requires an act method")
    if member["agent_state"] is not None and any(
        not callable(getattr(agent, hook, None)) for hook in ("checkpoint", "restore")
    ):
        raise Conflict("checkpointable agent requires checkpoint and restore hooks")


@contextmanager
def phase_guard(session, environment, researcher, lease, failures, signals, deadline):
    stop = threading.Event()

    def monitor():
        while not stop.wait(0.2):
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "agent phase deadline exceeded; unresolved work requires reconciliation"
                    )
                with session.store.transaction() as db:
                    row = session.store.environment(db, environment, researcher)
                    session._fence(row, lease)
                    if row["status"] != "running":
                        raise Conflict("environment stopped during agent work")
            except Exception as exc:
                failures.append(exc)
                for signal in signals.values():
                    signal.set()
                return

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        for signal in signals.values():
            signal.set()
        thread.join(timeout=2)


def run(session, environment, researcher, agents, *, turns=10, owner=None, phase_timeout=300):
    if not 0 < phase_timeout <= 86400:
        raise ValueError("phase_timeout must be between zero and one day")
    owner = owner or uid()
    for _ in range(turns):
        status = session.get(environment, researcher)
        if status["status"] != "running":
            break
        with writer(session, environment, researcher, owner) as (lease, failures):
            with session.store.transaction() as db:
                row = session.store.environment(db, environment, researcher)
                members = json.loads(row["participants"])
                accepted = {
                    r["participant"]
                    for r in db.execute(
                        "SELECT participant FROM actions WHERE environment=? AND revision=? AND status='accepted'",
                        (environment, row["revision"]),
                    )
                }
            signals = {p: threading.Event() for p in members}
            deadline = time.monotonic() + phase_timeout
            with phase_guard(session, environment, researcher, lease, failures, signals, deadline):
                pool = ThreadPoolExecutor(max_workers=min(32, len(members)))
                pending = {}
                try:
                    dispatches = []
                    # Validate the entire dispatch set before invoking any program.
                    for participant, member in members.items():
                        if not member["active"] or participant in accepted:
                            continue
                        principal = Principal(
                            tenant=researcher.tenant,
                            subject=member["controller"],
                            role="agent",
                            environment=environment,
                            participant=participant,
                            generation=member["generation"],
                        )
                        observation = session.observe(environment, principal)
                        if not observation["may_act"]:
                            continue
                        work = _prepare(session, environment, principal, observation, lease)
                        if work["response"] is None:
                            _validate_agent(agents.get(participant), member)
                        dispatches.append((principal, observation, work))
                    for principal, observation, work in dispatches:
                        if failures:
                            raise failures[0]
                        if work["response"] is not None:
                            future = pool.submit(lambda response: response, work["response"])
                        else:
                            future = pool.submit(
                                _invoke,
                                session,
                                environment,
                                principal,
                                observation,
                                agents[principal.participant],
                                work,
                                lease,
                                signals[principal.participant],
                            )
                        pending[future] = (principal, observation, work)
                    while pending:
                        if failures:
                            raise failures[0]
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "agent phase deadline exceeded; unresolved work requires reconciliation"
                            )
                        if session.get(environment, researcher)["status"] != "running":
                            raise Conflict("environment stopped during agent work")
                        done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "agent phase deadline exceeded; unresolved work requires reconciliation"
                            )
                        for future in done:
                            principal, observation, work = pending.pop(future)
                            payload = json.loads(future.result())
                            receipt = session.submit(
                                environment,
                                principal,
                                Action(
                                    operation_id=work["id"],
                                    participant=principal.participant,
                                    observation_id=observation["id"],
                                    revision=observation["revision"],
                                    payload=payload,
                                ),
                            )
                            if receipt["status"] not in ("accepted", "committed"):
                                raise Conflict(receipt["reason"])
                    if failures:
                        raise failures[0]
                    if session.environment.spec.phase_deadline == "coordinator":
                        session.close_phase(environment, researcher, lease, revision=status["revision"])
                    session.resolve(environment, researcher, lease)
                finally:
                    for signal in signals.values():
                        signal.set()
                    # Command agents finish bounded cleanup; opaque Python/remote work stays unresolved.
                    managed = [
                        f
                        for f, (p, _, _) in pending.items()
                        if getattr(agents.get(p.participant), "managed_cancellation", False)
                    ]
                    if managed:
                        wait(managed, timeout=3)
                    pool.shutdown(wait=False, cancel_futures=True)
    return session.get(environment, researcher)

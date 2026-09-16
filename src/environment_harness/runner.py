"""Durable agent dispatch and concurrent inference with fenced environment commits."""

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, suppress

from .contracts import Action, Principal
from .errors import Conflict
from .store import encode, uid


@contextmanager
def writer(session, environment, researcher, owner):
    lease = session.lease(environment, researcher, owner, ttl=90)
    stop = threading.Event()
    failures = []

    def renew():
        while not stop.wait(20):
            try:
                renewed = session.lease(environment, researcher, owner, ttl=90)
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
        existing = db.execute(
            "SELECT * FROM agent_work WHERE environment=? AND revision=? AND participant=? AND generation=?", key
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


def _invoke(session, environment, principal, observation, agent, work, lease):
    key = (environment, observation["revision"], principal.participant, principal.generation)
    with session.store.transaction() as db:
        row = session.store.environment(db, environment, principal, ("agent",))
        session._fence(row, lease)
        current = db.execute(
            "SELECT * FROM agent_work WHERE environment=? AND revision=? AND participant=? AND generation=?", key
        ).fetchone()
        if current["status"] != "prepared" or row["revision"] != observation["revision"]:
            raise Conflict("agent work already dispatched or phase changed")
        member = json.loads(row["participants"])[principal.participant]
        db.execute(
            "UPDATE agent_work SET status='dispatching' "
            "WHERE environment=? AND revision=? AND participant=? AND generation=?",
            key,
        )
    try:
        if member["agent_state"] is not None:
            agent.restore(member["agent_state"])
        payload = agent.act(observation)
        if not isinstance(payload, dict):
            raise ValueError("agent returned a non-object action")
        state = agent.checkpoint() if member["agent_state"] is not None else None
        response = encode(payload)
        with session.store.transaction() as db:
            row = session.store.environment(db, environment, principal, ("agent",))
            if row["revision"] != observation["revision"]:
                raise Conflict("agent response belongs to an expired phase")
            members = json.loads(row["participants"])
            members[principal.participant]["agent_state"] = state
            limit = json.loads(row["manifest"])["policy"]["max_state_bytes"]
            if len(response) > limit or len(encode(members)) > limit:
                raise Conflict("agent response or continuation exceeds state limit")
            db.execute("UPDATE environments SET participants=? WHERE id=?", (encode(members), environment))
            db.execute(
                "UPDATE agent_work SET status='responded',response=?,agent_state=? "
                "WHERE environment=? AND revision=? AND participant=? AND generation=?",
                (response, encode(state), *key),
            )
        return response
    except BaseException:
        with session.store.transaction() as db:
            db.execute(
                "UPDATE agent_work SET status='unknown' "
                "WHERE environment=? AND revision=? AND participant=? AND generation=? AND status='dispatching'",
                key,
            )
        raise


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
            pool = ThreadPoolExecutor(max_workers=min(32, len(members)))
            pending = {}
            try:
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
                    if work["response"] is not None:
                        future = pool.submit(lambda response: response, work["response"])
                    else:
                        future = pool.submit(
                            _invoke, session, environment, principal, observation, agents[participant], work, lease
                        )
                    pending[future] = (principal, observation, work)
                deadline = time.monotonic() + phase_timeout
                while pending:
                    if failures:
                        raise failures[0]
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "agent phase deadline exceeded; unresolved work requires reconciliation"
                        )
                    if session.get(environment, researcher)["status"] != "running":
                        raise Conflict("environment stopped during agent work")
                    done, _ = wait(
                        pending,
                        timeout=min(1, max(0, deadline - time.monotonic())),
                        return_when=FIRST_COMPLETED,
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
                # Python threads are cooperative. Hosted programs require bounded process backends.
                pool.shutdown(wait=False, cancel_futures=True)
    return session.get(environment, researcher)

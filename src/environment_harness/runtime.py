"""Single-writer environment execution with durable actions and explicit recovery."""

from __future__ import annotations

import json
import random
import time
from contextlib import suppress

from jsonschema import Draft202012Validator

from .contracts import Action, ExperimentSpec, Transition
from .errors import Conflict, Forbidden, Unsupported
from .history import inherit
from .operations import environment_operations
from .store import EvidenceStore, digest, encode, uid


def tuples(value):
    return tuple(tuples(x) for x in value) if isinstance(value, list) else value


class _SessionRuntime:
    """Private authorization-aware runtime. Not exported by the package.

    Every operation that can observe or mutate session state requires an
    ``_AccessContext``. ``EnvironmentHarness`` creates a trusted local context,
    HTTP authentication creates policy-constrained contexts, and the runner
    creates participant-scoped contexts, so local, HTTP, and runner paths cannot
    drift into separate authorization implementations.
    """

    def __init__(self, store: EvidenceStore, environment):
        self.store = store
        self.environment = environment
        self.action_validator = Draft202012Validator(environment.spec.action_schema)
        self.observation_validator = Draft202012Validator(environment.spec.observation_schema)

    def _compatible(self, row):
        spec = ExperimentSpec.model_validate_json(row["manifest"])
        if spec.environment != self.environment.spec:
            raise Conflict("environment version changed; explicit migration required")
        return spec

    def create(self, experiment: ExperimentSpec, access, *, environment_id=None):
        access.require("session.create")
        if access.session is not None:
            raise Forbidden("session-scoped credentials cannot create sessions")
        if experiment.environment != self.environment.spec:
            raise Conflict("environment contract mismatch")
        runtime_operations = environment_operations(self.environment)
        for selected in experiment.operations:
            operation = runtime_operations.get(selected.name)
            if operation is None or operation.spec != selected:
                raise Conflict(
                    f"environment operation {selected.name!r} has no matching runtime implementation"
                )
        environment = environment_id or uid()
        if len(environment) != 32 or any(c not in "0123456789abcdef" for c in environment):
            raise ValueError("environment ID must be 32 lowercase hex characters")
        manifest = experiment.model_dump(mode="json")
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()
            if existing:
                self.store.environment(db, environment, access, "session.read")
                if existing["manifest"] != encode(manifest):
                    raise Conflict("session id reused with different experiment")
                return self._public(existing)
        state = self.environment.initialize(experiment)
        if len(encode(state)) > experiment.policy.max_state_bytes:
            raise Conflict("state limit exceeded")
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()
            if existing:
                self.store.environment(db, environment, access, "session.read")
                if existing["manifest"] != encode(manifest):
                    raise Conflict("session id reused with different experiment")
                return self._public(existing)
            split_key = (
                access.tenant,
                experiment.environment.id,
                experiment.scenario,
                experiment.time_boundary,
            )
            split = db.execute(
                "SELECT split FROM splits WHERE tenant=? AND environment=? AND scenario=? AND time_boundary=?",
                split_key,
            ).fetchone()
            if split and split["split"] != experiment.split:
                raise Conflict("scenario and time boundary already assigned to another split")
            db.execute(
                "INSERT INTO splits VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", (*split_key, experiment.split)
            )
            participants = {
                p.id: dict(
                    active=True,
                    controller=p.id,
                    generation=0,
                    memory={},
                    agent_state={} if p.checkpoint else None,
                    implementation=p.implementation,
                    policy_version=p.policy_version,
                )
                for p in experiment.participants
            }
            scheduler = {
                "actor": experiment.participants[0].id,
                "deadline": time.time() + experiment.environment.phase_seconds,
                "pending_events": [],
                "phase": 0,
            }
            db.execute(
                """INSERT INTO environments (id,tenant,manifest,state,scheduler,rng,participants,cursors,status,lineage)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    environment,
                    access.tenant,
                    encode(manifest),
                    encode(state),
                    encode(scheduler),
                    encode(random.Random(experiment.seed).getstate()),
                    encode(participants),
                    "{}",
                    "running",
                    environment,
                ),
            )
            self.store.append(
                db,
                environment,
                0,
                "session.created",
                {"experiment": manifest, "manifest_hash": digest(manifest)},
            )
            self._register_session_row(db, environment, access, experiment, manifest)
            return self._public(
                db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()
            )

    def _register_session_row(self, db, environment, access, experiment, manifest):
        """Give every created session a durable Session row.

        The local scheduler inserts its own row before execution; a session
        created directly through the runtime or the implicit
        experiment-of-one gets one here so every Session is addressable and
        recoverable through the same projection.
        """

        if db.execute("SELECT 1 FROM session_runs WHERE environment=?", (environment,)).fetchone():
            return
        now = time.time()
        scenario = {
            "id": experiment.scenario,
            "input": manifest["scenario_input"],
            "reference": manifest["scenario_reference"],
            "metadata": manifest["scenario_metadata"],
        }
        db.execute(
            "INSERT INTO session_runs (environment,tenant,experiment,scenario,trial,seed,status,error,"
            "turns,target_turns,latest_activity,scenario_body,created,updated,environment_id,"
            "environment_version,spec_digest,blocked_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                environment,
                access.tenant,
                None,
                experiment.scenario,
                0,
                experiment.seed,
                "running",
                None,
                0,
                experiment.policy.max_turns,
                "Started",
                encode(scenario),
                now,
                now,
                manifest["environment"]["id"],
                manifest["environment"]["version"],
                digest(manifest["environment"]),
                None,
            ),
        )

    def _public(self, row):
        manifest = json.loads(row["manifest"])
        return {
            "id": row["id"],
            "revision": row["revision"],
            "status": row["status"],
            "environment": manifest["environment"],
            "participants": list(json.loads(row["participants"])),
            "lineage": row["lineage"],
            "parent": row["parent"],
            "checkpoint": row["checkpoint"],
            "spent_micros": row["spent"],
            "reserved_micros": row["reserved"],
        }

    def list_page(self, access, limit=100, cursor=None):
        access.require("session.read")
        if not 1 <= limit <= 1000:
            raise ValueError("invalid environment-session page size")
        if cursor is not None and (
            len(cursor) != 32 or any(character not in "0123456789abcdef" for character in cursor)
        ):
            raise ValueError("invalid environment-session cursor")
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM environments WHERE tenant=? AND (CAST(? AS TEXT) IS NULL OR id=?) "
                "AND (CAST(? AS TEXT) IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                (access.tenant, access.session, access.session, cursor, cursor, limit + 1),
            ).fetchall()
            page = [self._public(row) for row in rows[:limit]]
            return page, page[-1]["id"] if len(rows) > limit else None

    def list(self, access, limit=100):
        return self.list_page(access, limit)[0]

    def get(self, environment, access):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.read")
            result = self._public(row)
            if access.full_evidence:
                result["experiment"] = json.loads(row["manifest"])
                result["scheduler"] = json.loads(row["scheduler"])
            return result

    def lease(self, environment, access, owner, ttl=30):
        if not owner or not 0 < ttl <= 300:
            raise ValueError("invalid lease")
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
            now = time.time()
            if row["lease_until"] > now and row["lease_owner"] != owner:
                raise Conflict("environment has an active writer")
            epoch = row["lease_epoch"] + (row["lease_until"] <= now or row["lease_owner"] != owner)
            db.execute(
                "UPDATE environments SET lease_owner=?,lease_epoch=?,lease_until=? WHERE id=?",
                (owner, epoch, now + ttl, environment),
            )
            return {"owner": owner, "epoch": epoch, "expires": now + ttl}

    def release(self, environment, access, lease):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
            self._fence(row, lease)
            db.execute("UPDATE environments SET lease_until=0 WHERE id=?", (environment,))
        return {"released": True}

    def renew(self, environment, access, lease, ttl=90):
        """Extend an existing running writer, never reacquire an expired lease."""
        if not 0 < ttl <= 300:
            raise ValueError("invalid lease")
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
            self._fence(row, lease)
            if row["status"] != "running":
                raise Conflict("session is not running")
            expires = time.time() + ttl
            db.execute("UPDATE environments SET lease_until=? WHERE id=?", (expires, environment))
            return {"owner": lease["owner"], "epoch": lease["epoch"], "expires": expires}

    def _fence(self, row, lease):
        if (
            not lease
            or row["lease_owner"] != lease["owner"]
            or row["lease_epoch"] != lease["epoch"]
            or row["lease_until"] <= time.time()
        ):
            raise Conflict("writer lease expired or fenced")

    def participant_context(self, environment, access, participant):
        """Derive the participant-scoped context used to dispatch one actor.

        Session runners and environment extensions call this instead of building
        an access context themselves, so participant isolation has exactly one
        implementation.
        """

        from .access import _AccessContext

        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.read")
            member = json.loads(row["participants"]).get(participant)
            if not member or not member["active"]:
                raise Forbidden("inactive participant")
        return _AccessContext(
            tenant=access.tenant,
            subject=member["controller"],
            policy="participant",
            session=environment,
            participant=participant,
            generation=member["generation"],
        )

    def observe(self, environment, access, participant=None):
        participant = participant or access.participant
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "observation.read")
            self._compatible(row)
            if access.participant is not None and participant != access.participant:
                raise Forbidden("private observation")
            participants = json.loads(row["participants"])
            if participant not in participants or not participants[participant]["active"]:
                raise Forbidden("inactive participant")
            generation = participants[participant]["generation"]
            previous = db.execute(
                "SELECT * FROM observations WHERE environment=? AND participant=? AND generation=? AND revision=?",
                (environment, participant, generation, row["revision"]),
            ).fetchone()
            if previous:
                return json.loads(previous["body"])
            snapshot = dict(row)
        payload = self.environment.observe(json.loads(snapshot["state"]), participant)
        self.observation_validator.validate(payload)
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "observation.read")
            if any(
                row[key] != snapshot[key]
                for key in ("revision", "participants", "state", "scheduler", "status")
            ):
                raise Conflict("observation inputs changed during computation")
            previous = db.execute(
                "SELECT * FROM observations WHERE environment=? AND participant=? AND generation=? AND revision=?",
                (environment, participant, generation, row["revision"]),
            ).fetchone()
            if previous:
                return json.loads(previous["body"])
            scheduler = json.loads(row["scheduler"])
            spec = self.environment.spec
            result = {
                "id": uid(),
                "environment": environment,
                "participant": participant,
                "generation": generation,
                "revision": row["revision"],
                "payload": payload,
                "memory": participants[participant]["memory"],
                "may_act": row["status"] == "running"
                and (spec.scheduling != "sequential" or scheduler["actor"] == participant),
                "deadline": scheduler["deadline"],
            }
            self.store.append(
                db, environment, row["revision"], "observation.delivered", result, (participant,)
            )
            db.execute(
                "INSERT INTO observations VALUES (?,?,?,?,?,?)",
                (result["id"], environment, participant, generation, row["revision"], encode(result)),
            )
            return result

    def submit(self, environment, access, action: Action):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "participant.act")
            self._compatible(row)
            if access.participant != action.participant:
                raise Forbidden("cannot act for another participant")
            request = encode(action.model_dump(mode="json"))
            previous = db.execute(
                "SELECT * FROM actions WHERE environment=? AND id=?", (environment, action.operation_id)
            ).fetchone()
            if previous:
                if previous["request"] != request or previous["participant"] != access.participant:
                    raise Conflict("operation identifier reused")
                return json.loads(previous["receipt"])
            reason = None
            category = "model"
            if row["status"] != "running":
                reason = "session_not_running"
            elif row["revision"] != action.revision:
                reason = "stale_observation"
            observation = db.execute(
                "SELECT * FROM observations WHERE id=? AND environment=? AND participant=? AND generation=? AND revision=?",
                (action.observation_id, environment, access.participant, access.generation, action.revision),
            ).fetchone()
            if not observation:
                raise Forbidden("action does not identify a delivered observation")
            scheduler = json.loads(row["scheduler"])
            if self.environment.spec.scheduling == "sequential" and scheduler["actor"] != access.participant:
                reason = "not_selected_actor"
            if scheduler.get("closed") or (
                self.environment.spec.phase_deadline == "wall" and scheduler["deadline"] <= time.time()
            ):
                reason = "deadline_elapsed"
            if list(self.action_validator.iter_errors(action.payload)):
                reason, category = "invalid_action_schema", "malformed"
            duplicate = db.execute(
                "SELECT 1 FROM actions WHERE environment=? AND revision=? AND participant=? AND status IN ('accepted','committed')",
                (environment, action.revision, access.participant),
            ).fetchone()
            if duplicate:
                reason = "decision_already_submitted"
            receipt = {
                "operation_id": action.operation_id,
                "status": "blocked" if reason else "accepted",
                "reason": reason,
                "revision": row["revision"],
            }
            self.store.append(
                db,
                environment,
                row["revision"],
                "action.attempted",
                {"action": action.model_dump(mode="json"), "receipt": receipt, "category": category},
                (access.participant,),
            )
            db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?)",
                (
                    environment,
                    action.operation_id,
                    access.participant,
                    action.revision,
                    request,
                    receipt["status"],
                    encode(receipt),
                ),
            )
            return receipt

    def resolve(self, environment, access, lease):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
            self._fence(row, lease)
            spec = self._compatible(row)
            if row["status"] != "running":
                raise Conflict("session is not running")
            scheduler = json.loads(row["scheduler"])
            participants = json.loads(row["participants"])
            active = sorted(p for p, value in participants.items() if value["active"])
            required = [scheduler["actor"]] if spec.environment.scheduling == "sequential" else active
            submitted = db.execute(
                "SELECT * FROM actions WHERE environment=? AND revision=? AND status='accepted' ORDER BY participant",
                (environment, row["revision"]),
            ).fetchall()
            actions = {a["participant"]: json.loads(a["request"])["payload"] for a in submitted}
            expired = (
                scheduler.get("closed", False)
                if spec.environment.phase_deadline == "coordinator"
                else time.time() >= scheduler["deadline"]
            )
            if spec.environment.phase_deadline == "coordinator" and not expired:
                raise Conflict("coordinator must close phase")
            if spec.environment.scheduling == "event" and not (expired or scheduler["pending_events"]):
                raise Conflict("event phase awaits an event or deadline")
            missing = set(required) - actions.keys()
            if missing and (not expired or spec.environment.missing_action == "reject"):
                raise Conflict("phase awaits required decisions")
            decisions = {p: actions.get(p) for p in sorted(required)}
            request = {
                "state": row["state"],
                "rng": row["rng"],
                "scheduler": row["scheduler"],
                "participants": row["participants"],
                "decisions": decisions,
            }
            input_hash = digest(request)
            phase = row["revision"]
            intent = db.execute(
                "SELECT * FROM transitions WHERE environment=? AND revision=? AND input_hash=?",
                (environment, phase, input_hash),
            ).fetchone()
            if intent and intent["status"] == "computing" and intent["lease_epoch"] == lease["epoch"]:
                raise Conflict("transition computation already in progress")
            cached = intent if intent and intent["status"] == "computed" else None
            if not intent:
                db.execute(
                    "INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?)",
                    (
                        environment,
                        phase,
                        input_hash,
                        encode(request),
                        lease["epoch"],
                        "computing",
                        None,
                        None,
                    ),
                )
            elif not cached:
                db.execute(
                    "UPDATE transitions SET lease_epoch=?,status='computing' "
                    "WHERE environment=? AND revision=? AND input_hash=?",
                    (lease["epoch"], environment, phase, input_hash),
                )
        # Pure environment computation never holds a database transaction or environment lock.
        rng = random.Random()
        rng.setstate(tuples(json.loads(cached["rng"] if cached else request["rng"])))
        try:
            result = (
                Transition.model_validate_json(cached["result"])
                if cached
                else Transition.model_validate(
                    self.environment.resolve(
                        json.loads(request["state"]), decisions, rng, scheduler["pending_events"]
                    )
                )
            )
            if len(encode(result.state)) > spec.policy.max_state_bytes:
                raise Conflict("state limit exceeded")
        except BaseException:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE transitions SET status='failed' WHERE environment=? AND revision=? "
                    "AND input_hash=? AND lease_epoch=? AND status='computing'",
                    (environment, phase, input_hash, lease["epoch"]),
                )
            raise
        with self.store.transaction() as db:
            current = self.store.environment(db, environment, access, "session.write")
            self._fence(current, lease)
            if (
                current["revision"] != phase
                or current["status"] != "running"
                or any(current[key] != request[key] for key in ("state", "rng", "scheduler", "participants"))
            ):
                raise Conflict("transition inputs changed during computation")
            db.execute(
                "UPDATE transitions SET status='computed',result=?,rng=? "
                "WHERE environment=? AND revision=? AND input_hash=?",
                (result.model_dump_json(), encode(rng.getstate()), environment, phase, input_hash),
            )
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
            self._fence(row, lease)
            if (
                row["revision"] != phase
                or row["status"] != "running"
                or any(row[key] != request[key] for key in ("state", "rng", "scheduler", "participants"))
            ):
                raise Conflict("transition inputs changed before commit")
            revision = row["revision"] + 1
            terminated, truncated = result.terminated, result.truncated or revision >= spec.policy.max_turns
            status = (
                "outcomes_pending"
                if result.pending_outcomes and (terminated or truncated)
                else "completed"
                if terminated or truncated
                else "running"
            )
            outcome = self.store.append(
                db,
                environment,
                revision,
                "transition.committed",
                {
                    "participants": sorted(decisions),
                    "missing": sorted(missing),
                    "state_hash": digest(result.state),
                    "terminated": terminated,
                    "truncated": truncated,
                    "reason": result.reason or ("turn_budget" if revision >= spec.policy.max_turns else None),
                    "pending_outcomes": result.pending_outcomes,
                },
            )
            for action in submitted:
                participant = action["participant"]
                receipt = {
                    "operation_id": action["id"],
                    "status": "committed",
                    "revision": revision,
                    "event": outcome["seq"],
                }
                db.execute(
                    "UPDATE actions SET status='committed',receipt=? WHERE environment=? AND id=?",
                    (encode(receipt), environment, action["id"]),
                )
                self.store.append(
                    db,
                    environment,
                    revision,
                    "action.executed",
                    {
                        "action_id": action["id"],
                        "participant": participant,
                        "observation_id": json.loads(action["request"])["observation_id"],
                        "outcome": result.outcomes.get(participant, {}),
                        "reward": result.rewards.get(participant),
                        "policy_version": participants[participant]["policy_version"],
                        "terminated": terminated,
                        "truncated": truncated,
                        "reason": result.reason
                        or ("turn_budget" if revision >= spec.policy.max_turns else None),
                    },
                    (participant,),
                )
            for event in result.events:
                if any(p != "*" and p not in participants for p in event.audience):
                    raise Conflict("environment event has unknown audience")
                self.store.append(
                    db, environment, revision, event.kind, event.payload, event.audience, event.event_time
                )
            next_actor = result.next_actor or (
                active[(active.index(scheduler["actor"]) + 1) % len(active)] if active else None
            )
            if next_actor not in active:
                raise Conflict("invalid next actor")
            scheduler = {
                "actor": next_actor,
                "deadline": time.time() + spec.environment.phase_seconds,
                "pending_events": [],
                "phase": revision,
            }
            db.execute(
                "UPDATE environments SET state=?,revision=?,rng=?,scheduler=?,status=? WHERE id=?",
                (
                    encode(result.state),
                    revision,
                    encode(rng.getstate()),
                    encode(scheduler),
                    status,
                    environment,
                ),
            )
            db.execute(
                "UPDATE transitions SET status='committed' WHERE environment=? AND revision=? AND input_hash=?",
                (environment, phase, input_hash),
            )
            self._sync_session_row(db, environment, revision, status)
            self._fence(db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone(), lease)
            return {"revision": revision, "status": status, "event": outcome["seq"]}

    def _sync_session_row(self, db, environment, revision, status):
        """Keep the durable Session row's turn count and terminal state current.

        The local scheduler owns its own status transitions, so only a session
        this runtime drove to completion is marked terminal here.
        """

        db.execute(
            "UPDATE session_runs SET turns=?,updated=? WHERE environment=?",
            (revision, time.time(), environment),
        )
        if status == "completed":
            db.execute(
                "UPDATE session_runs SET status='succeeded',latest_activity='Completed',updated=? "
                "WHERE environment=? AND status='running'",
                (time.time(), environment),
            )

    def close_phase(self, environment, access, lease, *, revision, reason="decisions_complete"):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
            self._fence(row, lease)
            if self._compatible(row).environment.phase_deadline != "coordinator":
                raise Unsupported("environment uses wall-clock deadlines")
            if row["status"] != "running" or row["revision"] != revision:
                raise Conflict("phase no longer active")
            scheduler = json.loads(row["scheduler"])
            if not scheduler.get("closed"):
                scheduler["closed"] = True
                db.execute("UPDATE environments SET scheduler=? WHERE id=?", (encode(scheduler), environment))
                self.store.append(db, environment, revision, "phase.closed", {"reason": reason})
            return {"revision": revision, "closed": True}

    def external_event(self, environment, access, lease, *, source, cursor, event_time, payload, gap=False):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
            self._fence(row, lease)
            cursors = json.loads(row["cursors"])
            if source in cursors and cursor <= cursors[source]:
                raise Conflict("external event cursor must increase")
            scheduler = json.loads(row["scheduler"])
            event = {"source": source, "cursor": cursor, "payload": payload, "gap": gap}
            scheduler["pending_events"].append(event)
            if len(encode(scheduler)) > json.loads(row["manifest"])["policy"]["max_state_bytes"]:
                raise Conflict("pending event queue limit exceeded")
            cursors[source] = cursor
            db.execute(
                "UPDATE environments SET cursors=?,scheduler=? WHERE id=?",
                (encode(cursors), encode(scheduler), environment),
            )
            return self.store.append(
                db, environment, row["revision"], "feed.ingested", event, event_time=event_time
            )

    def memory(self, environment, access, memory, agent_state=None, expected_revision=None):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "participant.memory.write")
            if row["status"] != "running":
                raise Conflict("session is not running")
            if expected_revision is not None and row["revision"] != expected_revision:
                raise Conflict("agent state belongs to another revision")
            participants = json.loads(row["participants"])
            participant = participants[access.participant]
            participant["memory"] = memory
            if agent_state is not None:
                spec = self._compatible(row)
                if not next(p for p in spec.participants if p.id == access.participant).checkpoint:
                    raise Unsupported("agent has no checkpoint hook")
                participant["agent_state"] = agent_state
            if len(encode(participants)) > json.loads(row["manifest"])["policy"]["max_state_bytes"]:
                raise Conflict("participant context limit exceeded")
            db.execute(
                "UPDATE environments SET participants=? WHERE id=?", (encode(participants), environment)
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "participant.memory",
                {"participant": access.participant, "memory_hash": digest(memory)},
                (access.participant,),
            )

    def transfer(self, environment, access, lease, participant, controller, *, active=True):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.control")
            self._fence(row, lease)
            participants = json.loads(row["participants"])
            if participant not in participants:
                raise Unsupported("joining requires an environment-specific versioned participant contract")
            if db.execute(
                "SELECT 1 FROM actions WHERE environment=? AND status='accepted'", (environment,)
            ).fetchone():
                raise Conflict("authority transfer requires a decision boundary")
            if not active and sum(p["active"] for p in participants.values()) <= 1:
                raise Conflict("cannot remove last active participant")
            participants[participant].update(
                controller=controller, active=active, generation=participants[participant]["generation"] + 1
            )
            scheduler = json.loads(row["scheduler"])
            if not participants[scheduler["actor"]]["active"]:
                scheduler["actor"] = sorted(p for p in participants if participants[p]["active"])[0]
            db.execute(
                "UPDATE environments SET participants=?,scheduler=? WHERE id=?",
                (encode(participants), encode(scheduler), environment),
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "authority.transferred",
                {
                    "participant": participant,
                    "controller": controller,
                    "generation": participants[participant]["generation"],
                    "active": active,
                },
            )
            # Authority transfer records the new controller. Issuing a
            # participant credential remains a separate, purpose-specific
            # operation so transfer cannot mint remote access.
            return {
                "participant": participant,
                "controller": controller,
                "generation": participants[participant]["generation"],
                "active": active,
            }

    def reconcile_agent(
        self, environment, access, lease, *, operation_id, response, evidence, agent_state=None
    ):
        """Record an authorized recovery result without dispatching the agent again."""
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("reconciliation requires lookup or operator evidence")
        self.action_validator.validate(response)
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.control")
            self._fence(row, lease)
            work = db.execute(
                "SELECT * FROM agent_work WHERE environment=? AND id=?", (environment, operation_id)
            ).fetchone()
            if not work or work["revision"] != row["revision"]:
                raise Conflict("agent work is unavailable or belongs to an expired phase")
            if work["status"] not in ("dispatching", "unknown"):
                raise Conflict("agent work is not awaiting reconciliation")
            members = json.loads(row["participants"])
            member = members[work["participant"]]
            if member["generation"] != work["generation"] or not member["active"]:
                raise Conflict("agent controller changed")
            if member["agent_state"] is not None and agent_state is None:
                raise Unsupported("checkpointable agent requires its recovered continuation state")
            member["agent_state"] = agent_state
            limit = json.loads(row["manifest"])["policy"]["max_state_bytes"]
            if len(encode(members)) > limit or len(encode(response)) > limit:
                raise Conflict("recovered agent state exceeds limit")
            if row["status"] in ("running", "paused"):
                db.execute(
                    "UPDATE environments SET participants=? WHERE id=?", (encode(members), environment)
                )
            db.execute(
                "UPDATE agent_work SET status='responded',response=?,agent_state=? WHERE environment=? AND id=?",
                (encode(response), encode(agent_state), environment, operation_id),
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "agent.reconciled",
                {
                    "operation_id": operation_id,
                    "response_hash": digest(response),
                    "evidence": evidence,
                    "authorized_by": access.subject,
                },
                (work["participant"],),
            )
            return {"operation_id": operation_id, "status": "responded"}

    def checkpoint(self, environment, access, lease, *, exact_agents=False):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.control")
            self._fence(row, lease)
            spec = self._compatible(row)
            if not spec.environment.capabilities.checkpoint:
                raise Unsupported("environment does not support checkpoints")
            participants = json.loads(row["participants"])
            if exact_agents and any(p["agent_state"] is None for p in participants.values()):
                raise Unsupported("one or more agent programs cannot continue exactly")
            if db.execute(
                "SELECT 1 FROM operations WHERE environment=? AND status IN ('dispatching','unknown')",
                (environment,),
            ).fetchone():
                raise Conflict("ambiguous external operations must be reconciled before checkpoint")
            if db.execute(
                "SELECT 1 FROM agent_work WHERE environment=? AND "
                "(status IN ('prepared','dispatching','unknown') OR (revision=? AND status='responded'))",
                (environment, row["revision"]),
            ).fetchone():
                raise Conflict("agent work must be completed or reconciled before checkpoint")
            snapshot = {
                k: row[k]
                for k in (
                    "manifest",
                    "state",
                    "revision",
                    "scheduler",
                    "rng",
                    "participants",
                    "cursors",
                    "status",
                    "lineage",
                    "spent",
                    "reserved",
                )
            }
            snapshot["actions"] = [
                dict(a)
                for a in db.execute(
                    "SELECT * FROM actions WHERE environment=? AND status='accepted'", (environment,)
                )
            ]
            snapshot["operations"] = [
                dict(o)
                for o in db.execute(
                    "SELECT * FROM operations WHERE environment=? AND status NOT IN ('succeeded','failed')",
                    (environment,),
                )
            ]
            snapshot["exact_agents"] = exact_agents
            snapshot["evidence_cursor"] = db.execute(
                "SELECT max(seq) FROM events WHERE environment=?", (environment,)
            ).fetchone()[0]
            snapshot["artifacts"] = [
                dict(a) for a in db.execute("SELECT * FROM artifacts WHERE environment=?", (environment,))
            ]
            snapshot["artifact_aliases"] = [
                dict(a)
                for a in db.execute("SELECT * FROM artifact_aliases WHERE environment=?", (environment,))
            ]
            snapshot["evidence_head"] = db.execute(
                "SELECT hash FROM events WHERE environment=? ORDER BY seq DESC LIMIT 1", (environment,)
            ).fetchone()[0]
            key = uid()
            db.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?,?)",
                (key, environment, row["revision"], encode(snapshot), digest(snapshot)),
            )
            self.store.append(
                db,
                environment,
                row["revision"],
                "checkpoint.committed",
                {"id": key, "hash": digest(snapshot), "exact_agents": exact_agents},
            )
            return {
                "id": key,
                "revision": row["revision"],
                "hash": digest(snapshot),
                "exact_agents": exact_agents,
            }

    def resume(self, environment, access, lease, *, implementations=None):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
            self._fence(row, lease)
            spec = self._compatible(row)
            if not spec.environment.capabilities.resume:
                raise Unsupported("environment cannot resume")
            if row["status"] not in ("running", "paused"):
                raise Conflict("session cannot be resumed")
            participants = json.loads(row["participants"])
            if implementations is not None and implementations != {
                p: x["implementation"] for p, x in participants.items()
            }:
                raise Conflict("agent versions changed; explicit migration required")
            if db.execute(
                "SELECT 1 FROM operations WHERE environment=? AND status IN ('dispatching','unknown')",
                (environment,),
            ).fetchone():
                raise Conflict("reconcile ambiguous operations before resume")
            db.execute("UPDATE environments SET status='running' WHERE id=?", (environment,))
            # The deadline stays fixed. Real time did not stop while the worker was absent.
            self.store.append(db, environment, row["revision"], "session.resumed", {"epoch": lease["epoch"]})
            return self._public(
                db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()
            )

    def resume_if_paused(self, environment, access):
        """Return a recovered session to ``running`` without a public lease dance.

        Recovery never repeats a committed effect: it only clears a paused or
        interrupted execution flag so the existing durable phase can continue.
        """

        lease = self.lease(environment, access, "environment-harness-recovery", ttl=60)
        try:
            with self.store.transaction() as db:
                row = self.store.environment(db, environment, access, "session.write")
                self._fence(row, lease)
                if row["status"] == "paused":
                    db.execute("UPDATE environments SET status='running' WHERE id=?", (environment,))
                    self.store.append(
                        db, environment, row["revision"], "session.resumed", {"epoch": lease["epoch"]}
                    )
                return self._public(
                    db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()
                )
        finally:
            with suppress(Conflict):
                self.release(environment, access, lease)

    def branch(
        self, environment, access, checkpoint, interventions=None, *, new_environment=None, turns=None
    ):
        with self.store.transaction() as db:
            parent = self.store.environment(db, environment, access, "session.control")
            spec = self._compatible(parent)
            if not spec.environment.capabilities.branch:
                raise Unsupported("environment cannot implement counterfactual branches")
            saved = db.execute(
                "SELECT * FROM checkpoints WHERE environment=? AND id=?", (environment, checkpoint)
            ).fetchone()
            if not saved:
                raise Forbidden("checkpoint unavailable")
            snapshot = json.loads(saved["body"])
            if digest(snapshot) != saved["hash"]:
                raise Conflict("checkpoint integrity failure")
            if snapshot["manifest"] != parent["manifest"]:
                raise Conflict("checkpoint version mismatch")
            if snapshot["operations"] or snapshot["actions"]:
                raise Unsupported("branch requires a checkpoint without pending decisions or operations")
            if spec.policy.external_writes:
                raise Unsupported("live writes cannot be inherited by a counterfactual branch")
            child = new_environment or uid()
            if len(child) != 32 or any(c not in "0123456789abcdef" for c in child):
                raise ValueError("invalid branch ID")
            existing = db.execute("SELECT * FROM environments WHERE id=?", (child,)).fetchone()
            changes = interventions or {}
            if existing:
                self.store.environment(db, child, access, "session.control")
                if (
                    existing["parent"] != environment
                    or existing["checkpoint"] != checkpoint
                    or json.loads(existing["manifest"])["interventions"] != changes
                ):
                    raise Conflict("branch ID reused")
                return self._public(existing)
            state = self.environment.intervene(json.loads(snapshot["state"]), changes)
            manifest = json.loads(snapshot["manifest"])
            manifest["interventions"] = changes
            scheduler = json.loads(snapshot["scheduler"])
            scheduler.pop("closed", None)
            scheduler["deadline"] = time.time() + spec.environment.phase_seconds
            if len(encode(state)) > spec.policy.max_state_bytes:
                raise Conflict("state limit exceeded")
            db.execute(
                """INSERT INTO environments (id,tenant,manifest,state,revision,scheduler,rng,participants,cursors,status,lineage,parent,checkpoint)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    child,
                    parent["tenant"],
                    encode(manifest),
                    encode(state),
                    snapshot["revision"],
                    encode(scheduler),
                    snapshot["rng"],
                    snapshot["participants"],
                    snapshot["cursors"],
                    snapshot["status"],
                    parent["lineage"],
                    environment,
                    checkpoint,
                ),
            )
            if snapshot.get("evidence_cursor") is not None:
                # Preserve original evidence verbatim, inside a child-owned envelope.
                # Original artifact handles resolve only through explicit child aliases.
                import hashlib

                aliases = {}
                for artifact in snapshot.get("artifacts", []):
                    data = self.store._read_artifact(environment, artifact["id"])
                    if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                        raise Conflict("inherited artifact integrity failure")
                    key = uid()
                    self.store._write_artifact(child, key, data)
                    db.execute(
                        "INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
                        (
                            key,
                            child,
                            artifact["audience"],
                            artifact["sha256"],
                            artifact["size"],
                            artifact["media_type"],
                        ),
                    )
                    aliases[artifact["id"]] = key
                for alias in snapshot.get("artifact_aliases", []):
                    aliases[alias["alias"]] = aliases[alias["artifact"]]
                for alias, key in aliases.items():
                    db.execute("INSERT INTO artifact_aliases VALUES (?,?,?)", (child, alias, key))
                for event in db.execute(
                    "SELECT * FROM events WHERE environment=? AND seq<=? ORDER BY seq",
                    (environment, snapshot["evidence_cursor"]),
                ).fetchall():
                    inherit(self.store, db, child, snapshot["revision"], event, spec.policy.max_event_bytes)
            self.store.append(
                db,
                child,
                snapshot["revision"],
                "session.branched",
                {
                    "parent": environment,
                    "checkpoint": checkpoint,
                    "checkpoint_hash": saved["hash"],
                    "interventions": changes,
                    "lineage": parent["lineage"],
                    "split": spec.split,
                },
            )
            self._register_child_session(db, environment, child, checkpoint, snapshot, turns)
            return self._public(db.execute("SELECT * FROM environments WHERE id=?", (child,)).fetchone())

    def _register_child_session(self, db, parent, child, checkpoint, snapshot, turns):
        """Give a branched child a durable Session row of its own.

        The child starts ``interrupted`` so branching never repeats an
        externally visible effect without explicit caller intent.
        """

        origin = db.execute("SELECT * FROM session_runs WHERE environment=?", (parent,)).fetchone()
        if origin is None:
            return
        if db.execute("SELECT 1 FROM session_runs WHERE environment=?", (child,)).fetchone():
            return
        now = time.time()
        db.execute(
            "INSERT INTO session_runs (environment,tenant,experiment,scenario,trial,seed,status,error,"
            "turns,target_turns,latest_activity,scenario_body,created,updated,environment_id,"
            "environment_version,spec_digest,blocked_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                child,
                origin["tenant"],
                origin["experiment"],
                origin["scenario"],
                origin["trial"],
                origin["seed"],
                "interrupted",
                None,
                snapshot["revision"],
                turns or origin["target_turns"],
                "Branched",
                origin["scenario_body"],
                now,
                now,
                origin["environment_id"],
                origin["environment_version"],
                origin["spec_digest"],
                None,
            ),
        )
        db.execute(
            "INSERT INTO event_outbox (tenant,topic,experiment,environment,kind,body,created) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                origin["tenant"],
                "experiment" if origin["experiment"] else "environment_session",
                origin["experiment"],
                child,
                "environment_session.branched",
                encode(
                    {
                        "status": "interrupted",
                        "scenario_id": origin["scenario"],
                        "trial": origin["trial"],
                        "parent": parent,
                        "checkpoint": checkpoint,
                    }
                ),
                now,
            ),
        )

    def finalize_outcomes(self, environment, access, lease, report_revision):
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.write")
            self._fence(row, lease)
            if row["status"] != "outcomes_pending":
                raise Conflict("session has no pending outcome lifecycle")
            report = db.execute(
                "SELECT * FROM reports WHERE environment=? AND revision=?", (environment, report_revision)
            ).fetchone()
            if not report:
                raise Conflict("final outcome report is missing")
            if db.execute(
                "SELECT 1 FROM operations WHERE environment=? AND status NOT IN ('succeeded','failed')",
                (environment,),
            ).fetchone():
                raise Conflict("external operations remain unsettled")
            db.execute("UPDATE environments SET status='completed' WHERE id=?", (environment,))
            self.store.append(
                db,
                environment,
                row["revision"],
                "outcomes.finalized",
                {"report_revision": report_revision, "report_hash": report["hash"]},
            )
            return {"status": "completed", "report_revision": report_revision}

    def cancel(self, environment, access):
        """Cancel execution without acquiring its writer lease. Uncertain effects remain unsettled."""
        from .operations import Operations

        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.control")
            if row["status"] not in ("running", "paused", "cancelled"):
                raise Conflict("session already terminal")
            if row["status"] != "cancelled":
                db.execute(
                    "UPDATE environments SET status='cancelled',lease_epoch=lease_epoch+1,"
                    "lease_owner=NULL,lease_until=0 WHERE id=?",
                    (environment,),
                )
                db.execute(
                    "UPDATE agent_work SET status='failed' WHERE environment=? AND status='prepared'",
                    (environment,),
                )
                db.execute(
                    "UPDATE agent_work SET status='unknown' WHERE environment=? AND status='dispatching'",
                    (environment,),
                )
                Operations(self.store)._cancel_prepared(db, environment, row)
                self.store.append(
                    db, environment, row["revision"], "session.cancelled", {"reason": "researcher_control"}
                )
            return {
                "status": "cancelled",
                "unresolved_agent_work": [
                    r["id"]
                    for r in db.execute(
                        "SELECT id FROM agent_work WHERE environment=? AND status='unknown' ORDER BY id",
                        (environment,),
                    )
                ],
                "unresolved_operations": [
                    r["id"]
                    for r in db.execute(
                        "SELECT id FROM operations WHERE environment=? AND status IN ('dispatching','unknown') ORDER BY id",
                        (environment,),
                    )
                ],
            }

    def control(self, environment, access, lease, command):
        if command not in ("pause", "cancel"):
            raise ValueError("unknown control")
        if command == "cancel":
            return self.cancel(environment, access)
        with self.store.transaction() as db:
            row = self.store.environment(db, environment, access, "session.control")
            self._fence(row, lease)
            if row["status"] not in ("running", "paused"):
                raise Conflict("session already terminal")
            status = "paused" if command == "pause" else "cancelled"
            db.execute("UPDATE environments SET status=? WHERE id=?", (status, environment))
            self.store.append(
                db, environment, row["revision"], "session." + status, {"reason": "researcher_control"}
            )
            return {"status": status}

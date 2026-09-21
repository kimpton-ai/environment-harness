"""Typed local orchestration for standalone and grouped environment sessions."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from .contracts import AgentSpec, ExperimentSpec, Principal, RunPolicy, Scenario
from .errors import Conflict
from .runner import run as run_session
from .runtime import EnvironmentSession as RuntimeEnvironmentSession
from .store import EvidenceStore, encode, uid


def _session_seed(seed: int, scenario_id: str, trial: int) -> int:
    material = f"{seed}\0{scenario_id}\0{trial}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") & ((1 << 63) - 1)


class EnvironmentSession(RuntimeEnvironmentSession):
    """A single environment-session handle.

    Constructing this class with ``(store, environment)`` retains the advanced,
    low-level API. Handles returned by :class:`EnvironmentHarness` additionally
    expose lifecycle properties and ``wait``/``stop``/``resume``.
    """

    def __init__(
        self,
        store: EvidenceStore,
        environment: Any,
        *,
        harness: EnvironmentHarness | None = None,
        session_id: str | None = None,
    ):
        super().__init__(store, environment)
        self._harness = harness
        self._session_id = session_id

    def _run_record(self):
        if self._session_id is None:
            raise AttributeError("lifecycle properties are available on harness session handles")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM session_runs WHERE environment=?", (self._session_id,)).fetchone()
            if not row:
                raise Conflict("environment session record is unavailable")
            return dict(row)

    @property
    def id(self) -> str:
        if self._session_id is None:
            raise AttributeError("low-level sessions do not have an ID before create()")
        return self._session_id

    @property
    def status(self) -> str:
        return str(self._run_record()["status"])

    @property
    def scenario_id(self) -> str:
        return str(self._run_record()["scenario"])

    @property
    def experiment_id(self) -> str | None:
        value = self._run_record()["experiment"]
        return str(value) if value is not None else None

    @property
    def seed(self) -> int:
        return int(self._run_record()["seed"])

    @property
    def trial(self) -> int:
        return int(self._run_record()["trial"])

    def wait(self, timeout: float | None = None) -> EnvironmentSession:
        if self._harness is None:
            raise AttributeError("wait is available on harness session handles")
        self._harness._wait(self.id, timeout)
        return self

    def stop(self) -> EnvironmentSession:
        if self._harness is None:
            raise AttributeError("stop is available on harness session handles")
        self._harness._stop(self.id)
        return self

    def resume(self, *args, **kwargs):
        if args or kwargs:
            return super().resume(*args, **kwargs)
        if self._harness is None:
            raise AttributeError("resume is available on harness session handles")
        self._harness._resume(self.id)
        return self


@dataclass(frozen=True)
class ExperimentResult:
    id: str
    name: str
    status: str
    total: int
    completed: int
    running: int
    queued: int
    failed: int
    sessions: tuple[EnvironmentSession, ...]


class Experiment:
    """A frozen configuration spanning related scenarios and trials."""

    def __init__(
        self,
        harness: EnvironmentHarness,
        name: str,
        scenarios: tuple[Scenario[Any], ...],
        *,
        trials: int,
        seed: int,
        turns: int,
    ):
        self.harness = harness
        self.name = name
        self.scenarios = scenarios
        self.trials = trials
        self.seed = seed
        self.turns = turns
        self.id = uid()
        self._started = False

    def start(self) -> Experiment:
        if self._started:
            return self
        self.harness._start_experiment(self)
        self._started = True
        return self

    @property
    def sessions(self) -> tuple[EnvironmentSession, ...]:
        return self.harness._experiment_sessions(self.id)

    def wait(self, timeout: float | None = None) -> ExperimentResult:
        if not self._started:
            raise Conflict("experiment must be started before waiting")
        deadline = None if timeout is None else time.monotonic() + timeout
        for session in self.sessions:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            session.wait(remaining)
        return self.result()

    def run(self) -> ExperimentResult:
        return self.start().wait()

    def stop(self) -> ExperimentResult:
        if not self._started:
            raise Conflict("experiment must be started before stopping")
        for session in self.sessions:
            if session.status in ("queued", "running"):
                session.stop()
        return self.result()

    def resume(self) -> Experiment:
        if not self._started:
            # Reconnect to a persisted experiment when the caller retained its ID.
            with self.harness.store.transaction() as db:
                if not db.execute("SELECT 1 FROM experiments WHERE id=?", (self.id,)).fetchone():
                    raise Conflict("experiment must be started before resuming")
            self._started = True
        for session in self.sessions:
            if session.status == "interrupted":
                session.resume()
        return self

    def result(self) -> ExperimentResult:
        with self.harness.store.transaction() as db:
            row = db.execute("SELECT * FROM experiments WHERE id=?", (self.id,)).fetchone()
            if not row:
                raise Conflict("experiment is unavailable")
            record = dict(row)
        return ExperimentResult(
            id=self.id,
            name=record["name"],
            status=record["status"],
            total=record["total"],
            completed=record["completed"],
            running=record["running"],
            queued=record["queued"],
            failed=record["failed"],
            sessions=self.sessions,
        )


class EnvironmentHarness:
    """Run environment sessions locally with bounded threads and durable status."""

    def __init__(
        self,
        store: EvidenceStore | str | Path,
        *,
        environment_factory: Callable[[], Any],
        agent_factories: Mapping[str, Callable[[], Any]],
        scoring_versions: tuple[str, ...] = (),
        max_sessions: int = 1000,
        max_concurrency: int = 4,
        tenant: str = "local",
    ):
        if max_sessions < 1 or max_concurrency < 1:
            raise ValueError("session and concurrency limits must be positive")
        self.store = store if isinstance(store, EvidenceStore) else EvidenceStore(store)
        self.environment_factory = environment_factory
        self.agent_factories = dict(agent_factories)
        if not self.agent_factories:
            raise ValueError("at least one agent factory is required")
        self.scoring_versions = tuple(scoring_versions)
        self.max_sessions = max_sessions
        self.max_concurrency = max_concurrency
        self.tenant = tenant
        self.researcher = Principal(tenant=tenant, subject="environment-harness", role="researcher")
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrency, thread_name_prefix="environment-session"
        )
        self._futures: dict[str, Future[Any]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._served: dict[str, int] = {}
        self._running_jobs = 0
        self._lock = threading.RLock()

    def _validate_scenario(self, scenario: Scenario[Any]) -> Scenario[Any]:
        environment = self.environment_factory()
        declared = getattr(environment, "scenario_type", None)
        if declared is not None:
            value = TypeAdapter(declared).validate_python(scenario.input)
            scenario = scenario.model_copy(update={"input": value})
        else:
            Draft202012Validator(environment.spec.scenario_schema).validate(scenario.input)
        # Ensure factories cannot smuggle process-local values into durable snapshots.
        json.dumps(scenario.model_dump(mode="json"), allow_nan=False)
        return scenario

    def _reserve(
        self,
        scenario: Scenario[Any],
        *,
        seed: int,
        turns: int,
        experiment_id: str | None = None,
        trial: int = 0,
    ) -> EnvironmentSession:
        if turns < 1:
            raise ValueError("turns must be positive")
        scenario = self._validate_scenario(scenario)
        environment_id = uid()
        now = time.time()
        with self.store.transaction() as db:
            active = db.execute(
                "SELECT count(*) FROM session_runs WHERE tenant=? AND status IN ('queued','running')",
                (self.tenant,),
            ).fetchone()[0]
            if active >= self.max_sessions:
                raise Conflict("max_sessions limit reached")
            db.execute(
                "INSERT INTO session_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    environment_id,
                    self.tenant,
                    experiment_id,
                    scenario.id,
                    trial,
                    seed,
                    "queued",
                    None,
                    0,
                    turns,
                    "Queued",
                    encode(scenario.model_dump(mode="json")),
                    now,
                    now,
                ),
            )
            self._outbox(
                db,
                kind="environment_session.queued",
                body={"status": "queued", "scenario_id": scenario.id, "trial": trial},
                experiment=experiment_id,
                environment=environment_id,
            )
        job = {
            "id": environment_id,
            "scenario": scenario,
            "seed": seed,
            "turns": turns,
            "experiment": experiment_id,
            "trial": trial,
        }
        with self._lock:
            self._jobs[environment_id] = job
            self._schedule_locked(job)
        return EnvironmentSession(
            self.store,
            self.environment_factory(),
            harness=self,
            session_id=environment_id,
        )

    def start(self, scenario: Scenario[Any], *, seed: int = 0, turns: int = 10) -> EnvironmentSession:
        return self._reserve(scenario, seed=seed, turns=turns)

    def run(self, scenario: Scenario[Any], *, seed: int = 0, turns: int = 10) -> EnvironmentSession:
        return self.start(scenario, seed=seed, turns=turns).wait()

    def experiment(
        self,
        name: str,
        scenarios,
        *,
        trials: int = 1,
        seed: int = 0,
        turns: int = 10,
    ) -> Experiment:
        if not name.strip():
            raise ValueError("experiment name is required")
        if trials < 1 or turns < 1:
            raise ValueError("trials and turns must be positive")
        validated = tuple(self._validate_scenario(scenario) for scenario in scenarios)
        if not validated:
            raise ValueError("at least one scenario is required")
        ids = [scenario.id for scenario in validated]
        if len(ids) != len(set(ids)):
            raise ValueError("scenario IDs must be unique within an experiment")
        if len(validated) * trials > self.max_sessions:
            raise Conflict("experiment exceeds max_sessions")
        return Experiment(self, name, validated, trials=trials, seed=seed, turns=turns)

    def _start_experiment(self, experiment: Experiment) -> None:
        environment = self.environment_factory()
        preview_agents = {participant: factory() for participant, factory in self.agent_factories.items()}
        config = {
            "environment": environment.spec.model_dump(mode="json"),
            "participants": [
                self._agent_spec(name, agent).model_dump(mode="json")
                for name, agent in preview_agents.items()
            ],
            "scoring_versions": list(self.scoring_versions),
            "turns": experiment.turns,
        }
        now = time.time()
        jobs: list[dict[str, Any]] = []
        with self.store.transaction() as db:
            active = db.execute(
                "SELECT count(*) FROM session_runs WHERE tenant=? AND status IN ('queued','running')",
                (self.tenant,),
            ).fetchone()[0]
            total = len(experiment.scenarios) * experiment.trials
            if active + total > self.max_sessions:
                raise Conflict("max_sessions limit reached")
            db.execute(
                "INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    experiment.id,
                    self.tenant,
                    experiment.name,
                    "queued",
                    experiment.seed,
                    experiment.trials,
                    total,
                    0,
                    0,
                    total,
                    0,
                    encode(config),
                    None,
                    now,
                    now,
                ),
            )
            for position, scenario in enumerate(experiment.scenarios):
                body = encode(scenario.model_dump(mode="json"))
                db.execute(
                    "INSERT INTO scenario_snapshots VALUES (?,?,?,?)",
                    (experiment.id, scenario.id, position, body),
                )
                for trial in range(experiment.trials):
                    environment_id = uid()
                    session_seed = _session_seed(experiment.seed, scenario.id, trial)
                    db.execute(
                        "INSERT INTO session_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            environment_id,
                            self.tenant,
                            experiment.id,
                            scenario.id,
                            trial,
                            session_seed,
                            "queued",
                            None,
                            0,
                            experiment.turns,
                            "Queued",
                            body,
                            now,
                            now,
                        ),
                    )
                    self._outbox(
                        db,
                        kind="environment_session.queued",
                        body={"status": "queued", "scenario_id": scenario.id, "trial": trial},
                        experiment=experiment.id,
                        environment=environment_id,
                    )
                    jobs.append(
                        {
                            "id": environment_id,
                            "scenario": scenario,
                            "seed": session_seed,
                            "turns": experiment.turns,
                            "experiment": experiment.id,
                            "trial": trial,
                        }
                    )
            self._outbox(
                db,
                kind="experiment.queued",
                body={"status": "queued", "total": total, "name": experiment.name},
                experiment=experiment.id,
                environment=None,
            )
        with self._lock:
            for job in jobs:
                self._jobs[job["id"]] = job
                self._schedule_locked(job)

    def _schedule_locked(self, job: dict[str, Any]) -> None:
        group = job["experiment"] or "standalone:" + job["id"]
        self._pending.setdefault(group, []).append(job)
        self._served.setdefault(group, 0)
        self._futures[job["id"]] = Future()
        self._launch_locked()

    def _launch_locked(self) -> None:
        while self._running_jobs < self.max_concurrency and self._pending:
            group = min(self._pending, key=lambda key: self._served[key])
            job = self._pending[group].pop(0)
            if not self._pending[group]:
                del self._pending[group]
            self._served[group] += 1
            self._running_jobs += 1
            self._executor.submit(self._execute_scheduled, job)

    def _execute_scheduled(self, job: dict[str, Any]) -> None:
        try:
            self._execute(job)
        finally:
            with self._lock:
                completion = self._futures[job["id"]]
                if not completion.done():
                    completion.set_result(None)
                self._running_jobs -= 1
                self._launch_locked()

    def _experiment_sessions(self, experiment_id: str) -> tuple[EnvironmentSession, ...]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT environment FROM session_runs WHERE experiment=? ORDER BY created,scenario,trial",
                (experiment_id,),
            ).fetchall()
        return tuple(
            EnvironmentSession(
                self.store,
                self.environment_factory(),
                harness=self,
                session_id=row["environment"],
            )
            for row in rows
        )

    def _agent_spec(self, participant: str, agent: Any) -> AgentSpec:
        return AgentSpec(
            id=participant,
            implementation=str(agent.implementation),
            policy_version=str(getattr(agent, "policy_version", "1")),
            config=dict(getattr(agent, "config", {})),
            checkpoint=all(callable(getattr(agent, name, None)) for name in ("checkpoint", "restore")),
        )

    def _execute(self, job: dict[str, Any]) -> None:
        environment_id = job["id"]
        try:
            environment = self.environment_factory()
            agents = {participant: factory() for participant, factory in self.agent_factories.items()}
            scenario: Scenario[Any] = job["scenario"]
            spec = ExperimentSpec(
                environment=environment.spec,
                participants=tuple(self._agent_spec(name, agent) for name, agent in agents.items()),
                seed=job["seed"],
                scenario=scenario.id,
                scenario_input=scenario.model_dump(mode="json")["input"],
                scenario_reference=scenario.reference,
                scenario_metadata=scenario.metadata,
                scoring_versions=self.scoring_versions,
                policy=RunPolicy(max_turns=job["turns"]),
            )
            self._set_status(environment_id, "running", "Started")
            runtime = RuntimeEnvironmentSession(self.store, environment)
            runtime.create(spec, self.researcher, environment_id=environment_id)
            result = run_session(
                runtime,
                environment_id,
                self.researcher,
                agents,
                turns=job["turns"],
            )
            final = "succeeded" if result["status"] in ("running", "completed") else "stopped"
            self._set_status(environment_id, final, "Completed")
        except BaseException as error:
            with self.store.transaction() as db:
                current = db.execute(
                    "SELECT status FROM session_runs WHERE environment=?", (environment_id,)
                ).fetchone()
            if current and current["status"] == "stopped":
                return
            status = "interrupted" if isinstance(error, (InterruptedError, KeyboardInterrupt)) else "failed"
            activity = "Interrupted" if status == "interrupted" else "Failed"
            self._set_status(environment_id, status, activity, error=type(error).__name__)

    def _outbox(
        self,
        db,
        *,
        kind: str,
        body: dict[str, Any],
        experiment: str | None,
        environment: str | None,
    ) -> None:
        db.execute(
            "INSERT INTO event_outbox (tenant,topic,experiment,environment,kind,body,created) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                self.tenant,
                "experiment" if experiment else "environment_session",
                experiment,
                environment,
                kind,
                encode(body),
                time.time(),
            ),
        )

    def _set_status(
        self,
        environment: str,
        status: str,
        activity: str,
        *,
        error: str | None = None,
    ) -> None:
        with self.store.transaction() as db:
            db.execute(
                "UPDATE session_runs SET status=?,error=?,latest_activity=?,updated=? WHERE environment=?",
                (status, error, activity, time.time(), environment),
            )
            relation = db.execute(
                "SELECT experiment,scenario,trial FROM session_runs WHERE environment=?", (environment,)
            ).fetchone()
            self._outbox(
                db,
                kind="environment_session." + status,
                body={
                    "status": status,
                    "scenario_id": relation["scenario"],
                    "trial": relation["trial"],
                    "error": error,
                },
                experiment=relation["experiment"],
                environment=environment,
            )
            if relation["experiment"]:
                self._refresh_experiment(db, relation["experiment"])

    def _refresh_experiment(self, db, experiment: str) -> None:
        counts = {
            row["status"]: row["count"]
            for row in db.execute(
                "SELECT status,count(*) AS count FROM session_runs WHERE experiment=? GROUP BY status",
                (experiment,),
            )
        }
        queued = counts.get("queued", 0)
        running = counts.get("running", 0)
        failed = counts.get("failed", 0)
        completed = sum(counts.get(status, 0) for status in ("succeeded", "failed", "stopped", "interrupted"))
        total = sum(counts.values())
        if running or queued:
            status = "running" if running else "queued"
        elif counts.get("interrupted", 0):
            status = "interrupted"
        elif counts.get("stopped", 0):
            status = "stopped"
        elif failed:
            status = "failed"
        else:
            status = "succeeded"
        db.execute(
            "UPDATE experiments SET status=?,completed=?,running=?,queued=?,failed=?,updated=? WHERE id=?",
            (status, completed, running, queued, failed, time.time(), experiment),
        )
        self._outbox(
            db,
            kind="experiment.updated",
            body={
                "status": status,
                "completed": completed,
                "running": running,
                "queued": queued,
                "failed": failed,
                "total": total,
            },
            experiment=experiment,
            environment=None,
        )

    def _wait(self, environment: str, timeout: float | None = None) -> None:
        with self._lock:
            future = self._futures.get(environment)
        if future is None:
            if EnvironmentSession(
                self.store,
                self.environment_factory(),
                harness=self,
                session_id=environment,
            ).status not in ("succeeded", "failed", "stopped"):
                raise Conflict("environment session requires explicit resume")
            return
        future.result(timeout=timeout)

    def _stop(self, environment: str) -> None:
        with self._lock:
            job = self._jobs.get(environment)
            if job:
                group = job["experiment"] or "standalone:" + environment
                pending = self._pending.get(group, [])
                if job in pending:
                    pending.remove(job)
                    if not pending:
                        self._pending.pop(group, None)
                    completion = self._futures.get(environment)
                    if completion and not completion.done():
                        completion.set_result(None)
                    self._set_status(environment, "stopped", "Stopped")
                    return
        with self.store.transaction() as db:
            row = db.execute("SELECT 1 FROM environments WHERE id=?", (environment,)).fetchone()
        if row:
            RuntimeEnvironmentSession(self.store, self.environment_factory()).cancel(
                environment, self.researcher
            )
        self._set_status(environment, "stopped", "Stopped")

    def _resume(self, environment: str) -> None:
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM session_runs WHERE environment=?", (environment,)).fetchone()
            if not row or row["status"] != "interrupted":
                raise Conflict("only interrupted sessions can resume")
            job = {
                "id": environment,
                "scenario": Scenario[Any].model_validate_json(row["scenario_body"]),
                "seed": row["seed"],
                "turns": row["target_turns"],
                "experiment": row["experiment"],
                "trial": row["trial"],
            }
            db.execute(
                "UPDATE session_runs SET status='queued',error=NULL,latest_activity='Queued',updated=? "
                "WHERE environment=?",
                (time.time(), environment),
            )
            if row["experiment"]:
                self._refresh_experiment(db, row["experiment"])
        with self._lock:
            self._jobs[environment] = job
            self._schedule_locked(job)

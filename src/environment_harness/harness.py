"""Typed local orchestration for standalone and grouped environment sessions."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from .access import trusted_local
from .contracts import AgentSpec, BranchRequest, ExperimentSpec, RunPolicy, Scenario
from .errors import Conflict
from .operations import environment_operations
from .runner import run as run_session
from .runtime import _SessionRuntime
from .store import EvidenceStore, digest, encode, uid


def _session_seed(seed: int, scenario_id: str, trial: int) -> int:
    material = f"{seed}\0{scenario_id}\0{trial}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") & ((1 << 63) - 1)


SESSION_RUN_COLUMNS = (
    "environment",
    "tenant",
    "experiment",
    "scenario",
    "trial",
    "seed",
    "status",
    "error",
    "turns",
    "target_turns",
    "latest_activity",
    "scenario_body",
    "created",
    "updated",
    "environment_id",
    "environment_version",
    "spec_digest",
    "blocked_reason",
)
_INSERT_SESSION_RUN = "INSERT INTO session_runs ({}) VALUES ({})".format(
    ",".join(SESSION_RUN_COLUMNS), ",".join("?" * len(SESSION_RUN_COLUMNS))
)


@dataclass(frozen=True)
class EnvironmentReference:
    """The only environment identity that is ever serialized.

    Recovery matches this frozen reference against the specs produced by the
    currently supplied factories and verifies the schema/capability digest
    before scheduling. It never imports persisted text.
    """

    id: str
    version: str
    spec_digest: str


class _EnvironmentRegistry:
    """Typed environment factories configured once on one harness instance.

    This is explicit dependency injection, not a durable Environment resource,
    mutable global registry, or entry-point discovery system. Factories are
    selected by object or class identity; only ``(id, version, spec_digest)`` is
    persisted.
    """

    def __init__(self, factories):
        self._by_factory: dict[int, EnvironmentReference] = {}
        self._by_reference: dict[EnvironmentReference, Any] = {}
        self._order: list[Any] = []
        for factory in factories:
            if not callable(factory):
                raise TypeError("environment factories must be callable objects or classes")
            spec = factory().spec  # pyright: ignore[reportAttributeAccessIssue]
            reference = EnvironmentReference(
                id=spec.id,
                version=spec.version,
                spec_digest=digest(spec.model_dump(mode="json")),
            )
            if reference in self._by_reference:
                raise ValueError(f"duplicate environment factory for {reference.id}@{reference.version}")
            self._by_factory[id(factory)] = reference
            self._by_reference[reference] = factory
            self._order.append(factory)
        if not self._order:
            raise ValueError("at least one environment factory is required")

    @property
    def default(self):
        if len(self._order) != 1:
            raise ValueError("select one configured environment implementation explicitly")
        return self._order[0]

    def reference(self, factory) -> EnvironmentReference:
        """Return the frozen reference for a supplied typed factory."""

        try:
            return self._by_factory[id(factory)]
        except KeyError:
            raise ValueError("environment implementation was not supplied to this harness") from None

    def resolve(self, reference: EnvironmentReference):
        """Return the factory registered under ``reference``, or ``None``.

        The registry key is the reference computed from each factory's own spec,
        so a hit already matches identity and schema digest. Callers verify a
        freshly constructed instance with :meth:`verify` before execution, which
        also catches a factory whose spec changes between calls.
        """

        return self._by_reference.get(reference)

    @staticmethod
    def verify(environment, reference: EnvironmentReference) -> bool:
        spec = environment.spec
        return (
            EnvironmentReference(
                id=spec.id,
                version=spec.version,
                spec_digest=digest(spec.model_dump(mode="json")),
            )
            == reference
        )


class SessionRunner(Protocol):
    """Callable that advances one durable environment session."""

    def __call__(
        self,
        session: Any,
        environment: str,
        access: Any,
        agents: Mapping[str, Any],
        *,
        turns: int,
    ) -> Mapping[str, Any]: ...


class EnvironmentSession:
    """A typed domain handle for one environment session.

    The handle neither inherits from nor exposes the private session runtime,
    and every public method accepts only domain inputs. Authorization stays
    inside the trusted local context that :class:`EnvironmentHarness` owns.
    """

    def __init__(self, harness: EnvironmentHarness, session_id: str):
        self._harness = harness
        self._session_id = session_id

    def __repr__(self) -> str:
        return f"EnvironmentSession(id={self._session_id!r})"

    def _run_record(self):
        with self._harness.store.transaction() as db:
            row = db.execute("SELECT * FROM session_runs WHERE environment=?", (self._session_id,)).fetchone()
            if not row:
                raise Conflict("environment session record is unavailable")
            return dict(row)

    @property
    def id(self) -> str:
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
        self._harness._wait(self._session_id, timeout)
        return self

    def stop(self) -> EnvironmentSession:
        self._harness._stop(self._session_id)
        return self

    def resume(self) -> EnvironmentSession:
        self._harness._resume(self._session_id)
        return self

    def advance(self, *, turns: int = 1) -> dict[str, Any]:
        """Run more turns on this session inside its frozen turn budget."""

        return dict(self._harness._advance(self._session_id, turns))

    def record(self) -> dict[str, Any]:
        """Return the current durable session projection."""

        return dict(self._harness._runtime().get(self._session_id, self._harness._access))

    def observation(self, participant: str) -> dict[str, Any]:
        return dict(self._harness._runtime().observe(self._session_id, self._harness._access, participant))

    def events(self, *, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return self._harness.store.events(self._session_id, self._harness._access, after, limit)

    def replay(self):
        return self._harness.store.replay(self._session_id, self._harness._access)

    def reports(self) -> list[dict[str, Any]]:
        return self._harness.store.reports(self._session_id, self._harness._access)

    def verify(self) -> dict[str, Any]:
        return self._harness.store.verify(self._session_id, self._harness._access)

    def trajectory(self):
        from .trajectories import TrajectoryRepository

        return TrajectoryRepository(self._harness.store).get(self._session_id, self._harness._access)

    def records(self, *, after: int = 0, limit: int = 200):
        from .trajectories import TrajectoryRepository

        return TrajectoryRepository(self._harness.store).records_page(
            self._session_id, self._harness._access, after=after, limit=limit
        )

    def snapshot(self):
        from .trajectories import TrajectoryRepository

        return TrajectoryRepository(self._harness.store).freeze(self._session_id, self._harness._access)

    def resource(self):
        """Return the portable ``Session`` resource for this handle."""

        return self._harness.resources().session(self._session_id, self._harness._access)

    def checkpoint_resources(self, *, limit: int = 100):
        """Return the portable ``Checkpoint`` resources frozen for this session."""

        return self._harness.resources().checkpoints(self._session_id, self._harness._access, limit=limit)

    def checkpoint_resource(self, checkpoint: str):
        return self._harness.resources().checkpoint(self._session_id, checkpoint, self._harness._access)

    def checkpoint(self, *, exact_agents: bool = False) -> dict[str, Any]:
        """Freeze immutable resumable state for this session revision."""

        return dict(self._harness._checkpoint(self._session_id, exact_agents=exact_agents))

    def checkpoints(self) -> list[dict[str, Any]]:
        with self._harness.store.transaction() as db:
            self._harness.store.environment(db, self._session_id, self._harness._access, "session.read")
            return [
                {"id": row["id"], "revision": row["revision"], "hash": row["hash"]}
                for row in db.execute(
                    "SELECT id,revision,hash FROM checkpoints WHERE environment=? ORDER BY revision,id",
                    (self._session_id,),
                )
            ]

    def branch(self, request: BranchRequest) -> EnvironmentSession:
        """Create a child session that inherits this session's authorized prefix."""

        return self._harness._branch(self._session_id, request)

    def cancel(self) -> dict[str, Any]:
        return dict(self._harness._runtime().cancel(self._session_id, self._harness._access))

    def report(self, report) -> dict[str, Any]:
        return self._harness.store.report(self._session_id, self._harness._access, report)

    def artifact(self, data: bytes, *, audience=(), media_type="application/octet-stream"):
        return self._harness.store.artifact(
            self._session_id,
            self._harness._access,
            data,
            audience=audience,
            media_type=media_type,
        )

    def read_artifact(self, key: str):
        return self._harness.store.read_artifact(self._session_id, self._harness._access, key)

    def turn_series(self, **bounds) -> dict[str, Any]:
        from .evaluation import turn_series

        return turn_series(self._harness.store, self._session_id, self._harness._access, **bounds)

    def participant_credential(self, participant: str, *, ttl: int = 3600) -> str:
        """Issue an opaque credential bound to one participant and generation."""

        return self._harness._participant_credential(self._session_id, participant, ttl)


class TrajectoryAccess:
    """Trusted local management surface for trajectories, sources, and datasets.

    Every method accepts only domain inputs; the trusted local access context
    stays inside :class:`EnvironmentHarness`.
    """

    def __init__(self, harness: EnvironmentHarness):
        from .training import TrainingRepository
        from .trajectories import TrajectoryRepository

        self._access = harness._access
        self._trajectories = TrajectoryRepository(harness.store)
        self._training = TrainingRepository(harness.store)

    # Source registration and bounded historical ingestion.
    def register(self, registration):
        return self._trajectories.register_source(registration, self._access)

    def ingest(self, source: str, records):
        return self._trajectories.ingest(source, tuple(records), self._access)

    def update_status(self, source: str, update):
        return self._trajectories.update_source_status(source, update, self._access)

    def status(self, source: str):
        return self._trajectories.source_status(source, self._access)

    # Trajectory inspection.
    def list(self, *, limit: int = 100, cursor: str | None = None):
        return self._trajectories.list_page(self._access, limit, cursor)

    def trajectory(self, identity: str):
        return self._trajectories.get(identity, self._access)

    def records(self, identity: str, *, after: int = 0, limit: int = 200):
        return self._trajectories.records_page(identity, self._access, after=after, limit=limit)

    # Immutable snapshots and streaming export.
    def freeze(self, identity: str):
        return self._trajectories.freeze(identity, self._access)

    def snapshot(self, snapshot: str):
        return self._trajectories.get_snapshot(snapshot, self._access)

    def snapshots(self, identity: str, *, limit: int = 100):
        return self._trajectories.list_snapshots(identity, self._access, limit=limit)

    def export_snapshot(self, snapshot: str):
        return self._trajectories.export_snapshot(snapshot, self._access)

    # Training-entitled datasets and local integration receipts.
    def freeze_dataset(self, name: str, trajectories):
        return self._training.freeze_dataset(name, tuple(trajectories), self._access)

    def dataset(self, dataset: str):
        return self._training.get_dataset(dataset, self._access)

    def datasets(self, *, limit: int = 100):
        return self._training.list_datasets(self._access, limit=limit)

    def export_dataset(self, dataset: str):
        return self._training.export_dataset(dataset, self._access)

    def train(self, dataset: str, integration, config=None):
        return self._training.run(dataset, integration, config or {}, self._access)

    def training_run(self, training_run: str):
        return self._training.get_run(training_run, self._access)

    def training_runs(self, *, dataset: str | None = None, limit: int = 100):
        return self._training.list_runs(self._access, dataset=dataset, limit=limit)


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
        factory: Callable[[], Any] | None = None,
    ):
        self.harness = harness
        self.name = name
        self.scenarios = scenarios
        self.trials = trials
        self.seed = seed
        self.turns = turns
        # One Experiment freezes exactly one supplied environment implementation.
        self.factory = factory or harness.environments.default
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

    def resource(self):
        """Return the portable ``Experiment`` resource for this handle."""

        return self.harness.resources().experiment(self.id, self.harness._access)

    def scenario_set(self):
        """Return the portable ``ScenarioSet`` this experiment froze as input."""

        return self.harness.resources().scenario_set(
            self.resource().spec.scenario_set.id, self.harness._access
        )

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
    """Run environment sessions locally with bounded threads and durable status.

    This is the only public local-execution facade. It is a trusted in-process
    interface and requires no authentication; behind it, a private session
    runtime requires an access context on every observation and mutation.
    """

    def __init__(
        self,
        store: EvidenceStore | str | Path,
        *,
        environment_factory: Callable[[], Any] | None = None,
        environments: Sequence[Callable[[], Any]] = (),
        agent_factories: Mapping[str, Callable[[], Any]],
        scoring_versions: tuple[str, ...] = (),
        policy: RunPolicy | None = None,
        session_runner: SessionRunner = run_session,
        max_sessions: int = 1000,
        max_concurrency: int = 4,
        tenant: str = "local",
        reconcile: bool = True,
    ):
        if max_sessions < 1 or max_concurrency < 1:
            raise ValueError("session and concurrency limits must be positive")
        self.store = store if isinstance(store, EvidenceStore) else EvidenceStore(store)
        supplied = list(environments) or ([environment_factory] if environment_factory else [])
        self.environments = _EnvironmentRegistry(supplied)
        self.agent_factories = dict(agent_factories)
        if not self.agent_factories:
            raise ValueError("at least one agent factory is required")
        if not callable(session_runner):
            raise TypeError("session_runner must be callable")
        self.scoring_versions = tuple(scoring_versions)
        self.policy = policy or RunPolicy()
        self.session_runner = session_runner
        self.max_sessions = max_sessions
        self.max_concurrency = max_concurrency
        self.tenant = tenant
        self._access = trusted_local(tenant)
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrency, thread_name_prefix="environment-session"
        )
        self._futures: dict[str, Future[Any]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._served: dict[str, int] = {}
        self._running_jobs = 0
        self._lock = threading.RLock()
        if reconcile:
            self.reconcile()

    @property
    def environment_factory(self) -> Callable[[], Any]:
        """The single configured implementation, when exactly one is supplied."""

        return self.environments.default

    def reconcile(self) -> dict[str, Any]:
        """Reconstruct durable work before this process accepts new work.

        The database, not the thread pool, is the source of requested Session
        work. Reconciliation is idempotent, runs under the store's normal
        writer-concurrency mechanism, and produces durable activity evidence for
        every state correction. It assumes no other live process is executing
        this store's sessions; two processes reconciling concurrently converge
        on the same corrections without duplicating a Session.
        """

        interrupted: list[str] = []
        recovered: list[dict[str, Any]] = []
        with self.store.transaction() as db:
            orphaned = db.execute(
                "SELECT environment,experiment,scenario,trial FROM session_runs "
                "WHERE tenant=? AND status='running' ORDER BY created",
                (self.tenant,),
            ).fetchall()
            for row in orphaned:
                db.execute(
                    "UPDATE session_runs SET status='interrupted',error=?,latest_activity=?,updated=? "
                    "WHERE environment=? AND status='running'",
                    ("process_loss", "Interrupted by process loss", time.time(), row["environment"]),
                )
                self._outbox(
                    db,
                    kind="environment_session.interrupted",
                    body={
                        "status": "interrupted",
                        "scenario_id": row["scenario"],
                        "trial": row["trial"],
                        "error": "process_loss",
                        "reason": "a previous process was lost; explicit resume is required",
                    },
                    experiment=row["experiment"],
                    environment=row["environment"],
                )
                interrupted.append(row["environment"])
            queued = db.execute(
                "SELECT * FROM session_runs WHERE tenant=? AND status='queued' ORDER BY created,scenario,trial",
                (self.tenant,),
            ).fetchall()
            experiments = {row["experiment"] for row in orphaned} | {row["experiment"] for row in queued}
            for experiment in sorted(identity for identity in experiments if identity):
                self._refresh_experiment(db, experiment)
            candidates = [self._job_from_row(row) for row in queued]
        with self._lock:
            for job in candidates:
                if job["id"] in self._jobs:
                    continue
                self._jobs[job["id"]] = job
                self._schedule_locked(job)
                recovered.append(job)
        return {
            "interrupted": interrupted,
            "requeued": [job["id"] for job in recovered],
        }

    def _job_from_row(self, row) -> dict[str, Any]:
        reference = None
        if row["environment_id"] and row["spec_digest"]:
            reference = EnvironmentReference(
                id=row["environment_id"],
                version=row["environment_version"],
                spec_digest=row["spec_digest"],
            )
        return {
            "id": row["environment"],
            "scenario": Scenario[Any].model_validate_json(row["scenario_body"]),
            "seed": row["seed"],
            "turns": row["target_turns"],
            "experiment": row["experiment"],
            "trial": row["trial"],
            "reference": reference,
        }

    def _runtime(self, factory: Callable[[], Any] | None = None) -> _SessionRuntime:
        return _SessionRuntime(self.store, (factory or self.environments.default)())

    def management_credential(self, *, subject: str = "management", ttl: int = 3600) -> str:
        """Issue an opaque management credential for the authenticated HTTP API."""

        return self.store.issue_management(self.tenant, subject, ttl=ttl)

    def viewer_credential(self, *, ttl: int = 3600) -> str:
        """Issue the read-only loopback viewer credential."""

        return self.store.issue_viewer(self.tenant, ttl=ttl)

    def _advance(self, session: str, turns: int) -> Mapping[str, Any]:
        if turns < 1:
            raise ValueError("turns must be positive")
        agents = {participant: factory() for participant, factory in self.agent_factories.items()}
        runtime = _SessionRuntime(self.store, self._session_factory(session)())
        return self.session_runner(runtime, session, self._access, agents, turns=turns)

    def _session_factory(self, session: str) -> Callable[[], Any]:
        """Resolve the typed factory that reproduces one Session's frozen reference."""

        with self.store.transaction() as db:
            row = db.execute(
                "SELECT environment_id,environment_version,spec_digest FROM session_runs "
                "WHERE environment=? AND tenant=?",
                (session, self.tenant),
            ).fetchone()
        if row is None or not row["environment_id"]:
            return self.environments.default
        reference = EnvironmentReference(
            id=row["environment_id"],
            version=row["environment_version"],
            spec_digest=row["spec_digest"],
        )
        factory = self.environments.resolve(reference)
        if factory is None:
            raise Conflict(
                "no supplied environment factory reproduces "
                f"{reference.id}@{reference.version} ({reference.spec_digest[:12]})"
            )
        return factory

    def _checkpoint(self, session: str, *, exact_agents: bool) -> Mapping[str, Any]:
        runtime = self._runtime(self._session_factory(session))
        lease = runtime.lease(session, self._access, "environment-harness-checkpoint", ttl=60)
        try:
            return runtime.checkpoint(session, self._access, lease, exact_agents=exact_agents)
        finally:
            with suppress(Conflict):
                runtime.release(session, self._access, lease)

    def _branch(self, session: str, request: BranchRequest) -> EnvironmentSession:
        """Create the child Session for one BranchRequest.

        Repeating a request with the same idempotency key converges on one child
        Session; reusing the key with different interventions fails.
        """

        if not isinstance(request, BranchRequest):
            request = BranchRequest.model_validate(request)
        child = (
            uid()
            if request.idempotency_key is None
            else digest({"parent": session, "key": request.idempotency_key})[:32]
        )
        runtime = self._runtime(self._session_factory(session))
        runtime.branch(
            session,
            self._access,
            request.checkpoint,
            request.interventions,
            new_environment=child,
            turns=request.turns,
        )
        with self.store.transaction() as db:
            parent = db.execute(
                "SELECT experiment FROM session_runs WHERE environment=?", (session,)
            ).fetchone()
            if parent is not None and parent["experiment"]:
                self._refresh_experiment(db, parent["experiment"])
        return EnvironmentSession(self, child)

    def compare(self, sessions) -> dict[str, Any]:
        from .evaluation import compare as compare_sessions

        return compare_sessions(self.store, list(sessions), self._access)

    def resources(self):
        """Return the portable experiment-resource projection for this store."""

        from .resources import ResourceProjection

        return ResourceProjection(self.store)

    def experiment_resources(self, *, limit: int = 100):
        from .resources import ResourceProjection

        return ResourceProjection(self.store).experiments(self._access, limit=limit)

    def session_resources(self, *, experiment: str | None = None, limit: int = 100):
        from .resources import ResourceProjection

        return ResourceProjection(self.store).sessions(self._access, experiment=experiment, limit=limit)

    def scenario_sets(self, *, limit: int = 100):
        from .resources import ResourceProjection

        return ResourceProjection(self.store).scenario_sets(self._access, limit=limit)

    def sources(self) -> TrajectoryAccess:
        """Return the trusted local trajectory, source, and dataset surface."""

        return TrajectoryAccess(self)

    def trajectories(self, *, limit: int = 100, cursor: str | None = None):
        return TrajectoryAccess(self).list(limit=limit, cursor=cursor)

    def _participant_credential(self, session: str, participant: str, ttl: int) -> str:
        with self.store.transaction() as db:
            row = self.store.environment(db, session, self._access, "credential.participant.issue")
            members = json.loads(row["participants"])
            if participant not in members:
                raise Conflict("unknown participant")
            member = members[participant]
        return self.store.issue_participant(
            self.tenant,
            member["controller"],
            session=session,
            participant=participant,
            generation=member["generation"],
            ttl=min(ttl, 86400),
        )

    def _validate_scenario(self, scenario: Scenario[Any], factory=None) -> Scenario[Any]:
        environment = (factory or self.environments.default)()
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
        factory=None,
    ) -> EnvironmentSession:
        if turns < 1:
            raise ValueError("turns must be positive")
        factory = factory or self.environments.default
        reference = self.environments.reference(factory)
        environment_operations(factory())
        scenario = self._validate_scenario(scenario, factory)
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
                _INSERT_SESSION_RUN,
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
                    reference.id,
                    reference.version,
                    reference.spec_digest,
                    None,
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
            "reference": reference,
        }
        with self._lock:
            self._jobs[environment_id] = job
            self._schedule_locked(job)
        return EnvironmentSession(self, environment_id)

    def start(
        self,
        scenario: Scenario[Any],
        *,
        seed: int = 0,
        turns: int = 10,
        environment: Callable[[], Any] | None = None,
    ) -> EnvironmentSession:
        """Queue one session, selecting a supplied typed environment factory."""

        return self._reserve(scenario, seed=seed, turns=turns, factory=environment)

    def run(
        self,
        scenario: Scenario[Any],
        *,
        seed: int = 0,
        turns: int = 10,
        environment: Callable[[], Any] | None = None,
    ) -> EnvironmentSession:
        return self.start(scenario, seed=seed, turns=turns, environment=environment).wait()

    def session(self, session_id: str) -> EnvironmentSession:
        """Reconnect to a persisted session by its durable identity."""

        with self.store.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM session_runs WHERE environment=? AND tenant=?",
                (session_id, self.tenant),
            ).fetchone():
                raise Conflict("environment session is unavailable")
        return EnvironmentSession(self, session_id)

    def sessions(self, *, limit: int = 100) -> tuple[EnvironmentSession, ...]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT environment FROM session_runs WHERE tenant=? ORDER BY created DESC LIMIT ?",
                (self.tenant, limit),
            ).fetchall()
        return tuple(EnvironmentSession(self, row["environment"]) for row in rows)

    def activity(self, *, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.activity(self._access, after, limit)

    def hierarchy(self) -> dict[str, Any]:
        """Return the current experiment/session ownership projection."""

        return self.store.activity_hierarchy(self._access)

    def experiment(
        self,
        name: str,
        scenarios,
        *,
        trials: int = 1,
        seed: int = 0,
        turns: int = 10,
        environment: Callable[[], Any] | None = None,
    ) -> Experiment:
        if not name.strip():
            raise ValueError("experiment name is required")
        if trials < 1 or turns < 1:
            raise ValueError("trials and turns must be positive")
        factory = environment or self.environments.default
        # Fail before queueing when the selected implementation was not supplied.
        self.environments.reference(factory)
        validated = tuple(self._validate_scenario(scenario, factory) for scenario in scenarios)
        if not validated:
            raise ValueError("at least one scenario is required")
        ids = [scenario.id for scenario in validated]
        if len(ids) != len(set(ids)):
            raise ValueError("scenario IDs must be unique within an experiment")
        if len(validated) * trials > self.max_sessions:
            raise Conflict("experiment exceeds max_sessions")
        return Experiment(self, name, validated, trials=trials, seed=seed, turns=turns, factory=factory)

    def _start_experiment(self, experiment: Experiment) -> None:
        """Commit the frozen Experiment and every queued Session atomically.

        Only after this transaction commits may the bounded local scheduler
        claim work; submitting to the thread pool is a post-commit delivery
        attempt.
        """

        reference = self.environments.reference(experiment.factory)
        environment = experiment.factory()
        runtime_operations = environment_operations(environment)
        preview_agents = {participant: factory() for participant, factory in self.agent_factories.items()}
        operations = [
            runtime_operations[declaration.name].spec for declaration in environment.spec.operations
        ]
        policy = self.policy.model_copy(
            update={
                "max_turns": experiment.turns,
                "allowed_endpoints": tuple(
                    dict.fromkeys(
                        (
                            *self.policy.allowed_endpoints,
                            *(runtime_operations[item.name].endpoint for item in operations),
                        )
                    )
                ),
                "allowed_operations": tuple(
                    dict.fromkeys((*self.policy.allowed_operations, *(item.name for item in operations)))
                ),
            }
        )
        config = {
            "environment": environment.spec.model_dump(mode="json"),
            "participants": [
                self._agent_spec(name, agent).model_dump(mode="json")
                for name, agent in preview_agents.items()
            ],
            "environment_reference": {
                "id": reference.id,
                "version": reference.version,
                "spec_digest": reference.spec_digest,
            },
            "scoring_versions": list(self.scoring_versions),
            "operations": [operation.model_dump(mode="json") for operation in operations],
            "policy": policy.model_dump(mode="json"),
            "execution": {
                "seed": experiment.seed,
                "trials": experiment.trials,
                "turns": experiment.turns,
                "max_concurrency": self.max_concurrency,
            },
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
                        _INSERT_SESSION_RUN,
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
                            reference.id,
                            reference.version,
                            reference.spec_digest,
                            None,
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
                            "reference": reference,
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
            try:
                self._executor.submit(self._execute_scheduled, job)
            except RuntimeError as error:
                # Submission is a post-commit delivery attempt. The Session stays
                # durably queued and a later reconciliation retries it without
                # creating another Session.
                self._running_jobs -= 1
                self._pending.setdefault(group, []).insert(0, job)
                self._served[group] -= 1
                self._record_submission_failure(job, type(error).__name__)
                return

    def _record_submission_failure(self, job: dict[str, Any], reason: str) -> None:
        with self.store.transaction() as db:
            db.execute(
                "UPDATE session_runs SET latest_activity=?,updated=? WHERE environment=? AND status='queued'",
                ("Queued; executor rejected delivery", time.time(), job["id"]),
            )
            self._outbox(
                db,
                kind="scheduler.submission_failed",
                body={
                    "status": "queued",
                    "scenario_id": job["scenario"].id,
                    "trial": job["trial"],
                    "error": reason,
                },
                experiment=job["experiment"],
                environment=job["id"],
            )

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
        return tuple(EnvironmentSession(self, row["environment"]) for row in rows)

    def _agent_spec(self, participant: str, agent: Any) -> AgentSpec:
        # Prefer a declared capability. An adapter may define checkpoint hooks
        # only to reject them, so a callable probe alone would freeze a
        # continuation contract the program cannot honor.
        declared = getattr(agent, "supports_checkpoint", None)
        supported = (
            bool(declared)
            if declared is not None
            else all(callable(getattr(agent, name, None)) for name in ("checkpoint", "restore"))
        )
        return AgentSpec(
            id=participant,
            implementation=str(agent.implementation),
            policy_version=str(getattr(agent, "policy_version", "1")),
            config=dict(getattr(agent, "config", {})),
            checkpoint=supported,
        )

    def _execute(self, job: dict[str, Any]) -> None:
        environment_id = job["id"]
        reference: EnvironmentReference | None = job.get("reference")
        factory = self.environments.resolve(reference) if reference is not None else self.environments.default
        if reference is not None and factory is None:
            self._block(job, reference)
            return
        assert factory is not None
        try:
            environment = factory()
            if reference is not None and not self.environments.verify(environment, reference):
                # A factory that no longer reproduces its frozen identity never
                # runs; the Session stays blocked for inspection.
                self._block(job, reference)
                return
            agents = {participant: factory_() for participant, factory_ in self.agent_factories.items()}
            scenario: Scenario[Any] = job["scenario"]
            runtime_operations = environment_operations(environment)
            operations = tuple(
                runtime_operations[declared.name].spec for declared in environment.spec.operations
            )
            operation_endpoints = tuple(
                dict.fromkeys(
                    (
                        *self.policy.allowed_endpoints,
                        *(runtime_operations[item.name].endpoint for item in operations),
                    )
                )
            )
            operation_names = tuple(
                dict.fromkeys((*self.policy.allowed_operations, *(item.name for item in operations)))
            )
            policy = self.policy.model_copy(
                update={
                    "max_turns": job["turns"],
                    "allowed_endpoints": operation_endpoints,
                    "allowed_operations": operation_names,
                }
            )
            spec = ExperimentSpec(
                environment=environment.spec,
                participants=tuple(self._agent_spec(name, agent) for name, agent in agents.items()),
                seed=job["seed"],
                scenario=scenario.id,
                scenario_input=scenario.model_dump(mode="json")["input"],
                scenario_reference=scenario.reference,
                scenario_metadata=scenario.metadata,
                scoring_versions=self.scoring_versions,
                policy=policy,
                operations=operations,
            )
            self._set_status(environment_id, "running", "Started")
            runtime = _SessionRuntime(self.store, environment)
            with self.store.transaction() as db:
                created = db.execute("SELECT 1 FROM environments WHERE id=?", (environment_id,)).fetchone()
            if created is None:
                runtime.create(spec, self._access, environment_id=environment_id)
            else:
                # Resumed and branched sessions keep their frozen manifest.
                runtime.resume_if_paused(environment_id, self._access)
            result = self.session_runner(
                runtime,
                environment_id,
                self._access,
                agents,
                turns=job["turns"],
            )
            current = runtime.get(environment_id, self._access)
            if not isinstance(result, Mapping) or any(
                result.get(field) != current[field] for field in ("id", "revision", "status")
            ):
                raise Conflict("session_runner must return the current environment-session record")
            final = "succeeded" if current["status"] in ("running", "completed") else "stopped"
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

    def _block(self, job: dict[str, Any], reference: EnvironmentReference) -> None:
        """Leave a Session durably blocked rather than running substitute code."""

        detail = (
            "no supplied environment factory reproduces "
            f"{reference.id}@{reference.version} ({reference.spec_digest[:12]})"
        )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE session_runs SET status='blocked',error=?,blocked_reason=?,"
                "latest_activity=?,updated=? WHERE environment=?",
                ("environment_factory_unavailable", detail, "Blocked", time.time(), job["id"]),
            )
            self._outbox(
                db,
                kind="environment_session.blocked",
                body={
                    "status": "blocked",
                    "scenario_id": job["scenario"].id,
                    "trial": job["trial"],
                    "error": "environment_factory_unavailable",
                    "reason": detail,
                },
                experiment=job["experiment"],
                environment=job["id"],
            )
            if job["experiment"]:
                self._refresh_experiment(db, job["experiment"])

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
        completed = sum(
            counts.get(status, 0) for status in ("succeeded", "failed", "stopped", "interrupted", "blocked")
        )
        total = sum(counts.values())
        if running or queued:
            status = "running" if running else "queued"
        elif counts.get("blocked", 0):
            status = "blocked"
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
            if EnvironmentSession(self, environment).status not in (
                "succeeded",
                "failed",
                "stopped",
            ):
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
            self._runtime(self._session_factory(environment)).cancel(environment, self._access)
        self._set_status(environment, "stopped", "Stopped")

    def _resume(self, environment: str) -> None:
        """Requeue interrupted or blocked work at explicit caller request."""

        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM session_runs WHERE environment=?", (environment,)).fetchone()
            if not row or row["status"] not in ("interrupted", "blocked"):
                raise Conflict("only interrupted or blocked sessions can resume")
            job = self._job_from_row(row)
            db.execute(
                "UPDATE session_runs SET status='queued',error=NULL,blocked_reason=NULL,"
                "latest_activity='Queued',updated=? WHERE environment=?",
                (time.time(), environment),
            )
            if row["experiment"]:
                self._refresh_experiment(db, row["experiment"])
        with self._lock:
            self._jobs[environment] = job
            self._schedule_locked(job)

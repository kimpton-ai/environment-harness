"""Supplier-owned environment service. Run behind HTTPS outside loopback."""

import json
import logging
import secrets
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import capabilities as capability_module
from .capabilities import CapabilityDocument
from .contracts import (
    Action,
    ActivityHierarchy,
    ActivityPage,
    BranchRequest,
    EvidencePage,
    ExperimentSpec,
    ScoreReport,
)
from .coordinator import advance
from .errors import (
    BudgetExceeded,
    CapabilityUnavailable,
    Conflict,
    EvidenceIncomplete,
    Forbidden,
    HarnessError,
    PayloadTooLarge,
    Unauthenticated,
    Unavailable,
    Unsupported,
)
from .evaluation import compare, turn_series
from .operations import Operations
from .resources import Checkpoint, Experiment, PolicyProjection, ResourceProjection, ScenarioSet, Session
from .store import encode
from .training import DatasetCreate, TrainingRepository, TrainingRun, TrajectoryDataset
from .trajectories import (
    Policy,
    SourceAcknowledgement,
    SourceIngestionBatch,
    SourceRegistration,
    SourceRegistrationReceipt,
    SourceStatus,
    SourceStatusUpdate,
    Trajectory,
    TrajectoryRecordPage,
    TrajectoryRepository,
    TrajectorySnapshot,
    TrajectorySummary,
)

logger = logging.getLogger(__name__)


class CommandOperation(StrEnum):
    ADVANCE = "advance"
    LEASE = "lease"
    RELEASE = "release"
    CANCEL = "cancel"
    RESOLVE = "resolve"
    CLOSE_PHASE = "close_phase"
    CHECKPOINT = "checkpoint"
    RECONCILE_AGENT = "reconcile_agent"
    RESUME = "resume"
    BRANCH = "branch"
    CONTROL = "control"
    MEMORY = "memory"
    TRANSFER = "transfer"
    EXTERNAL_EVENT = "external_event"
    FINALIZE_OUTCOMES = "finalize_outcomes"


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: CommandOperation
    arguments: dict[str, Any] = Field(default_factory=dict)


class CredentialRequest(BaseModel):
    """Participant credentials are scoped by the route, never by the body."""

    model_config = ConfigDict(extra="forbid")
    ttl: int = Field(
        default=3600,
        ge=1,
        description="Credential lifetime in seconds. Values above 86400 are capped at 86400.",
    )


class CheckpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exact_agents: bool = Field(
        default=False,
        description="Require exact participant continuation state for every participant.",
    )


class ComparisonRequest(BaseModel):
    """A typed derived-comparison request. No comparison authority is stored."""

    model_config = ConfigDict(extra="forbid")
    sessions: tuple[str, ...] = Field(min_length=1, max_length=100)


class CredentialResponse(BaseModel):
    token: str = Field(description="Opaque participant credential. Treat this value as a secret.")


class OperationIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    payload: dict[str, Any]
    maximum_cost_micros: int = Field(default=0, ge=0)
    write: bool = False


class OperationIntentResponse(BaseModel):
    id: str
    status: Literal["prepared", "dispatching", "unknown", "succeeded", "failed"]


class ErrorDetail(BaseModel):
    field: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=512)
    type: str = Field(min_length=1, max_length=100)


class ApiError(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=512)
    status: int = Field(ge=400, le=599)
    request_id: str = Field(min_length=1, max_length=128)
    timestamp: str = Field(min_length=1, max_length=100)
    details: list[ErrorDetail] | None = Field(default=None, max_length=100)


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "Request validation failed",
                        "status": 422,
                        "request_id": "0123456789abcdef0123456789abcdef",
                        "timestamp": "2026-09-21T12:00:00.000Z",
                        "details": [
                            {
                                "field": "body.operation",
                                "message": "Input should be a supported command",
                                "type": "enum",
                            }
                        ],
                    }
                }
            ]
        }
    )
    error: ApiError


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorEnvelope, "description": description}
    for status, description in {
        400: "Invalid request",
        401: "Authentication required",
        402: "Budget exhausted",
        403: "Operation forbidden",
        404: "Resource not found",
        405: "Method not allowed",
        409: "State conflict",
        413: "Request too large",
        422: "Request validation failed",
        500: "Internal server error",
        503: "Service unavailable",
    }.items()
}

ERROR_RESPONSES[501] = {"model": ErrorEnvelope, "description": "Capability not enabled"}

OPENAPI_TAGS = [
    {"name": "Service", "description": "Service health and deployment capability discovery."},
    {
        "name": "Experiments",
        "description": "Create and inspect experiments, their scenario sets, and their sessions.",
    },
    {
        "name": "Sessions",
        "description": "Inspect, observe, control, checkpoint, and branch environment sessions.",
    },
    {
        "name": "Trajectories",
        "description": "Inspect native and imported trajectories, snapshots, datasets, and policies.",
    },
    {
        "name": "Sources",
        "description": "Register external sources, ingest bounded evidence, and inspect collection health.",
    },
    {
        "name": "Evidence",
        "description": "Read event evidence and store or retrieve session artifacts.",
    },
    {
        "name": "Activity",
        "description": "Read resumable activity feeds and the ownership hierarchy used by the viewer.",
    },
    {
        "name": "Evaluation",
        "description": "Publish score reports, export evidence, and compare environment sessions.",
    },
]


class ListLinks(BaseModel):
    """Relative navigation for one management collection page."""

    self: str = Field(min_length=1)
    next: str | None = None


def _page_model(name: str, item):
    """Build one typed management-list envelope for a resource kind.

    Ordered evidence and activity feeds keep their durable integer cursors; only
    management collections use this opaque-cursor envelope.
    """

    return type(
        name,
        (BaseModel,),
        {
            "__annotations__": {
                "items": tuple[item, ...],
                "nextCursor": str | None,
                "links": ListLinks,
            },
            "nextCursor": None,
            "model_config": ConfigDict(extra="forbid"),
            "__doc__": f"A cursor-paged index of {item.__name__} resources.",
        },
    )


ScenarioSetPage = _page_model("ScenarioSetPage", ScenarioSet)
ExperimentPage = _page_model("ExperimentPage", Experiment)
SessionPage = _page_model("SessionPage", Session)
CheckpointPage = _page_model("CheckpointPage", Checkpoint)
PolicyPage = _page_model("PolicyPage", Policy)
TrajectoryPage = _page_model("TrajectoryPage", TrajectorySummary)
SnapshotPage = _page_model("SnapshotPage", TrajectorySnapshot)
DatasetPage = _page_model("DatasetPage", TrajectoryDataset)
SourcePage = _page_model("SourcePage", SourceStatus)
TrainingRunPage = _page_model("TrainingRunPage", TrainingRun)


def _error_response(request: Request, code: str, message: str, status: int, details=None):
    request_id = getattr(request.state, "request_id", secrets.token_hex(16))
    body = {
        "code": code,
        "message": message,
        "status": status,
        "request_id": request_id,
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    if details is not None:
        body["details"] = details
    return JSONResponse(
        {"error": body},
        status_code=status,
        headers={"X-Request-ID": request_id},
    )


def create_app(session, *, local_access=None, trajectory_ingestion=False):
    """Build the authenticated session API for one environment implementation.

    ``session`` is normally an :class:`~environment_harness.EnvironmentHarness`;
    the app resolves its private session runtime internally so embedders never
    construct an authorization-aware object themselves.
    """

    if hasattr(session, "environment_factory"):
        session = session._runtime()
    store = session.store
    trajectories = TrajectoryRepository(store)
    training = TrainingRepository(store)
    app = FastAPI(
        title="EnvironmentHarness HTTP API",
        version="1.0.0",
        description=(
            "Authenticated HTTP access to EnvironmentHarness environment sessions, evidence, "
            "activity streams, artifacts, and evaluation results. See docs/PROTOCOL.md for "
            "authority, lifecycle, recovery, and evidence semantics."
        ),
        license_info={
            "name": "MIT",
            "identifier": "MIT",
        },
        openapi_tags=OPENAPI_TAGS,
        servers=[{"url": "/", "description": "Current EnvironmentHarness service"}],
        responses=ERROR_RESPONSES,
    )
    bearer = HTTPBearer(
        auto_error=False,
        scheme_name="BearerAuth",
        bearerFormat="opaque",
        description=(
            "Opaque EnvironmentHarness credential. The server resolves it to an identity and one "
            "of its fixed management, viewer, or participant access policies."
        ),
    )

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        request.state.request_id = secrets.token_hex(16)
        if request.headers.get("origin") and request.headers["origin"] != str(request.base_url).rstrip("/"):
            response = _error_response(
                request,
                "forbidden",
                "Cross-origin requests are not allowed",
                403,
            )
        elif request.method in ("POST", "PUT", "PATCH"):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16777216:
                    response = _error_response(
                        request,
                        "payload_too_large",
                        "Request body exceeds the 16 MiB limit",
                        413,
                    )
                    break
            else:
                request._body = bytes(body)
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path in ("/docs", "/docs/oauth2-redirect"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://fastapi.tiangolo.com; "
                "connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "object-src 'none'; frame-ancestors 'none'"
            )
        return response

    @app.exception_handler(HarnessError)
    async def failure(request, error):
        # One error taxonomy: every code maps to exactly one status, and no
        # client depends on message text.
        status = (
            401
            if isinstance(error, Unauthenticated)
            else 403
            if isinstance(error, Forbidden)
            else 409
            if isinstance(error, (Conflict, EvidenceIncomplete))
            else 422
            if isinstance(error, Unsupported)
            else 413
            if isinstance(error, PayloadTooLarge)
            else 501
            if isinstance(error, CapabilityUnavailable)
            else 402
            if isinstance(error, BudgetExceeded)
            else 503
            if isinstance(error, Unavailable)
            else 503
        )
        if status >= 500:
            logger.error(
                "Environment service failure request_id=%s method=%s path=%s",
                request.state.request_id,
                request.method,
                request.url.path,
                exc_info=(type(error), error, error.__traceback__),
            )
        return _error_response(request, error.code, str(error), status)

    @app.exception_handler(ValueError)
    async def invalid_value(request, error):
        del error
        return _error_response(request, "validation_error", "Invalid request", 422)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        details = [
            {
                "field": ".".join(str(part) for part in item["loc"]),
                "message": item["msg"],
                "type": item["type"],
            }
            for item in error.errors()
        ]
        return _error_response(
            request,
            "validation_error",
            "Request validation failed",
            422,
            details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_failure(request, error):
        status = error.status_code
        code = {
            400: "validation_error",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            413: "payload_too_large",
            422: "validation_error",
            501: "capability_unavailable",
            503: "service_unavailable",
        }.get(status, "http_error" if status < 500 else "internal_error")
        message = str(error.detail) if isinstance(error.detail, str) and status < 500 else "Request failed"
        return _error_response(request, code, message, status)

    @app.exception_handler(Exception)
    async def internal_failure(request, error):
        logger.error(
            "Unhandled environment service error request_id=%s method=%s path=%s",
            request.state.request_id,
            request.method,
            request.url.path,
            exc_info=(type(error), error, error.__traceback__),
        )
        return _error_response(request, "internal_error", "Internal server error", 500)

    def actor(credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]):
        """Resolve the opaque bearer credential into its server-owned policy.

        Callers never send permissions: the stored policy alone decides what an
        operation may do.
        """

        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(401, "Bearer credential required")
        return store.authenticate(credentials.credentials)

    if local_access is not None:

        @app.post("/local/connect", include_in_schema=False)
        def local_connect(request: Request):
            if (
                request.client is None
                or request.client.host not in ("127.0.0.1", "::1")
                or str(request.base_url).rstrip("/") != local_access.origin
                or request.headers.get("origin") != local_access.origin
            ):
                raise HTTPException(403, "Local connection requires the loopback viewer origin")
            return {"token": local_access.credential}

    projection = ResourceProjection(store)
    policies = PolicyProjection(store)
    capability_document = capability_module.document(
        trajectory_ingestion=trajectory_ingestion, local_access=local_access is not None
    )

    def page(request: Request, items, next_cursor, *, parameter="cursor"):
        """Return one typed management-list envelope plus its RFC Link header."""

        path = request.url.path
        query = dict(request.query_params)
        query.pop(parameter, None)
        base = path + ("?" + "&".join(f"{k}={v}" for k, v in sorted(query.items())) if query else "")
        following = None
        if next_cursor is not None:
            separator = "&" if "?" in base else "?"
            following = f"{base}{separator}{parameter}={next_cursor}"
        return {
            "items": list(items),
            "nextCursor": next_cursor,
            "links": {
                "self": str(request.url.path) + (("?" + request.url.query) if request.url.query else ""),
                "next": following,
            },
        }

    def link_header(response: Response, envelope):
        following = envelope["links"]["next"]
        if following is not None:
            response.headers["Link"] = f'<{following}>; rel="next"'
            response.headers["X-Next-Cursor"] = envelope["nextCursor"]
        return envelope

    def slice_page(items, cursor, limit, key=lambda item: item.metadata.id):
        """Apply one opaque management cursor to an ordered projection."""

        ordered = list(items)
        start = 0
        if cursor is not None:
            try:
                start = next(index for index, item in enumerate(ordered) if key(item) == cursor) + 1
            except StopIteration:
                raise ValueError("invalid collection cursor") from None
        selected = ordered[start : start + limit]
        following = key(selected[-1]) if selected and start + len(selected) < len(ordered) else None
        return selected, following

    def etag(response: Response, value: str):
        response.headers["ETag"] = f'"{value}"'

    def require_capability(name: str):
        for capability in capability_document.capabilities:
            if capability.name == name and not capability.enabled:
                raise CapabilityUnavailable(name, capability.reason)

    # -- Service ------------------------------------------------------------
    @app.get("/health", tags=["Service"], summary="Check service health")
    def health():
        return {"status": "ok", "protocol": "environment-session.v1"}

    @app.get("/viewer/config", include_in_schema=False)
    def viewer_config():
        return {"authentication": "local" if local_access is not None else "credential"}

    @app.get(
        "/v1/capabilities",
        tags=["Service"],
        summary="Discover enabled deployment capabilities",
        response_model=CapabilityDocument,
    )
    def deployment_capabilities(who=Depends(actor)):
        who.require("capabilities.read")
        return capability_document

    # -- Scenario sets ------------------------------------------------------
    @app.get(
        "/v1/scenario-sets",
        tags=["Experiments"],
        summary="List frozen scenario sets",
        response_model=ScenarioSetPage,
    )
    def scenario_sets(
        request: Request,
        response: Response,
        who=Depends(actor),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        items, following = slice_page(projection.scenario_sets(who, limit=1000), cursor, limit)
        return link_header(response, page(request, items, following))

    @app.get(
        "/v1/scenario-sets/{scenario_set_id}",
        tags=["Experiments"],
        summary="Get a frozen scenario set",
        response_model=ScenarioSet,
    )
    def scenario_set(scenario_set_id: str, response: Response, who=Depends(actor)):
        resource = projection.scenario_set(scenario_set_id, who)
        etag(response, resource.status.set_digest)
        return resource

    # -- Experiments --------------------------------------------------------
    @app.post(
        "/v1/experiments",
        tags=["Experiments"],
        summary="Create an experiment and queue its sessions",
        status_code=201,
    )
    def create_experiment(
        spec: ExperimentSpec,
        response: Response,
        x_operation_id: str = Header(),
        who=Depends(actor),
    ):
        """Create the supported implicit experiment-of-one.

        The request carries the structured environment reference for one
        server-configured implementation; the server resolves it against its
        typed factory dependencies and never loads executable code from a
        request. Multi-scenario experiments are created through the trusted
        in-process SDK.
        """

        created = session.create(spec, who, environment_id=x_operation_id)
        response.headers["Location"] = f"/v1/sessions/{created['id']}"
        return created

    @app.get(
        "/v1/experiments",
        tags=["Experiments"],
        summary="List experiments",
        response_model=ExperimentPage,
    )
    def experiments(
        request: Request,
        response: Response,
        who=Depends(actor),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        items, following = slice_page(projection.experiments(who, limit=1000), cursor, limit)
        return link_header(response, page(request, items, following))

    @app.get(
        "/v1/experiments/{experiment_id}",
        tags=["Experiments"],
        summary="Get an experiment",
        response_model=Experiment,
    )
    def experiment(experiment_id: str, response: Response, who=Depends(actor)):
        resource = projection.experiment(experiment_id, who)
        etag(response, resource.status.lock_digest)
        return resource

    @app.get(
        "/v1/experiments/{experiment_id}/sessions",
        tags=["Experiments"],
        summary="List the sessions an experiment derived",
        response_model=SessionPage,
    )
    def experiment_sessions(
        experiment_id: str,
        request: Request,
        response: Response,
        who=Depends(actor),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        projection.experiment(experiment_id, who)
        items, following = slice_page(
            projection.sessions(who, experiment=experiment_id, limit=1000), cursor, limit
        )
        return link_header(response, page(request, items, following))

    @app.get(
        "/v1/experiments/{experiment_id}/scores",
        tags=["Evaluation"],
        summary="List score reports across an experiment",
    )
    def experiment_scores(experiment_id: str, who=Depends(actor), limit: int = Query(100, ge=1, le=1000)):
        projection.experiment(experiment_id, who)
        with store.transaction() as db:
            rows = db.execute(
                "SELECT environment FROM session_runs WHERE experiment=? AND tenant=? "
                "ORDER BY created LIMIT ?",
                (experiment_id, who.tenant, limit),
            ).fetchall()
        return {"items": [report for row in rows for report in store.reports(row["environment"], who)]}

    # -- Sessions -----------------------------------------------------------
    @app.get(
        "/v1/sessions",
        tags=["Sessions"],
        summary="List environment sessions",
        response_model=SessionPage,
    )
    def sessions(
        request: Request,
        response: Response,
        who=Depends(actor),
        experiment: str | None = Query(None, min_length=1, max_length=200),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        items, following = slice_page(
            projection.sessions(who, experiment=experiment, limit=1000), cursor, limit
        )
        return link_header(response, page(request, items, following))

    @app.get(
        "/v1/sessions/{session_id}",
        tags=["Sessions"],
        summary="Get an environment session",
        response_model=Session,
    )
    def get_session(session_id: str, who=Depends(actor)):
        return projection.session(session_id, who)

    @app.get(
        "/v1/sessions/{session_id}/participants/{participant_id}/observation",
        tags=["Sessions"],
        summary="Read a participant observation",
    )
    def observation(session_id: str, participant_id: str, who=Depends(actor)):
        return session.observe(session_id, who, participant_id)

    @app.post(
        "/v1/sessions/{session_id}/participants/{participant_id}/actions",
        tags=["Sessions"],
        summary="Submit a participant action",
    )
    def submit_action(session_id: str, participant_id: str, action: Action, who=Depends(actor)):
        if action.participant != participant_id:
            raise Forbidden("cannot act for another participant")
        return session.submit(session_id, who, action)

    @app.post(
        "/v1/sessions/{session_id}/participants/{participant_id}/credentials",
        tags=["Sessions"],
        summary="Issue a participant credential",
        response_model=CredentialResponse,
        openapi_extra={"x-capability": capability_module.PARTICIPANT_CREDENTIALS},
    )
    def credential(
        session_id: str,
        participant_id: str,
        body: CredentialRequest | None = None,
        who=Depends(actor),
    ):
        """Issue a credential constrained to one session, participant, and generation.

        This is the only operation that mints a participant credential, so no
        other route can widen a caller's scope.
        """

        require_capability(capability_module.PARTICIPANT_CREDENTIALS)
        ttl = body.ttl if body is not None else 3600
        with store.transaction() as db:
            row = store.environment(db, session_id, who, "credential.participant.issue")
            participants = json.loads(row["participants"])
            if participant_id not in participants:
                raise Forbidden("unknown participant")
            member = participants[participant_id]
        return {
            "token": store.issue_participant(
                who.tenant,
                member["controller"],
                session=session_id,
                participant=participant_id,
                generation=member["generation"],
                ttl=min(ttl, 86400),
            )
        }

    @app.get(
        "/v1/sessions/{session_id}/checkpoints",
        tags=["Sessions"],
        summary="List immutable checkpoints",
        response_model=CheckpointPage,
    )
    def session_checkpoints(
        session_id: str,
        request: Request,
        response: Response,
        who=Depends(actor),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        items, following = slice_page(projection.checkpoints(session_id, who, limit=1000), cursor, limit)
        return link_header(response, page(request, items, following))

    @app.post(
        "/v1/sessions/{session_id}/checkpoints",
        tags=["Sessions"],
        summary="Freeze an immutable checkpoint",
        response_model=Checkpoint,
        status_code=201,
    )
    def create_checkpoint(
        session_id: str, response: Response, body: CheckpointRequest | None = None, who=Depends(actor)
    ):
        exact = bool(body.exact_agents) if body is not None else False
        lease = session.lease(session_id, who, "http-checkpoint", ttl=60)
        try:
            created = session.checkpoint(session_id, who, lease, exact_agents=exact)
        finally:
            with suppress(Conflict):
                session.release(session_id, who, lease)
        response.headers["Location"] = f"/v1/sessions/{session_id}/checkpoints/{created['id']}"
        return projection.checkpoint(session_id, created["id"], who)

    @app.get(
        "/v1/sessions/{session_id}/checkpoints/{checkpoint_id}",
        tags=["Sessions"],
        summary="Get an immutable checkpoint",
        response_model=Checkpoint,
    )
    def get_checkpoint(session_id: str, checkpoint_id: str, response: Response, who=Depends(actor)):
        resource = projection.checkpoint(session_id, checkpoint_id, who)
        etag(response, resource.status.checkpoint_digest)
        return resource

    @app.post(
        "/v1/sessions/{session_id}/branches",
        tags=["Sessions"],
        summary="Create a child session from a checkpoint",
        response_model=Session,
        status_code=201,
    )
    def create_branch(session_id: str, body: BranchRequest, response: Response, who=Depends(actor)):
        """Accept a strict `BranchRequest` and return the created child Session.

        There is no Branch resource: the child Session carries the parent
        Session, Checkpoint, lineage, and intervention references.
        """

        child = session.branch(session_id, who, body.checkpoint, body.interventions)
        response.headers["Location"] = f"/v1/sessions/{child['id']}"
        return projection.session(child["id"], who)

    @app.post(
        "/v1/sessions/{session_id}/commands",
        tags=["Sessions"],
        summary="Run an environment-session command",
        status_code=202,
        openapi_extra={"x-command-operations": [operation.value for operation in CommandOperation]},
    )
    def command(session_id: str, cmd: Command, who=Depends(actor)):
        a = cmd.arguments
        allowed = {
            "advance": lambda environment, who, **arguments: advance(session, environment, who, **arguments),
            "lease": session.lease,
            "release": session.release,
            "cancel": session.cancel,
            "resolve": session.resolve,
            "close_phase": session.close_phase,
            "checkpoint": session.checkpoint,
            "reconcile_agent": session.reconcile_agent,
            "resume": session.resume,
            "branch": session.branch,
            "control": session.control,
            "memory": session.memory,
            "transfer": session.transfer,
            "external_event": session.external_event,
            "finalize_outcomes": session.finalize_outcomes,
        }
        operation = cmd.operation.value
        try:
            import inspect

            inspect.signature(allowed[operation]).bind(session_id, who, **a)
            result = allowed[operation](session_id, who, **a)
        except TypeError:
            raise HTTPException(422, "invalid command arguments") from None
        return {"operation": operation, "accepted": True, "result": result}

    @app.get(
        "/v1/sessions/{session_id}/invocations",
        tags=["Sessions"],
        summary="List durable agent invocations",
    )
    def invocations(session_id: str, who=Depends(actor), limit: int = Query(100, ge=1, le=1000)):
        with store.transaction() as db:
            store.environment(db, session_id, who, "session.read")
            records = db.execute(
                "SELECT id,revision,participant,generation,status FROM agent_work WHERE environment=? "
                "AND (? = 1 OR participant=?) ORDER BY revision DESC,participant LIMIT ?",
                (session_id, int(who.full_evidence), who.participant, limit),
            ).fetchall()
            return {"items": [dict(record) for record in records]}

    @app.post(
        "/v1/sessions/{session_id}/operations",
        tags=["Sessions"],
        summary="Prepare an external operation",
        response_model=OperationIntentResponse,
    )
    def prepare(session_id: str, body: OperationIntentRequest, who=Depends(actor)):
        return Operations(store).prepare(session_id, who, **body.model_dump())

    @app.get(
        "/v1/sessions/{session_id}/evidence",
        tags=["Evidence"],
        summary="Read environment-session evidence events",
        response_model=EvidencePage,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
    )
    def evidence(
        session_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        last_event_id: str | None = Header(None),
        accept: str = Header("application/json"),
        who=Depends(actor),
    ):
        if last_event_id:
            try:
                after = max(after, int(last_event_id))
            except ValueError:
                raise HTTPException(422, "invalid event cursor") from None
        items = store.events(session_id, who, after, limit)
        if "text/event-stream" in accept:
            # Finite resumable SSE pages: reconnect with Last-Event-ID.
            body = "".join(f"id: {e['seq']}\nevent: evidence\ndata: {encode(e)}\n\n" for e in items)
            return Response(body or ": caught up\n\n", media_type="text/event-stream")
        return {"events": items, "cursor": items[-1]["seq"] if items else after}

    @app.post(
        "/v1/sessions/{session_id}/artifacts",
        tags=["Evidence"],
        summary="Store an artifact",
    )
    async def artifact(session_id: str, request: Request, who=Depends(actor)):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 16777216:
                raise PayloadTooLarge("artifact exceeds the 16 MiB limit")
            chunks.append(chunk)
        return store.artifact(
            session_id,
            who,
            b"".join(chunks),
            media_type=request.headers.get("content-type", "application/octet-stream"),
        )

    @app.get(
        "/v1/sessions/{session_id}/artifacts/{artifact_id}",
        tags=["Evidence"],
        summary="Download an artifact",
    )
    def read_artifact(session_id: str, artifact_id: str, who=Depends(actor)):
        data, media = store.read_artifact(session_id, who, artifact_id)
        del media
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{artifact_id}"'},
        )

    @app.get(
        "/v1/sessions/{session_id}/scores",
        tags=["Evaluation"],
        summary="List score reports",
    )
    def scores(session_id: str, who=Depends(actor)):
        return {"items": store.reports(session_id, who)}

    @app.post(
        "/v1/sessions/{session_id}/scores",
        tags=["Evaluation"],
        summary="Publish a score report",
        status_code=201,
    )
    def publish_score(session_id: str, body: ScoreReport, response: Response, who=Depends(actor)):
        stored = store.report(session_id, who, body)
        response.headers["Location"] = f"/v1/sessions/{session_id}/scores"
        etag(response, stored["hash"])
        return stored

    @app.get(
        "/v1/sessions/{session_id}/turn-series",
        tags=["Evaluation"],
        summary="Read bounded turn-level evidence series",
    )
    def session_turn_series(
        session_id: str,
        start_turn: int = Query(1, ge=1),
        end_turn: int | None = Query(None, ge=1),
        max_points: int = Query(300, ge=20, le=1000),
        who=Depends(actor),
    ):
        if end_turn is not None and end_turn < start_turn:
            raise HTTPException(422, "end_turn must be greater than or equal to start_turn")
        return turn_series(
            store,
            session_id,
            who,
            start_turn=start_turn,
            end_turn=end_turn,
            max_points=max_points,
        )

    # -- Policies -----------------------------------------------------------
    @app.get(
        "/v1/policies",
        tags=["Trajectories"],
        summary="List derived policy resources",
        response_model=PolicyPage,
    )
    def policy_index(
        request: Request,
        response: Response,
        who=Depends(actor),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        items, following = slice_page(policies.list(who, limit=1000), cursor, limit)
        return link_header(response, page(request, items, following))

    @app.get(
        "/v1/policies/{policy_id}",
        tags=["Trajectories"],
        summary="Get a derived policy resource",
        response_model=Policy,
    )
    def policy(policy_id: str, response: Response, who=Depends(actor)):
        resource = policies.get(policy_id, who)
        etag(response, resource.status.digest)
        return resource

    # -- Trajectories -------------------------------------------------------
    @app.get(
        "/v1/trajectories",
        tags=["Trajectories"],
        summary="List native and imported trajectories",
        response_model=TrajectoryPage,
    )
    def trajectory_index(
        request: Request,
        response: Response,
        who=Depends(actor),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        items, following = trajectories.list_page(who, limit, cursor)
        return link_header(response, page(request, items, following))

    @app.get(
        "/v1/trajectories/{trajectory_id}",
        tags=["Trajectories"],
        summary="Get a portable trajectory",
        response_model=Trajectory,
    )
    def trajectory_resource(trajectory_id: str, response: Response, who=Depends(actor)):
        resource = trajectories.get(trajectory_id, who)
        etag(response, resource.status.trajectory_digest)
        return resource

    @app.get(
        "/v1/trajectories/{trajectory_id}/records",
        tags=["Trajectories"],
        summary="Page through trajectory records",
        response_model=TrajectoryRecordPage,
    )
    def trajectory_records(
        trajectory_id: str,
        response: Response,
        who=Depends(actor),
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
    ):
        # Ordered evidence keeps its durable integer sequence cursor.
        result = trajectories.records_page(trajectory_id, who, after=after, limit=limit)
        if result.has_more:
            response.headers["X-Next-Cursor"] = str(result.cursor)
            response.headers["Link"] = (
                f'</v1/trajectories/{trajectory_id}/records?after={result.cursor}&limit={limit}>; rel="next"'
            )
        return result

    @app.get(
        "/v1/trajectories/{trajectory_id}/scores",
        tags=["Evaluation"],
        summary="List score reports referenced by a trajectory",
    )
    def trajectory_scores(trajectory_id: str, who=Depends(actor)):
        trajectories.get(trajectory_id, who)
        with store.transaction() as db:
            native = db.execute("SELECT 1 FROM environments WHERE id=?", (trajectory_id,)).fetchone()
        return {"items": store.reports(trajectory_id, who) if native is not None else []}

    @app.get(
        "/v1/trajectories/{trajectory_id}/snapshots",
        tags=["Trajectories"],
        summary="List snapshot boundaries frozen for a trajectory",
        response_model=SnapshotPage,
    )
    def trajectory_snapshots(
        trajectory_id: str,
        request: Request,
        response: Response,
        who=Depends(actor),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        items, following = slice_page(
            trajectories.list_snapshots(trajectory_id, who, limit=1000), cursor, limit
        )
        return link_header(response, page(request, items, following))

    @app.post(
        "/v1/trajectories/{trajectory_id}/snapshots",
        tags=["Trajectories"],
        summary="Freeze an authorized trajectory snapshot",
        response_model=TrajectorySnapshot,
        status_code=201,
    )
    def freeze_trajectory_snapshot(trajectory_id: str, response: Response, who=Depends(actor)):
        snapshot = trajectories.freeze(trajectory_id, who)
        response.headers["Location"] = f"/v1/snapshots/{snapshot.metadata.id}"
        return snapshot

    # -- Snapshots ----------------------------------------------------------
    @app.get(
        "/v1/snapshots",
        tags=["Trajectories"],
        summary="List immutable trajectory snapshots",
        response_model=SnapshotPage,
    )
    def snapshots(
        request: Request,
        response: Response,
        who=Depends(actor),
        trajectory: str | None = Query(None, min_length=1, max_length=200),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        items, following = slice_page(
            trajectories.list_all_snapshots(who, trajectory=trajectory, limit=1000), cursor, limit
        )
        return link_header(response, page(request, items, following))

    @app.get(
        "/v1/snapshots/{snapshot_id}",
        tags=["Trajectories"],
        summary="Get an immutable trajectory snapshot",
        response_model=TrajectorySnapshot,
    )
    def trajectory_snapshot(snapshot_id: str, response: Response, who=Depends(actor)):
        resource = trajectories.get_snapshot(snapshot_id, who)
        etag(response, resource.status.snapshot_digest)
        return resource

    @app.get(
        "/v1/snapshots/{snapshot_id}/records",
        tags=["Trajectories"],
        summary="Read snapshot records as a JSON page or streamed NDJSON",
        response_model=TrajectoryRecordPage,
        responses={200: {"content": {"application/x-ndjson": {"schema": {"type": "string"}}}}},
    )
    def snapshot_records(
        snapshot_id: str,
        accept: str = Header("application/json"),
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        who=Depends(actor),
    ):
        """Content negotiation replaces an `/export` verb path.

        The typed JSON cursor page is the generated-client response; NDJSON
        streaming is the same authenticated `GET` with an explicit `Accept`.
        """

        if "application/x-ndjson" in accept:
            return StreamingResponse(
                (encode(row) + "\n" for row in trajectories.export_snapshot(snapshot_id, who)),
                media_type="application/x-ndjson",
            )
        return trajectories.snapshot_records_page(snapshot_id, who, after=after, limit=limit)

    # -- Datasets -----------------------------------------------------------
    @app.post(
        "/v1/datasets",
        tags=["Trajectories"],
        summary="Freeze a training-entitled trajectory dataset",
        response_model=TrajectoryDataset,
        status_code=201,
    )
    def freeze_trajectory_dataset(body: DatasetCreate, response: Response, who=Depends(actor)):
        dataset = training.freeze_dataset(body.name, body.trajectories, who)
        response.headers["Location"] = f"/v1/datasets/{dataset.metadata.id}"
        return dataset

    @app.get(
        "/v1/datasets",
        tags=["Trajectories"],
        summary="List immutable trajectory datasets",
        response_model=DatasetPage,
    )
    def trajectory_datasets(
        request: Request,
        response: Response,
        who=Depends(actor),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        items, following = slice_page(training.list_datasets(who, limit=1000), cursor, limit)
        return link_header(response, page(request, items, following))

    @app.get(
        "/v1/datasets/{dataset_id}",
        tags=["Trajectories"],
        summary="Get an immutable trajectory dataset",
        response_model=TrajectoryDataset,
    )
    def trajectory_dataset(dataset_id: str, response: Response, who=Depends(actor)):
        resource = training.get_dataset(dataset_id, who)
        etag(response, resource.status.dataset_digest)
        return resource

    @app.get(
        "/v1/datasets/{dataset_id}/records",
        tags=["Trajectories"],
        summary="Read dataset records as a JSON page or streamed NDJSON",
        response_model=TrajectoryRecordPage,
        responses={200: {"content": {"application/x-ndjson": {"schema": {"type": "string"}}}}},
    )
    def dataset_records(
        dataset_id: str,
        accept: str = Header("application/json"),
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        who=Depends(actor),
    ):
        if "application/x-ndjson" in accept:
            return StreamingResponse(
                (encode(row) + "\n" for row in training.export_dataset(dataset_id, who)),
                media_type="application/x-ndjson",
            )
        return training.dataset_records_page(dataset_id, who, after=after, limit=limit)

    # -- Training runs ------------------------------------------------------
    @app.get(
        "/v1/training-runs",
        tags=["Trajectories"],
        summary="List recorded local training results",
        response_model=TrainingRunPage,
    )
    def training_runs(
        request: Request,
        response: Response,
        who=Depends(actor),
        dataset: str | None = Query(None, min_length=1, max_length=200),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        items, following = slice_page(training.list_runs(who, dataset=dataset, limit=1000), cursor, limit)
        return link_header(response, page(request, items, following))

    @app.get(
        "/v1/training-runs/{training_run_id}",
        tags=["Trajectories"],
        summary="Get a recorded local training result",
        response_model=TrainingRun,
    )
    def training_run(training_run_id: str, response: Response, who=Depends(actor)):
        resource = training.get_run(training_run_id, who)
        etag(response, resource.spec.dataset_digest)
        return resource

    # -- Sources ------------------------------------------------------------
    @app.get(
        "/v1/sources",
        tags=["Sources"],
        summary="List registered external sources",
        response_model=SourcePage,
    )
    def sources(
        request: Request,
        response: Response,
        who=Depends(actor),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=1, max_length=200),
    ):
        items, following = slice_page(
            trajectories.list_sources(who, limit=1000), cursor, limit, key=lambda item: item.source
        )
        return link_header(response, page(request, items, following))

    @app.get(
        "/v1/sources/{source_id}",
        tags=["Sources"],
        summary="Get a registered external source",
        response_model=SourceStatus,
    )
    def source(source_id: str, who=Depends(actor)):
        return trajectories.source_status(source_id, who)

    @app.get(
        "/v1/sources/{source_id}/status",
        tags=["Sources"],
        summary="Inspect source collection and execution status",
        response_model=SourceStatus,
    )
    def source_status(source_id: str, who=Depends(actor)):
        return trajectories.source_status(source_id, who)

    @app.get(
        "/v1/sources/{source_id}/records",
        tags=["Sources"],
        summary="Page through ingested source records",
        response_model=TrajectoryRecordPage,
    )
    def source_records(
        source_id: str,
        response: Response,
        who=Depends(actor),
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
    ):
        result = trajectories.records_page(source_id, who, after=after, limit=limit)
        if result.has_more:
            response.headers["X-Next-Cursor"] = str(result.cursor)
            response.headers["Link"] = (
                f'</v1/sources/{source_id}/records?after={result.cursor}&limit={limit}>; rel="next"'
            )
        return result

    @app.post(
        "/v1/sources",
        tags=["Sources"],
        summary="Register an external trajectory source",
        response_model=SourceRegistrationReceipt,
        status_code=201,
        openapi_extra={"x-capability": capability_module.HISTORICAL_INGESTION},
    )
    def register_source(body: SourceRegistration, response: Response, who=Depends(actor)):
        require_capability(capability_module.HISTORICAL_INGESTION)
        receipt = trajectories.register_source(body, who)
        response.headers["Location"] = f"/v1/sources/{receipt.id}"
        return receipt

    @app.post(
        "/v1/sources/{source_id}/records",
        tags=["Sources"],
        summary="Ingest a bounded source batch",
        response_model=SourceAcknowledgement,
        openapi_extra={"x-capability": capability_module.HISTORICAL_INGESTION},
    )
    def ingest_source_records(source_id: str, body: SourceIngestionBatch, who=Depends(actor)):
        require_capability(capability_module.HISTORICAL_INGESTION)
        return trajectories.ingest(source_id, body.records, who)

    @app.post(
        "/v1/sources/{source_id}/status-reports",
        tags=["Sources"],
        summary="Declare source collection and execution status",
        response_model=SourceStatusUpdate,
        status_code=201,
        openapi_extra={"x-capability": capability_module.HISTORICAL_INGESTION},
    )
    def report_source_status(source_id: str, body: SourceStatusUpdate, who=Depends(actor)):
        require_capability(capability_module.HISTORICAL_INGESTION)
        return trajectories.update_source_status(source_id, body, who)

    # -- Comparisons --------------------------------------------------------
    @app.post(
        "/v1/comparisons",
        tags=["Evaluation"],
        summary="Compare environment sessions",
    )
    def comparison(body: ComparisonRequest, who=Depends(actor)):
        """Return a derived comparison. No comparison authority is persisted."""

        return compare(store, list(body.sessions), who)

    # -- Activity -----------------------------------------------------------
    def activity_response(items, after, accept):
        cursor = items[-1]["id"] if items else after
        if "text/event-stream" in accept:
            body = "retry: 2000\n\n"
            body += "".join(
                f"id: {event['id']}\nevent: {event['kind']}\ndata: {encode(event)}\n\n" for event in items
            )
            body += ": heartbeat\n\n"
            return Response(body, media_type="text/event-stream")
        return {"events": items, "cursor": cursor}

    def activity_cursor(after, last_event_id):
        if last_event_id:
            try:
                return max(after, int(last_event_id))
            except ValueError:
                raise HTTPException(422, "invalid event cursor") from None
        return after

    @app.get(
        "/v1/activity",
        tags=["Activity"],
        summary="Read tenant activity",
        response_model=ActivityPage,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
    )
    def global_activity(
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        last_event_id: str | None = Header(None),
        accept: str = Header("application/json"),
        who=Depends(actor),
    ):
        after = activity_cursor(after, last_event_id)
        return activity_response(store.activity(who, after, limit), after, accept)

    @app.get(
        "/v1/activity/hierarchy",
        tags=["Activity"],
        summary="Get the current ownership hierarchy",
        response_model=ActivityHierarchy,
    )
    def activity_hierarchy(who=Depends(actor)):
        return store.activity_hierarchy(who)

    @app.get(
        "/v1/experiments/{experiment_id}/activity",
        tags=["Activity"],
        summary="Read activity for an experiment",
        response_model=ActivityPage,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
    )
    def experiment_activity(
        experiment_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        last_event_id: str | None = Header(None),
        accept: str = Header("application/json"),
        who=Depends(actor),
    ):
        after = activity_cursor(after, last_event_id)
        return activity_response(store.activity(who, after, limit, experiment=experiment_id), after, accept)

    @app.get(
        "/v1/sessions/{session_id}/activity",
        tags=["Activity"],
        summary="Read activity for an environment session",
        response_model=ActivityPage,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
    )
    def session_activity(
        session_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        last_event_id: str | None = Header(None),
        accept: str = Header("application/json"),
        who=Depends(actor),
    ):
        after = activity_cursor(after, last_event_id)
        return activity_response(store.activity(who, after, limit, environment=session_id), after, accept)

    # -- Viewer shell -------------------------------------------------------
    @app.get("/", include_in_schema=False)
    @app.get("/overview", include_in_schema=False)
    @app.get("/comparisons", include_in_schema=False)
    @app.get("/experiments", include_in_schema=False)
    @app.get("/sessions", include_in_schema=False)
    @app.get("/trajectories", include_in_schema=False)
    def viewer():
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/sessions/{session_id}", include_in_schema=False)
    def viewer_session(session_id: str):
        del session_id
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/sessions/{session_id}/{section}", include_in_schema=False)
    def viewer_session_section(session_id: str, section: str):
        del session_id
        if section not in ("overview", "turns", "progression", "trajectory", "configuration"):
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/experiments/{experiment_id}", include_in_schema=False)
    def viewer_experiment(experiment_id: str):
        del experiment_id
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/experiments/{experiment_id}/scenarios/{scenario_id}", include_in_schema=False)
    def viewer_experiment_scenario(experiment_id: str, scenario_id: str):
        del experiment_id, scenario_id
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/experiments/{experiment_id}/{section}", include_in_schema=False)
    def viewer_experiment_section(experiment_id: str, section: str):
        del experiment_id
        if section not in ("overview", "scenarios", "sessions", "trajectories", "configuration"):
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/trajectories/{trajectory_id}", include_in_schema=False)
    def viewer_trajectory(trajectory_id: str):
        del trajectory_id
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/trajectories/{trajectory_id}/{section}", include_in_schema=False)
    def viewer_trajectory_section(trajectory_id: str, section: str):
        del trajectory_id
        if section not in ("overview", "records", "snapshots", "provenance"):
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/viewer/{file}", include_in_schema=False)
    def asset(file: str):
        if file not in ("app.js", "timeline.js", "client.js", "types.js", "style.css"):
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / "viewer" / file)

    return app

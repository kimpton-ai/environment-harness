"""Supplier-owned environment service. Run behind HTTPS outside loopback."""

import json
import logging
import secrets
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

from .contracts import Action, ExperimentSpec, ScoreReport
from .coordinator import advance
from .errors import BudgetExceeded, Conflict, Forbidden, HarnessError, Unsupported
from .evaluation import compare, rollouts, turn_series
from .operations import Operations
from .store import encode

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
    model_config = ConfigDict(extra="forbid")
    participant: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    ttl: int = Field(
        default=3600,
        ge=1,
        description="Credential lifetime in seconds. Values above 86400 are capped at 86400.",
    )


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

OPENAPI_TAGS = [
    {"name": "Service", "description": "Service health and the active environment contract."},
    {
        "name": "Environment sessions",
        "description": "Create, list, inspect, observe, and control environment sessions.",
    },
    {
        "name": "Evidence",
        "description": "Read event evidence and store or retrieve session artifacts.",
    },
    {
        "name": "Activity",
        "description": "Read resumable activity feeds and the hierarchy snapshot used by the viewer.",
    },
    {
        "name": "Evaluation",
        "description": "Publish score reports, export evidence, and compare environment sessions.",
    },
]


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


def create_app(session, *, local_access=None):
    store = session.store
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
        description="Opaque EnvironmentHarness credential issued for a scoped principal.",
    )

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        request.state.request_id = secrets.token_hex(16)
        if request.headers.get("origin") and request.headers["origin"] != str(request.base_url).rstrip("/"):
            response = _error_response(
                request,
                "cross_origin_denied",
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
                        "request_too_large",
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
        status = (
            403
            if isinstance(error, Forbidden)
            else 409
            if isinstance(error, Conflict)
            else 422
            if isinstance(error, Unsupported)
            else 402
            if isinstance(error, BudgetExceeded)
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
        return _error_response(request, "invalid_request", "Invalid request", 422)

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
            "invalid_request",
            "Request validation failed",
            422,
            details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_failure(request, error):
        status = error.status_code
        code = {
            400: "invalid_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            413: "request_too_large",
            422: "invalid_request",
            503: "unavailable",
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

    @app.get("/health", tags=["Service"], summary="Check service health")
    def health():
        return {"status": "ok", "protocol": "environment-session.v1"}

    @app.get("/viewer/config", include_in_schema=False)
    def viewer_config():
        return {"authentication": "local" if local_access is not None else "credential"}

    @app.get(
        "/v1/environment",
        tags=["Service"],
        summary="Get the active environment contract",
        openapi_extra={"x-roles": ["researcher", "worker", "scorer", "agent"]},
    )
    def environment(who=Depends(actor)):
        return session.environment.spec

    @app.post(
        "/v1/environments",
        tags=["Environment sessions"],
        summary="Create an environment session",
        openapi_extra={"x-roles": ["researcher"]},
    )
    def create(spec: ExperimentSpec, x_operation_id: str = Header(), who=Depends(actor)):
        return session.create(spec, who, environment_id=x_operation_id)

    @app.get(
        "/v1/environments",
        tags=["Environment sessions"],
        summary="List environment sessions",
        openapi_extra={"x-roles": ["researcher"]},
        responses={
            200: {
                "description": "A descending page of environment sessions",
                "headers": {
                    "X-Next-Cursor": {
                        "description": "Pass this opaque cursor to retrieve the next page.",
                        "schema": {"type": "string"},
                    },
                    "Link": {
                        "description": 'Relative next-page link with rel="next".',
                        "schema": {"type": "string"},
                    },
                },
            }
        },
    )
    def environments(
        response: Response,
        who=Depends(actor),
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, min_length=32, max_length=32, pattern=r"^[0-9a-f]+$"),
    ):
        page, next_cursor = session.list_page(who, limit, cursor)
        if next_cursor is not None:
            response.headers["X-Next-Cursor"] = next_cursor
            response.headers["Link"] = f'</v1/environments?limit={limit}&cursor={next_cursor}>; rel="next"'
        return page

    @app.get(
        "/v1/environments/{environment}",
        tags=["Environment sessions"],
        summary="Get an environment session",
        openapi_extra={"x-roles": ["researcher", "worker", "scorer", "agent"]},
    )
    def get(environment: str, who=Depends(actor)):
        return session.get(environment, who)

    @app.get(
        "/v1/environments/{environment}/observation",
        tags=["Environment sessions"],
        summary="Observe an environment session",
        openapi_extra={"x-roles": ["researcher", "worker", "scorer", "agent"]},
    )
    def observation(environment: str, participant: str | None = None, who=Depends(actor)):
        return session.observe(environment, who, participant)

    @app.post(
        "/v1/environments/{environment}/actions",
        tags=["Environment sessions"],
        summary="Submit a participant action",
        openapi_extra={"x-roles": ["agent"]},
    )
    def action(environment: str, action: Action, who=Depends(actor)):
        return session.submit(environment, who, action)

    @app.get(
        "/v1/environments/{environment}/events",
        tags=["Evidence"],
        summary="Read environment-session evidence events",
        openapi_extra={"x-roles": ["researcher", "worker", "scorer", "agent"]},
    )
    def events(
        environment: str,
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
        page = store.events(environment, who, after, limit)
        if "text/event-stream" in accept:
            # Finite resumable SSE pages: reconnect with Last-Event-ID. No viewer backpressure on writes.
            body = "".join(f"id: {e['seq']}\nevent: evidence\ndata: {encode(e)}\n\n" for e in page)
            return Response(body or ": caught up\n\n", media_type="text/event-stream")
        return {"events": page, "cursor": page[-1]["seq"] if page else after}

    def activity_response(page, after, accept):
        cursor = page[-1]["id"] if page else after
        if "text/event-stream" in accept:
            body = "retry: 2000\n\n"
            body += "".join(
                f"id: {event['id']}\nevent: {event['kind']}\ndata: {encode(event)}\n\n" for event in page
            )
            body += ": heartbeat\n\n"
            return Response(body, media_type="text/event-stream")
        return {"events": page, "cursor": cursor}

    def activity_cursor(after, last_event_id):
        if last_event_id:
            try:
                return max(after, int(last_event_id))
            except ValueError:
                raise HTTPException(422, "invalid event cursor") from None
        return after

    @app.get(
        "/v1/activity/events",
        tags=["Activity"],
        summary="Read tenant activity",
        openapi_extra={"x-roles": ["researcher", "worker"]},
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
        "/v1/activity/snapshot",
        tags=["Activity"],
        summary="Get the current activity hierarchy",
        openapi_extra={"x-roles": ["researcher", "worker"]},
    )
    def activity_snapshot(who=Depends(actor)):
        return store.activity_snapshot(who)

    @app.get(
        "/v1/experiments/{experiment}/events",
        tags=["Activity"],
        summary="Read activity for an experiment",
        openapi_extra={"x-roles": ["researcher", "worker"]},
    )
    def experiment_activity(
        experiment: str,
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        last_event_id: str | None = Header(None),
        accept: str = Header("application/json"),
        who=Depends(actor),
    ):
        after = activity_cursor(after, last_event_id)
        return activity_response(store.activity(who, after, limit, experiment=experiment), after, accept)

    @app.get(
        "/v1/environments/{environment}/activity",
        tags=["Activity"],
        summary="Read activity for an environment session",
        openapi_extra={"x-roles": ["researcher", "worker"]},
    )
    def environment_activity(
        environment: str,
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        last_event_id: str | None = Header(None),
        accept: str = Header("application/json"),
        who=Depends(actor),
    ):
        after = activity_cursor(after, last_event_id)
        return activity_response(store.activity(who, after, limit, environment=environment), after, accept)

    @app.get(
        "/v1/environments/{environment}/agent-work",
        tags=["Environment sessions"],
        summary="List agent work records",
        openapi_extra={"x-roles": ["researcher", "worker", "agent"]},
    )
    def agent_work(environment: str, who=Depends(actor), limit: int = Query(100, ge=1, le=1000)):
        with store.transaction() as db:
            store.environment(db, environment, who, ("researcher", "worker", "agent"))
            records = db.execute(
                "SELECT id,revision,participant,generation,status FROM agent_work WHERE environment=? "
                "AND (? <> 'agent' OR participant=?) ORDER BY revision DESC,participant LIMIT ?",
                (environment, who.role, who.participant, limit),
            ).fetchall()
            return {"work": [dict(record) for record in records]}

    @app.post(
        "/v1/environments/{environment}/commands",
        tags=["Environment sessions"],
        summary="Run an environment-session command",
        openapi_extra={
            "x-roles": ["researcher", "worker", "agent"],
            "x-command-operations": [operation.value for operation in CommandOperation],
        },
    )
    def command(environment: str, cmd: Command, who=Depends(actor)):
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

            inspect.signature(allowed[operation]).bind(environment, who, **a)
            result = allowed[operation](environment, who, **a)
        except TypeError:
            raise HTTPException(422, "invalid command arguments") from None
        return result

    @app.post(
        "/v1/environments/{environment}/credentials",
        tags=["Environment sessions"],
        summary="Issue a participant credential",
        response_model=CredentialResponse,
        openapi_extra={"x-roles": ["researcher"]},
    )
    def credential(environment: str, body: CredentialRequest, who=Depends(actor)):
        with store.transaction() as db:
            row = store.environment(db, environment, who, ("researcher",))
            participants = json.loads(row["participants"])
            participant = body.participant
            if participant not in participants:
                raise Forbidden("unknown participant")
            p = participants[participant]
            principal = who.model_copy(
                update={
                    "environment": environment,
                    "role": "agent",
                    "subject": p["controller"],
                    "participant": participant,
                    "generation": p["generation"],
                }
            )
        return {"token": store.issue(principal, min(body.ttl, 86400))}

    @app.post(
        "/v1/environments/{environment}/operations",
        tags=["Environment sessions"],
        summary="Prepare an external operation",
        response_model=OperationIntentResponse,
        openapi_extra={"x-roles": ["agent"]},
    )
    def prepare(environment: str, body: OperationIntentRequest, who=Depends(actor)):
        return Operations(store).prepare(environment, who, **body.model_dump())

    @app.post(
        "/v1/environments/{environment}/artifacts",
        tags=["Evidence"],
        summary="Store an artifact",
        openapi_extra={"x-roles": ["researcher", "worker", "scorer", "agent"]},
    )
    async def artifact(environment: str, request: Request, who=Depends(actor)):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 16777216:
                raise HTTPException(413, "artifact too large")
            chunks.append(chunk)
        return store.artifact(
            environment,
            who,
            b"".join(chunks),
            media_type=request.headers.get("content-type", "application/octet-stream"),
        )

    @app.get(
        "/v1/environments/{environment}/artifacts/{key}",
        tags=["Evidence"],
        summary="Download an artifact",
        openapi_extra={"x-roles": ["researcher", "worker", "scorer", "agent"]},
    )
    def read_artifact(environment: str, key: str, who=Depends(actor)):
        data, media = store.read_artifact(environment, who, key)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{key}"'},
        )

    @app.get(
        "/v1/environments/{environment}/reports",
        tags=["Evaluation"],
        summary="List score reports",
        openapi_extra={"x-roles": ["researcher", "scorer"]},
    )
    def reports(environment: str, who=Depends(actor)):
        return store.reports(environment, who)

    @app.get(
        "/v1/environments/{environment}/turn-series",
        tags=["Evaluation"],
        summary="Read bounded turn-level evidence series",
        openapi_extra={"x-roles": ["researcher", "scorer"]},
    )
    def environment_turn_series(
        environment: str,
        start_turn: int = Query(1, ge=1),
        end_turn: int | None = Query(None, ge=1),
        max_points: int = Query(300, ge=20, le=1000),
        who=Depends(actor),
    ):
        if end_turn is not None and end_turn < start_turn:
            raise HTTPException(422, "end_turn must be greater than or equal to start_turn")
        return turn_series(
            store,
            environment,
            who,
            start_turn=start_turn,
            end_turn=end_turn,
            max_points=max_points,
        )

    @app.post(
        "/v1/environments/{environment}/reports",
        tags=["Evaluation"],
        summary="Publish a score report",
        openapi_extra={"x-roles": ["researcher", "scorer"]},
    )
    def report(environment: str, body: ScoreReport, who=Depends(actor)):
        return store.report(environment, who, body)

    @app.get(
        "/v1/environments/{environment}/export",
        tags=["Evaluation"],
        summary="Export environment-session records",
        openapi_extra={"x-roles": ["researcher", "worker", "scorer", "agent"]},
    )
    def export(environment: str, format: str = "evidence", who=Depends(actor)):
        session.get(environment, who)
        if format == "training":
            source = rollouts(store, environment, who)
            # Validate entitlement before starting a streaming HTTP response.
            first = next(source, None)

            def rows():
                if first is not None:
                    yield encode(first) + "\n"
                for row in source:
                    yield encode(row) + "\n"
        elif format == "evidence":

            def rows():
                for row in store.replay(environment, who):
                    yield encode(row) + "\n"
        else:
            raise HTTPException(422, "unknown export format")
        return StreamingResponse(rows(), media_type="application/x-ndjson")

    @app.post(
        "/v1/compare",
        tags=["Evaluation"],
        summary="Compare environment sessions",
        openapi_extra={"x-roles": ["researcher", "scorer"]},
    )
    def comparison(body: dict, who=Depends(actor)):
        ids = body.get("environments", [])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise HTTPException(422, "supply 1 to 100 environments")
        return compare(store, ids, who)

    @app.get("/", include_in_schema=False)
    @app.get("/home", include_in_schema=False)
    @app.get("/compare", include_in_schema=False)
    def viewer():
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/session/{environment}", include_in_schema=False)
    def viewer_session(environment: str):
        del environment
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/session/{environment}/{section}", include_in_schema=False)
    def viewer_session_section(environment: str, section: str):
        del environment
        if section not in ("overview", "turns", "progression", "reports"):
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/experiment/{experiment}", include_in_schema=False)
    def viewer_experiment(experiment: str):
        del experiment
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/viewer/{file}", include_in_schema=False)
    def asset(file: str):
        if file not in ("app.js", "timeline.js", "client.js", "types.js", "style.css"):
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / "viewer" / file)

    return app

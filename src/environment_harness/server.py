"""Supplier-owned environment service. Run behind HTTPS outside loopback."""

import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .contracts import Action, ExperimentSpec, ScoreReport
from .coordinator import advance
from .errors import BudgetExceeded, Conflict, Forbidden, HarnessError, Unsupported
from .evaluation import compare, rollouts
from .operations import Operations
from .store import encode


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def create_app(session, *, local_login=None):
    store = session.store
    app = FastAPI(title="Environment session service", version="1.0.0")

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.headers.get("origin") and request.headers["origin"] != str(request.base_url).rstrip("/"):
            return JSONResponse({"error": "cross_origin_denied"}, status_code=403)
        if request.method in ("POST", "PUT", "PATCH"):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16777216:
                    return JSONResponse({"error": "request_too_large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
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
        return JSONResponse({"error": error.code, "detail": str(error)}, status_code=status)

    @app.exception_handler(ValueError)
    async def invalid_value(request, error):
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    def actor(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer credential required")
        return store.authenticate(authorization[7:])

    if local_login is not None:

        @app.post("/local/connect", include_in_schema=False)
        def local_connect(request: Request, x_local_login: str = Header(default="")):
            if (
                request.client is None
                or request.client.host not in ("127.0.0.1", "::1")
                or str(request.base_url).rstrip("/") != local_login.origin
                or request.headers.get("origin") != local_login.origin
            ):
                raise HTTPException(403, "Local connection requires the loopback viewer origin")
            return {"token": local_login.redeem(x_local_login)}

    @app.get("/health")
    def health():
        return {"status": "ok", "protocol": "environment-session.v1"}

    @app.get("/v1/environment")
    def environment(who=Depends(actor)):
        return session.environment.spec

    @app.post("/v1/environments")
    def create(spec: ExperimentSpec, x_operation_id: str = Header(), who=Depends(actor)):
        return session.create(spec, who, environment_id=x_operation_id)

    @app.get("/v1/environments")
    def environments(who=Depends(actor), limit: int = Query(100, ge=1, le=1000)):
        return session.list(who, limit)

    @app.get("/v1/environments/{environment}")
    def get(environment: str, who=Depends(actor)):
        return session.get(environment, who)

    @app.get("/v1/environments/{environment}/observation")
    def observation(environment: str, participant: str | None = None, who=Depends(actor)):
        return session.observe(environment, who, participant)

    @app.post("/v1/environments/{environment}/actions")
    def action(environment: str, action: Action, who=Depends(actor)):
        return session.submit(environment, who, action)

    @app.get("/v1/environments/{environment}/events")
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

    @app.get("/v1/environments/{environment}/agent-work")
    def agent_work(environment: str, who=Depends(actor), limit: int = Query(100, ge=1, le=1000)):
        with store.transaction() as db:
            store.environment(db, environment, who, ("researcher", "worker", "agent"))
            records = db.execute(
                "SELECT id,revision,participant,generation,status FROM agent_work WHERE environment=? "
                "AND (? <> 'agent' OR participant=?) ORDER BY revision DESC,participant LIMIT ?",
                (environment, who.role, who.participant, limit),
            ).fetchall()
            return {"work": [dict(record) for record in records]}

    @app.post("/v1/environments/{environment}/commands")
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
        if cmd.operation not in allowed:
            raise Unsupported("unknown command")
        try:
            import inspect

            inspect.signature(allowed[cmd.operation]).bind(environment, who, **a)
            result = allowed[cmd.operation](environment, who, **a)
        except TypeError:
            raise HTTPException(422, "invalid command arguments") from None
        return result

    @app.post("/v1/environments/{environment}/credentials")
    def credential(environment: str, body: dict, who=Depends(actor)):
        with store.transaction() as db:
            row = store.environment(db, environment, who, ("researcher",))
            participants = json.loads(row["participants"])
            participant = body.get("participant")
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
        return {"token": store.issue(principal, min(int(body.get("ttl", 3600)), 86400))}

    @app.post("/v1/environments/{environment}/operations")
    def prepare(environment: str, body: dict, who=Depends(actor)):
        return Operations(store).prepare(environment, who, **body)

    @app.post("/v1/environments/{environment}/artifacts")
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

    @app.get("/v1/environments/{environment}/artifacts/{key}")
    def read_artifact(environment: str, key: str, who=Depends(actor)):
        data, media = store.read_artifact(environment, who, key)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{key}"'},
        )

    @app.get("/v1/environments/{environment}/reports")
    def reports(environment: str, who=Depends(actor)):
        return store.reports(environment, who)

    @app.post("/v1/environments/{environment}/reports")
    def report(environment: str, body: ScoreReport, who=Depends(actor)):
        return store.report(environment, who, body)

    @app.get("/v1/environments/{environment}/export")
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

    @app.post("/v1/compare")
    def comparison(body: dict, who=Depends(actor)):
        ids = body.get("environments", [])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise HTTPException(422, "supply 1 to 100 environments")
        return compare(store, ids, who)

    @app.get("/")
    def viewer():
        return FileResponse(Path(__file__).parent / "viewer" / "index.html")

    @app.get("/viewer/{file}")
    def asset(file: str):
        if file not in ("app.js", "timeline.js", "client.js", "types.js", "style.css"):
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / "viewer" / file)

    return app

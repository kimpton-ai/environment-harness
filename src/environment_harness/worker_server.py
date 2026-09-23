"""Authenticated supplier container entrypoint with no evidence-store access."""

import argparse
import hmac
import os
import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .adapters.remote import MAX_BYTES, PROTOCOL, PROTOCOL_V2
from .contracts import EnvironmentSpecV2
from .store import encode
from .worker import dispatch


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["environment-worker.v1"]
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    method: Literal["spec", "initialize", "observe", "resolve", "intervene"]
    arguments: dict[str, Any]


class WorkerRequestV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["environment-worker.v2"]
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    method: Literal["spec", "initialize", "observe", "intervene", "plan_transition", "resolve_transition"]
    arguments: dict[str, Any]


def create_worker_app(environment, token):
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.responses import JSONResponse

    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("worker token requires at least 32 characters")
    is_v2 = isinstance(environment.spec, EnvironmentSpecV2)
    protocol = PROTOCOL_V2 if is_v2 else PROTOCOL
    path = "/v2/worker/call" if is_v2 else "/v1/worker/call"
    request_model = WorkerRequestV2 if is_v2 else WorkerRequest
    app = FastAPI(title="Environment worker", docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "protocol": protocol}

    def handle_call(request, authorization):
        # Authentication also covers health and malformed requests via middleware.
        if not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "unauthorized")
        try:
            with lock:
                result = dispatch(environment, request.method, request.arguments)
            response = {"protocol": protocol, "id": request.id, "result": result}
            if len(encode(response).encode()) > MAX_BYTES:
                raise ValueError("response too large")
            return response
        except Exception:
            return JSONResponse(
                {"protocol": protocol, "id": request.id, "error": "worker_failure"}, status_code=422
            )

    if is_v2:

        @app.post(path)
        def call_v2(request: request_model, authorization: str = Header()):
            return handle_call(request, authorization)

    else:

        @app.post(path)
        def call_v1(request: request_model, authorization: str = Header()):
            return handle_call(request, authorization)

    return app


def main():
    import uvicorn

    from .plugins import environment

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    token = os.environ.pop("ENVIRONMENT_WORKER_TOKEN", "")
    app = create_worker_app(environment(args.plugin), token)
    uvicorn.run(app, host="0.0.0.0", port=args.port, access_log=False)


if __name__ == "__main__":
    main()

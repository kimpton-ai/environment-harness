"""Bounded command, HTTP, instrumented model and MCP boundaries."""

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from ..client import EnvironmentClient
from ..errors import Conflict, Unsupported
from ..store import encode


class CommandAgent:
    """Trusted local command. Use DockerBackend for hostile agent programs."""

    def __init__(self, command, implementation, *, timeout=30, max_bytes=1048576):
        self.command, self.implementation = list(command), implementation
        self.timeout, self.max_bytes = timeout, max_bytes

    def act(self, observation):
        payload = encode(observation).encode()
        if len(payload) > self.max_bytes:
            raise Conflict("agent input exceeds limit")
        with tempfile.TemporaryDirectory(prefix="environment-agent-") as directory:
            # stdin is a file to avoid blocking on a child that never reads input.
            source = Path(directory) / "input.json"
            source.write_bytes(payload)
            with source.open("rb") as stdin:
                process = subprocess.Popen(
                    self.command,
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=directory,
                    env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHON_DOTENV_DISABLED": "1"},
                    start_new_session=True,
                )
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                output = bytearray()
                deadline = time.monotonic() + self.timeout
                try:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise Conflict("agent deadline exceeded")
                        ready = selector.select(min(remaining, 0.1))
                        if ready:
                            chunk = os.read(process.stdout.fileno(), 65536)
                            if not chunk:
                                break
                            output.extend(chunk)
                            if len(output) > self.max_bytes:
                                raise Conflict("agent output exceeds limit")
                    code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
                    if code:
                        raise Conflict("agent program failed")
                    result = json.loads(output)
                    if not isinstance(result, dict):
                        raise Conflict("agent returned malformed action")
                    return result
                finally:
                    selector.close()
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    process.stdout.close()

    def checkpoint(self):
        raise Unsupported("arbitrary command programs have no checkpoint hook")

    def restore(self, state):
        raise Unsupported("arbitrary command process memory cannot be restored")


class HTTPAgent:
    def __init__(self, endpoint, token, implementation, *, allow_loopback=False):
        self.client = EnvironmentClient(endpoint, token, allow_loopback=allow_loopback)
        self.implementation = implementation

    def act(self, observation):
        return self.client.request("POST", "/act", {"observation": observation})

    def checkpoint(self):
        raise Unsupported("HTTP agent must advertise explicit checkpoint hooks")

    def restore(self, state):
        raise Unsupported("HTTP agent continuation unsupported")


class InstrumentedModel:
    """Record exactly the supplied rendered request. Never infer missing token metadata."""

    def __init__(self, store, environment, principal, generate):
        self.store, self.environment, self.principal, self.generate = store, environment, principal, generate

    def call(self, request, *, context_changes=None):
        with self.store.transaction() as db:
            row = self.store.environment(db, self.environment, self.principal, ("agent",))
            self.store.append(
                db,
                self.environment,
                row["revision"],
                "model.request",
                {
                    "rendered_request": request,
                    "context_changes": context_changes,
                    "visibility": "instrumented",
                },
                (self.principal.participant,),
            )
        try:
            response = self.generate(request)
        except Exception:
            with self.store.transaction() as db:
                row = self.store.environment(db, self.environment, self.principal)
                self.store.append(
                    db,
                    self.environment,
                    row["revision"],
                    "model.failure",
                    {"category": "infrastructure"},
                    (self.principal.participant,),
                )
            raise
        with self.store.transaction() as db:
            row = self.store.environment(db, self.environment, self.principal)
            self.store.append(
                db,
                self.environment,
                row["revision"],
                "model.response",
                {
                    "response": response,
                    "token_ids": response.get("token_ids"),
                    "logprobs": response.get("logprobs"),
                },
                (self.principal.participant,),
            )
        return response


class MCPTools:
    """Async adapter for an already isolated and authenticated MCP ClientSession."""

    def __init__(self, session, allowed_tools, record):
        self.session, self.allowed_tools, self.record = session, frozenset(allowed_tools), record

    async def call(self, name, arguments):
        if name not in self.allowed_tools:
            raise Unsupported("tool not in participant allowlist")
        await self.record("tool.intent", {"name": name, "arguments": arguments})
        result = await self.session.call_tool(name, arguments=arguments)
        value = result.model_dump(mode="json")
        await self.record("tool.result", {"name": name, "result": value})
        return value

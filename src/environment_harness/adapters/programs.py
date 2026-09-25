"""Bounded command, HTTP, instrumented model and MCP boundaries."""

import json
import math
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ..client import EnvironmentClient
from ..errors import Conflict, Unsupported
from ..store import encode
from ._subprocess import popen_group, terminate_tree


class CommandAgent:
    """Trusted local command. Use DockerBackend for hostile agent programs."""

    managed_cancellation = True
    # Declared rather than inferred: the hooks below exist only to reject use.
    supports_checkpoint = False

    def __init__(self, command, implementation, *, timeout=30, max_bytes=1048576):
        self.command, self.implementation = list(command), implementation
        self.timeout, self.max_bytes = timeout, max_bytes

    def act(self, observation):
        return self.act_cancellable(observation, threading.Event())

    def act_cancellable(self, observation, cancel_event):
        payload = encode(observation).encode()
        if len(payload) > self.max_bytes:
            raise Conflict("agent input exceeds limit")
        with tempfile.TemporaryDirectory(prefix="environment-agent-") as directory:
            # stdin is a file to avoid blocking on a child that never reads input.
            source = Path(directory) / "input.json"
            source.write_bytes(payload)
            with source.open("rb") as stdin:
                if cancel_event.is_set():
                    raise Conflict("agent execution cancelled")
                process = popen_group(
                    self.command,
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=directory,
                    env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHON_DOTENV_DISABLED": "1"},
                )
                stdout = process.stdout
                if stdout is None:
                    self._terminate_group(process)
                    raise Conflict("agent output pipe is unavailable")
                chunks = queue.Queue()

                def read_output():
                    try:
                        while chunk := stdout.read(65536):
                            chunks.put(chunk)
                    except (OSError, ValueError) as exc:
                        chunks.put(exc)
                    finally:
                        chunks.put(None)

                reader = threading.Thread(target=read_output, daemon=True)
                reader.start()
                output = bytearray()
                deadline = time.monotonic() + self.timeout
                output_closed = False
                try:
                    while True:
                        if cancel_event.is_set():
                            raise Conflict("agent execution cancelled")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise Conflict("agent deadline exceeded")
                        if output_closed:
                            if process.poll() is not None:
                                break
                            cancel_event.wait(min(remaining, 0.05))
                            continue
                        try:
                            chunk = chunks.get(timeout=min(remaining, 0.1))
                        except queue.Empty:
                            continue
                        if chunk is None:
                            output_closed = True
                        elif isinstance(chunk, Exception):
                            raise Conflict("agent output pipe failed") from chunk
                        else:
                            output.extend(chunk)
                            if len(output) > self.max_bytes:
                                raise Conflict("agent output exceeds limit")
                    code = process.returncode
                    if code:
                        raise Conflict("agent program failed")
                    result = json.loads(output)
                    if not isinstance(result, dict):
                        raise Conflict("agent returned malformed action")
                    return result
                finally:
                    self._terminate_group(process)
                    stdout.close()
                    reader.join(timeout=1)

    @staticmethod
    def _terminate_group(process):
        terminate_tree(process)

    def checkpoint(self):
        raise Unsupported("arbitrary command programs have no checkpoint hook")

    def restore(self, state):
        raise Unsupported("arbitrary command process memory cannot be restored")


class HTTPAgent:
    supports_checkpoint = False

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

    def __init__(
        self,
        store,
        environment,
        access,
        generate,
        *,
        model="unidentified",
        tokenizer="unavailable",
        renderer="unavailable",
        seed=None,
        capture_content=False,
    ):
        self.store, self.environment, self.access, self.generate = (
            store,
            environment,
            access,
            generate,
        )
        self.model = model
        self.tokenizer = tokenizer
        self.renderer = renderer
        self.seed = seed
        self.capture_content = capture_content

    def call(self, request, *, context_changes=None):
        from ..runner import current_inference_context
        from ..store import digest, uid

        try:
            request_digest = digest(request)
        except (TypeError, ValueError):
            raise Conflict("invalid inference evidence request") from None
        call_id = uid()
        correlation = current_inference_context()
        identity = {
            "call_id": call_id,
            "correlation": correlation,
            "model": self.model,
            "tokenizer": self.tokenizer,
            "renderer": self.renderer,
            "seed": self.seed,
        }
        request_payload = identity | {
            "request_digest": request_digest,
            "rendered_request": request if self.capture_content else None,
            "context_changes": context_changes,
            "visibility": "instrumented",
        }
        request_payload = self._spill_detail(
            request_payload,
            ("rendered_request", "context_changes"),
            "inference.request",
        )
        with self.store.transaction() as db:
            row = self.store.environment(db, self.environment, self.access, "participant.act")
            self.store.append(
                db,
                self.environment,
                row["revision"],
                "model.request",
                request_payload,
                (self.access.participant,),
            )
        try:
            response = self.generate(request)
            self._validate_response(response)
        except Exception as error:
            with self.store.transaction() as db:
                row = self.store.environment(db, self.environment, self.access, "session.read")
                self.store.append(
                    db,
                    self.environment,
                    row["revision"],
                    "model.failure",
                    identity
                    | {
                        "category": "malformed" if isinstance(error, Conflict) else "infrastructure",
                        "request_digest": request_digest,
                    },
                    (self.access.participant,),
                )
            raise
        response_payload = identity | {
            "response": response if self.capture_content else None,
            "token_ids": response.get("token_ids"),
            "logprobs": response.get("logprobs"),
            "usage": response.get("usage"),
            "finish_reason": response.get("finish_reason"),
            "request_digest": request_digest,
            "validation": "validated",
        }
        response_payload = self._spill_detail(
            response_payload,
            ("response", "token_ids", "logprobs"),
            "inference.response",
        )
        with self.store.transaction() as db:
            row = self.store.environment(db, self.environment, self.access, "session.read")
            self.store.append(
                db,
                self.environment,
                row["revision"],
                "model.response",
                response_payload,
                (self.access.participant,),
            )
        return response

    def _spill_detail(self, payload, fields, purpose):
        with self.store.transaction() as db:
            row = self.store.environment(db, self.environment, self.access, "participant.act")
            max_event_bytes = json.loads(row["manifest"])["policy"]["max_event_bytes"]
        if len(encode(payload).encode()) <= max_event_bytes:
            return payload
        detail = {field: payload[field] for field in fields}
        artifact = self.store.artifact(
            self.environment,
            self.access,
            encode(detail).encode(),
            audience=(self.access.participant,),
            media_type="application/json",
        )
        summary = payload | {field: None for field in fields}
        summary["detail_artifact"] = artifact | {"purpose": purpose}
        if len(encode(summary).encode()) > max_event_bytes:
            raise Conflict("inference evidence summary exceeds event size limit")
        return summary

    @staticmethod
    def _validate_response(response):
        if not isinstance(response, dict):
            raise Conflict("invalid inference evidence response")
        token_ids = response.get("token_ids")
        logprobs = response.get("logprobs")
        if token_ids is not None and (
            not isinstance(token_ids, list) or any(type(token) is not int or token < 0 for token in token_ids)
        ):
            raise Conflict("invalid inference evidence token IDs")
        if logprobs is not None and (
            not isinstance(logprobs, list)
            or any(
                type(value) not in (int, float) or not math.isfinite(value) or value > 0 for value in logprobs
            )
        ):
            raise Conflict("invalid inference evidence log probabilities")
        if token_ids is not None and logprobs is not None and len(token_ids) != len(logprobs):
            raise Conflict("invalid inference evidence token/logprob lengths")


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

"""ORS HTTP adapter. Reconnection is explicit and never creates a replacement call.

An ORS session handle is not a checkpoint. This client makes no claim that an
upstream server preserves its state across container loss.
"""

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ..client import NoRedirect
from ..errors import Unavailable
from ..store import encode

MAX_BYTES = 16 * 1024 * 1024


class ORSError(Unavailable):
    def __init__(self, message, *, session_id=None, task_id=None, outcome_unknown=False):
        super().__init__(message)
        self.session_id = session_id
        self.task_id = task_id
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class ORSResult:
    task_id: str
    output: dict

    @property
    def reward(self):
        return self.output.get("reward")

    @property
    def finished(self):
        return self.output["finished"]


def read_result(lines, *, session_id=None, task_id=None, max_bytes=MAX_BYTES, on_task=None):
    """Decode ORS task_id/chunk/end SSE, retaining the original upstream payload."""
    total, event, data, chunks = 0, "", [], []

    def failure(message, unknown=True):
        return ORSError(message, session_id=session_id, task_id=task_id, outcome_unknown=unknown)

    try:
        for raw in lines:
            total += len(raw)
            if total > max_bytes:
                raise failure("ORS response exceeds limit")
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith(":"):
                continue
            if line:
                field, _, value = line.partition(":")
                if field == "event":
                    event = value.removeprefix(" ")
                elif field == "data":
                    data.append(value.removeprefix(" "))
                continue
            value = "\n".join(data)
            if event == "task_id":
                if not value or len(value) > 256 or (task_id is not None and task_id != value):
                    raise failure("ORS task receipt changed")
                task_id = value
                if on_task is not None:
                    on_task(task_id)
            elif event == "chunk":
                if not task_id:
                    raise failure("ORS result precedes task receipt")
                chunks.append(value)
            elif event == "error":
                # Unknown upstream tasks can have executed before their receipt expired.
                raise failure("ORS tool failed or its original receipt is unavailable")
            elif event == "end":
                if not task_id:
                    raise failure("ORS result has no task receipt")
                result = json.loads("".join([*chunks, value]))
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise failure("ORS tool returned an error", unknown=False)
                output = result.get("output")
                if (
                    not isinstance(output, dict)
                    or not isinstance(output.get("blocks"), list)
                    or type(output.get("finished")) is not bool
                ):
                    raise failure("ORS tool returned an invalid result")
                reward = output.get("reward")
                if reward is not None and (type(reward) not in (int, float) or not math.isfinite(reward)):
                    raise failure("ORS tool returned an invalid reward")
                return ORSResult(task_id, output)
            elif event:
                raise failure("unsupported ORS stream event")
            event, data = "", []
    except (UnicodeError, ValueError, OSError, TimeoutError):
        raise failure("ORS stream interrupted or invalid") from None
    raise failure("ORS stream ended without a completed result")


class ORSClient:
    def __init__(self, endpoint, token, *, timeout=300, allow_loopback=False):
        url = urllib.parse.urlsplit(endpoint)
        local = allow_loopback and url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")
        if (
            (url.scheme != "https" and not local)
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not token
            or not 0 < timeout <= 3600
        ):
            raise ValueError("ORS requires an authenticated HTTPS endpoint and bounded timeout")
        self.endpoint, self.token, self.timeout = endpoint.rstrip("/"), token, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _request(self, method, path, body=None, *, session_id=None, task_id=None, stream=False, on_task=None):
        data = None if body is None else encode(body).encode()
        if data is not None and len(data) > MAX_BYTES:
            raise ValueError("ORS request exceeds limit")
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if session_id:
            headers["X-Session-ID"] = session_id
        request = urllib.request.Request(self.endpoint + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if stream:
                    # Bounded readline also handles malicious responses without newlines.
                    return read_result(
                        iter(lambda: response.readline(MAX_BYTES + 1), b""),
                        session_id=session_id,
                        task_id=task_id,
                        on_task=on_task,
                    )
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("response too large")
                return json.loads(raw)
        except ORSError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            raise ORSError(
                "ORS request failed; reconcile before retrying a write",
                session_id=session_id,
                task_id=task_id,
                outcome_unknown=method != "GET",
            ) from None

    def environments(self):
        return self._request("GET", "/list_environments")

    def splits(self, environment):
        return self._request("GET", f"/{urllib.parse.quote(environment, safe='')}/splits")

    def tasks(self, environment, split, *, start=0, stop=None):
        return self._request(
            "POST",
            f"/{urllib.parse.quote(environment, safe='')}/task_range",
            {"split": split, "start": start, "stop": stop},
        )

    def create_session(self):
        result = self._request("POST", "/create_session", {})
        sid = result.get("sid") if isinstance(result, dict) else None
        if not isinstance(sid, str) or not sid or len(sid) > 256 or any(ord(c) < 33 for c in sid):
            raise ORSError("ORS returned an invalid session ID", outcome_unknown=True)
        return sid

    def initialize(self, session_id, environment, *, task_spec=None, split=None, index=None, secrets=None):
        if (task_spec is not None) == (split is not None and index is not None):
            raise ValueError("supply task_spec or both split and index")
        if task_spec is not None and (split is not None or index is not None):
            raise ValueError("task_spec and split/index cannot be mixed")
        body = {"env_name": environment, "secrets": secrets or {}}
        body.update({"task_spec": task_spec} if task_spec is not None else {"split": split, "index": index})
        return self._request("POST", "/create", body, session_id=session_id)

    def prompt(self, session_id, environment):
        return self._request(
            "GET", f"/{urllib.parse.quote(environment, safe='')}/prompt", session_id=session_id
        )

    def tools(self, session_id, environment):
        return self._request(
            "GET", f"/{urllib.parse.quote(environment, safe='')}/task_tools", session_id=session_id
        )

    def call(self, session_id, environment, name, arguments, *, task_id=None, on_task=None):
        body = {"name": name, "input": arguments}
        if task_id is not None:
            body["task_id"] = task_id
        return self._request(
            "POST",
            f"/{urllib.parse.quote(environment, safe='')}/call",
            body,
            session_id=session_id,
            task_id=task_id,
            stream=True,
            on_task=on_task,
        )

    def ping(self, session_id):
        return self._request("POST", "/ping", {}, session_id=session_id)

    def close(self, session_id):
        return self._request("POST", "/delete", {}, session_id=session_id)

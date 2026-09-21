"""Container transport for the same serializable contract as ProcessEnvironment.

Only the supervisor holds the evidence store. The supplier receives explicit
state and randomness, never the supervisor's database or platform credentials.
Writes are not retried by this transport.
"""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

from ..client import NoRedirect
from ..contracts import EnvironmentSpec, Transition
from ..errors import Unavailable
from ..runtime import tuples
from ..store import encode

PROTOCOL = "environment-worker.v1"
MAX_BYTES = 16 * 1024 * 1024


class HTTPWorkerTransport:
    def __init__(self, endpoint, token, *, timeout=60, allow_loopback=False, max_bytes=MAX_BYTES):
        url = urllib.parse.urlsplit(endpoint)
        local = allow_loopback and url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")
        if (
            (url.scheme != "https" and not local)
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("worker requires an explicit HTTPS endpoint")
        if not token or not 0 < timeout <= 3600 or not 1 <= max_bytes <= MAX_BYTES:
            raise ValueError("worker requires credentials and bounded transport limits")
        self.endpoint, self.token = endpoint.rstrip("/"), token
        self.timeout, self.max_bytes = timeout, max_bytes
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __call__(self, request):
        data = encode(request).encode()
        if len(data) > self.max_bytes:
            raise Unavailable("worker request exceeds limit")
        req = urllib.request.Request(
            self.endpoint + "/v1/worker/call",
            data=data,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise Unavailable("worker response exceeds limit")
            return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise Unavailable("worker transport failed; reconcile before retrying") from None


class RemoteEnvironment:
    def __init__(self, transport, *, expected_spec=None):
        self.transport = transport
        self.lock = threading.Lock()
        self.spec = EnvironmentSpec.model_validate(self._call("spec", {}))
        if expected_spec is not None and self.spec != expected_spec:
            raise Unavailable("worker environment differs from admitted version")

    def _call(self, method, arguments):
        request_id = uuid4().hex
        with self.lock:
            response = self.transport(
                {"protocol": PROTOCOL, "id": request_id, "method": method, "arguments": arguments}
            )
        if (
            not isinstance(response, dict)
            or response.get("protocol") != PROTOCOL
            or response.get("id") != request_id
            or "error" in response
            or "result" not in response
        ):
            raise Unavailable("worker returned an invalid or mismatched receipt")
        return response["result"]

    def initialize(self, experiment):
        return self._call("initialize", {"experiment": experiment.model_dump(mode="json")})

    def observe(self, state, participant):
        return self._call("observe", {"state": state, "participant": participant})

    def resolve(self, state, actions, random, events):
        result = self._call(
            "resolve", {"state": state, "actions": actions, "rng": random.getstate(), "events": events}
        )
        transition = Transition.model_validate(result["transition"])
        random.setstate(tuples(result["rng"]))
        return transition

    def intervene(self, state, changes):
        return self._call("intervene", {"state": state, "changes": changes})

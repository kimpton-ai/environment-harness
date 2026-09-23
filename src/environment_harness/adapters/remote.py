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
from ..contracts import (
    EnvironmentSpecUnion,
    EnvironmentSpecV2,
    OperationPlan,
    OperationReceipt,
    Transition,
)
from ..errors import Unavailable
from ..runtime import tuples
from ..store import encode

PROTOCOL = "environment-worker.v1"
PROTOCOL_V2 = "environment-worker.v2"
MAX_BYTES = 16 * 1024 * 1024


class HTTPWorkerTransport:
    def __init__(
        self,
        endpoint,
        token,
        *,
        timeout=60,
        allow_loopback=False,
        max_bytes=MAX_BYTES,
        protocol=PROTOCOL,
    ):
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
        if (
            not token
            or not 0 < timeout <= 3600
            or not 1 <= max_bytes <= MAX_BYTES
            or protocol not in (PROTOCOL, PROTOCOL_V2)
        ):
            raise ValueError("worker requires credentials and bounded transport limits")
        self.endpoint, self.token = endpoint.rstrip("/"), token
        self.timeout, self.max_bytes = timeout, max_bytes
        self.protocol = protocol
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __call__(self, request):
        data = encode(request).encode()
        if len(data) > self.max_bytes:
            raise Unavailable("worker request exceeds limit")
        req = urllib.request.Request(
            self.endpoint + ("/v2/worker/call" if self.protocol == PROTOCOL_V2 else "/v1/worker/call"),
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
    def __init__(self, transport, *, expected_spec=None, operations=None):
        self.transport = transport
        self.operations = dict(operations or {})
        expected_protocol = (
            PROTOCOL_V2
            if expected_spec is not None
            and getattr(expected_spec, "protocol", None) == "environment-session.v2"
            else getattr(transport, "protocol", PROTOCOL)
        )
        self.protocol = expected_protocol
        if hasattr(transport, "protocol"):
            transport.protocol = expected_protocol
        self.lock = threading.Lock()
        from pydantic import TypeAdapter

        self.spec = TypeAdapter(EnvironmentSpecUnion).validate_python(self._call("spec", {}))
        if (self.spec.protocol == "environment-session.v2") != (self.protocol == PROTOCOL_V2):
            raise Unavailable("worker protocol does not match the admitted environment contract")
        if expected_spec is not None and self.spec != expected_spec:
            raise Unavailable("worker environment differs from admitted version")

    def _call(self, method, arguments):
        request_id = uuid4().hex
        with self.lock:
            response = self.transport(
                {"protocol": self.protocol, "id": request_id, "method": method, "arguments": arguments}
            )
        if (
            not isinstance(response, dict)
            or response.get("protocol") != self.protocol
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

    def plan_transition(self, state, actions, random, events):
        if not isinstance(self.spec, EnvironmentSpecV2):
            raise Unavailable("v1 workers do not support transition planning")
        result = self._call(
            "plan_transition",
            {"state": state, "actions": actions, "rng": random.getstate(), "events": events},
        )
        plan = OperationPlan.model_validate(result["plan"])
        random.setstate(tuples(result["rng"]))
        return plan

    def resolve_transition(self, state, actions, random, events, plan, receipts):
        if not isinstance(self.spec, EnvironmentSpecV2):
            raise Unavailable("v1 workers do not support transition resolution")
        typed_plan = OperationPlan.model_validate(plan)
        typed_receipts = {key: OperationReceipt.model_validate(value) for key, value in receipts.items()}
        planned = {request.key: request for request in typed_plan.operations}
        if set(typed_receipts) != set(planned) or any(
            receipt.key != key
            or receipt.operation != planned[key].operation
            or receipt.version != planned[key].version
            for key, receipt in typed_receipts.items()
        ):
            raise ValueError("operation receipts do not match the persisted plan")
        result = self._call(
            "resolve_transition",
            {
                "state": state,
                "actions": actions,
                "rng": random.getstate(),
                "events": events,
                "plan": typed_plan.model_dump(mode="json"),
                "receipts": {key: value.model_dump(mode="json") for key, value in typed_receipts.items()},
            },
        )
        transition = Transition.model_validate(result["transition"])
        random.setstate(tuples(result["rng"]))
        return transition

    def intervene(self, state, changes):
        return self._call("intervene", {"state": state, "changes": changes})

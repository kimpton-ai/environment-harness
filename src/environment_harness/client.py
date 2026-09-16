"""Dependency-free remote client. Commands are never retried implicitly."""

import json
import urllib.error
import urllib.parse
import urllib.request

from .errors import HarnessError
from .store import encode, uid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HarnessError("redirect refused")


class EnvironmentClient:
    def __init__(self, endpoint, token, *, allow_loopback=False, timeout=30):
        parsed = urllib.parse.urlsplit(endpoint)
        local = (
            parsed.scheme == "http"
            and parsed.hostname in ("127.0.0.1", "localhost", "::1")
            and allow_loopback
        )
        if (
            (parsed.scheme != "https" and not local)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("HTTPS endpoint required, with no embedded credentials")
        self.endpoint, self.token, self.timeout = endpoint.rstrip("/"), token, timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None, *, operation_id=None):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if operation_id:
            headers["X-Operation-ID"] = operation_id
        data = None if body is None else encode(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.endpoint + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(16777217)
                if len(raw) > 16777216:
                    raise HarnessError("response size limit exceeded")
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            # Server errors never include caller credentials or arbitrary remote response bodies.
            raise HarnessError(f"environment service returned HTTP {error.code}") from None
        except urllib.error.URLError:
            raise HarnessError("environment service unavailable; reconcile before retrying a write") from None

    def create(self, experiment, operation_id=None):
        body = experiment.model_dump(mode="json") if hasattr(experiment, "model_dump") else experiment
        return self.request("POST", "/v1/environments", body, operation_id=operation_id or uid())

    def get(self, environment):
        return self.request("GET", "/v1/environments/" + urllib.parse.quote(environment, safe=""))

    def observe(self, environment, participant=None):
        path = f"/v1/environments/{urllib.parse.quote(environment, safe='')}/observation"
        if participant is not None:
            path += "?" + urllib.parse.urlencode({"participant": participant})
        return self.request("GET", path)

    def submit(self, environment, action):
        return self.request("POST", f"/v1/environments/{urllib.parse.quote(environment, safe='')}/actions", action)

    def command(self, environment, operation, **arguments):
        return self.request(
            "POST",
            f"/v1/environments/{urllib.parse.quote(environment, safe='')}/commands",
            {"operation": operation, "arguments": arguments},
        )

    def agent_work(self, environment):
        return self.request("GET", f"/v1/environments/{urllib.parse.quote(environment, safe='')}/agent-work")

    def events(self, environment, after=0):
        return self.request("GET", f"/v1/environments/{urllib.parse.quote(environment, safe='')}/events?after={after}")

    def replay(self, environment):
        cursor = 0
        while True:
            page = self.events(environment, cursor)
            if not page["events"]:
                return
            yield from page["events"]
            cursor = page["cursor"]

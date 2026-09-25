"""Dependency-free remote client. Commands are never retried implicitly."""

import json
import urllib.error
import urllib.parse
import urllib.request

from .errors import HarnessError, ServiceError
from .store import encode, uid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HarnessError("redirect refused")


def _service_error(error):
    fallback = f"Environment service returned HTTP {error.code}"
    headers = getattr(error, "headers", None)
    content_type = headers.get("Content-Type", "") if headers is not None else ""
    if "application/json" not in content_type.lower():
        return HarnessError(fallback)
    try:
        raw = error.read(4097)
        if len(raw) > 4096:
            return HarnessError(fallback)
        envelope = json.loads(raw)
        body = envelope["error"]
        code = body["code"]
        message = body["message"]
        status = body["status"]
        request_id = body["request_id"]
        timestamp = body["timestamp"]
        details = body.get("details")
        if (
            not isinstance(code, str)
            or not 1 <= len(code) <= 100
            or not isinstance(message, str)
            or not 1 <= len(message) <= 512
            or status != error.code
            or not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 128
            or not isinstance(timestamp, str)
            or not 1 <= len(timestamp) <= 100
            or (details is not None and (not isinstance(details, list) or len(details) > 100))
        ):
            return HarnessError(fallback)
        return ServiceError(
            f"{fallback}: {message}",
            code=code,
            status=status,
            request_id=request_id,
            timestamp=timestamp,
            details=details,
        )
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return HarnessError(fallback)


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
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

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
            # Only the bounded standard envelope is surfaced; arbitrary response bodies stay private.
            raise _service_error(error) from None
        except urllib.error.URLError:
            raise HarnessError("environment service unavailable; reconcile before retrying a write") from None

    def stream_jsonl(self, path):
        request = urllib.request.Request(
            self.endpoint + path,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/x-ndjson",
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                for raw in response:
                    if len(raw) > 16777216:
                        raise HarnessError("stream record size limit exceeded")
                    if not raw.strip():
                        continue
                    try:
                        value = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise HarnessError("environment service returned malformed JSONL") from None
                    if not isinstance(value, dict):
                        raise HarnessError("environment service returned malformed JSONL")
                    yield value
        except urllib.error.HTTPError as error:
            raise _service_error(error) from None
        except urllib.error.URLError:
            raise HarnessError("environment service unavailable; reconcile before retrying a write") from None

    def create(self, experiment, operation_id=None):
        body = experiment.model_dump(mode="json") if hasattr(experiment, "model_dump") else experiment
        return self.request("POST", "/v1/environments", body, operation_id=operation_id or uid())

    def list(self, *, limit=100, cursor=None):
        query = {"limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        return self.request("GET", "/v1/environments?" + urllib.parse.urlencode(query))

    def get(self, environment):
        return self.request("GET", "/v1/environments/" + urllib.parse.quote(environment, safe=""))

    def observe(self, environment, participant=None):
        path = f"/v1/environments/{urllib.parse.quote(environment, safe='')}/observation"
        if participant is not None:
            path += "?" + urllib.parse.urlencode({"participant": participant})
        return self.request("GET", path)

    def submit(self, environment, action):
        return self.request(
            "POST", f"/v1/environments/{urllib.parse.quote(environment, safe='')}/actions", action
        )

    def command(self, environment, operation, **arguments):
        return self.request(
            "POST",
            f"/v1/environments/{urllib.parse.quote(environment, safe='')}/commands",
            {"operation": operation, "arguments": arguments},
        )

    def agent_work(self, environment):
        return self.request("GET", f"/v1/environments/{urllib.parse.quote(environment, safe='')}/agent-work")

    def cancel(self, environment):
        return self.command(environment, "cancel")

    def advance(self, environment):
        return self.command(environment, "advance")

    def credentials(self, environment, participant, *, ttl=3600):
        return self.request(
            "POST",
            f"/v1/environments/{urllib.parse.quote(environment, safe='')}/credentials",
            {"participant": participant, "ttl": ttl},
        )

    def reports(self, environment):
        return self.request("GET", f"/v1/environments/{urllib.parse.quote(environment, safe='')}/reports")

    def events(self, environment, after=0):
        return self.request(
            "GET", f"/v1/environments/{urllib.parse.quote(environment, safe='')}/events?after={after}"
        )

    def activity_hierarchy(self):
        return self.request("GET", "/v1/activity/snapshot")

    def activity(self, after=0):
        return self.request("GET", f"/v1/activity/events?after={after}")

    def experiment_activity(self, experiment, after=0):
        key = urllib.parse.quote(experiment, safe="")
        return self.request("GET", f"/v1/experiments/{key}/events?after={after}")

    def session_activity(self, environment, after=0):
        key = urllib.parse.quote(environment, safe="")
        return self.request("GET", f"/v1/environments/{key}/activity?after={after}")

    def trajectories(self, *, limit=100, cursor=None):
        query = {"limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        return self.request("GET", "/v1/trajectories?" + urllib.parse.urlencode(query))

    def trajectory(self, trajectory):
        key = urllib.parse.quote(trajectory, safe="")
        return self.request("GET", f"/v1/trajectories/{key}")

    def trajectory_records(self, trajectory, *, after=0, limit=200):
        key = urllib.parse.quote(trajectory, safe="")
        query = urllib.parse.urlencode({"after": after, "limit": limit})
        return self.request("GET", f"/v1/trajectories/{key}/records?{query}")

    def register_trajectory_source(self, registration):
        body = registration.model_dump(mode="json") if hasattr(registration, "model_dump") else registration
        return self.request("POST", "/v1/trajectory-sources", body)

    def ingest_trajectory_source(self, source, batch):
        key = urllib.parse.quote(source, safe="")
        body = batch.model_dump(mode="json") if hasattr(batch, "model_dump") else batch
        return self.request("POST", f"/v1/trajectory-sources/{key}/records", body)

    def update_trajectory_source(self, source, status):
        key = urllib.parse.quote(source, safe="")
        body = status.model_dump(mode="json") if hasattr(status, "model_dump") else status
        return self.request("PUT", f"/v1/trajectory-sources/{key}/status", body)

    def trajectory_source_status(self, source):
        key = urllib.parse.quote(source, safe="")
        return self.request("GET", f"/v1/trajectory-sources/{key}/status")

    def freeze_trajectory(self, trajectory):
        key = urllib.parse.quote(trajectory, safe="")
        return self.request("POST", f"/v1/trajectories/{key}/snapshots")

    def trajectory_snapshots(self, trajectory, *, limit=100):
        query = urllib.parse.urlencode({"trajectory": trajectory, "limit": limit})
        return self.request("GET", f"/v1/trajectory-snapshots?{query}")

    def trajectory_snapshot(self, snapshot):
        key = urllib.parse.quote(snapshot, safe="")
        return self.request("GET", f"/v1/trajectory-snapshots/{key}")

    def export_trajectory_snapshot(self, snapshot):
        key = urllib.parse.quote(snapshot, safe="")
        yield from self.stream_jsonl(f"/v1/trajectory-snapshots/{key}/export")

    def freeze_trajectory_dataset(self, name, trajectories):
        return self.request("POST", "/v1/trajectory-datasets", {"name": name, "trajectories": trajectories})

    def trajectory_dataset(self, dataset):
        key = urllib.parse.quote(dataset, safe="")
        return self.request("GET", f"/v1/trajectory-datasets/{key}")

    def export_trajectory_dataset(self, dataset):
        key = urllib.parse.quote(dataset, safe="")
        yield from self.stream_jsonl(f"/v1/trajectory-datasets/{key}/export")

    def trajectory_datasets(self, *, limit=100):
        return self.request("GET", "/v1/trajectory-datasets?" + urllib.parse.urlencode({"limit": limit}))

    def training_run(self, training_run):
        key = urllib.parse.quote(training_run, safe="")
        return self.request("GET", f"/v1/training-runs/{key}")

    def training_runs(self, *, dataset=None, limit=100):
        query = {}
        if dataset is not None:
            query["dataset"] = dataset
        query["limit"] = limit
        return self.request("GET", "/v1/training-runs?" + urllib.parse.urlencode(query))

    def replay(self, environment):
        cursor = 0
        while True:
            page = self.events(environment, cursor)
            if not page["events"]:
                return
            yield from page["events"]
            cursor = page["cursor"]

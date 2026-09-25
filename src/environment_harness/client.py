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

    # -- Experiments and sessions -------------------------------------------
    def create_experiment(self, experiment, operation_id=None):
        """Create the implicit experiment-of-one and its single Session."""

        body = experiment.model_dump(mode="json") if hasattr(experiment, "model_dump") else experiment
        return self.request("POST", "/v1/experiments", body, operation_id=operation_id or uid())

    def experiments(self, *, limit=100, cursor=None):
        return self.request("GET", "/v1/experiments?" + self._query(limit=limit, cursor=cursor))

    def experiment(self, experiment):
        return self.request("GET", f"/v1/experiments/{self._key(experiment)}")

    def experiment_sessions(self, experiment, *, limit=100, cursor=None):
        return self.request(
            "GET",
            f"/v1/experiments/{self._key(experiment)}/sessions?" + self._query(limit=limit, cursor=cursor),
        )

    def scenario_sets(self, *, limit=100, cursor=None):
        return self.request("GET", "/v1/scenario-sets?" + self._query(limit=limit, cursor=cursor))

    def scenario_set(self, scenario_set):
        return self.request("GET", f"/v1/scenario-sets/{self._key(scenario_set)}")

    def sessions(self, *, experiment=None, limit=100, cursor=None):
        return self.request(
            "GET", "/v1/sessions?" + self._query(experiment=experiment, limit=limit, cursor=cursor)
        )

    def session(self, session):
        return self.request("GET", f"/v1/sessions/{self._key(session)}")

    def observe(self, session, participant):
        return self.request(
            "GET", f"/v1/sessions/{self._key(session)}/participants/{self._key(participant)}/observation"
        )

    def submit(self, session, participant, action):
        return self.request(
            "POST",
            f"/v1/sessions/{self._key(session)}/participants/{self._key(participant)}/actions",
            action,
        )

    def credentials(self, session, participant, *, ttl=3600):
        return self.request(
            "POST",
            f"/v1/sessions/{self._key(session)}/participants/{self._key(participant)}/credentials",
            {"ttl": ttl},
        )

    def command(self, session, operation, **arguments):
        return self.request(
            "POST",
            f"/v1/sessions/{self._key(session)}/commands",
            {"operation": operation, "arguments": arguments},
        )

    def cancel(self, session):
        return self.command(session, "cancel")

    def advance(self, session):
        return self.command(session, "advance")

    def invocations(self, session):
        return self.request("GET", f"/v1/sessions/{self._key(session)}/invocations")

    def checkpoints(self, session, *, limit=100, cursor=None):
        return self.request(
            "GET", f"/v1/sessions/{self._key(session)}/checkpoints?" + self._query(limit=limit, cursor=cursor)
        )

    def create_checkpoint(self, session, *, exact_agents=False):
        return self.request(
            "POST", f"/v1/sessions/{self._key(session)}/checkpoints", {"exact_agents": exact_agents}
        )

    def checkpoint(self, session, checkpoint):
        return self.request("GET", f"/v1/sessions/{self._key(session)}/checkpoints/{self._key(checkpoint)}")

    def branch(self, session, request):
        body = request.model_dump(mode="json") if hasattr(request, "model_dump") else request
        return self.request("POST", f"/v1/sessions/{self._key(session)}/branches", body)

    def scores(self, session):
        return self.request("GET", f"/v1/sessions/{self._key(session)}/scores")

    def publish_score(self, session, report):
        body = report.model_dump(mode="json") if hasattr(report, "model_dump") else report
        return self.request("POST", f"/v1/sessions/{self._key(session)}/scores", body)

    def evidence(self, session, after=0):
        return self.request("GET", f"/v1/sessions/{self._key(session)}/evidence?after={after}")

    # -- Activity -----------------------------------------------------------
    def activity_hierarchy(self):
        return self.request("GET", "/v1/activity/hierarchy")

    def activity(self, after=0):
        return self.request("GET", f"/v1/activity?after={after}")

    def experiment_activity(self, experiment, after=0):
        return self.request("GET", f"/v1/experiments/{self._key(experiment)}/activity?after={after}")

    def session_activity(self, session, after=0):
        return self.request("GET", f"/v1/sessions/{self._key(session)}/activity?after={after}")

    # -- Trajectories, snapshots, and datasets --------------------------------
    def capabilities(self):
        return self.request("GET", "/v1/capabilities")

    def policies(self, *, limit=100, cursor=None):
        return self.request("GET", "/v1/policies?" + self._query(limit=limit, cursor=cursor))

    def policy(self, policy):
        return self.request("GET", f"/v1/policies/{self._key(policy)}")

    def trajectories(self, *, limit=100, cursor=None):
        return self.request("GET", "/v1/trajectories?" + self._query(limit=limit, cursor=cursor))

    def trajectory(self, trajectory):
        return self.request("GET", f"/v1/trajectories/{self._key(trajectory)}")

    def trajectory_records(self, trajectory, *, after=0, limit=200):
        return self.request(
            "GET",
            f"/v1/trajectories/{self._key(trajectory)}/records?" + self._query(after=after, limit=limit),
        )

    def freeze_trajectory(self, trajectory):
        return self.request("POST", f"/v1/trajectories/{self._key(trajectory)}/snapshots")

    def trajectory_snapshots(self, trajectory, *, limit=100, cursor=None):
        return self.request(
            "GET",
            f"/v1/trajectories/{self._key(trajectory)}/snapshots?" + self._query(limit=limit, cursor=cursor),
        )

    def snapshots(self, *, trajectory=None, limit=100, cursor=None):
        return self.request(
            "GET", "/v1/snapshots?" + self._query(trajectory=trajectory, limit=limit, cursor=cursor)
        )

    def snapshot(self, snapshot):
        return self.request("GET", f"/v1/snapshots/{self._key(snapshot)}")

    def snapshot_records(self, snapshot, *, after=0, limit=200):
        return self.request(
            "GET", f"/v1/snapshots/{self._key(snapshot)}/records?" + self._query(after=after, limit=limit)
        )

    def stream_snapshot_records(self, snapshot):
        """Stream NDJSON through a hand-written iterator, not generated decoding."""

        yield from self.stream_jsonl(f"/v1/snapshots/{self._key(snapshot)}/records")

    def freeze_dataset(self, name, trajectories):
        return self.request("POST", "/v1/datasets", {"name": name, "trajectories": trajectories})

    def datasets(self, *, limit=100, cursor=None):
        return self.request("GET", "/v1/datasets?" + self._query(limit=limit, cursor=cursor))

    def dataset(self, dataset):
        return self.request("GET", f"/v1/datasets/{self._key(dataset)}")

    def dataset_records(self, dataset, *, after=0, limit=200):
        return self.request(
            "GET", f"/v1/datasets/{self._key(dataset)}/records?" + self._query(after=after, limit=limit)
        )

    def stream_dataset_records(self, dataset):
        yield from self.stream_jsonl(f"/v1/datasets/{self._key(dataset)}/records")

    def training_run(self, training_run):
        return self.request("GET", f"/v1/training-runs/{self._key(training_run)}")

    def training_runs(self, *, dataset=None, limit=100, cursor=None):
        return self.request(
            "GET", "/v1/training-runs?" + self._query(dataset=dataset, limit=limit, cursor=cursor)
        )

    # -- Sources --------------------------------------------------------------
    def register_source(self, registration):
        body = registration.model_dump(mode="json") if hasattr(registration, "model_dump") else registration
        return self.request("POST", "/v1/sources", body)

    def sources(self, *, limit=100, cursor=None):
        return self.request("GET", "/v1/sources?" + self._query(limit=limit, cursor=cursor))

    def source(self, source):
        return self.request("GET", f"/v1/sources/{self._key(source)}")

    def ingest_source(self, source, batch):
        body = batch.model_dump(mode="json") if hasattr(batch, "model_dump") else batch
        return self.request("POST", f"/v1/sources/{self._key(source)}/records", body)

    def source_records(self, source, *, after=0, limit=200):
        return self.request(
            "GET", f"/v1/sources/{self._key(source)}/records?" + self._query(after=after, limit=limit)
        )

    def report_source_status(self, source, status):
        body = status.model_dump(mode="json") if hasattr(status, "model_dump") else status
        return self.request("POST", f"/v1/sources/{self._key(source)}/status-reports", body)

    def source_status(self, source):
        return self.request("GET", f"/v1/sources/{self._key(source)}/status")

    # -- Evaluation -----------------------------------------------------------
    def compare(self, sessions):
        return self.request("POST", "/v1/comparisons", {"sessions": list(sessions)})

    @staticmethod
    def _key(value):
        return urllib.parse.quote(str(value), safe="")

    @staticmethod
    def _query(**values):
        return urllib.parse.urlencode({key: value for key, value in values.items() if value is not None})

    def replay(self, session):
        cursor = 0
        while True:
            page = self.evidence(session, cursor)
            if not page["events"]:
                return
            yield from page["events"]
            cursor = page["cursor"]

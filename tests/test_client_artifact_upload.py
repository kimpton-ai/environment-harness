import json
import urllib.error

import pytest

from environment_harness.client import EnvironmentClient
from environment_harness.errors import HarnessError


class _Response:
    def __init__(self, body=None):
        self.body = body or json.dumps({"id": "artifact-id", "size": 4}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, limit):
        assert limit == 4097
        return self.body


class _Opener:
    def __init__(self, response=None):
        self.requests = []
        self.response = response or _Response()

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        return self.response


def test_upload_artifact_forwards_stable_idempotency_key():
    client = EnvironmentClient("https://example.test", "opaque", timeout=7)
    opener = _Opener()
    client.opener = opener

    result = client.upload_artifact("a" * 32, b"data", idempotency_key="spill:stable-1")

    request, timeout = opener.requests[0]
    assert result["id"] == "artifact-id"
    assert request.get_method() == "POST"
    assert request.full_url == "https://example.test/v1/sessions/" + "a" * 32 + "/artifacts"
    assert request.data == b"data"
    assert request.get_header("Idempotency-key") == "spill:stable-1"
    assert timeout == 7


def test_upload_artifact_rejects_oversize_before_request():
    client = EnvironmentClient("https://example.test", "opaque")
    opener = _Opener()
    client.opener = opener

    with pytest.raises(ValueError, match="16 MiB"):
        client.upload_artifact("a" * 32, b"x" * (16_777_216 + 1), idempotency_key="too-large")

    assert opener.requests == []


@pytest.mark.parametrize(
    ("data", "key", "media_type", "message"),
    [
        ("not bytes", "stable", "application/octet-stream", "artifact data must be bytes"),
        (b"data", "invalid key", "application/octet-stream", "idempotency key"),
        (b"data", "stable", "text/plain\r\nInjected: yes", "media type"),
    ],
)
def test_upload_artifact_rejects_invalid_request_before_io(data, key, media_type, message):
    client = EnvironmentClient("https://example.test", "opaque")
    opener = _Opener()
    client.opener = opener
    with pytest.raises(ValueError, match=message):
        client.upload_artifact("a" * 32, data, idempotency_key=key, media_type=media_type)
    assert opener.requests == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"x" * 4097, "response size limit"),
        (b"[]", "malformed artifact receipt"),
    ],
)
def test_upload_artifact_rejects_invalid_receipt(body, message):
    client = EnvironmentClient("https://example.test", "opaque")
    client.opener = _Opener(_Response(body))
    with pytest.raises(HarnessError, match=message):
        client.upload_artifact("a" * 32, b"data", idempotency_key="stable")


def test_upload_artifact_explains_ambiguous_network_failure():
    client = EnvironmentClient("https://example.test", "opaque")

    class FailingOpener:
        def open(self, _request, *, timeout):
            raise urllib.error.URLError("lost response")

    client.opener = FailingOpener()
    with pytest.raises(HarnessError, match="reconcile the artifact key before retrying"):
        client.upload_artifact("a" * 32, b"data", idempotency_key="stable")

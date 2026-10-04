import json

import pytest

from environment_harness.client import EnvironmentClient


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, limit):
        assert limit == 4097
        return json.dumps({"id": "artifact-id", "size": 4}).encode()


class _Opener:
    def __init__(self):
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        return _Response()


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

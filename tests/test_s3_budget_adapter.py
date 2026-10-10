"""The hosted S3 adapter must make one bounded provider attempt per journal entry."""

import io
from types import SimpleNamespace

import pytest

from environment_harness.hosted import S3Artifacts


class FakeS3:
    def __init__(self):
        self.meta = SimpleNamespace(config=SimpleNamespace(retries={"total_max_attempts": 1}))
        self.objects = {}
        self.deleted = []
        self.last_body = None

    def put_object(self, *, Bucket, Key, Body, ContentType):
        assert Bucket == "test-bucket"
        assert ContentType == "application/octet-stream"
        self.objects[Key] = Body

    def get_object(self, *, Bucket, Key):
        assert Bucket == "test-bucket"
        body = io.BytesIO(self.objects[Key])
        self.last_body = body
        return {"Body": body, "ContentLength": len(self.objects[Key])}

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys, **kwargs):
        assert Bucket == "test-bucket"
        assert MaxKeys == 1000
        assert not kwargs
        return {
            "Contents": [
                {"Key": key, "Size": len(value)}
                for key, value in self.objects.items()
                if key.startswith(Prefix)
            ],
            "IsTruncated": False,
        }

    def delete_objects(self, *, Bucket, Delete):
        assert Bucket == "test-bucket"
        keys = [item["Key"] for item in Delete["Objects"]]
        self.deleted.extend(keys)
        for key in keys:
            self.objects.pop(key, None)
        return {"Deleted": [{"Key": key} for key in keys]}


def test_s3_budgeted_put_read_inventory_and_delete_are_exact_and_bounded():
    client = FakeS3()
    artifacts = S3Artifacts(client, "test-bucket")
    prefix = "a" * 32 + "/"
    key = prefix + "artifact"

    artifacts.put_once(key, b"data")
    assert artifacts.get_bounded(key, 4) == b"data"
    assert client.last_body.closed
    page, size = artifacts.list_page(prefix)
    assert page == {"objects": [{"key": key, "size": 4}], "cursor": None, "truncated": False}
    assert 0 < size <= artifacts.max_list_response_bytes

    receipt, size = artifacts.delete_batch(prefix, [key])
    assert receipt == {"deleted": 1}
    assert 0 < size <= artifacts.max_delete_response_bytes
    assert client.deleted == ["environment-harness/" + key]
    assert not client.objects


def test_s3_budgeted_operations_reject_hidden_retries_and_oversize_reads():
    client = FakeS3()
    artifacts = S3Artifacts(client, "test-bucket")
    key = "a" * 32 + "/artifact"
    client.meta.config.retries = {"total_max_attempts": 2}
    with pytest.raises(ValueError, match="retries disabled"):
        artifacts.put_once(key, b"data")

    client.meta.config.retries = {"total_max_attempts": 1}
    artifacts.put_once(key, b"data")
    with pytest.raises(ValueError, match="size limit"):
        artifacts.get_bounded(key, 3)
    assert client.last_body.closed


def test_s3_budgeted_cleanup_rejects_cross_prefix_and_missing_acknowledgement():
    client = FakeS3()
    artifacts = S3Artifacts(client, "test-bucket")
    prefix = "a" * 32 + "/"
    with pytest.raises(ValueError, match="exact environment object batch"):
        artifacts.delete_batch(prefix, ["b" * 32 + "/artifact"])

    client.list_objects_v2 = lambda **_kwargs: {
        "Contents": [{"Key": "environment-harness/" + "b" * 32 + "/artifact", "Size": 4}],
        "IsTruncated": False,
    }
    with pytest.raises(ValueError, match="escaped exact environment prefix"):
        artifacts.list_page(prefix)

    client.delete_objects = lambda **_kwargs: {"Deleted": []}
    with pytest.raises(ValueError, match="acknowledgement is incomplete"):
        artifacts.delete_batch(prefix, [prefix + "artifact"])

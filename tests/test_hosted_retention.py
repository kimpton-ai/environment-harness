from types import SimpleNamespace

import pytest

from environment_harness.hosted import S3Artifacts


def test_s3_erasure_requires_exact_acknowledgments_and_empty_inventory():
    prefix = "a" * 32 + "/"
    key = "environment-harness/" + prefix + "object"
    pages = iter([{"Contents": [{"Key": key}]}, {}])
    client = SimpleNamespace(
        get_bucket_versioning=lambda **kw: {},
        list_objects_v2=lambda **kw: next(pages),
        delete_objects=lambda **kw: {"Deleted": [{"Key": key}]},
    )
    assert S3Artifacts(client, "synthetic").purge_prefix(prefix) == 1
    for value in ("", "../", "a" * 32, "other/"):
        with pytest.raises(ValueError, match="exact"):
            S3Artifacts(client, "synthetic").purge_prefix(value)


@pytest.mark.parametrize(
    "versioned,page,deleted",
    [
        (True, {}, {}),
        (False, {"IsTruncated": True}, {}),
        (False, {"Contents": [{"Key": "outside"}]}, {}),
        (False, {"Contents": [{"Key": "environment-harness/" + "a" * 32 + "/x"}]}, {"Errors": [{}]}),
        (False, {"Contents": [{"Key": "environment-harness/" + "a" * 32 + "/x"}]}, {}),
    ],
)
def test_s3_erasure_never_claims_missing_or_unsupported_deletion(versioned, page, deleted):
    client = SimpleNamespace(
        get_bucket_versioning=lambda **kw: {"Status": "Enabled"} if versioned else {},
        list_objects_v2=lambda **kw: page,
        delete_objects=lambda **kw: deleted,
    )
    with pytest.raises(ValueError):
        S3Artifacts(client, "synthetic").purge_prefix("a" * 32 + "/")

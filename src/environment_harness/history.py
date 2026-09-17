"""Bounded, lossless inheritance. Existing evidence is never rewritten."""

import base64
import hashlib
import json

from .errors import Conflict
from .store import encode

INHERITED = ("history.inherited", "history.inherited.chunk")


def inherit(store, db, environment, revision, original, limit):
    audience = json.loads(original["audience"])
    kind = original["kind"]
    if kind in INHERITED:
        # Already inherited records are copied without adding another encoding layer.
        store.append(
            db, environment, revision, kind, json.loads(original["body"]), audience, original["event_time"]
        )
        return
    raw = encode(dict(original)).encode()
    if len(raw) <= limit:
        store.append(
            db, environment, revision, "history.inherited", dict(original), audience, original["event_time"]
        )
        return
    encoded = base64.b64encode(raw).decode("ascii")
    header = {
        "environment": original["environment"],
        "seq": original["seq"],
        "hash": original["hash"],
        "record_sha256": hashlib.sha256(raw).hexdigest(),
        "encoding": "base64-json-v1",
    }
    # Using the encoded length for both integers safely overestimates the final metadata size.
    overhead = len(encode(header | {"part": len(encoded), "parts": len(encoded), "data": ""}))
    capacity = ((limit - overhead) // 4) * 4
    if capacity < 4:
        raise Conflict("event limit cannot contain inheritance metadata")
    parts = (len(encoded) + capacity - 1) // capacity
    for part, offset in enumerate(range(0, len(encoded), capacity)):
        payload = header | {"part": part, "parts": parts, "data": encoded[offset : offset + capacity]}
        store.append(
            db, environment, revision, "history.inherited.chunk", payload, audience, original["event_time"]
        )


def reconstruct_inherited(events, *, strict=True):
    """Reconstruct original database rows from an authorized export or event page.

    Partial inspection pages return explicit incomplete entries when strict=False.
    Verification of the enclosing event chain remains EvidenceStore.verify's job.
    """
    groups, order = {}, []
    for event in events:
        payload = event["payload"]
        if event["kind"] == "history.inherited":
            order.append({"complete": True, "record": payload})
        elif event["kind"] == "history.inherited.chunk":
            key = (
                event["environment"],
                event["revision"],
                payload["environment"],
                payload["seq"],
                payload["hash"],
            )
            if key not in groups:
                groups[key] = {"header": payload, "chunks": {}, "audience": event["audience"]}
                order.append(key)
            group = groups[key]
            if (
                any(group["header"][k] != payload[k] for k in ("parts", "encoding", "record_sha256"))
                or event["audience"] != group["audience"]
                or type(payload["part"]) is not int
                or type(payload["parts"]) is not int
                or not 0 <= payload["part"] < payload["parts"]
                or payload["part"] in group["chunks"]
            ):
                raise Conflict("invalid inherited chunk sequence")
            group["chunks"][payload["part"]] = payload["data"]
    for item in order:
        if isinstance(item, dict):
            yield item
            continue
        group = groups[item]
        header, chunks = group["header"], group["chunks"]
        if len(chunks) != header["parts"]:
            if strict:
                raise Conflict("incomplete inherited record")
            yield {"complete": False, "environment": header["environment"], "seq": header["seq"]}
            continue
        try:
            if header["encoding"] != "base64-json-v1":
                raise ValueError("unsupported encoding")
            raw = base64.b64decode("".join(chunks[i] for i in range(header["parts"])), validate=True)
            record = json.loads(raw)
            if (
                hashlib.sha256(raw).hexdigest() != header["record_sha256"]
                or any(record[k] != header[k] for k in ("environment", "seq", "hash"))
                or json.loads(record["audience"]) != group["audience"]
            ):
                raise ValueError("record identity or digest mismatch")
        except (ValueError, KeyError, TypeError) as error:
            raise Conflict("inherited record integrity failure") from error
        yield {"complete": True, "record": record}

"""Optional supplier signatures. Verifiers cannot mint supplier receipts."""

import base64

from .errors import Conflict
from .store import encode


class ReceiptSigner:
    def __init__(self, key_id, private_key):
        self.key_id, self.private_key = key_id, private_key

    def sign(self, payload):
        return {
            "algorithm": "Ed25519",
            "key_id": self.key_id,
            "payload": payload,
            "signature": base64.b64encode(self.private_key.sign(encode(payload).encode())).decode(),
        }


def verify_receipt(envelope, trusted_keys):
    from cryptography.exceptions import InvalidSignature

    if envelope.get("algorithm") != "Ed25519" or envelope.get("key_id") not in trusted_keys:
        raise Conflict("untrusted receipt signing key")
    try:
        trusted_keys[envelope["key_id"]].verify(
            base64.b64decode(envelope["signature"], validate=True), encode(envelope["payload"]).encode()
        )
    except (InvalidSignature, ValueError, KeyError):
        raise Conflict("invalid supplier receipt") from None
    return envelope["payload"]

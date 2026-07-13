"""Capability tokens for the public /keep endpoint.

A keep link carries a signed, expiring token scoped to exactly one
(media item, collection). Holding it lets you do ONE thing — remove that item
from that deletion collection — and nothing else. Tokens are opaque
HMAC-SHA256 blobs: unforgeable without SIGNING_SECRET, non-enumerable, and
self-expiring. No server-side state required.

Token format:  base64url(payload) + "." + base64url(hmac_sha256(secret, payload))
Payload:       "v1:{media_id}:{collection_id}:{exp_unix}"                 (item-only)
               "v2:{media_id}:{collection_id}:{email}:{exp_unix}"        (per-recipient)
v2 carries the recipient email so a keep click can be attributed to a person.
"""
from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256
from typing import Optional


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def _sign(secret: str, payload: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload, sha256).digest()


def mint(secret: str, media_id: str, collection_id: int, exp_unix: int,
         email: Optional[str] = None) -> str:
    """Create a keep token for one (media_id, collection_id) valid until exp_unix.
    When `email` is given, a v2 token carries it so the keep can be attributed."""
    if not secret:
        raise ValueError("SIGNING_SECRET is not set; cannot mint keep tokens")
    if email:
        payload = f"v2:{media_id}:{collection_id}:{email}:{int(exp_unix)}".encode("utf-8")
    else:
        payload = f"v1:{media_id}:{collection_id}:{int(exp_unix)}".encode("utf-8")
    return f"{_b64e(payload)}.{_b64e(_sign(secret, payload))}"


def verify(secret: str, token: str, now: Optional[float] = None) -> Optional[dict]:
    """Return {media_id, collection_id, email, exp} if the token is authentic and
    not expired, else None. `email` is None for v1 tokens. Constant-time compare."""
    if not secret or not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64d(payload_b64)
        expected = _sign(secret, payload)
        if not hmac.compare_digest(expected, _b64d(sig_b64)):
            return None
        parts = payload.decode("utf-8").split(":")
        if parts[0] == "v1" and len(parts) == 4:
            _, media_id, collection_id, exp = parts
            email = None
        elif parts[0] == "v2" and len(parts) == 5:
            _, media_id, collection_id, email, exp = parts
        else:
            return None
        exp_i = int(exp)
        if (now if now is not None else time.time()) > exp_i:
            return None
        return {"media_id": media_id, "collection_id": int(collection_id),
                "email": email, "exp": exp_i}
    except (ValueError, TypeError):
        return None

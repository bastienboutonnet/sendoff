"""Capability tokens for the public /keep endpoint.

A keep link carries a signed, expiring token scoped to exactly one
(media item, collection). Holding it lets you do ONE thing — remove that item
from that deletion collection — and nothing else. Tokens are opaque
HMAC-SHA256 blobs: unforgeable without SIGNING_SECRET, non-enumerable, and
self-expiring. No server-side state required.

Token format:  base64url(payload) + "." + base64url(hmac_sha256(secret, payload))
Payload:       "v1:{media_id}:{collection_id}:{exp_unix}"
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


def mint(secret: str, media_id: str, collection_id: int, exp_unix: int) -> str:
    """Create a keep token for one (media_id, collection_id) valid until exp_unix."""
    if not secret:
        raise ValueError("SIGNING_SECRET is not set; cannot mint keep tokens")
    payload = f"v1:{media_id}:{collection_id}:{int(exp_unix)}".encode("utf-8")
    return f"{_b64e(payload)}.{_b64e(_sign(secret, payload))}"


def verify(secret: str, token: str, now: Optional[float] = None) -> Optional[dict]:
    """Return {media_id, collection_id, exp} if the token is authentic and not
    expired, else None. Constant-time signature comparison."""
    if not secret or not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64d(payload_b64)
        expected = _sign(secret, payload)
        if not hmac.compare_digest(expected, _b64d(sig_b64)):
            return None
        parts = payload.decode("utf-8").split(":")
        if len(parts) != 4 or parts[0] != "v1":
            return None
        _, media_id, collection_id, exp = parts
        exp_i = int(exp)
        if (now if now is not None else time.time()) > exp_i:
            return None
        return {"media_id": media_id, "collection_id": int(collection_id), "exp": exp_i}
    except (ValueError, TypeError):
        return None

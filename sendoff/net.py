"""Shared HTTP session with retries.

All services are reached by LAN hostname (e.g. wanker.lan). The container's DNS
can occasionally drop a single query under load, surfacing as a
NameResolutionError mid-run. Retry connection failures with backoff so a
transient blip doesn't fail a whole cycle. `requests` is imported lazily so the
pure-logic tests still run without it installed.
"""
from __future__ import annotations

import threading

_session = None
_lock = threading.Lock()


def session():
    global _session
    if _session is not None:
        return _session
    with _lock:
        if _session is not None:
            return _session
        import requests
        from requests.adapters import HTTPAdapter
        try:
            from urllib3.util import Retry
        except ImportError:  # pragma: no cover - very old urllib3
            from urllib3.util.retry import Retry
        retry = Retry(
            total=4, connect=4, read=2, backoff_factor=0.5,
            status_forcelist=(),        # don't retry on HTTP status (avoid double-writes)
            allowed_methods=None,       # retry connect failures for all verbs incl. DELETE/POST
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        s = requests.Session()
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _session = s
        return s

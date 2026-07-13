"""End-to-end web-surface tests via Flask's test client. Needs Flask installed.

Run: .venv/bin/python tests/test_web.py

Maintainerr is stubbed so no network is touched; we verify auth (fail-closed),
the /keep capability flow, and the dashboard render.
"""
import base64
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sendoff import config, tokens  # noqa: E402
from sendoff.maintainerr import ScheduledItem  # noqa: E402

TODAY = date.today()


def _item(mid="jf-1", cid=3, title="Dune"):
    return ScheduledItem(
        collection_id=cid, collection_title="Cull", media_server_id=mid,
        tmdb_id=42, tvdb_id=None, title=title, media_type="movie",
        add_date=TODAY - timedelta(days=1), delete_after_days=6,
    )


class FakeMaintainerr:
    removed = []

    def __init__(self, *a, **k):
        pass

    def scheduled_items(self):
        return [_item()]

    def remove_media(self, collection_id, media_id):
        FakeMaintainerr.removed.append((collection_id, media_id))
        return True


def _basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def main():
    import sendoff.web as web
    web.MaintainerrClient = FakeMaintainerr
    # Jellyseerr/Jellystat disabled -> dashboard still renders items with no people.
    config.SIGNING_SECRET = "s3cret"
    config.PUBLIC_BASE_URL = "https://sendoff.example.com"
    c = web.app.test_client()

    # healthz open
    assert c.get("/healthz").status_code == 200
    print("ok  /healthz open")

    # dashboard fail-closed when no auth configured
    config.DASHBOARD_USER = ""
    config.DASHBOARD_PASSWORD = ""
    config.TRUST_PROXY_AUTH = False
    assert c.get("/").status_code == 503
    print("ok  dashboard fail-closed (503) with no auth configured")

    # basic auth required + wrong creds rejected
    config.DASHBOARD_USER = "admin"
    config.DASHBOARD_PASSWORD = "hunter2"
    assert c.get("/").status_code == 401
    assert c.get("/", headers=_basic("admin", "wrong")).status_code == 401
    r = c.get("/", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200 and b"Dune" in r.data and b"Leaving soon" in r.data
    print("ok  dashboard basic auth: 401 no/wrong creds, 200 + item on match")

    # trust-proxy bypass
    config.DASHBOARD_PASSWORD = ""
    config.TRUST_PROXY_AUTH = True
    assert c.get("/").status_code == 200
    config.TRUST_PROXY_AUTH = False
    print("ok  dashboard TRUST_PROXY_AUTH bypass")

    # /keep invalid token -> 400
    assert c.get("/keep?token=garbage").status_code == 400
    print("ok  /keep invalid token -> 400")

    # /keep valid token for a queued item -> removes it
    exp = int(time.time()) + 3600
    tok = tokens.mint("s3cret", "jf-1", 3, exp)
    FakeMaintainerr.removed.clear()
    r = c.get(f"/keep?token={tok}")
    assert r.status_code == 200 and b"Kept" in r.data
    assert FakeMaintainerr.removed == [(3, "jf-1")], FakeMaintainerr.removed
    print("ok  /keep valid token -> removes item, shows Kept")

    # /keep valid token but item not queued -> Nothing to do, no removal
    tok2 = tokens.mint("s3cret", "jf-NOPE", 3, exp)
    FakeMaintainerr.removed.clear()
    r = c.get(f"/keep?token={tok2}")
    assert r.status_code == 200 and b"Nothing to do" in r.data
    assert FakeMaintainerr.removed == []
    print("ok  /keep for absent item -> Nothing to do, no removal")

    print("\n8/8 web tests passed")


if __name__ == "__main__":
    main()

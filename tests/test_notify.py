"""Unit tests for the pure decision logic — no network, no `requests` needed.

Run: python tests/test_notify.py
"""
import os
import sys
import tempfile
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sendoff import config, tokens  # noqa: E402
from sendoff.maintainerr import ScheduledItem, _parse_date  # noqa: E402
from sendoff.notify import select_due, classify_disappeared, compose, keep_link, Recipient  # noqa: E402
from sendoff.jellystat import JellystatClient, Watch  # noqa: E402
from sendoff.store import Store  # noqa: E402

TODAY = date(2026, 7, 13)


def _item(days_from_today, add_offset=-3, title="Blade Runner", tmdb=78, mtype="movie"):
    """Build a ScheduledItem whose deletion lands `days_from_today` from TODAY."""
    add_date = TODAY + timedelta(days=add_offset)
    delete_after = (TODAY + timedelta(days=days_from_today) - add_date).days
    return ScheduledItem(
        collection_id=1,
        collection_title="Cull",
        media_server_id=f"jf-{title}",
        tmdb_id=tmdb,
        tvdb_id=None,
        title=title,
        media_type=mtype,
        add_date=add_date,
        delete_after_days=delete_after,
    )


def test_select_due_window():
    items = [_item(10, title="Far"), _item(7, title="Edge"), _item(3, title="Soon"), _item(-1, title="Overdue")]
    due = {i.title for i in select_due(items, TODAY, notify_days_before=7)}
    assert due == {"Edge", "Soon", "Overdue"}, due
    print("ok  select_due window + overdue")


def test_select_due_immediate():
    items = [_item(30, title="Far"), _item(3, title="Soon"), _item(-1, title="Overdue")]
    # 0 (falsy) -> immediate: every scheduled item is due regardless of distance.
    due = {i.title for i in select_due(items, TODAY, notify_days_before=0)}
    assert due == {"Far", "Soon", "Overdue"}, due
    print("ok  select_due immediate (0 = notify on first sight)")


def test_compose_countdown():
    it = _item(14, title="Dune")
    _, text, html = compose(it, Recipient(email="a@b.com", role="requester"), days_until=14)
    assert "in 14 days" in text and "in <strong>14 days</strong>" in html
    _, t1, _ = compose(it, Recipient(email="a@b.com", role="requester"), days_until=1)
    assert "tomorrow" in t1
    _, t0, _ = compose(it, Recipient(email="a@b.com", role="requester"), days_until=0)
    assert "today" in t0
    print("ok  compose countdown (in N days / tomorrow / today)")


def test_days_until_and_deletion_date():
    it = _item(5)
    assert it.days_until_deletion(TODAY) == 5
    assert it.deletion_date == TODAY + timedelta(days=5)
    print("ok  deletion_date arithmetic")


def test_classify_disappeared():
    assert classify_disappeared(TODAY - timedelta(days=1), TODAY) == "removed"
    assert classify_disappeared(TODAY, TODAY) == "removed"
    assert classify_disappeared(TODAY + timedelta(days=2), TODAY) == "reprieved"
    assert classify_disappeared(None, TODAY) == "unknown"
    print("ok  classify_disappeared")


def test_parse_date_formats():
    assert _parse_date("2026-07-13") == date(2026, 7, 13)
    assert _parse_date("2026-07-13T09:30:00.000Z") == date(2026, 7, 13)
    assert _parse_date("") is None
    assert _parse_date(None) is None
    print("ok  _parse_date formats")


def test_compose_role_copy():
    it = _item(4, title="Dune", mtype="movie")
    subj, text, html = compose(it, Recipient(email="a@b.com", role="requester"))
    assert "Dune" in subj and "which you requested" in text
    assert "<strong>" in html
    _, wtext, _ = compose(it, Recipient(email="a@b.com", role="watcher"))
    assert "which you watched recently" in wtext
    # series wording
    show = _item(4, title="Severance", mtype="show")
    _, stext, _ = compose(show, Recipient(email="a@b.com", role="requester"))
    assert "series" in stext
    print("ok  compose role + media-type copy")


def test_recent_watchers_lookback():
    stat = JellystatClient(base_url="http://x", token="t")
    # Monkeypatch item_history to avoid network.
    stat.item_history = lambda item_id: [
        Watch(user_id="u1", user_name="alice", watched_on=TODAY - timedelta(days=5)),
        Watch(user_id="u2", user_name="bob", watched_on=TODAY - timedelta(days=200)),
        Watch(user_id="u1", user_name="alice", watched_on=TODAY - timedelta(days=1)),  # dup, newer
        Watch(user_id="u3", user_name="cara", watched_on=None),  # undated -> fail-open recent
    ]
    watchers = stat.recent_watchers("item", TODAY, lookback_days=90)
    by_id = {w.user_id: w for w in watchers}
    assert set(by_id) == {"u1", "u3"}, by_id            # bob dropped (too old)
    assert by_id["u1"].watched_on == TODAY - timedelta(days=1)  # kept newest for alice
    print("ok  recent_watchers lookback + dedupe + fail-open")


def test_store_dedupe():
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "t.db"))
        assert not s.already_notified("m1", "a@b.com", "leaving")
        s.record_notified("m1", "a@b.com", "leaving", "2026-07-13T00:00:00")
        assert s.already_notified("m1", "a@b.com", "leaving")
        assert not s.already_notified("m1", "a@b.com", "removed")  # different phase
        s.record_notified("m1", "a@b.com", "leaving", "later")  # idempotent
        rows = s.db.execute("SELECT COUNT(*) c FROM notified").fetchone()
        assert rows["c"] == 1
        print("ok  store dedupe key (item,email,phase)")


def test_token_round_trip():
    secret = "s3cret"
    exp = int(time.mktime((TODAY + timedelta(days=5)).timetuple()))
    tok = tokens.mint(secret, "jf-42", 7, exp)
    data = tokens.verify(secret, tok, now=time.mktime(TODAY.timetuple()))
    assert data == {"media_id": "jf-42", "collection_id": 7, "exp": exp}, data
    # expired
    assert tokens.verify(secret, tok, now=exp + 1) is None
    # wrong secret
    assert tokens.verify("other", tok, now=time.mktime(TODAY.timetuple())) is None
    # tampered payload
    bad = "A" + tok[1:] if tok[0] != "A" else "B" + tok[1:]
    assert tokens.verify(secret, bad) is None
    # garbage
    assert tokens.verify(secret, "not-a-token") is None
    assert tokens.verify(secret, "") is None
    print("ok  token mint/verify: authentic, expiry, tamper, wrong-secret")


def test_keep_link_and_button():
    it = _item(4, title="Arrival", tmdb=329)
    # Not configured -> no link, email falls back to instructions text.
    config.PUBLIC_BASE_URL = ""
    config.SIGNING_SECRET = ""
    assert keep_link(it) is None
    _, text, html = compose(it, Recipient(email="a@b.com", role="requester"))
    assert "/keep?token=" not in text and "Keep this" not in html
    # Configured -> link present and verifiable.
    config.PUBLIC_BASE_URL = "https://sendoff.example.com"
    config.SIGNING_SECRET = "s3cret"
    link = keep_link(it)
    assert link and link.startswith("https://sendoff.example.com/keep?token=")
    tok = link.split("token=", 1)[1]
    data = tokens.verify("s3cret", tok, now=time.mktime(TODAY.timetuple()))
    assert data["media_id"] == it.media_server_id and data["collection_id"] == 1
    _, text2, html2 = compose(it, Recipient(email="a@b.com", role="watcher"))
    assert link in text2 and "Keep this" in html2
    config.PUBLIC_BASE_URL = ""  # reset for isolation
    config.SIGNING_SECRET = ""
    print("ok  keep_link gating + button in email")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)}/{len(tests)} passed")

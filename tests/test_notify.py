"""Unit tests for the pure decision logic — no network, no `requests` needed.

Run: python tests/test_notify.py
"""
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sendoff import config, tokens  # noqa: E402
import sendoff.notify as notify  # noqa: E402
from sendoff.maintainerr import ScheduledItem, _parse_date  # noqa: E402
from sendoff.notify import (select_due, classify_disappeared, compose, compose_digest,  # noqa: E402
                            keep_link, run_once, Recipient)
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


def test_store_dedupe_and_requeue():
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "t.db"))
        A1, A2 = "2026-07-12", "2026-08-20"   # two queue stints of the same item
        assert not s.already_notified("m1", "a@b.com", "leaving", A1)
        s.record_notified("m1", "a@b.com", "leaving", A1, "t0")
        assert s.already_notified("m1", "a@b.com", "leaving", A1)
        assert not s.already_notified("m1", "a@b.com", "removed", A1)   # different phase
        # kept then re-queued later (new add-date) -> eligible to notify again
        assert not s.already_notified("m1", "a@b.com", "leaving", A2)
        s.record_notified("m1", "a@b.com", "leaving", A1, "later")      # idempotent
        assert s.db.execute("SELECT COUNT(*) c FROM notified").fetchone()["c"] == 1
        print("ok  store dedupe keyed on (item,email,phase,add_date) + re-queue")


def test_dashboard_poster():
    from sendoff.notify import build_dashboard

    def item(image_path):
        return ScheduledItem(collection_id=1, collection_title="C", media_server_id="m",
                             tmdb_id=1, tvdb_id=None, title="Dune", media_type="movie",
                             add_date=TODAY, delete_after_days=5, image_path=image_path)

    class FM:
        def __init__(self, it): self.it = it
        def scheduled_items(self): return [self.it]

    v = build_dashboard(FM(item("https://image.tmdb.org/x.jpg")), None, None, today=TODAY)
    assert v[0].poster == "https://image.tmdb.org/x.jpg"
    v2 = build_dashboard(FM(item("/relative.jpg")), None, None, today=TODAY)  # non-http dropped
    assert v2[0].poster is None
    v3 = build_dashboard(FM(item(None)), None, None, today=TODAY)
    assert v3[0].poster is None
    print("ok  build_dashboard maps http image_path -> poster")


def test_store_keep_events():
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "t.db"))
        assert s.recent_keeps() == []
        s.record_keep("a@b.com", "m1", "Dune", 3, "2026-07-13T16:45:00")
        s.record_keep(None, "m2", "Alien", 3, "2026-07-13T17:00:00")   # legacy v1, no email
        rows = s.recent_keeps()
        assert len(rows) == 2
        assert rows[0]["title"] == "Alien" and rows[0]["email"] is None   # newest first
        assert rows[1]["email"] == "a@b.com" and rows[1]["title"] == "Dune"
        print("ok  store keep events record + recent (newest first)")


def test_store_last_emailed():
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "t.db"))
        assert s.last_emailed("a@b.com") is None
        s.set_last_emailed("a@b.com", "2026-07-13T09:00:00")
        assert s.last_emailed("a@b.com") == "2026-07-13T09:00:00"
        s.set_last_emailed("a@b.com", "2026-07-14T09:00:00")   # upsert
        assert s.last_emailed("a@b.com") == "2026-07-14T09:00:00"
        print("ok  store last_emailed upsert")


def test_compose_digest():
    a = _item(14, title="Dune", tmdb=1)
    b = _item(3, title="Alien", tmdb=2)
    subj, text, html = compose_digest([(a, "requester"), (b, "watcher")],
                                      Recipient("x@y.com", "requester"), TODAY)
    assert "2 titles" in subj
    assert "Dune" in text and "Alien" in text
    assert "you requested it" in text and "you watched it recently" in text
    assert text.index("Alien") < text.index("Dune")   # sorted soonest-first
    assert "Dune" in html and "Alien" in html
    s1, _, _ = compose_digest([(a, "requester")], Recipient("x@y.com", "requester"), TODAY)
    assert "Dune" in s1 and "titles" not in s1          # single falls back to personal
    print("ok  compose_digest lists multiple; single falls back")


class _FakeSeerr:
    enabled = True
    def requester_email(self, tmdb):  # every item requested by the same person
        return "fan@x.com"
    def users(self):
        return []
    def user_by_jellyfin_id(self, uid):
        return None
    def user_by_jellyfin_username(self, name):
        return None


class _FakeStat:
    def recent_watchers(self, media_server_id, today):
        return []


def test_run_once_batches_dryrun_and_ratelimit():
    a = _item(14, title="Dune", tmdb=1)
    b = _item(10, title="Alien", tmdb=2)
    c = _item(20, title="Blade", tmdb=3)

    class FM:
        items = [a, b]
        def scheduled_items(self):
            return self.items

    saved = (config.DRY_RUN, config.NOTIFY_DAYS_BEFORE, config.NOTIFY_REQUESTER,
             config.NOTIFY_WATCHERS, config.DIGEST_HOUR, config.DIGEST_MINUTE, notify.send_email)
    try:
        config.NOTIFY_DAYS_BEFORE = 0
        config.NOTIFY_REQUESTER = True
        config.NOTIFY_WATCHERS = False
        config.DIGEST_HOUR = 9
        config.DIGEST_MINUTE = 0
        notify.send_email = lambda *a, **k: True
        now = datetime(2026, 7, 13, 9, 0, 0)   # 09:00 == DIGEST_HOUR -> eligible
        with tempfile.TemporaryDirectory() as d:
            s = Store(os.path.join(d, "t.db"))
            fm = FM()

            # DRY_RUN: one batched preview of two titles, and it writes NOTHING.
            config.DRY_RUN = True
            r = run_once(s, fm, _FakeSeerr(), _FakeStat(), today=TODAY, now=now)
            assert r.sent == 1 and r.titles_sent == 2, (r.sent, r.titles_sent)
            assert s.db.execute("SELECT COUNT(*) c FROM notified").fetchone()["c"] == 0

            # LIVE: sends the batch and records both titles.
            config.DRY_RUN = False
            r2 = run_once(s, fm, _FakeSeerr(), _FakeStat(), today=TODAY, now=now)
            assert r2.sent == 1 and r2.titles_sent == 2
            assert s.db.execute("SELECT COUNT(*) c FROM notified").fetchone()["c"] == 2

            # A new title the SAME day waits — already had today's digest.
            fm.items = [a, b, c]
            r3 = run_once(s, fm, _FakeSeerr(), _FakeStat(), today=TODAY, now=now)
            assert r3.batched_waiting == 1 and r3.sent == 0, (r3.batched_waiting, r3.sent)

            # Next day, at/after the digest hour, the new title goes out.
            r4 = run_once(s, fm, _FakeSeerr(), _FakeStat(),
                          today=date(2026, 7, 14), now=datetime(2026, 7, 14, 10, 0, 0))
            assert r4.sent == 1 and r4.titles_sent == 1, (r4.sent, r4.titles_sent)

        # Before DIGEST_HOUR, a fresh recipient holds for the daily send time.
        with tempfile.TemporaryDirectory() as d2:
            s2 = Store(os.path.join(d2, "t.db"))
            rh = run_once(s2, FM(), _FakeSeerr(), _FakeStat(),
                          today=date(2026, 7, 15), now=datetime(2026, 7, 15, 7, 0, 0))
            assert rh.sent == 0 and rh.batched_waiting >= 1, (rh.sent, rh.batched_waiting)
        print("ok  run_once: batches, DRY_RUN writes nothing, daily digest hour")
    finally:
        (config.DRY_RUN, config.NOTIFY_DAYS_BEFORE, config.NOTIFY_REQUESTER,
         config.NOTIFY_WATCHERS, config.DIGEST_HOUR, config.DIGEST_MINUTE, notify.send_email) = saved


def test_store_deletions():
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "t.db"))
        assert s.unreported_deletions() == []
        s.record_deletion("m1", "Dune", "movie", "2026-07-10", "2026-07-13T09:00:00")
        s.record_deletion("m2", "Severance", "show", "2026-07-11", "2026-07-13T09:05:00")
        new = s.unreported_deletions()
        assert [r["title"] for r in new] == ["Dune", "Severance"]    # oldest-first by removed_at
        # An older removal falls outside a 30-day window (ISO string compare).
        s.record_deletion("m0", "Old", "movie", "2026-05-01", "2026-05-01T09:00:00")
        recent = s.recent_deletions("2026-06-13")
        assert {r["title"] for r in recent} == {"Dune", "Severance"}  # 'Old' excluded
        assert recent[0]["removed_at"] >= recent[-1]["removed_at"]    # newest-first
        # Marking everything reported empties the unreported set.
        s.mark_deletions_reported([r["id"] for r in s.unreported_deletions()], "2026-07-13T09:10:00")
        assert s.unreported_deletions() == []
        print("ok  store deletions: record, unreported (oldest-first), window, mark reported")


def test_store_migrates_items_media_type():
    import sqlite3
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        # Simulate a DB from before media_type existed on the items table.
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE items (media_server_id TEXT PRIMARY KEY, title TEXT, "
                  "deletion_date TEXT, last_seen TEXT)")
        c.execute("INSERT INTO items VALUES ('m1','Dune','2026-07-10','2026-07-01T00:00:00')")
        c.commit(); c.close()
        s = Store(path)   # opening runs the migration
        cols = {r["name"] for r in s.db.execute("PRAGMA table_info(items)")}
        assert "media_type" in cols                        # column added, row preserved
        s.upsert_item("m1", "Dune", "movie", "2026-07-10", "2026-07-13T00:00:00")
        row = s.db.execute("SELECT media_type FROM items WHERE media_server_id='m1'").fetchone()
        assert row["media_type"] == "movie"
        print("ok  store migrates old items table (adds media_type)")


def test_run_once_deletion_digest():
    it = _item(0, title="Dune", tmdb=1)   # deletion date == TODAY

    class FM:
        items = [it]
        def scheduled_items(self):
            return self.items

    sent = []
    saved = (config.DRY_RUN, config.NOTIFY_REQUESTER, config.NOTIFY_WATCHERS,
             config.NOTIFY_ON_REMOVAL, config.DELETION_DIGEST_TO, config.DELETION_DIGEST_DAYS,
             config.DIGEST_HOUR, config.DIGEST_MINUTE, notify.send_email)
    try:
        config.DRY_RUN = False
        config.NOTIFY_REQUESTER = False       # isolate: no "leaving" emails this test
        config.NOTIFY_WATCHERS = False
        config.NOTIFY_ON_REMOVAL = False      # deletion recording must NOT depend on this
        config.DELETION_DIGEST_TO = "admin@x.com"
        config.DELETION_DIGEST_DAYS = 30
        config.DIGEST_HOUR = 9
        config.DIGEST_MINUTE = 0
        notify.send_email = lambda to, subject, text, html=None: (
            sent.append((to, subject, text, html)) or True)
        now = datetime(2026, 7, 13, 9, 0, 0)   # 09:00 == DIGEST_HOUR -> eligible
        with tempfile.TemporaryDirectory() as d:
            s = Store(os.path.join(d, "t.db"))
            fm = FM()

            # Cycle 1: item present -> tracked, nothing removed yet.
            r1 = run_once(s, fm, _FakeSeerr(), _FakeStat(), today=TODAY, now=now)
            assert r1.deletion_digest_sent == 0 and sent == []

            # Cycle 2: item gone AND its date has passed -> recorded + digest sent.
            fm.items = []
            r2 = run_once(s, fm, _FakeSeerr(), _FakeStat(), today=TODAY, now=now)
            assert r2.deletion_digest_sent == 1, r2
            assert len(sent) == 1
            to, subject, text, html = sent[0]
            assert to == "admin@x.com"
            assert "removed from" in subject and "Dune" in text
            assert "last 30 days" in text and "Dune" in html

            # Cycle 3: same day, nothing new -> capped at one/day, no second email.
            r3 = run_once(s, fm, _FakeSeerr(), _FakeStat(), today=TODAY, now=now)
            assert r3.deletion_digest_sent == 0 and len(sent) == 1

            # The deletion is on the ledger, now marked reported.
            assert len(s.recent_deletions("2000-01-01")) == 1
            assert s.unreported_deletions() == []
        print("ok  run_once: records deletions + sends daily admin digest once")
    finally:
        (config.DRY_RUN, config.NOTIFY_REQUESTER, config.NOTIFY_WATCHERS,
         config.NOTIFY_ON_REMOVAL, config.DELETION_DIGEST_TO, config.DELETION_DIGEST_DAYS,
         config.DIGEST_HOUR, config.DIGEST_MINUTE, notify.send_email) = saved


def test_resolve_recipients_skips_non_email():
    from sendoff.notify import resolve_recipients, valid_email
    from sendoff.jellyseerr import User
    from sendoff.jellystat import Watch

    # The validator: real addresses pass, usernames / empties / partials fail.
    assert valid_email("fan@x.com") and valid_email("  a.b@sub.dom.io ")
    assert not valid_email("bingus") and not valid_email("") and not valid_email(None)
    assert not valid_email("a@b") and not valid_email("no domain@")

    it = _item(5, title="Dune", tmdb=1)

    class Seerr:
        enabled = True
        def __init__(self, req=None, watcher_user=None):
            self._req, self._wu = req, watcher_user
        def requester_email(self, tmdb):
            return self._req
        def users(self):
            return []
        def user_by_jellyfin_id(self, uid):
            return self._wu
        def user_by_jellyfin_username(self, n):
            return None

    class Stat:
        def __init__(self, watchers=()):
            self._w = list(watchers)
        def recent_watchers(self, msid, today):
            return self._w

    saved = (config.NOTIFY_REQUESTER, config.NOTIFY_WATCHERS)
    try:
        # Requester whose "email" is really a username -> no recipient, no send.
        config.NOTIFY_REQUESTER, config.NOTIFY_WATCHERS = True, False
        assert resolve_recipients(it, Seerr(req="bingus"), Stat(), TODAY) == []
        r = resolve_recipients(it, Seerr(req="fan@x.com"), Stat(), TODAY)
        assert len(r) == 1 and r[0].email == "fan@x.com"

        # Watcher whose Jellyseerr record carries the username as email -> skipped.
        config.NOTIFY_REQUESTER, config.NOTIFY_WATCHERS = False, True
        bingus = User(id=1, email="bingus", display_name="Bingus",
                      jellyfin_user_id="u1", jellyfin_username="bingus")
        stat = Stat([Watch(user_id="u1", user_name="bingus", watched_on=TODAY)])
        assert resolve_recipients(it, Seerr(watcher_user=bingus), stat, TODAY) == []
    finally:
        (config.NOTIFY_REQUESTER, config.NOTIFY_WATCHERS) = saved
    print("ok  resolve_recipients skips empty / username-as-email addresses")


def test_from_header():
    from sendoff import mail
    saved = (config.EMAIL_FROM, config.SENDER_NAME, config.SMTP_USER)
    try:
        # EMAIL_FROM already "Name <addr>" -> use as-is, no double-wrap
        config.EMAIL_FROM = "Bastien Boutonnet - Media Server <bastien.b@icloud.com>"
        config.SENDER_NAME = "Ignored"
        h = mail._from_header()
        assert h.count("<") == 1 and "bastien.b@icloud.com" in h and "Media Server" in h, h
        # bare address -> wrap with SENDER_NAME
        config.EMAIL_FROM = "bastien.b@icloud.com"
        config.SENDER_NAME = "My Server"
        assert mail._from_header() == "My Server <bastien.b@icloud.com>", mail._from_header()
        # empty EMAIL_FROM -> falls back to SMTP_USER
        config.EMAIL_FROM = ""
        config.SMTP_USER = "u@x.com"
        assert mail._from_header() == "My Server <u@x.com>"
        print("ok  _from_header: no double-wrap, bare-addr wrap, fallback")
    finally:
        (config.EMAIL_FROM, config.SENDER_NAME, config.SMTP_USER) = saved


def test_token_round_trip():
    secret = "s3cret"
    exp = int(time.mktime((TODAY + timedelta(days=5)).timetuple()))
    tok = tokens.mint(secret, "jf-42", 7, exp)
    data = tokens.verify(secret, tok, now=time.mktime(TODAY.timetuple()))
    assert data == {"media_id": "jf-42", "collection_id": 7, "email": None, "exp": exp}, data
    # v2 carries the recipient email
    tok_v2 = tokens.mint(secret, "jf-42", 7, exp, email="who@x.com")
    d2 = tokens.verify(secret, tok_v2, now=time.mktime(TODAY.timetuple()))
    assert d2["email"] == "who@x.com" and d2["media_id"] == "jf-42" and d2["collection_id"] == 7
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
    link = keep_link(it, "a@b.com")
    assert link and link.startswith("https://sendoff.example.com/keep?token=")
    tok = link.split("token=", 1)[1]
    data = tokens.verify("s3cret", tok, now=time.mktime(TODAY.timetuple()))
    assert data["media_id"] == it.media_server_id and data["collection_id"] == 1
    assert data["email"] == "a@b.com"                 # per-recipient token
    # compose embeds a link whose token carries that recipient's email
    _, text2, html2 = compose(it, Recipient(email="watcher@x.com", role="watcher"))
    assert "/keep?token=" in text2 and "Keep this" in html2
    tok2 = text2.split("token=", 1)[1].split()[0].strip()
    assert tokens.verify("s3cret", tok2, now=time.mktime(TODAY.timetuple()))["email"] == "watcher@x.com"
    config.PUBLIC_BASE_URL = ""  # reset for isolation
    config.SIGNING_SECRET = ""
    print("ok  keep_link gating + per-recipient token in email")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)}/{len(tests)} passed")

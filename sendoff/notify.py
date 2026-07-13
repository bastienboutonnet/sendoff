"""The join: turn Maintainerr's deletion schedule into per-user emails.

The decision logic (`select_due`, `classify_disappeared`) is pure and unit-tested.
Recipient resolution and sending are the I/O layer on top.
"""
from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from . import config, tokens
from .mail import send_email
from .maintainerr import ScheduledItem

log = logging.getLogger("sendoff.notify")


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------

def select_due(items: Iterable[ScheduledItem], today: date, notify_days_before: int) -> list[ScheduledItem]:
    """Which items warrant a "leaving soon" notice this cycle.

    Immediate mode (`notify_days_before` falsy, the default): every scheduled
    item is due the moment it appears — the dedupe ledger then ensures each
    person is emailed once. Windowed mode (`> 0`): only items whose deletion is
    within that many days (including overdue ones still in the collection)."""
    if not notify_days_before:
        return list(items)
    return [it for it in items if it.days_until_deletion(today) <= notify_days_before]


def classify_disappeared(deletion_date: Optional[date], today: date) -> str:
    """An item we tracked is no longer in any collection. Decide why:
      - 'removed'  : its deletion date has arrived/passed -> Maintainerr deleted it.
      - 'reprieved': it vanished before its date -> someone pulled it out (kept).
      - 'unknown'  : we have no deletion date on file.
    """
    if deletion_date is None:
        return "unknown"
    return "removed" if today >= deletion_date else "reprieved"


# ---------------------------------------------------------------------------
# Recipient resolution (I/O)
# ---------------------------------------------------------------------------

@dataclass
class Recipient:
    email: str
    role: str          # requester | watcher
    name: Optional[str] = None


def resolve_recipients(item: ScheduledItem, seerr, stat, today: date) -> list[Recipient]:
    """Who should hear that `item` is leaving: the requester (Jellyseerr) and
    recent watchers (Jellystat -> Jellyseerr email). De-duplicated by email;
    requester role wins if someone is both."""
    by_email: dict[str, Recipient] = {}

    if config.NOTIFY_REQUESTER and seerr is not None:
        try:
            email = seerr.requester_email(item.tmdb_id)
        except Exception as e:
            log.warning("requester lookup failed for %r: %s", item.title, e)
            email = None
        if email:
            by_email[email.lower()] = Recipient(email=email, role="requester")

    if config.NOTIFY_WATCHERS and stat is not None and seerr is not None:
        try:
            watchers = stat.recent_watchers(item.media_server_id, today)
        except Exception as e:
            log.warning("watcher lookup failed for %r: %s", item.title, e)
            watchers = []
        for w in watchers:
            user = None
            if w.user_id:
                user = seerr.user_by_jellyfin_id(w.user_id)
            if user is None and w.user_name:
                user = seerr.user_by_jellyfin_username(w.user_name)
            email = user.email if user else None
            if not email:
                continue
            key = email.lower()
            if key in by_email:      # already a requester -> keep that role
                continue
            by_email[key] = Recipient(email=email, role="watcher",
                                      name=user.display_name if user else w.user_name)
    return list(by_email.values())


# ---------------------------------------------------------------------------
# Email composition
# ---------------------------------------------------------------------------

def _friendly_type(media_type: str) -> str:
    return {"show": "series", "season": "series", "episode": "episode"}.get(media_type, "movie")


def keep_link(item: ScheduledItem, email: Optional[str] = None) -> Optional[str]:
    """A one-click self-service keep URL for this item, or None if the web app /
    signing secret isn't configured. `email` is embedded so the keep can be
    attributed to the person who clicked. The token expires one day after the
    scheduled deletion, so a link is useless once the item is gone."""
    if not (config.PUBLIC_BASE_URL and config.SIGNING_SECRET):
        return None
    exp = int(time.mktime((item.deletion_date + timedelta(days=1)).timetuple()))
    token = tokens.mint(config.SIGNING_SECRET, item.media_server_id,
                        item.collection_id, exp, email=email)
    return f"{config.PUBLIC_BASE_URL}/keep?token={token}"


def _when_phrases(deletion_date: date, days_until: Optional[int]) -> tuple[str, str]:
    """(plain, html) phrasings of when the deletion happens, with the countdown.
    The html variant bolds the salient part."""
    when = deletion_date.strftime("%A %-d %B %Y")
    if days_until is None:
        return f"on {when}", f"on <strong>{html.escape(when)}</strong>"
    if days_until <= 0:
        return f"today ({when})", f"<strong>today</strong> ({html.escape(when)})"
    if days_until == 1:
        return f"tomorrow ({when})", f"<strong>tomorrow</strong> ({html.escape(when)})"
    e = html.escape(when)
    return (f"on {when} — in {days_until} days",
            f"on <strong>{e}</strong> — in <strong>{days_until} days</strong>")


def compose(item: ScheduledItem, recipient: Recipient,
            days_until: Optional[int] = None) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) tailored to the recipient's role.
    `days_until` (deletion date minus today) is woven in as a countdown."""
    kind = _friendly_type(item.media_type)
    when, when_html = _when_phrases(item.deletion_date, days_until)
    subject = f"“{item.title}” is scheduled to leave {config.SERVER_NAME} {when}"

    if recipient.role == "requester":
        lede = f"The {kind} “{item.title}”, which you requested, is scheduled to be removed"
    else:
        lede = f"The {kind} “{item.title}”, which you watched recently, is scheduled to be removed"

    link = keep_link(item, recipient.email)
    if link:
        keep_text = f"If you'd like to keep it, open this link before then:\n{link}"
    else:
        keep_text = f"If you'd like to keep it, {config.KEEP_INSTRUCTIONS}"

    text = (
        f"{lede} from {config.SERVER_NAME} {when} to free up space.\n\n"
        f"{keep_text}\n\n"
        f"— {config.SENDER_NAME}"
    )

    e_title = html.escape(item.title)
    e_lede = html.escape(lede)
    e_server = html.escape(config.SERVER_NAME)
    e_sender = html.escape(config.SENDER_NAME)
    if link:
        keep_html = (
            f'<a href="{html.escape(link)}" '
            'style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;'
            'padding:10px 18px;border-radius:8px;font-size:14px;font-weight:600">Keep this</a>'
            '<div style="font-size:12px;color:#71717a;margin-top:8px">'
            'Clicking keeps it on the server. Do nothing and it will be removed.</div>'
        )
    else:
        keep_html = f'Want to keep it? {html.escape(config.KEEP_INSTRUCTIONS)}'
    html_body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:520px;margin:0 auto;color:#1a1a1a">
  <h2 style="font-size:18px;margin:0 0 12px">Leaving soon: {e_title}</h2>
  <p style="font-size:15px;line-height:1.5;margin:0 0 16px">
    {e_lede} from <strong>{e_server}</strong> {when_html} to free up space.
  </p>
  <div style="background:#f4f4f5;border-radius:8px;padding:14px;font-size:14px;line-height:1.5;margin:0 0 16px">
    {keep_html}
  </div>
  <p style="font-size:13px;color:#71717a;margin:0">— {e_sender}</p>
</div>"""
    return subject, text, html_body


def _reason(role: str) -> str:
    return "you requested it" if role == "requester" else "you watched it recently"


def compose_digest(entries, recipient: Recipient, today: date) -> tuple[str, str, str]:
    """Batch email for one person. `entries` is a list of (item, role). One entry
    yields the personal single-title email; several yield a listed digest."""
    if len(entries) == 1:
        item, role = entries[0]
        return compose(item, Recipient(recipient.email, role, recipient.name),
                       days_until=item.days_until_deletion(today))

    entries = sorted(entries, key=lambda e: e[0].deletion_date)
    n = len(entries)
    server = config.SERVER_NAME
    subject = f"{n} titles are scheduled to leave {server} soon"

    lines = [f"These {n} titles on {server} are scheduled to be removed to free up space:", ""]
    for item, role in entries:
        when, _ = _when_phrases(item.deletion_date, item.days_until_deletion(today))
        lines.append(f"• {item.title} — leaves {when} — {_reason(role)}")
        link = keep_link(item, recipient.email)
        if link:
            lines.append(f"    Keep it: {link}")
    lines += ["", "Do nothing and each will be removed on the date shown."]
    if not (config.PUBLIC_BASE_URL and config.SIGNING_SECRET):
        lines.append(f"To keep any of them, {config.KEEP_INSTRUCTIONS}")
    lines += ["", f"— {config.SENDER_NAME}"]
    text = "\n".join(lines)

    rows = []
    for item, role in entries:
        _, when_html = _when_phrases(item.deletion_date, item.days_until_deletion(today))
        link = keep_link(item, recipient.email)
        if link:
            action = (f'<a href="{html.escape(link)}" style="display:inline-block;background:#2563eb;'
                      'color:#fff;text-decoration:none;padding:8px 14px;border-radius:6px;font-size:13px;'
                      'font-weight:600">Keep this</a>')
        else:
            action = f'<span style="font-size:13px;color:#71717a">{html.escape(config.KEEP_INSTRUCTIONS)}</span>'
        rows.append(
            '<tr><td style="padding:14px 0;border-bottom:0.5px solid #e5e5e5">'
            f'<div style="font-size:15px;font-weight:600;color:#1a1a1a">{html.escape(item.title)}</div>'
            f'<div style="font-size:13px;color:#71717a;margin:2px 0 8px">leaves {when_html} · {html.escape(_reason(role))}</div>'
            f'{action}</td></tr>'
        )
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:540px;margin:0 auto;color:#1a1a1a">'
        f'<h2 style="font-size:18px;margin:0 0 6px">{n} titles leaving {html.escape(server)} soon</h2>'
        '<p style="font-size:14px;line-height:1.5;color:#444;margin:0 0 8px">'
        'These are scheduled to be removed to free up space. Keep any you want to hold onto:</p>'
        f'<table style="width:100%;border-collapse:collapse">{"".join(rows)}</table>'
        f'<p style="font-size:13px;color:#71717a;margin:16px 0 0">— {html.escape(config.SENDER_NAME)}</p>'
        '</div>'
    )
    return subject, text, html_body


# ---------------------------------------------------------------------------
# Dashboard view model (read-only)
# ---------------------------------------------------------------------------

@dataclass
class WatcherView:
    name: Optional[str]
    email: Optional[str]
    last_watched: Optional[date]


@dataclass
class ItemView:
    title: str
    collection_title: str
    media_type: str
    deletion_date: date
    days_until: int
    size_bytes: Optional[int]
    requester_email: Optional[str]
    requester_name: Optional[str]
    watchers: list[WatcherView] = field(default_factory=list)


def build_dashboard(maintainerr, seerr, stat, today: Optional[date] = None) -> list[ItemView]:
    """Live read-only view of everything scheduled for deletion, with the people
    who requested/watched each item. Sorted soonest-to-delete first. Does live
    lookups per item — fine for personal library sizes."""
    today = today or date.today()
    views: list[ItemView] = []
    for item in maintainerr.scheduled_items():
        req_email = req_name = None
        if seerr is not None and item.tmdb_id:
            try:
                req_email = seerr.requester_email(item.tmdb_id)
                if req_email:
                    for u in seerr.users():
                        if u.email and u.email.lower() == req_email.lower():
                            req_name = u.display_name
                            break
            except Exception as e:
                log.warning("dashboard requester lookup failed for %r: %s", item.title, e)

        watchers: list[WatcherView] = []
        if stat is not None and seerr is not None:
            try:
                for w in stat.recent_watchers(item.media_server_id, today):
                    user = (seerr.user_by_jellyfin_id(w.user_id) if w.user_id else None)
                    if user is None and w.user_name:
                        user = seerr.user_by_jellyfin_username(w.user_name)
                    watchers.append(WatcherView(
                        name=(user.display_name if user else w.user_name),
                        email=(user.email if user else None),
                        last_watched=w.watched_on,
                    ))
            except Exception as e:
                log.warning("dashboard watcher lookup failed for %r: %s", item.title, e)

        views.append(ItemView(
            title=item.title,
            collection_title=item.collection_title,
            media_type=item.media_type,
            deletion_date=item.deletion_date,
            days_until=item.days_until_deletion(today),
            size_bytes=None,
            requester_email=req_email,
            requester_name=req_name,
            watchers=watchers,
        ))
    views.sort(key=lambda v: v.deletion_date)
    return views


# ---------------------------------------------------------------------------
# One poll cycle
# ---------------------------------------------------------------------------

@dataclass
class RunSummary:
    scheduled: int = 0
    due: int = 0
    sent: int = 0             # digest emails sent
    titles_sent: int = 0      # titles across those digests
    skipped_dupes: int = 0
    no_recipients: int = 0
    batched_waiting: int = 0  # recipients holding for their next batch window
    removed_notifications: int = 0

    def __str__(self) -> str:
        extra = ""
        if self.batched_waiting:
            extra += f", {self.batched_waiting} waiting for batch window"
        if self.removed_notifications:
            extra += f", {self.removed_notifications} removal notices"
        return (f"{self.scheduled} scheduled, {self.due} due, {self.sent} email(s) sent"
                f" covering {self.titles_sent} title(s)"
                f" ({self.skipped_dupes} already-sent, {self.no_recipients} no-recipient){extra}")


def run_once(store, maintainerr, seerr, stat,
             today: Optional[date] = None, now: Optional[datetime] = None) -> RunSummary:
    today = today or date.today()
    now = now or datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    summary = RunSummary()

    items = maintainerr.scheduled_items()
    summary.scheduled = len(items)
    present_ids = {it.media_server_id for it in items}
    due = select_due(items, today, config.NOTIFY_DAYS_BEFORE)
    summary.due = len(due)

    # Group each person's not-yet-notified titles together (batching). Dedupe is
    # keyed on the item's add-date, so a re-queued title (new stint) is eligible
    # again while a continuous stint is only emailed once.
    pending: dict[str, dict] = {}   # email -> {"name": str|None, "entries": [(item, role)]}
    for item in due:
        recips = resolve_recipients(item, seerr, stat, today)
        if not recips:
            summary.no_recipients += 1
            continue
        add_key = item.add_date.isoformat()
        for r in recips:
            if store.already_notified(item.media_server_id, r.email, "leaving", add_key):
                summary.skipped_dupes += 1
                continue
            slot = pending.setdefault(r.email, {"name": r.name, "entries": []})
            slot["entries"].append((item, r.role))

    # Track present items for removal detection (skip all writes in DRY_RUN).
    if not config.DRY_RUN:
        for item in items:
            store.upsert_item(item.media_server_id, item.title,
                              item.deletion_date.isoformat(), now_iso)

    # One batched digest per recipient per calendar day, held until DIGEST_HOUR.
    for email, slot in pending.items():
        last = store.last_emailed(email)
        if last:
            try:
                if datetime.fromisoformat(last).date() == today:
                    summary.batched_waiting += 1      # already sent today's digest
                    continue
            except ValueError:
                pass
        if (now.hour, now.minute) < (config.DIGEST_HOUR, config.DIGEST_MINUTE):
            summary.batched_waiting += 1              # holding until the daily digest time
            continue
        entries = slot["entries"]
        recipient = Recipient(email=email, role=entries[0][1], name=slot["name"])
        subject, text, html_body = compose_digest(entries, recipient, today)
        titles = ", ".join(f"“{it.title}”" for it, _ in entries)
        if config.DRY_RUN:
            log.info("[DRY_RUN] would email %s about %d title(s): %s", email, len(entries), titles)
        else:
            if not send_email(email, subject, text, html_body):
                log.warning("digest send to %s failed, will retry next cycle", email)
                continue
            for it, _role in entries:
                store.record_notified(it.media_server_id, email, "leaving",
                                      it.add_date.isoformat(), now_iso)
            store.set_last_emailed(email, now_iso)
        summary.sent += 1
        summary.titles_sent += len(entries)

    # Optional removal confirmations for items that have disappeared.
    if config.NOTIFY_ON_REMOVAL and not config.DRY_RUN:
        for row in store.all_items():
            msid = row["media_server_id"]
            if msid in present_ids:
                continue
            del_date = None
            if row["deletion_date"]:
                try:
                    del_date = date.fromisoformat(row["deletion_date"])
                except ValueError:
                    pass
            if classify_disappeared(del_date, today) != "removed":
                store.delete_item(msid)   # reprieved / unknown -> stop tracking, don't notify
                continue
            summary.removed_notifications += _notify_removed(store, row, now_iso)
            store.delete_item(msid)

    return summary


def _notify_removed(store, row, now_iso: str) -> int:
    """Tell everyone we warned that the item is now gone. Returns count sent."""
    title = row["title"] or "A title"
    msid = row["media_server_id"]
    stint = row["deletion_date"] or ""   # stint key for the 'removed' phase
    sent = 0
    for email in store.warned_emails(msid):
        if store.already_notified(msid, email, "removed", stint):
            continue
        subject = f"“{title}” has been removed from {config.SERVER_NAME}"
        text = (f"“{title}” has now been removed from {config.SERVER_NAME} to free up space.\n\n"
                f"You can request it again any time if you'd like it back.\n\n— {config.SENDER_NAME}")
        if not send_email(email, subject, text):
            continue
        store.record_notified(msid, email, "removed", stint, now_iso)
        sent += 1
    return sent

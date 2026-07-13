"""Jellystat REST client (read-only).

Given a Jellyfin item id (Maintainerr's `mediaServerId`), return the distinct
Jellyfin users who played it within the lookback window. Those user ids are then
mapped to email addresses via Jellyseerr's user table.

Auth: the `x-api-token` header (Jellystat Settings -> API Keys).

Endpoint:
    POST /api/getItemHistory   body: {"itemid": "<jellyfin item id>"}
    -> rows of playback activity, each carrying a UserId / UserName and a
       timestamp (ActivityDateInserted).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from . import config

log = logging.getLogger("sendoff.jellystat")


@dataclass
class Watch:
    user_id: Optional[str]      # Jellyfin user id
    user_name: Optional[str]
    watched_on: Optional[date]


def _parse_dt(value) -> Optional[date]:
    if not value or not isinstance(value, str):
        return None
    value = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


class JellystatClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base = (base_url or config.JELLYSTAT_URL).rstrip("/")
        self.token = token if token is not None else config.JELLYSTAT_TOKEN

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.token)

    def _post(self, path: str, body: dict):
        from . import net
        r = net.session().post(
            f"{self.base}{path}",
            json=body,
            headers={"x-api-token": self.token},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def item_history(self, item_id: str) -> list[Watch]:
        if not self.enabled or not item_id:
            return []
        try:
            rows = self._post("/api/getItemHistory", {"itemid": item_id})
        except Exception as e:
            log.warning("getItemHistory(%s) failed: %s", item_id, e)
            return []
        # Jellystat may return a bare list or {"results": [...]}.
        if isinstance(rows, dict):
            rows = rows.get("results") or rows.get("data") or []
        out: list[Watch] = []
        for row in rows or []:
            out.append(Watch(
                user_id=row.get("UserId") or row.get("userId"),
                user_name=row.get("UserName") or row.get("userName"),
                watched_on=_parse_dt(
                    row.get("ActivityDateInserted")
                    or row.get("date")
                    or row.get("LastActivityDate")
                ),
            ))
        return out

    def recent_watchers(self, item_id: str, today: date,
                        lookback_days: int | None = None) -> list[Watch]:
        """Distinct users who watched `item_id` within the lookback window.
        A watch with no parseable date is treated as recent (fail-open: better
        to over-notify a watcher than to silently delete under someone)."""
        lookback = lookback_days if lookback_days is not None else config.WATCHER_LOOKBACK_DAYS
        cutoff = today - timedelta(days=lookback)
        seen: dict[str, Watch] = {}
        for w in self.item_history(item_id):
            if w.watched_on is not None and w.watched_on < cutoff:
                continue
            key = w.user_id or (w.user_name or "").lower()
            if not key:
                continue
            # Keep the most recent watch per user.
            prev = seen.get(key)
            if prev is None or (w.watched_on and (not prev.watched_on or w.watched_on > prev.watched_on)):
                seen[key] = w
        return list(seen.values())

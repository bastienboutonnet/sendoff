"""Maintainerr REST client (read-only).

Maintainerr is the source of truth for what is scheduled for deletion. Each
Collection has a `deleteAfterDays` grace window; each CollectionMedia row carries
`mediaServerId` (the Jellyfin item id), `tmdbId`, `tvdbId` and `addDate` (when it
entered the collection). The scheduled deletion date is therefore:

    deletion_date = addDate + deleteAfterDays

Maintainerr has NO authentication — do not expose it publicly; we only reach it
on the internal network.

Endpoints used (verified against Maintainerr 3.17.x source):
    GET /api/collections                       -> list of collections
    GET /api/collections/media/?collectionId=  -> CollectionMedia rows
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from . import config

log = logging.getLogger("sendoff.maintainerr")


@dataclass
class ScheduledItem:
    """One media item scheduled for deletion by Maintainerr."""
    collection_id: int            # needed to reprieve (remove) the item
    collection_title: str
    media_server_id: str          # Jellyfin item id -> Jellystat lookups
    tmdb_id: Optional[int]        # -> Jellyseerr requester lookup
    tvdb_id: Optional[int]
    title: str
    media_type: str               # movie | show | season | episode
    add_date: date                # entered the collection
    delete_after_days: int
    image_path: Optional[str] = None

    @property
    def deletion_date(self) -> date:
        return self.add_date + timedelta(days=self.delete_after_days)

    def days_until_deletion(self, today: date) -> int:
        return (self.deletion_date - today).days


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, str):
        # Accept both "2026-07-13" and full ISO timestamps.
        value = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            try:
                return datetime.strptime(value[:10], "%Y-%m-%d").date()
            except ValueError:
                return None
    return None


class MaintainerrClient:
    def __init__(self, base_url: str | None = None):
        self.base = (base_url or config.MAINTAINERR_URL).rstrip("/")

    def _get(self, path: str, params: dict | None = None):
        from . import net
        r = net.session().get(f"{self.base}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def remove_media(self, collection_id: int, media_id: str) -> bool:
        """Reprieve an item: remove it from a deletion collection. Per Maintainerr
        docs this also creates a collection exclusion, so a rule won't re-add it.
        Used by the self-service /keep endpoint.

        VERIFY against your instance: `mediaId` here is Maintainerr's media
        identifier for the row. We pass the mediaServerId (Jellyfin item id); if
        your Maintainerr build expects the collection_media row id instead,
        adjust the caller to pass that.
        """
        from . import net
        try:
            r = net.session().delete(
                f"{self.base}/api/collections/media",
                params={"mediaId": media_id, "collectionId": collection_id},
                timeout=30,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            log.warning("remove_media(collection=%s, media=%s) failed: %s",
                        collection_id, media_id, e)
            return False

    def collections(self) -> list[dict]:
        return self._get("/api/collections") or []

    def collection_media(self, collection_id: int) -> list[dict]:
        """All media in a collection, WITH metadata (title, type). Uses the
        paginated content endpoint whose items carry `mediaData` — the bare
        /api/collections/media endpoint omits the human title."""
        out: list[dict] = []
        page = 1
        while True:
            d = self._get(f"/api/collections/media/{collection_id}/content/{page}", {"size": 100})
            items = (d.get("items") if isinstance(d, dict) else d) or []
            out.extend(items)
            total = d.get("totalSize", len(out)) if isinstance(d, dict) else len(out)
            if not items or len(out) >= total:
                break
            page += 1
        return out

    def scheduled_items(self) -> list[ScheduledItem]:
        """Every media item currently in a deleting collection, with its
        computed deletion date. Collections without a deleteAfterDays never
        delete and are skipped. COLLECTION_ALLOWLIST, if set, further restricts
        to named collections."""
        out: list[ScheduledItem] = []
        allow = {t.lower() for t in config.COLLECTION_ALLOWLIST}
        for col in self.collections():
            title = col.get("title") or ""
            delete_after = col.get("deleteAfterDays")
            if not delete_after:  # None or 0 -> collection does not delete
                continue
            if allow and title.lower() not in allow:
                continue
            try:
                media = self.collection_media(col["id"])
            except Exception as e:
                log.warning("failed to read media for collection %r: %s", title, e)
                continue
            for m in media:
                add_date = _parse_date(m.get("addDate"))
                msid = m.get("mediaServerId")
                if not add_date or not msid:
                    log.debug("skipping media without addDate/mediaServerId: %s", m)
                    continue
                out.append(ScheduledItem(
                    collection_id=int(col["id"]),
                    collection_title=title,
                    media_server_id=str(msid),
                    tmdb_id=m.get("tmdbId"),
                    tvdb_id=m.get("tvdbId"),
                    title=_media_title(m) or title,
                    media_type=(m.get("mediaData") or {}).get("type") or col.get("type") or "movie",
                    add_date=add_date,
                    delete_after_days=int(delete_after),
                    image_path=m.get("image_path"),
                ))
        return out


def _media_title(m: dict) -> Optional[str]:
    """Best-effort human title from a CollectionMedia row. The bare entity has
    no title, but list endpoints often join `mediaData`; fall back gracefully."""
    data = m.get("mediaData") or {}
    for key in ("title", "name", "originalTitle"):
        if data.get(key):
            return data[key]
    return m.get("title")

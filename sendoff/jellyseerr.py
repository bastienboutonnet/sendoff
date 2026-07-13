"""Jellyseerr REST client (read-only).

Two jobs:
  1. tmdbId -> the user who REQUESTED an item (+ their email).
  2. the full user table, so a Jellyfin userId from Jellystat can be mapped to
     an email address.

Auth: the `X-Api-Key` header (Jellyseerr Settings -> General -> API Key).

Endpoints (Jellyseerr / Overseerr v1):
    GET /api/v1/request?take=&skip=&filter=all  -> paginated requests
    GET /api/v1/user?take=&skip=                 -> paginated users
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from . import config

log = logging.getLogger("sendoff.jellyseerr")


@dataclass
class User:
    id: int
    email: Optional[str]
    display_name: Optional[str]
    jellyfin_user_id: Optional[str]
    jellyfin_username: Optional[str]


class JellyseerrClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base = (base_url or config.JELLYSEERR_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else config.JELLYSEERR_API_KEY
        self._users: Optional[list[User]] = None
        self._requester_by_tmdb: Optional[dict[int, int]] = None  # tmdbId -> userId

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.api_key)

    def _get(self, path: str, params: dict | None = None):
        import requests
        r = requests.get(
            f"{self.base}{path}",
            params=params,
            headers={"X-Api-Key": self.api_key},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def _paginate(self, path: str, extra: dict | None = None):
        skip, take = 0, 50
        while True:
            params = {"take": take, "skip": skip}
            if extra:
                params.update(extra)
            page = self._get(path, params)
            results = page.get("results", []) if isinstance(page, dict) else []
            for row in results:
                yield row
            total = (page.get("pageInfo", {}) or {}).get("results", 0)
            skip += take
            if skip >= total or not results:
                break

    # --- users ---------------------------------------------------------------
    def users(self) -> list[User]:
        if self._users is not None:
            return self._users
        out: list[User] = []
        if self.enabled:
            for u in self._paginate("/api/v1/user"):
                out.append(User(
                    id=u.get("id"),
                    email=(u.get("email") or "").strip() or None,
                    display_name=u.get("displayName"),
                    jellyfin_user_id=u.get("jellyfinUserId"),
                    jellyfin_username=u.get("jellyfinUsername") or u.get("plexUsername"),
                ))
        self._users = out
        return out

    def user_by_jellyfin_id(self, jellyfin_user_id: str) -> Optional[User]:
        if not jellyfin_user_id:
            return None
        for u in self.users():
            if u.jellyfin_user_id and u.jellyfin_user_id == jellyfin_user_id:
                return u
        return None

    def user_by_jellyfin_username(self, username: str) -> Optional[User]:
        if not username:
            return None
        uname = username.lower()
        for u in self.users():
            if (u.jellyfin_username or "").lower() == uname:
                return u
        return None

    # --- requests ------------------------------------------------------------
    def _load_requests(self) -> None:
        mapping: dict[int, int] = {}
        if self.enabled:
            for req in self._paginate("/api/v1/request", {"filter": "all"}):
                media = req.get("media") or {}
                tmdb = media.get("tmdbId")
                requested_by = (req.get("requestedBy") or {}).get("id")
                if tmdb and requested_by:
                    # Keep the FIRST requester seen for a tmdbId; requests come
                    # back newest-first by default, so this is the latest ask.
                    mapping.setdefault(int(tmdb), requested_by)
        self._requester_by_tmdb = mapping

    def requester_email(self, tmdb_id: Optional[int]) -> Optional[str]:
        """Email of the user who requested the item with this tmdbId, if any."""
        if not tmdb_id or not self.enabled:
            return None
        if self._requester_by_tmdb is None:
            self._load_requests()
        user_id = self._requester_by_tmdb.get(int(tmdb_id))
        if user_id is None:
            return None
        for u in self.users():
            if u.id == user_id:
                return u.email
        return None

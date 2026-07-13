"""SQLite persistence: a durable dedupe ledger so each user is emailed at most
once per (item, phase). sendoff is otherwise stateless — Maintainerr owns the
deletion schedule, Jellyseerr/Jellystat own the people. All we must remember is
"already told <email> that <item> is <leaving|removed>".

We also keep a small `items` table recording the last deletion date we saw per
item, used only to distinguish a real deletion (date passed, item gone) from a
manual reprieve (item removed from the collection before its date).
"""
from __future__ import annotations

import os
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS notified (
    media_server_id TEXT NOT NULL,
    email           TEXT NOT NULL,
    phase           TEXT NOT NULL,   -- leaving | removed
    sent_at         TEXT NOT NULL,   -- ISO timestamp
    PRIMARY KEY (media_server_id, email, phase)
);
CREATE TABLE IF NOT EXISTS items (
    media_server_id TEXT PRIMARY KEY,
    title           TEXT,
    deletion_date   TEXT,   -- ISO yyyy-mm-dd, last computed
    last_seen        TEXT   -- ISO timestamp we last saw it in a collection
);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- dedupe ledger -------------------------------------------------------
    def already_notified(self, media_server_id: str, email: str, phase: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM notified WHERE media_server_id=? AND email=? AND phase=?",
            (media_server_id, email, phase),
        ).fetchone() is not None

    def record_notified(self, media_server_id: str, email: str, phase: str, sent_at: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO notified(media_server_id,email,phase,sent_at) VALUES(?,?,?,?)",
            (media_server_id, email, phase, sent_at),
        )
        self.db.commit()

    # --- item tracking (for removal detection) -------------------------------
    def upsert_item(self, media_server_id: str, title: str, deletion_date: str, seen_at: str) -> None:
        self.db.execute(
            "INSERT INTO items(media_server_id,title,deletion_date,last_seen) VALUES(?,?,?,?) "
            "ON CONFLICT(media_server_id) DO UPDATE SET "
            "title=excluded.title, deletion_date=excluded.deletion_date, last_seen=excluded.last_seen",
            (media_server_id, title, deletion_date, seen_at),
        )
        self.db.commit()

    def all_items(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM items").fetchall()

    def get_item(self, media_server_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM items WHERE media_server_id=?", (media_server_id,)
        ).fetchone()

    def delete_item(self, media_server_id: str) -> None:
        self.db.execute("DELETE FROM items WHERE media_server_id=?", (media_server_id,))
        self.db.commit()

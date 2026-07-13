"""SQLite persistence.

Three tables:
- `notified` — the dedupe ledger, one row per (item, email, phase, add_date).
  Keying on the item's collection *add-date* (its stint in the collection) means
  a re-queue after a keep — same item, new add-date — is eligible again, while a
  single continuous stint is still emailed only once.
- `recipients` — per-email timestamp of the last digest we sent, to cap sends to
  one per calendar day (see DIGEST_HOUR).
- `items` — last-seen deletion date per item, used only for removal detection.

DRY_RUN never writes here (see notify.py) — it's a pure preview.
"""
from __future__ import annotations

import os
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS notified (
    media_server_id TEXT NOT NULL,
    email           TEXT NOT NULL,
    phase           TEXT NOT NULL,   -- leaving | removed
    add_date        TEXT NOT NULL,   -- the item's collection add-date (this queue stint)
    sent_at         TEXT NOT NULL,
    PRIMARY KEY (media_server_id, email, phase, add_date)
);
CREATE TABLE IF NOT EXISTS recipients (
    email        TEXT PRIMARY KEY,
    last_emailed TEXT               -- ISO timestamp of the last digest we sent
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
    def already_notified(self, media_server_id: str, email: str, phase: str, add_date: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM notified WHERE media_server_id=? AND email=? AND phase=? AND add_date=?",
            (media_server_id, email, phase, add_date),
        ).fetchone() is not None

    def record_notified(self, media_server_id: str, email: str, phase: str,
                        add_date: str, sent_at: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO notified(media_server_id,email,phase,add_date,sent_at) "
            "VALUES(?,?,?,?,?)",
            (media_server_id, email, phase, add_date, sent_at),
        )
        self.db.commit()

    def warned_emails(self, media_server_id: str) -> list[str]:
        """Distinct addresses warned that this item is leaving (for removal notices)."""
        rows = self.db.execute(
            "SELECT DISTINCT email FROM notified WHERE media_server_id=? AND phase='leaving'",
            (media_server_id,),
        ).fetchall()
        return [r["email"] for r in rows]

    # --- per-recipient send cap ----------------------------------------------
    def last_emailed(self, email: str) -> str | None:
        row = self.db.execute(
            "SELECT last_emailed FROM recipients WHERE email=?", (email,)
        ).fetchone()
        return row["last_emailed"] if row else None

    def set_last_emailed(self, email: str, iso: str) -> None:
        self.db.execute(
            "INSERT INTO recipients(email,last_emailed) VALUES(?,?) "
            "ON CONFLICT(email) DO UPDATE SET last_emailed=excluded.last_emailed",
            (email, iso),
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

    def delete_item(self, media_server_id: str) -> None:
        self.db.execute("DELETE FROM items WHERE media_server_id=?", (media_server_id,))
        self.db.commit()

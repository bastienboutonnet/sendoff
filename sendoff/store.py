"""SQLite persistence.

Five tables:
- `notified` — the dedupe ledger, one row per (item, email, phase, add_date).
  Keying on the item's collection *add-date* (its stint in the collection) means
  a re-queue after a keep — same item, new add-date — is eligible again, while a
  single continuous stint is still emailed only once.
- `recipients` — per-email timestamp of the last digest we sent, to cap sends to
  one per calendar day (see DIGEST_HOUR).
- `items` — last-seen deletion date per item, used only for removal detection.
- `keep_events` — who asked to keep what (shown on the dashboard).
- `deletions` — a ledger of items we confirmed removed, for the admin deletion
  digest (its rolling "deleted in the last N days" list). Rows here outlive the
  `items` row, which is dropped the moment removal is confirmed.

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
    media_type      TEXT,   -- movie | show | season | episode (for the deletion digest)
    deletion_date   TEXT,   -- ISO yyyy-mm-dd, last computed
    last_seen        TEXT   -- ISO timestamp we last saw it in a collection
);
CREATE TABLE IF NOT EXISTS keep_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT,          -- who clicked keep (None for legacy v1 links)
    media_server_id TEXT,
    title           TEXT,          -- captured at click time (item may be gone later)
    collection_id   INTEGER,
    kept_at         TEXT           -- ISO timestamp
);
CREATE TABLE IF NOT EXISTS deletions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    media_server_id TEXT,
    title           TEXT,
    media_type      TEXT,
    deletion_date   TEXT,          -- the scheduled/confirmed deletion date (yyyy-mm-dd)
    removed_at      TEXT,          -- ISO timestamp we confirmed the removal
    reported_at     TEXT           -- ISO timestamp it was included in an admin digest (NULL until then)
);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        # Poll thread and web threads share the file; wait rather than error on
        # a concurrent write.
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns to tables that predate them — `CREATE TABLE IF NOT EXISTS`
        in SCHEMA never alters an existing table, so a DB from an older version
        keeps its old shape until we patch it here."""
        item_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(items)")}
        if "media_type" not in item_cols:
            self.db.execute("ALTER TABLE items ADD COLUMN media_type TEXT")

    def close(self) -> None:
        self.db.close()

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
    def upsert_item(self, media_server_id: str, title: str, media_type: str | None,
                    deletion_date: str, seen_at: str) -> None:
        self.db.execute(
            "INSERT INTO items(media_server_id,title,media_type,deletion_date,last_seen) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(media_server_id) DO UPDATE SET "
            "title=excluded.title, media_type=excluded.media_type, "
            "deletion_date=excluded.deletion_date, last_seen=excluded.last_seen",
            (media_server_id, title, media_type, deletion_date, seen_at),
        )
        self.db.commit()

    def all_items(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM items").fetchall()

    def delete_item(self, media_server_id: str) -> None:
        self.db.execute("DELETE FROM items WHERE media_server_id=?", (media_server_id,))
        self.db.commit()

    # --- keep events (who asked to keep what) --------------------------------
    def record_keep(self, email: str | None, media_server_id: str, title: str | None,
                    collection_id: int, kept_at: str) -> None:
        self.db.execute(
            "INSERT INTO keep_events(email,media_server_id,title,collection_id,kept_at) "
            "VALUES(?,?,?,?,?)",
            (email, media_server_id, title, collection_id, kept_at),
        )
        self.db.commit()

    def recent_keeps(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM keep_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # --- deletion ledger (admin digest) --------------------------------------
    def record_deletion(self, media_server_id: str, title: str | None,
                        media_type: str | None, deletion_date: str | None,
                        removed_at: str) -> None:
        self.db.execute(
            "INSERT INTO deletions(media_server_id,title,media_type,deletion_date,removed_at) "
            "VALUES(?,?,?,?,?)",
            (media_server_id, title, media_type, deletion_date, removed_at),
        )
        self.db.commit()

    def unreported_deletions(self) -> list[sqlite3.Row]:
        """Confirmed deletions not yet included in an admin digest, oldest first."""
        return self.db.execute(
            "SELECT * FROM deletions WHERE reported_at IS NULL ORDER BY removed_at, id"
        ).fetchall()

    def mark_deletions_reported(self, ids: list[int], reported_at: str) -> None:
        self.db.executemany(
            "UPDATE deletions SET reported_at=? WHERE id=?",
            [(reported_at, i) for i in ids],
        )
        self.db.commit()

    def recent_deletions(self, since_iso: str) -> list[sqlite3.Row]:
        """All deletions removed on/after `since_iso` (a date or timestamp string),
        newest first. ISO strings sort lexicographically, so a plain compare works."""
        return self.db.execute(
            "SELECT * FROM deletions WHERE removed_at >= ? ORDER BY removed_at DESC, id DESC",
            (since_iso,),
        ).fetchall()

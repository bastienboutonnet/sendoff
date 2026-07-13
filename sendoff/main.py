"""Entry point: poll Maintainerr on an interval, email the people who care about
each title before it's deleted. Outbound-only; no ports.

Run once and exit:   RUN_ONCE=1 python -m sendoff.main
Run forever:         python -m sendoff.main
"""
from __future__ import annotations

import logging
import os
import time

from . import config
from .jellyseerr import JellyseerrClient
from .jellystat import JellystatClient
from .maintainerr import MaintainerrClient
from .notify import run_once
from .store import Store

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sendoff")


def _cycle(store: Store) -> None:
    maintainerr = MaintainerrClient()
    seerr = JellyseerrClient()
    stat = JellystatClient()
    if not seerr.enabled:
        log.warning("Jellyseerr not configured — cannot resolve requesters or emails")
    if not stat.enabled:
        log.info("Jellystat not configured — watcher notifications disabled")
    try:
        summary = run_once(store, maintainerr, seerr, stat)
        log.info("cycle complete: %s%s", summary, " [DRY_RUN]" if config.DRY_RUN else "")
    except Exception as e:
        log.exception("cycle failed: %s", e)


def _poll_loop() -> None:
    # The SQLite connection MUST be created in the thread that uses it
    # (sqlite3 forbids sharing a connection across threads). When the web app
    # runs, this loop is a background thread, so open the Store here — not in
    # main() — so the connection lives in this thread.
    store = Store(config.DB_PATH)
    while True:
        _cycle(store)
        time.sleep(config.POLL_INTERVAL)


def main() -> None:
    mode = "DRY_RUN" if config.DRY_RUN else "LIVE"
    log.info("sendoff starting (%s) — Maintainerr=%s, notify %d days before deletion",
             mode, config.MAINTAINERR_URL, config.NOTIFY_DAYS_BEFORE)

    # One-shot cycle for dry-run validation; no web server. Store is created and
    # used in this (main) thread, which is fine.
    if os.environ.get("RUN_ONCE", "").strip().lower() in ("1", "true", "yes"):
        _cycle(Store(config.DB_PATH))
        return

    if config.WEB_ENABLED:
        # Poll in a background thread; serve the web app (dashboard + /keep) in
        # the foreground so the container's main process is the server.
        import threading
        from .web import run as run_web
        threading.Thread(target=_poll_loop, daemon=True).start()
        run_web()
    else:
        _poll_loop()


if __name__ == "__main__":
    main()

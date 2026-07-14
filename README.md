# sendoff

Emails the people who care about a title — the **Jellyseerr requester** and
**recent watchers** (via Jellystat) — *before* [Maintainerr](https://maintainerr.info)
deletes it during a storage cull, so nobody is surprised when something vanishes.

Maintainerr already does the hard parts: the review UI (hand-pick titles into a
manual collection), the grace window (`Media deleted after days`), the deletion
itself (via Radarr/Sonarr), and the Seerr request cleanup. The one thing it does
**not** do is tell the individual users who requested or watched a title that
it's on the chopping block. That gap is all `sendoff` fills.

```
Maintainerr (unchanged)                    sendoff (this repo)
────────────────────────                   ───────────────────
hand-pick → manual collection
  "deleted after N days" grace   ──poll──▶ GET /api/collections (+ media)
                                           deletion_date = addDate + deleteAfterDays
                                           for each item within NOTIFY_DAYS_BEFORE:
                                             ├─ tmdbId       → Jellyseerr requester email
                                             └─ mediaServerId → Jellystat watchers → email
                                             └─ ONE batched email per person (~daily)
                                             └─ SQLite dedupe (item, email, phase, add-date)
  grace expires → deletes via *arr
                → resets Seerr request       (optional) "was removed" confirmation
```

`sendoff` **never deletes media.** It reads Maintainerr/Jellyseerr/Jellystat,
sends SMTP mail, and serves a tiny web app with two deliberately different
surfaces:

- **`GET /keep?token=…`** — public, capability-token gated. The one mutating
  action: it removes a single item from its deletion collection (self-service
  "keep"). The signed token *is* the authorization — no login. Fail-safe: the
  only thing it can do is *stop* a deletion. Visiting the bare host or any bad/
  expired token gets a blank 4xx; it never proxies the rest of Maintainerr.
- **`GET /`** — read-only dashboard behind HTTP Basic Auth (or an upstream like
  Cloudflare Access). **Fail-closed**: refuses to serve if no auth is
  configured. Shows what's queued, who requested/watched each item, and when it
  deletes.

Deletion itself stays entirely with Maintainerr.

## How the workflow feels

1. In Maintainerr, add titles you want gone to a collection with a
   `Media deleted after days` grace (e.g. 14). Use a **manual collection**
   (Rules → "Use rules" off) to hand-pick, or let a rule populate it.
2. `sendoff` polls, and `NOTIFY_DAYS_BEFORE` days before each item's deletion it
   emails the requester + recent watchers: *"X leaves on <date> — reply to keep
   it."*
3. If someone wants it kept, you remove it from the collection in Maintainerr
   (or tag it `janitorr_keep`-style however you like). Otherwise Maintainerr
   deletes it when the grace runs out.

## Run

```bash
pip install -r requirements.txt          # only dep is requests
cp .env.example .env                      # fill in URLs, keys, SMTP
set -a; source .env; set +a
RUN_ONCE=1 python -m sendoff.main         # one dry-run cycle, logs who WOULD be mailed
python -m sendoff.main                    # poll forever
```

Docker: `docker compose up -d --build` (state persists in `./data`).

**Start with `DRY_RUN=true`** (the default). It resolves and logs every
recipient without sending, so you can confirm the requester/watcher mapping
looks right before any real mail goes out. Flip to `false` when happy.

## Auth model (why it's safe to expose)

Only `sendoff` is reachable from the tunnel; **Maintainerr stays private** —
`sendoff` calls it over the LAN (`wanker.lan:6246`), which bypasses the tunnel
entirely, so any Cloudflare Access policy on Maintainerr is irrelevant to
`sendoff`.

- **Dashboard (`/`)** — for you. Put **Cloudflare Access** in front of it
  (`TRUST_PROXY_AUTH=true`) and/or set `DASHBOARD_USER`/`DASHBOARD_PASSWORD`.
  With neither set it returns 503 rather than exposing itself. Each queued item
  has a **Keep** button that posts to `POST /dashboard/keep` — same auth as the
  dashboard, no token. It sits *off* the `/keep` path on purpose (see below).
- **`/keep`** — for end users, who have no Access accounts, so it **must not**
  sit behind a login wall. Add a Cloudflare Access **bypass/public rule for the
  `/keep` path** — scope it to `/keep` **only**, not a broad prefix, so it does
  not also expose `/dashboard/keep` (whose sole guard is the dashboard auth).
  `/keep` is protected instead by unforgeable HMAC tokens scoped to one
  (item, collection) and expiring one day after the deletion date.

## Tests

```bash
python tests/test_notify.py           # 9/9 — pure logic (selection, tokens, email), no deps
.venv/bin/python tests/test_web.py    # 8/8 — web surface: auth fail-closed, /keep flow (needs Flask)
```

## Configuration

See `.env.example` for the full list. The essentials:

| Var | What |
|-----|------|
| `MAINTAINERR_URL` | Internal Maintainerr address. **Never expose Maintainerr** — it has no auth. |
| `COLLECTION_ALLOWLIST` | Restrict to named collections, or blank = all deleting collections. |
| `JELLYSEERR_URL` / `JELLYSEERR_API_KEY` | Requester + email lookup (by tmdbId / jellyfinUserId). |
| `JELLYSTAT_URL` / `JELLYSTAT_TOKEN` | Recent-watcher lookup (by Jellyfin item id). |
| `NOTIFY_DAYS_BEFORE` | `0` = email immediately when a title is marked (announcing the countdown); `>0` = wait until within that many days. |
| `WATCHER_LOOKBACK_DAYS` | A watch within this many days counts as "recent". |
| `DRY_RUN` | Log recipients, send nothing. Default true. |

## Architecture (`sendoff/`)

- `config.py` — settings from env vars.
- `maintainerr.py` — read collections + media; compute each item's deletion date.
- `jellyseerr.py` — tmdbId → requester email; Jellyfin userId → email.
- `jellystat.py` — Jellyfin item id → recent watchers.
- `notify.py` — pure selection (`select_due`, `classify_disappeared`) + recipient
  fan-out + email composition (incl. the keep link) + `build_dashboard` view model.
- `tokens.py` — HMAC-signed capability tokens for `/keep` (mint/verify).
- `web.py` — Flask app: `/` dashboard (auth), `/keep` (token), `/healthz`.
- `store.py` — SQLite dedupe ledger `(item, email, phase, add-date)` + per-recipient send cap + item tracking. Add-date keying re-notifies on re-queue; DRY_RUN writes nothing.
- `mail.py` — SMTP send (per-recipient).
- `main.py` — polls in a background thread and serves the web app. `RUN_ONCE=1`
  runs a single cycle with no web server (for dry-run validation).

## Notes / assumptions to verify against your instances

- **Maintainerr** endpoints (`GET /api/collections`, `GET /api/collections/media/`)
  and the `CollectionMedia` fields (`mediaServerId`, `tmdbId`, `addDate`) are from
  Maintainerr 3.17.x source. Confirm shapes against your instance's
  `/api/swagger` if you're on a very different version.
- **Jellyseerr** uses `X-Api-Key`; users are matched to watchers by
  `jellyfinUserId` (falling back to username). Users with no email in Jellyseerr
  are silently skipped.
- **Jellystat** watch history via `POST /api/getItemHistory` with `x-api-token`.
  Field names are parsed defensively (`UserId`/`userId`, etc.).

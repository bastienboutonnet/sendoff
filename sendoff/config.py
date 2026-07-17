"""All configuration comes from environment variables (see .env.example).

sendoff is outbound-only: it polls Maintainerr, Jellyseerr and Jellystat over
HTTP and sends SMTP mail. It never listens on a port. Maintainerr itself has NO
authentication (documented upstream limitation), so MAINTAINERR_URL must point
at an internal address only — never expose Maintainerr to the internet.
"""
import os


def _bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _list(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [x.strip() for x in raw.split(",") if x.strip()]


# --- Maintainerr (no auth; internal URL only) --------------------------------
# The source of truth for what is scheduled for deletion. sendoff reads its
# collections + collection media and computes each item's deletion date as
# (item.addDate + collection.deleteAfterDays).
MAINTAINERR_URL = os.environ.get("MAINTAINERR_URL", "http://maintainerr:6246").rstrip("/")
# Only watch these collection titles (comma-separated). Empty = every collection
# that has a deleteAfterDays set (i.e. actually deletes).
COLLECTION_ALLOWLIST = _list("COLLECTION_ALLOWLIST", [])

# --- Jellyseerr (maps tmdbId -> requester, and gives the user/email table) ----
JELLYSEERR_URL = os.environ.get("JELLYSEERR_URL", "").rstrip("/")
JELLYSEERR_API_KEY = os.environ.get("JELLYSEERR_API_KEY", "")

# --- Jellystat (maps a Jellyfin item id -> recent watchers) ------------------
JELLYSTAT_URL = os.environ.get("JELLYSTAT_URL", "").rstrip("/")
JELLYSTAT_TOKEN = os.environ.get("JELLYSTAT_TOKEN", "")
# A watch counts as "recent" if it happened within this many days.
WATCHER_LOOKBACK_DAYS = _int("WATCHER_LOOKBACK_DAYS", 90)

# --- Who gets told -----------------------------------------------------------
NOTIFY_REQUESTER = _bool("NOTIFY_REQUESTER", True)   # the Jellyseerr requester
NOTIFY_WATCHERS = _bool("NOTIFY_WATCHERS", True)     # recent watchers (Jellystat)
# Optional admin address that always gets a copy / the error digest.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip()
# Admin deletion digest: when this is set, sendoff emails a daily summary of the
# titles it confirmed deleted — sent only on days something was actually removed,
# held until DIGEST_HOUR:DIGEST_MINUTE, at most one per day — plus a rolling list
# of everything deleted in the last DELETION_DIGEST_DAYS. Empty = disabled; falls
# back to ADMIN_EMAIL so the existing knob works on its own.
DELETION_DIGEST_TO = os.environ.get("DELETION_DIGEST_TO", "").strip() or ADMIN_EMAIL
DELETION_DIGEST_DAYS = _int("DELETION_DIGEST_DAYS", 30)

# --- Timing ------------------------------------------------------------------
# 0 (default) = notify IMMEDIATELY when a title is marked, announcing the full
# grace window ("deletes on <date>, in N days"). Set > 0 to instead wait until
# the deletion is within that many days (a later reminder-style window).
NOTIFY_DAYS_BEFORE = _int("NOTIFY_DAYS_BEFORE", 0)
# Also send a "was removed" confirmation once the item actually leaves. Off by
# default — the heuristic (item gone AND deletion date passed) can't perfectly
# tell a real deletion from a manual reprieve.
NOTIFY_ON_REMOVAL = _bool("NOTIFY_ON_REMOVAL", False)
# Batching cadence. Each person gets at most ONE email per calendar day, listing
# all their pending titles. The day's digest is held until DIGEST_HOUR:DIGEST_MINUTE
# (local time, per TZ) and then goes out on the next poll once they have pending
# titles. So: marked overnight -> arrives ~that time; marked later in the day ->
# arrives that afternoon; marked after that day's send -> waits for the next day.
DIGEST_HOUR = _int("DIGEST_HOUR", 9)
DIGEST_MINUTE = _int("DIGEST_MINUTE", 0)

# --- Email (SMTP) ------------------------------------------------------------
# Master switch: set false to run dashboard-only (resolve + show, never mail).
EMAIL_ENABLED = _bool("EMAIL_ENABLED", True)
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").lower()  # starttls | ssl | none
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")  # iCloud/Gmail: app-specific password
EMAIL_FROM = os.environ.get("EMAIL_FROM", "").strip() or SMTP_USER
SENDER_NAME = os.environ.get("SENDER_NAME", "Media Server")

# --- Copy / branding ---------------------------------------------------------
# Shown in the email body: "scheduled for removal from {SERVER_NAME}".
SERVER_NAME = os.environ.get("SERVER_NAME", "the media server")
# How a user asks to keep something. Free text, shown at the bottom of the mail
# (e.g. "just reply to this email" or "message me on Telegram").
KEEP_INSTRUCTIONS = os.environ.get(
    "KEEP_INSTRUCTIONS", "reply to this email before then and I'll keep it."
)

# --- Web app (read-only dashboard + self-service keep endpoint) --------------
WEB_ENABLED = _bool("WEB_ENABLED", True)
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = _int("WEB_PORT", 8623)
# Public base URL the keep links point at (the tunnel hostname), e.g.
# https://sendoff.example.com — used to build the "Keep this" button in emails.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
# HMAC secret for signing keep tokens. REQUIRED for the keep links to work;
# generate one with `python -c "import secrets;print(secrets.token_urlsafe(32))"`.
SIGNING_SECRET = os.environ.get("SIGNING_SECRET", "")

# --- Dashboard auth ----------------------------------------------------------
# HTTP Basic Auth for the read-only dashboard (the /keep route is NEVER behind
# this — it is capability-token gated instead). Fail-closed: if neither Basic
# Auth nor an explicit trusted-proxy opt-in is configured, the dashboard refuses
# to serve rather than expose itself openly. Recommended: also put Cloudflare
# Access in front of everything EXCEPT /keep.
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
# Set to true ONLY if an upstream (Cloudflare Access / authenticated reverse
# proxy) already gates the dashboard and sendoff is not reachable directly.
TRUST_PROXY_AUTH = _bool("TRUST_PROXY_AUTH", False)

# --- Behaviour ---------------------------------------------------------------
# DRY_RUN logs exactly who WOULD be emailed but sends nothing. Default ON so the
# first deploy is safe; flip to false once the recipient resolution looks right.
DRY_RUN = _bool("DRY_RUN", True)
POLL_INTERVAL = _int("POLL_INTERVAL", 3600)          # seconds between polls
DB_PATH = os.environ.get("DB_PATH", "/data/sendoff.db")
TZ = os.environ.get("TZ", "Europe/Amsterdam")

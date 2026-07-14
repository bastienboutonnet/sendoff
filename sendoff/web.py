"""The sendoff web app: two surfaces with deliberately different auth.

  GET /keep?token=…   PUBLIC, capability-token gated. Removes one item from one
                      deletion collection (self-service "keep"). The signed token
                      IS the authorization; no login. Fail-safe: the only thing it
                      can do is stop a deletion.
  GET /               Dashboard, behind HTTP Basic Auth (or an upstream like
                      Cloudflare Access). Fail-closed: refuses to serve if no auth
                      is configured. Shows what's queued + who's affected.
  POST /dashboard/keep  Keep an item straight from the dashboard, same auth as
                      GET /. No token — the dashboard auth IS the authorization.
                      Removes one item from one deletion collection, like the link
                      flow. Deliberately NOT under /keep: the /keep path has a
                      Cloudflare Access bypass (for public email links), so a keep
                      that relies on dashboard auth must sit on a protected path.
  GET /healthz        Liveness, open.

Nothing here deletes media or mutates anything except the single keep action.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import logging
from datetime import date, datetime

from flask import Flask, Response, redirect, render_template_string, request

from . import config, tokens
from .jellyseerr import JellyseerrClient
from .jellystat import JellystatClient
from .maintainerr import MaintainerrClient
from .notify import build_dashboard
from .store import Store

log = logging.getLogger("sendoff.web")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Dashboard auth (fail-closed)
# ---------------------------------------------------------------------------

def _basic_auth_ok() -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, _, pw = raw.partition(":")
    return (hmac.compare_digest(user, config.DASHBOARD_USER)
            and hmac.compare_digest(pw, config.DASHBOARD_PASSWORD))


def _require_dashboard_auth():
    """Return None if allowed, else a Response to short-circuit with."""
    if config.TRUST_PROXY_AUTH:
        return None  # an upstream (Cloudflare Access / proxy) gates this route
    if config.DASHBOARD_PASSWORD:
        if _basic_auth_ok():
            return None
        return Response(
            "Authentication required.", 401,
            {"WWW-Authenticate": 'Basic realm="sendoff"'},
        )
    # Nothing configured -> do NOT expose the dashboard.
    log.error("dashboard requested but no auth configured (set DASHBOARD_USER/"
              "DASHBOARD_PASSWORD or TRUST_PROXY_AUTH)")
    return Response(
        "Dashboard auth is not configured; refusing to serve. Set "
        "DASHBOARD_USER + DASHBOARD_PASSWORD, or TRUST_PROXY_AUTH=true behind an "
        "authenticated proxy.", 503,
    )


def _dashboard_actor() -> str:
    """Best-effort identity for a keep performed from the (authenticated)
    dashboard, recorded in the keep log. The Basic-auth user if we have one,
    otherwise a generic marker (e.g. behind Cloudflare Access)."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            user = base64.b64decode(header[6:]).decode("utf-8").partition(":")[0]
            if user:
                return f"{user} (dashboard)"
        except (binascii.Error, UnicodeDecodeError):
            pass
    return "dashboard"


# ---------------------------------------------------------------------------
# Shared keep flow (used by both the token link and the dashboard button)
# ---------------------------------------------------------------------------

def _perform_keep(collection_id: int, media_id: str, email: str | None, via: str):
    """Confirm the item is still queued, remove it from Maintainerr, and record
    the keep. Returns one of 'already' | 'done' | 'error' plus the item title."""
    maintainerr = MaintainerrClient()
    title = None
    still_queued = False
    try:
        for it in maintainerr.scheduled_items():
            if it.media_server_id == media_id and it.collection_id == collection_id:
                title, still_queued = it.title, True
                break
    except Exception as e:
        log.warning("keep: lookup failed: %s", e)

    if not still_queued:
        return "already", title

    ok = maintainerr.remove_media(collection_id, media_id)
    log.info("keep %s (collection %s) by %s via %s -> %s",
             media_id, collection_id, email or "unknown", via,
             "ok" if ok else "FAILED")
    if ok:
        store = Store(config.DB_PATH)
        try:
            store.record_keep(email, media_id, title, collection_id,
                              datetime.now().isoformat(timespec="seconds"))
        except Exception as e:
            log.warning("failed to record keep event: %s", e)
        finally:
            store.close()
    return ("done" if ok else "error"), title


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/keep")
def keep():
    data = tokens.verify(config.SIGNING_SECRET, request.args.get("token", ""))
    if not data:
        return render_template_string(_KEEP_INVALID), 400
    status, title = _perform_keep(
        data["collection_id"], data["media_id"], data.get("email"), "link")
    if status == "already":
        return render_template_string(_KEEP_ALREADY)
    tmpl = _KEEP_DONE if status == "done" else _KEEP_ERROR
    return render_template_string(tmpl, title=title or "This title")


@app.post("/dashboard/keep")
def keep_from_dashboard():
    """Keep an item straight from the dashboard. Same auth as the dashboard
    itself; no token needed. Lives OFF the /keep path on purpose — /keep has a
    Cloudflare Access bypass for public email links, so a keep gated only by
    dashboard auth must not share it. Redirects back to the dashboard
    (POST/redirect/GET) so a refresh doesn't resubmit."""
    guard = _require_dashboard_auth()
    if guard is not None:
        return guard
    media_id = request.form.get("media_id", "")
    try:
        collection_id = int(request.form.get("collection_id", ""))
    except ValueError:
        return Response("bad request", 400)
    if not media_id:
        return Response("bad request", 400)
    _perform_keep(collection_id, media_id, _dashboard_actor(), "dashboard")
    return redirect("/")


@app.get("/")
def dashboard():
    guard = _require_dashboard_auth()
    if guard is not None:
        return guard
    seerr = JellyseerrClient()
    stat = JellystatClient()
    try:
        items = build_dashboard(MaintainerrClient(), seerr if seerr.enabled else None,
                                stat if stat.enabled else None)
        error = None
    except Exception as e:
        log.exception("dashboard build failed: %s", e)
        items, error = [], str(e)
    keeps = []
    store = Store(config.DB_PATH)
    try:
        for r in store.recent_keeps(100):
            keeps.append({"when": (r["kept_at"] or "")[:16].replace("T", " "),
                          "email": r["email"] or "unknown",
                          "title": r["title"] or "(unknown)"})
    except Exception as e:
        log.warning("failed to load keep events: %s", e)
    finally:
        store.close()
    return render_template_string(
        _DASHBOARD, items=items, error=error, today=date.today(),
        server_name=config.SERVER_NAME, dry_run=config.DRY_RUN, keeps=keeps,
    )


def run() -> None:
    """Serve with waitress (a real WSGI server), falling back to Flask's dev
    server if waitress isn't installed."""
    log.info("web app listening on %s:%s", config.WEB_HOST, config.WEB_PORT)
    try:
        from waitress import serve
        serve(app, host=config.WEB_HOST, port=config.WEB_PORT, threads=8)
    except ImportError:
        app.run(host=config.WEB_HOST, port=config.WEB_PORT)


# ---------------------------------------------------------------------------
# Templates (Jinja autoescapes; inline to avoid a templates dir)
# ---------------------------------------------------------------------------

_PAGE = """
<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title_tag }}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>👋</text></svg>">
<style>
  :root{color-scheme:light dark}
  body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#18181b;background:#fff}
  @media(prefers-color-scheme:dark){body{color:#e4e4e7;background:#0b0b0c}}
  .card{border:1px solid #8883;border-radius:10px;padding:1rem 1.25rem;margin:1rem 0}
  .big{font-size:1.4rem;font-weight:700;margin:.2rem 0}
  .muted{color:#71717a;font-size:.9rem}
  table{border-collapse:collapse;width:100%;font-size:.92rem}
  th,td{text-align:left;padding:.5rem .6rem;border-bottom:1px solid #8882;vertical-align:top}
  th{font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;color:#71717a}
  .soon{color:#dc2626;font-weight:700}.ok{color:#16a34a;font-weight:600}
  .pill{display:inline-block;background:#8882;border-radius:999px;padding:.05rem .5rem;font-size:.8rem;margin:.1rem}
  .btn{display:inline-block;background:#2563eb;color:#fff;border:0;border-radius:8px;
       padding:.45rem .9rem;font-size:.9rem;font-weight:600;cursor:pointer}
  .btn:hover{background:#1d4ed8}
</style>
"""

_DASHBOARD = _PAGE.replace("{{ title_tag }}", "sendoff — leaving soon") + """
<h1 style="margin:.2rem 0">Leaving soon{% if dry_run %} <span class="pill">DRY_RUN</span>{% endif %}</h1>
<p class="muted">{{ items|length }} item(s) scheduled for deletion from {{ server_name }}. Keep an item to stop its deletion; marking &amp; deletion otherwise happen in Maintainerr.</p>
{% if error %}<div class="card"><b>Could not load:</b> {{ error }}</div>{% endif %}
{% if not items and not error %}<div class="card">Nothing is scheduled for deletion right now.</div>{% endif %}
{% for it in items %}
<div class="card" style="display:flex;gap:14px;align-items:flex-start">
  {% if it.poster %}<img src="{{ it.poster }}" alt="" loading="lazy" style="width:70px;height:105px;object-fit:cover;border-radius:6px;flex:none;background:#8882">{% endif %}
  <div style="flex:1;min-width:0">
  <div class="big">{{ it.title }}</div>
  <div class="muted">{{ it.media_type }} · collection “{{ it.collection_title }}”</div>
  <p style="margin:.6rem 0">
    Deletes <b>{{ it.deletion_date.strftime('%a %d %b %Y') }}</b> —
    {% if it.days_until < 0 %}<span class="soon">{{ -it.days_until }} day(s) overdue</span>
    {% elif it.days_until == 0 %}<span class="soon">today</span>
    {% else %}<span class="{{ 'soon' if it.days_until <= 3 else '' }}">in {{ it.days_until }} day(s)</span>{% endif %}
  </p>
  <table>
    <tr><th>Requester</th><td>
      {% if it.requester_email %}{{ it.requester_name or it.requester_email }} <span class="muted">&lt;{{ it.requester_email }}&gt;</span>
      {% else %}<span class="muted">none / unknown</span>{% endif %}
    </td></tr>
    <tr><th>Recent watchers</th><td>
      {% if it.watchers %}{% for w in it.watchers %}
        <div>{{ w.name or 'unknown' }}
          {% if w.email %}<span class="muted">&lt;{{ w.email }}&gt;</span>{% else %}<span class="muted">(no email)</span>{% endif %}
          {% if w.last_watched %}<span class="muted">· {{ w.last_watched.strftime('%d %b %Y') }}</span>{% endif %}
        </div>
      {% endfor %}{% else %}<span class="muted">none in the lookback window</span>{% endif %}
    </td></tr>
  </table>
  <form method="post" action="/dashboard/keep" style="margin-top:.8rem"
        onsubmit="return confirm('Keep this title and stop its deletion?')">
    <input type="hidden" name="collection_id" value="{{ it.collection_id }}">
    <input type="hidden" name="media_id" value="{{ it.media_server_id }}">
    <button class="btn" type="submit">Keep this</button>
  </form>
  </div>
</div>
{% endfor %}

{% if keeps %}
<h2 style="font-size:16px;margin:2rem 0 .5rem">Kept by users <span class="muted">({{ keeps|length }})</span></h2>
<table>
  <tr><th>When</th><th>Who asked</th><th>Title kept</th></tr>
  {% for k in keeps %}
  <tr><td class="muted">{{ k.when }}</td><td>{{ k.email }}</td><td>{{ k.title }}</td></tr>
  {% endfor %}
</table>
{% endif %}
"""

_KEEP_DONE = _PAGE.replace("{{ title_tag }}", "Kept") + """
<div class="card"><div class="big ok">Kept ✓</div>
<p>{{ title }} will stay on the server. You can close this page.</p></div>
"""
_KEEP_ALREADY = _PAGE.replace("{{ title_tag }}", "Nothing to do") + """
<div class="card"><div class="big">Nothing to do</div>
<p>This title is no longer queued for deletion — it was already kept or already removed.</p></div>
"""
_KEEP_INVALID = _PAGE.replace("{{ title_tag }}", "Invalid link") + """
<div class="card"><div class="big">Invalid or expired link</div>
<p>This keep link is not valid or has expired. If the title still matters to you, reply to the email you received.</p></div>
"""
_KEEP_ERROR = _PAGE.replace("{{ title_tag }}", "Something went wrong") + """
<div class="card"><div class="big">Couldn’t keep it</div>
<p>Something went wrong keeping {{ title }}. Please reply to the email you received so it can be handled manually.</p></div>
"""

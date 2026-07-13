"""The sendoff web app: two surfaces with deliberately different auth.

  GET /keep?token=…   PUBLIC, capability-token gated. Removes one item from one
                      deletion collection (self-service "keep"). The signed token
                      IS the authorization; no login. Fail-safe: the only thing it
                      can do is stop a deletion.
  GET /               READ-ONLY dashboard, behind HTTP Basic Auth (or an upstream
                      like Cloudflare Access). Fail-closed: refuses to serve if no
                      auth is configured. Shows what's queued + who's affected.
  GET /healthz        Liveness, open.

Nothing here deletes media or mutates anything except the single keep action.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import logging
from datetime import date

from flask import Flask, Response, render_template_string, request

from . import config, tokens
from .jellyseerr import JellyseerrClient
from .jellystat import JellystatClient
from .maintainerr import MaintainerrClient
from .notify import build_dashboard

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
    maintainerr = MaintainerrClient()
    # Look the item up first so we can name it and know if it's still queued.
    title = None
    still_queued = False
    try:
        for it in maintainerr.scheduled_items():
            if it.media_server_id == data["media_id"] and it.collection_id == data["collection_id"]:
                title, still_queued = it.title, True
                break
    except Exception as e:
        log.warning("keep: lookup failed: %s", e)

    if not still_queued:
        return render_template_string(_KEEP_ALREADY)

    ok = maintainerr.remove_media(data["collection_id"], data["media_id"])
    log.info("keep %s (collection %s) via link -> %s",
             data["media_id"], data["collection_id"], "ok" if ok else "FAILED")
    return render_template_string(_KEEP_DONE if ok else _KEEP_ERROR, title=title or "This title")


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
    return render_template_string(
        _DASHBOARD, items=items, error=error, today=date.today(),
        server_name=config.SERVER_NAME, dry_run=config.DRY_RUN,
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
</style>
"""

_DASHBOARD = _PAGE.replace("{{ title_tag }}", "sendoff — leaving soon") + """
<h1 style="margin:.2rem 0">Leaving soon{% if dry_run %} <span class="pill">DRY_RUN</span>{% endif %}</h1>
<p class="muted">Read-only. {{ items|length }} item(s) scheduled for deletion from {{ server_name }}. Marking &amp; deletion happen in Maintainerr.</p>
{% if error %}<div class="card"><b>Could not load:</b> {{ error }}</div>{% endif %}
{% if not items and not error %}<div class="card">Nothing is scheduled for deletion right now.</div>{% endif %}
{% for it in items %}
<div class="card">
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
</div>
{% endfor %}
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

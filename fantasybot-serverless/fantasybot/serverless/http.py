"""Request plumbing shared by every Vercel function.

Kept inside the package rather than in `api/` because Vercel turns each file
under `api/` into its own deployed function — a shared helper there would be
deployed as a route nobody should be able to call.
"""

import hmac
import json
import os
import sys
import traceback
import urllib.parse

# Vercel bundles the repository but does not necessarily put its root on the
# path, so a function importing `fantasybot` needs this.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from .. import config   # noqa: E402


def query(handler):
    parsed = urllib.parse.urlparse(handler.path)
    return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}


def body(handler):
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return {}
    if length <= 0:
        return {}
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except (ValueError, OSError):
        return {}


def presented_secret(handler):
    """The caller's credential, from the most to the least private channel.

    A query parameter is accepted because it makes a browser dashboard and a
    curl smoke test possible at all — but it is the one that ends up in access
    logs, so headers come first and the docs say so.
    """
    auth = handler.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for header in ("X-Cron-Secret", "X-Bot-Secret"):
        value = handler.headers.get(header)
        if value:
            return value.strip()
    return (query(handler).get("token") or "").strip()


def authorized(handler):
    """Constant-time check against BOT_CRON_SECRET.

    An unset secret is a hard NO, never an open door: a misconfigured deployment
    must fail closed, or the first thing to find /api/tick would be a crawler.
    """
    presented = str(presented_secret(handler))
    accepted = [s for s in (config.BOT_CRON_SECRET, config.VERCEL_CRON_SECRET) if s]
    if not accepted:
        return False
    # compare_digest on every candidate (no early exit) so a mismatch takes the
    # same time whichever secret it was checked against.
    ok = False
    for secret in accepted:
        ok |= hmac.compare_digest(presented, str(secret))
    return ok


def send(handler, status, payload, content_type="application/json"):
    if content_type.startswith("application/json"):
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    else:
        raw = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    # No CORS header on purpose: these endpoints are for the scheduler and for
    # the dashboard served from this same deployment, not for other origins.
    handler.end_headers()
    handler.wfile.write(raw)


def deny(handler):
    send(handler, 401, {"ok": False, "error": "unauthorized"})


def guarded(handler, fn):
    """Run `fn` behind the secret, turning any blow-up into a JSON 500.

    A scheduler that gets an HTML stack trace back cannot tell a crash from a
    cold start; a JSON body with `ok:false` it can log and alert on.
    """
    if not authorized(handler):
        return deny(handler)
    try:
        status, payload = fn()
    except Exception as e:                       # noqa: BLE001
        return send(handler, 500, {
            "ok": False, "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-1200:]})
    return send(handler, status, payload)

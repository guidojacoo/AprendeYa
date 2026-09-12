"""Request plumbing shared by every Vercel function.

Kept inside the package rather than in `api/` because Vercel turns each file
under `api/` into its own deployed function — a shared helper there would be
deployed as a route nobody should be able to call.
"""

import hmac
import json
import os
import sys
import time
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


# How long a deployment trusts its cached copy of the scheduler's secret. Short
# enough that changing it takes effect within minutes; long enough that someone
# hammering /api/tick cannot turn every request into a database read.
SHARED_SECRET_TTL = 300

_shared = {"value": None, "at": 0.0}


def shared_secret():
    """The secret the scheduler was handed, read back from the database.

    BOT_CRON_SECRET is a seed, not the authority. A deployment whose environment
    is missing it — added after the last build, set on the wrong environment,
    never set at all — refuses every call from its own clock, and that failure is
    invisible from both ends: pg_cron reports success because net.http_post is
    fire-and-forget, Vercel answers 401 before a line of our code runs, and the
    bot simply never wakes. This cost a night of 401s.

    Accepting the value the database holds closes that hole without weakening
    anything. Reading it needs the service-role key, which is exactly as private
    as the environment variable it stands in for.
    """
    now = time.monotonic()
    if _shared["value"] is not None and now - _shared["at"] < SHARED_SECRET_TTL:
        return _shared["value"]
    value = ""
    try:
        from ..storage import get_storage
        store = get_storage()
        if store.kind == "supabase":
            rows = store._request("GET", "scheduler_config",
                                  params={"select": "bot_secret", "limit": "1"})
            value = str((rows or [{}])[0].get("bot_secret") or "").strip()
    except Exception:                            # noqa: BLE001
        value = ""                               # no database, no fallback
    _shared.update({"value": value, "at": now})
    return value


def authorized(handler):
    """Constant-time check against the secrets this deployment accepts.

    No secret anywhere is a hard NO, never an open door: a misconfigured
    deployment must fail closed, or the first thing to find /api/tick would be a
    crawler.
    """
    presented = str(presented_secret(handler))
    # A bare probe is refused without touching the database, so a crawler cannot
    # make each of its requests cost a query.
    if not presented:
        return False
    accepted = [s for s in (config.BOT_CRON_SECRET, config.VERCEL_CRON_SECRET) if s]
    # compare_digest on every candidate (no early exit) so a mismatch takes the
    # same time whichever secret it was checked against.
    ok = False
    for secret in accepted:
        ok |= hmac.compare_digest(presented, str(secret))
    if ok:
        return True
    shared = shared_secret()
    return bool(shared) and hmac.compare_digest(presented, shared)


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

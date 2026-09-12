"""POST /api/tick — one wake-up of the bot.

This is the whole serverless bot: the scheduler calls it, it does whatever is
due, and it dies. Nothing survives the response except what went into Supabase.

Auth: BOT_CRON_SECRET, as `Authorization: Bearer …` (preferred),
`X-Cron-Secret: …`, or `?token=…`.

Query parameters
  mode=tick|sniper   sniper skips the review and spends its whole budget racing
                     a close (the scheduler sets it when a bid is imminent)
  force=1            run the review even if it is not due by cadence
  dry_run=1          decide everything, send nothing

The response carries `sleep_seconds`: how long until the next queued action is
due. The GitHub Actions runner uses it to wait for free and come back exactly on
time, which is how a 5-minute cron still lands a bid on the second.
"""

import os
import sys

from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fantasybot import tick                      # noqa: E402
from fantasybot.serverless.http import body, guarded, query  # noqa: E402


def _run(handler):
    params = dict(query(handler))
    params.update({k: v for k, v in (body(handler) or {}).items()
                   if isinstance(v, (str, int, bool))})

    def flag(name):
        return str(params.get(name, "")).lower() in ("1", "true", "yes", "on")

    mode = "sniper" if params.get("mode") == "sniper" else "tick"
    # Who woke us. Recorded so "is anything actually scheduling this?" has an
    # answer — a run triggered by hand is indistinguishable from an automated one
    # until you ask where it came from.
    src = str(params.get("src") or "unknown")[:20]
    lines = []
    result = tick.run(mode=mode, dry_run=flag("dry_run"),
                      force_review=flag("force"), log=lines.append,
                      source=src)
    result["log"] = lines[-40:]
    return (200 if result.get("ok") else 500), result


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        guarded(self, lambda: _run(self))

    # GET is allowed so you can smoke-test from a browser or curl without a body.
    def do_GET(self):
        guarded(self, lambda: _run(self))

    def log_message(self, fmt, *args):
        pass

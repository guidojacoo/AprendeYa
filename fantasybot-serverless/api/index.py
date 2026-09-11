"""GET /api — health check.

Public, and deliberately says almost nothing: whether the process boots and
whether it can reach its database. No balance, no squad, no configuration — an
unauthenticated endpoint should be useful to you and boring to everyone else.
"""

import os
import sys

from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fantasybot import config                            # noqa: E402
from fantasybot.storage import get_storage, to_iso, utcnow  # noqa: E402
from fantasybot.serverless.http import send              # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            storage_ok = get_storage().ping()
            kind = get_storage().kind
        except Exception as e:                            # noqa: BLE001
            storage_ok, kind = False, f"error: {type(e).__name__}"
        send(self, 200 if storage_ok else 503, {
            "ok": bool(storage_ok),
            "service": "fantasybot",
            "storage": kind,
            "configured": bool(config.BOT_CRON_SECRET),
            "now": to_iso(utcnow()),
        })

    def log_message(self, fmt, *args):
        pass

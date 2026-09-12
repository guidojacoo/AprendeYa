"""GET /api — health check.

Public, and deliberately says almost nothing: whether the process boots and
whether it can reach its database. No balance, no squad, no configuration — an
unauthenticated endpoint should be useful to you and boring to everyone else.
"""

import os
import sys

from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fantasybot import config, tick                      # noqa: E402
from fantasybot.storage import get_storage, to_iso, utcnow  # noqa: E402
from fantasybot.serverless.http import send              # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            storage_ok = get_storage().ping()
            kind = get_storage().kind
        except Exception as e:                            # noqa: BLE001
            storage_ok, kind = False, f"error: {type(e).__name__}"

        # Repair the database's clock if it is still pointing at the migration's
        # placeholder. Unusual for a health check to write, and deliberate: an
        # unrepaired placeholder means NOTHING is waking the bot, and the repair
        # needs a running deployment because only it knows its own address.
        #
        # Safe to expose here because it takes no input, derives the value from
        # this deployment's own environment, and touches nothing once the URL is
        # real — so it is a single write, not a hit that costs on every request.
        healed = False
        if storage_ok:
            try:
                healed = bool(tick._heal_scheduler_url(get_storage()))
            except Exception:                             # noqa: BLE001
                pass

        send(self, 200 if storage_ok else 503, {
            "ok": bool(storage_ok),
            "service": "fantasybot",
            "storage": kind,
            "configured": bool(config.BOT_CRON_SECRET),
            # Which commit is answering. Public on purpose and harmless — it is
            # the same sha as the repository's HEAD — and it is what lets a deploy
            # check wait for the build it pushed instead of for a fixed number of
            # seconds and whatever happens to be live when they elapse.
            "deploy": config.DEPLOY_SHA[:7] or None,
            "clock_repaired": healed,
            "now": to_iso(utcnow()),
        })

    def log_message(self, fmt, *args):
        pass

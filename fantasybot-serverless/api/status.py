"""GET /api/status — everything the dashboard shows, in one call.

Behind the same secret as /api/tick. There is no public variant: the payload
names your squad, your balance and your bidding plan, and a rival with that in
front of them is a rival who never loses an auction to you again.
"""

import os
import sys

from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fantasybot import config, scheduler                 # noqa: E402
from fantasybot.llm import describe as describe_llm      # noqa: E402
from fantasybot.storage import get_storage, to_iso, utcnow  # noqa: E402
from fantasybot.serverless.http import guarded, query    # noqa: E402


def _status(handler):
    store = get_storage()
    limit = min(int(query(handler).get("events") or 40), 200)
    executions = store.recent_executions(limit=10)
    last = executions[0] if executions else None

    pending = store.pending_actions(limit=25)
    report = store.get_doc("last_report", {}) or {}

    return 200, {
        "ok": True,
        "now": to_iso(utcnow()),
        "scope": config.SCOPE,
        "storage": store.kind,
        "llm": describe_llm(),
        "autonomy": {
            "execute": config.AUTO_EXECUTE,
            "lineup": config.AUTO_LINEUP,
            "bids": config.AUTO_BIDS,
            "clauses": config.AUTO_CLAUSES,
            "sells": config.AUTO_SELLS,
        },
        "last_execution": last,
        "executions": executions,
        "next_deadline": to_iso(scheduler.next_deadline()),
        "pending_actions": pending,
        "report": report,
        "decisions": store.recent_decisions(limit=5),
        "events": list(reversed(store.load_events(limit=limit))),
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        guarded(self, lambda: _status(self))

    def log_message(self, fmt, *args):
        pass

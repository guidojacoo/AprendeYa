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
from fantasybot.notify import describe as describe_notify  # noqa: E402
from fantasybot.storage import (StorageUnavailable, get_storage,  # noqa: E402
                                to_iso, utcnow)
from fantasybot.serverless.http import guarded, query    # noqa: E402


def _part(name, read, default, degraded):
    """One piece of the page, or a note that this piece is missing.

    The dashboard used to be a single read away from showing NOTHING: one slow
    query on the executions history and the whole page came back as an error
    string with no balance, no squad, no queue — while every other table
    answered fine. A status page whose job is telling you the bot is alive
    cannot be the thing that goes dark when the database has a slow minute.

    So each piece is fetched on its own and a failure costs only that piece.
    """
    try:
        return read()
    except StorageUnavailable as e:
        degraded.append({"part": name, "why": str(e)[:160]})
        return default


def _status(handler):
    store = get_storage()
    limit = min(int(query(handler).get("events") or 40), 200)
    degraded = []
    executions = _part("executions", lambda: store.recent_executions(limit=10),
                       [], degraded)
    last = executions[0] if executions else None

    pending = _part("pending", lambda: store.pending_actions(limit=25),
                    [], degraded)
    report = _part("report", lambda: store.get_doc("last_report", {}) or {},
                   {}, degraded)
    decisions = _part("decisions", lambda: store.recent_decisions(limit=5),
                      [], degraded)
    stance = _part("stance", lambda: store.get_doc("stance", None),
                   None, degraded)
    events = _part("events",
                   lambda: list(reversed(store.load_events(limit=limit))),
                   [], degraded)
    deadline = _part("deadline", lambda: to_iso(scheduler.next_deadline()),
                     None, degraded)

    return 200, {
        "ok": True,
        # Which pieces the database was too slow to hand over, if any. The page
        # says so in place of that section instead of pretending it is empty.
        "degraded": degraded,
        "now": to_iso(utcnow()),
        "scope": config.STORAGE_SCOPE,
        "storage": store.kind,
        "llm": describe_llm(),
        "notify": describe_notify(),
        "autonomy": {
            "execute": config.AUTO_EXECUTE,
            "lineup": config.AUTO_LINEUP,
            "bids": config.AUTO_BIDS,
            "clauses": config.AUTO_CLAUSES,
            "sells": config.AUTO_SELLS,
            # The three that were missing. `list` decides whether the squad goes
            # on the market at all — so with it off nothing is ever offered and
            # nothing can ever sell, and the page showed no way to find that out.
            "list": config.AUTO_LIST,
            "shield": config.AUTO_SHIELD,
            "raise_clause": config.AUTO_RAISE_CLAUSE,
            "matchday": config.AUTO_MATCHDAY_LINEUP,
        },
        "last_execution": last,
        "executions": executions,
        "next_deadline": deadline,
        "pending_actions": pending,
        "report": report,
        # The most recent tick's offer handling lives on the execution row, not
        # in the hourly report — offers are decided every tick.
        "offers": ((last or {}).get("summary") or {}).get("offers"),
        "decisions": decisions,
        "stance": stance,
        "events": events,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        guarded(self, lambda: _status(self))

    def log_message(self, fmt, *args):
        pass

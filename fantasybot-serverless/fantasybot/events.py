"""Agent event trace for the supervision UI ("mission control").

Every meaningful action (review, lineup, bid, sniping, sale, buyout) appends ONE
JSON line to `.state/events.jsonl`. The UI (`fantasybot watch`) reads that file
and follows it live over SSE.

It's fantasybot's NATIVE trace: it doesn't depend on Hermes or any external
format, so it works the same with or without an agent on top. That makes it
stable and reproducible: what you see is what the CLI actually did.

Serverless changes nothing about the shape of an event, only where the line
lands: with a database backend it becomes a row in `events` and the dashboard
polls it, because a Vercel function has neither a file to append to nor a
connection to hold an SSE stream open on.
"""

import json
import os
import time
from datetime import datetime, timezone

from . import config
from .storage import get_storage

STATE_DIR = os.path.join(config.ROOT, ".state")
EVENTS_PATH = os.path.join(STATE_DIR, "events.jsonl")
RUN_PATH = os.path.join(STATE_DIR, "run.current")
RUN_IDLE = 20 * 60      # s without activity => treated as a new agent session
MAX_EVENTS = 5000       # trims the file so it doesn't grow forever


def _run_id() -> str:
    """Groups the actions of a single agent cycle under one session id.

    An agent review chains several CLI calls (`agent --json`, `optimize --apply`,
    `bid-plan`...), each in its own process. They share a `run` as long as no more
    than RUN_IDLE min pass without activity; then a new one begins.
    """
    now = time.time()
    store = get_storage()
    cur = None
    if store.kind == "local":
        if os.path.exists(RUN_PATH):
            try:
                with open(RUN_PATH, encoding="utf-8") as f:
                    cur = json.load(f)
            except (ValueError, OSError):
                cur = None
    else:
        try:
            cur = store.get_doc("run_current", None)
        except Exception:
            cur = None
    if cur and (now - cur.get("ts", 0)) < RUN_IDLE:
        rid = cur["id"]
    else:
        rid = datetime.fromtimestamp(now, timezone.utc).strftime("run_%Y%m%d_%H%M%S")
    marker = {"id": rid, "ts": now}
    try:
        if store.kind == "local":
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(RUN_PATH, "w", encoding="utf-8") as f:
                json.dump(marker, f)
        else:
            store.put_doc("run_current", marker)
    except Exception:
        pass
    return rid


def emit(kind: str, title: str, detail=None, status: str = "ok") -> dict:
    """Append an event to the trace. Never raises: telemetry must not break an action.

    kind: read | review | lineup | bid | cancel | bid-plan | sell | clause | note | error
    status: ok | plan | error
    """
    ev = {
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(),
        "run": _run_id(),
        "kind": kind,
        "title": title,
        "status": status,
    }
    if detail is not None:
        ev["detail"] = detail
    try:
        get_storage().emit_event(ev)
    except Exception:
        pass   # telemetry must never break an action
    return ev


def _trim():
    """Kept for callers that trimmed by hand; the backend trims on write now."""
    store = get_storage()
    if store.kind == "local":
        store._trim_events()


def load(limit: int = 500) -> list:
    """Last `limit` events, in chronological order."""
    try:
        return get_storage().load_events(limit)
    except Exception:
        return []

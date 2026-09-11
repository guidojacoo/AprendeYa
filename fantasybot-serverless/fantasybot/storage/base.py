"""The persistence contract the rest of fantasybot talks to.

The bot's logic must not know whether its state lives in a JSON file or in
PostgreSQL: on a laptop the CLI keeps writing `.state/`, on Vercel the very same
code writes Supabase, and neither `agent.py` nor `strategy/` notices.

Everything the agent needs to survive a process that dies after 10 seconds is
expressed here:

  documents          the old `.state/*.json` blobs (snapshot, tasks, bids...)
  cache              TTL'd scrape results (was `.cache/`)
  events             the append-only action trace (was `.state/events.jsonl`)
  market snapshots   one day of official values (was `.state/value_history/`)
  scheduled actions  work with a deadline — the heart of serverless bidding
  executions         one row per tick, so you can see the bot is alive
  locks              a mutex two concurrent ticks can agree on
  settings           runtime knobs you can change without redeploying
  decisions          what the LLM decided, and whether it was carried out

Times crossing this boundary are always timezone-aware UTC datetimes or ISO-8601
strings; never naive ones, and never local time.
"""

from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


def to_iso(dt):
    """Timezone-aware ISO-8601. Accepts a datetime, an ISO string or None."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value):
    """ISO-8601 -> aware datetime, or None. Tolerates a trailing 'Z' and naive
    strings (assumed UTC), which is what both PostgREST and the LaLiga API send."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Action lifecycle. `running` is a lease, not a state you can be stuck in
# forever: a tick that dies mid-action leaves a stale lease that the next tick
# reclaims once `locked_until` has passed.
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
SKIPPED = "skipped"


class StorageError(Exception):
    pass


class Storage:
    """Interface. Subclasses implement it; nothing here reaches the network."""

    kind = "base"

    # --- documents -----------------------------------------------------------
    def get_doc(self, name, default=None):
        raise NotImplementedError

    def put_doc(self, name, value):
        raise NotImplementedError

    def delete_doc(self, name):
        raise NotImplementedError

    # --- TTL cache -----------------------------------------------------------
    def cache_get(self, key):
        """The cached value if still fresh, else None. `None` is never a
        legitimate cached value here, so the sentinel is unambiguous."""
        raise NotImplementedError

    def cache_put(self, key, value, ttl_seconds):
        raise NotImplementedError

    def cache_clear(self):
        raise NotImplementedError

    # --- events --------------------------------------------------------------
    def emit_event(self, event):
        raise NotImplementedError

    def load_events(self, limit=500):
        """Chronological (oldest first), at most `limit` of the most recent."""
        raise NotImplementedError

    # --- market snapshots ----------------------------------------------------
    def get_market_snapshot(self, day_iso):
        raise NotImplementedError

    def put_market_snapshot(self, day_iso, values, keep_days=40):
        raise NotImplementedError

    # --- scheduled actions ---------------------------------------------------
    def schedule_action(self, action_type, payload, execute_at, idempotency_key,
                        expires_at=None):
        """Insert, or return the existing row with the same idempotency_key.

        Re-scheduling an action that is already `done` must NOT resurrect it:
        that is exactly the double-bid we are defending against.
        """
        raise NotImplementedError

    def due_actions(self, now=None, limit=25):
        """Pending actions whose execute_at has passed, earliest first."""
        raise NotImplementedError

    def pending_actions(self, limit=50):
        """Everything still queued, earliest first — for the dashboard."""
        raise NotImplementedError

    def claim_action(self, action, lease_seconds=120):
        """Atomically pending -> running. False means someone else got it."""
        raise NotImplementedError

    def finish_action(self, action, status, result=None, error=None):
        raise NotImplementedError

    def cancel_action(self, idempotency_key):
        raise NotImplementedError

    def next_deadline(self):
        """execute_at of the earliest pending action, or None."""
        raise NotImplementedError

    # --- executions ----------------------------------------------------------
    def start_execution(self, trigger):
        raise NotImplementedError

    def finish_execution(self, execution_id, status, summary=None, error=None):
        raise NotImplementedError

    def recent_executions(self, limit=20):
        raise NotImplementedError

    # --- locks ---------------------------------------------------------------
    def acquire_lock(self, name, ttl_seconds, holder):
        raise NotImplementedError

    def release_lock(self, name, holder):
        raise NotImplementedError

    # --- settings ------------------------------------------------------------
    def get_settings(self):
        raise NotImplementedError

    def set_setting(self, key, value):
        raise NotImplementedError

    # --- decisions -----------------------------------------------------------
    def save_decision(self, source, model, decision, input_digest=None):
        raise NotImplementedError

    def recent_decisions(self, limit=10):
        raise NotImplementedError

    # --- health --------------------------------------------------------------
    def ping(self):
        """True when the backend is reachable and writable."""
        raise NotImplementedError

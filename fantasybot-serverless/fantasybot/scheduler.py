"""Actions with a deadline, and the guarantee they happen exactly once.

The original bot held its future in RAM: `bid-plan` wrote a list of targets and
`bid-run` spawned one thread per target that sat on the market until the close.
That is correct and it is also the one thing a serverless platform cannot do —
there is no process to sit in.

So the plan moves into the database. `bid-plan` now enqueues a row saying "bid on
this listing, starting at 20:44:00Z, cap 4.2M", and every tick asks "what is due?"
and runs it. The close still gets hit to the second; what changed is who is
holding the stopwatch.

Exactly-once rests on three independent guards, deliberately not one:

  1. UNIQUE (scope, idempotency_key) — the same plan enqueued twice is one row.
  2. claim() is a conditional UPDATE — two ticks racing, one winner.
  3. before bidding we re-read the listing and stand down if OUR bid is already
     on it — the only guard that still holds when the database itself is wrong
     (a tick that bid and died before it could record the fact).

Guard 3 is the important one. 1 and 2 protect against concurrency; 3 protects
against amnesia, which is the failure mode that actually costs money.
"""

import time
import uuid
from collections.abc import Callable
from datetime import timedelta

from . import config, events
from .storage import (DONE, FAILED, PENDING, SKIPPED,
                      get_storage, parse_iso, to_iso, utcnow)

# Action types.
BID = "bid"                 # last-minute bid on a market listing
REVIEW = "review"           # full deterministic agent review (+ optional execute)
LINEUP = "lineup"           # apply the optimal XI
REMINDER = "reminder"       # fire a due reminder into the event log
LLM_STRATEGY = "llm_strategy"  # the strategic pass (costs tokens)
LIST_SQUAD = "list_squad"   # put one player on the market at his reserve price
CLAUSE = "clause"           # pay a rival's buyout clause the second it unlocks
SHIELD = "shield"           # protect one of ours from being claused

_EXECUTORS: dict[str, Callable] = {}
_EXECUTORS_LOADED = False


def _ensure_executors():
    """Import the module that registers the executors, once.

    `fantasybot.tick` holds them, and it imports this module — so the import has
    to be deferred to call time. Doing it here rather than at every call site
    means a caller cannot forget: before this, a `run_due()` from code that had
    not happened to import `tick` failed every action with "no executor", which
    looks exactly like a bug in the action.
    """
    global _EXECUTORS_LOADED
    if not _EXECUTORS_LOADED:
        _EXECUTORS_LOADED = True
        from . import tick   # noqa: F401  (imported for its @executor registrations)


def executor(action_type):
    """Register the function that carries out one action type."""
    def wrap(fn):
        _EXECUTORS[action_type] = fn
        return fn
    return wrap


# --- enqueueing --------------------------------------------------------------
def bid_key(league_id, market_id, close_at):
    """One listing closes once. Tying the key to that close time means a re-plan
    is a no-op, while the NEXT listing for the same player (new close time) is
    correctly a different action."""
    return f"bid:{league_id}:{market_id}:{to_iso(close_at)}"


def schedule_bid(league_id, market_id, max_bid, close_at, nombre=None,
                 lead_seconds=None):
    """Queue a last-minute bid. Returns the stored row.

    `execute_at` is placed a lead time BEFORE the close, not at it: the tick that
    picks the action up needs room to read the market, size the bid and send it.
    """
    lead = config.BID_LEAD_SECONDS if lead_seconds is None else lead_seconds
    close_dt = parse_iso(close_at)
    if close_dt is None:
        raise ValueError(f"schedule_bid needs a parseable close time, got {close_at!r}")
    execute_at = close_dt - timedelta(seconds=lead)
    return get_storage().schedule_action(
        BID,
        {"league_id": str(league_id), "market_id": str(market_id),
         "max_bid": int(max_bid), "nombre": nombre, "close_at": to_iso(close_dt)},
        execute_at=execute_at,
        idempotency_key=bid_key(league_id, market_id, close_dt),
        # A bid is worthless once the listing is gone; don't let a backlogged
        # queue fire it hours late against a listing that no longer exists.
        expires_at=close_dt + timedelta(minutes=5))


def schedule(action_type, payload, execute_at, idempotency_key, expires_at=None):
    return get_storage().schedule_action(action_type, payload, execute_at,
                                         idempotency_key, expires_at)


def cancel(idempotency_key):
    return get_storage().cancel_action(idempotency_key)


def pending(limit=50):
    return get_storage().pending_actions(limit)


def next_deadline():
    return get_storage().next_deadline()


def seconds_to_next_deadline(now=None):
    """How long until the next queued action is due. None when nothing is queued;
    negative/zero when something is already overdue."""
    at = next_deadline()
    if at is None:
        return None
    return (at - (now or utcnow())).total_seconds()


# --- running -----------------------------------------------------------------
def run_due(ctx, now=None, limit=10, log=print):
    """Claim and execute everything that is due. Returns one result per action.

    `ctx` carries whatever the executors need (an API client, a budget, a logger).
    Each action is isolated: one blowing up must not stop the rest of the queue,
    because the rest of the queue may be the bid that pays for the week.
    """
    _ensure_executors()
    store = get_storage()
    now = now or utcnow()
    results = []
    for action in store.due_actions(now=now, limit=limit):
        if ctx.out_of_time():
            # Leave it pending; it is still due, so the next tick takes it.
            break
        results.append(_run_one(store, action, ctx, now, log))
    return results


def _run_one(store, action, ctx, now, log):
    atype = action.get("type")
    key = action.get("idempotency_key")

    expires = parse_iso(action.get("expires_at"))
    if expires is not None and expires <= now:
        if store.claim_action(action, lease_seconds=ctx.lease_seconds):
            store.finish_action(action, SKIPPED, error="expired before it could run")
        log(f"[tick] skipped {atype} {key}: expired")
        return {"key": key, "type": atype, "status": SKIPPED, "reason": "expired"}

    if not store.claim_action(action, lease_seconds=ctx.lease_seconds):
        # Another tick owns it. Not an error — this is the guard doing its job.
        log(f"[tick] {atype} {key}: claimed by another tick, skipping")
        return {"key": key, "type": atype, "status": "claimed_elsewhere"}

    fn = _EXECUTORS.get(atype)
    if fn is None:
        store.finish_action(action, FAILED, error=f"no executor for {atype!r}")
        return {"key": key, "type": atype, "status": FAILED,
                "error": f"no executor for {atype!r}"}

    try:
        result = fn(ctx, action) or {}
    except Exception as e:               # noqa: BLE001 - one bad action, not a dead tick
        attempts = action.get("attempts") or 1
        # Retrying is only safe because every executor re-checks the world before
        # it acts (see guard 3 above). Past a few tries we stop and leave it FAILED
        # rather than hammering an endpoint that is clearly refusing us.
        status = PENDING if attempts < ctx.max_attempts else FAILED
        store.finish_action(action, status, error=f"{type(e).__name__}: {e}")
        events.emit("error", f"Action {atype} failed: {e}", status="error",
                    detail={"key": key, "attempt": attempts})
        log(f"[tick] {atype} {key} FAILED (attempt {attempts}): {e}")
        return {"key": key, "type": atype, "status": status, "error": str(e)}

    # "retry" lets an executor say "nothing happened yet, ask me again" — the
    # sniper uses it when its budget ran out before the close.
    #
    # The executor's own outcome stays nested under "result": `status` on this
    # dict always means the ACTION's lifecycle (done/pending/failed), never what
    # the executor did. Flattening the two was a real bug — a successful bid
    # reported status "bid", and nothing downstream could tell completion from
    # a retry.
    if result.get("retry"):
        store.finish_action(action, PENDING, result=result)
        return {"key": key, "type": atype, "status": PENDING, "result": result}

    store.finish_action(action, DONE, result=result)
    log(f"[tick] {atype} {key}: done ({result.get('status', 'ok')})")
    return {"key": key, "type": atype, "status": DONE, "result": result}


class TickContext:
    """What an executor is allowed to use, and how long it may take.

    The budget is the whole point: on Vercel Hobby the platform kills the request
    at 60s with no chance to clean up, so every executor checks `out_of_time()`
    and returns rather than being killed mid-action.
    """

    def __init__(self, client=None, budget_seconds=None, lease_seconds=None,
                 max_attempts=3, dry_run=False, log=print, mode="tick",
                 holder=None):
        self.client = client
        # Who this tick is, for the lock table. Generated rather than required so
        # a context built in a test or a script can still take the review lock.
        self.holder = holder or uuid.uuid4().hex
        self.budget_seconds = budget_seconds or config.TICK_BUDGET_SECONDS
        self.lease_seconds = lease_seconds or config.TICK_LOCK_SECONDS
        self.max_attempts = max_attempts
        self.dry_run = dry_run
        self.log = log
        self.mode = mode
        self._started = time.monotonic()

    def elapsed(self):
        return time.monotonic() - self._started

    def remaining(self):
        return max(0.0, self.budget_seconds - self.elapsed())

    def out_of_time(self, margin=3.0):
        return self.remaining() <= margin

    def get_client(self):
        """Lazily built: a tick with nothing due must not pay for a token refresh."""
        if self.client is None:
            from .api import FantasyClient
            self.client = FantasyClient()
        return self.client

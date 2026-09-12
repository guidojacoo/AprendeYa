"""One tick: everything the bot does between waking up and dying again.

This is the serverless replacement for "the agent is running on a VPS". Nothing
here loops forever, sleeps indefinitely, spawns a daemon or assumes the process
will still exist in a minute — the whole cycle is written to finish inside a
Vercel function's lifetime and leave every scrap of state in the database.

A tick, in order:

  1. run whatever is DUE (bids at a close, reminders, a scheduled review)
  2. run the deterministic review, but only if one is due by cadence
  3. run the LLM strategic pass, only if enabled AND due (this is the only step
     that costs money, so it is the most heavily gated)
  4. report when it next needs to be woken, so the scheduler can hold the line
     for an imminent bid instead of blindly waiting five more minutes

Step 4 is what makes a 5-minute cron good enough for a to-the-second deadline:
the tick tells the GitHub Actions runner "come back in 214 seconds and hold",
and the runner does the waiting for free.
"""

import time
import traceback
from datetime import timedelta

from . import agent as agent_mod
from . import bidding, config, events, scheduler
from . import execute as execute_mod
from . import state
from .scheduler import BID, LINEUP, LLM_STRATEGY, REMINDER, REVIEW, TickContext
from .storage import (DONE, FAILED, get_storage, parse_iso, to_iso,
                      utcnow)

REVIEW_LOCK = "review"

# Runtime knobs that live in the DB (settings table) and fall back to env config,
# so cadence can be retuned from the dashboard without a redeploy.
_SETTING_DEFAULTS = {
    "review_interval": config.REVIEW_INTERVAL,
    "llm_interval": config.LLM_INTERVAL,
    "auto_execute": config.AUTO_EXECUTE,
    "bid_mode": "snipe",        # "snipe" = last-minute; "immediate" = bid on sight
}


def bids_allowed():
    """Whether the bot may actually send a bid right now.

    Both flags have to be on. FANTASYBOT_AUTO_EXECUTE is the master switch people
    reach for to watch the bot before trusting it, so it must gate bidding too —
    not just the lineup.
    """
    return bool(config.AUTO_EXECUTE and config.AUTO_BIDS)


def setting(name):
    try:
        value = get_storage().get_settings().get(name)
    except Exception:
        value = None
    return _SETTING_DEFAULTS.get(name) if value is None else value


# =============================================================================
# Executors
# =============================================================================
@scheduler.executor(BID)
def _execute_bid(ctx, action):
    """Hold the line on one listing and bid at the close.

    Bounded by whatever time the tick has left. If the close is still further out
    than our budget, we bid nothing and ask to be called again — a bid that was
    never sent can always be retried, which is exactly why the budget check comes
    BEFORE the request and never after it.

    The autonomy flags are re-checked HERE, not only where the bid was planned.
    An action queued while autonomy was on must not fire after you turned it off:
    the switch has to hold at the moment money would actually move, or it is not
    a switch.
    """
    p = action.get("payload") or {}
    league_id = p.get("league_id")
    market_id = p.get("market_id")
    budget = max(2.0, ctx.remaining() - 4.0)
    dry = ctx.dry_run or not bids_allowed()
    res = bidding.snipe(league_id, market_id, int(p.get("max_bid") or 0),
                        dry_run=dry, log=ctx.log,
                        client=ctx.get_client(), budget_seconds=budget)
    if res.get("status") == "waiting":
        # Still early. Stay queued; the scheduler will wake us closer to the close.
        return {"retry": True, **res}
    if res.get("status") == "bid" and not dry:
        # Mirror it into the local bid ledger so `sync_bids` knows this player is
        # already covered and does not propose him all over again.
        bids = state.load_bids()
        bids[str(market_id)] = {"bid_id": res.get("bid_id"),
                                "amount": res.get("amount"),
                                "nombre": res.get("nombre") or p.get("nombre")}
        state.save_bids(bids)
    return res


@scheduler.executor(REMINDER)
def _execute_reminder(ctx, action):
    p = action.get("payload") or {}
    events.emit("note", p.get("message") or "Reminder",
                detail={"event_at": p.get("event_at")})
    key = p.get("key")
    if key:
        state.mark_reminder_fired(key)
    return {"status": "fired", "message": p.get("message")}


@scheduler.executor(LINEUP)
def _execute_lineup(ctx, action):
    client = ctx.get_client()
    lid, tid = client.default_ids()
    team = client.team(lid, tid)
    return _apply_best_lineup(ctx, client, lid, tid, team)


@scheduler.executor(REVIEW)
def _execute_review(ctx, action):
    return run_review(ctx, force=True)


@scheduler.executor(LLM_STRATEGY)
def _execute_llm(ctx, action):
    return run_llm_strategy(ctx, force=True)


# =============================================================================
# The deterministic review
# =============================================================================
def _apply_best_lineup(ctx, client, lid, tid, team):
    from .strategy import lineup as lineup_opt
    premium = agent_mod.league_allows_premium_formations(client, lid)
    fixture_difficulty = agent_mod.captain_fixture_difficulty(client) if premium else None
    try:
        best = lineup_opt.optimize(team, premium=premium,
                                   fixture_difficulty=fixture_difficulty)
    except ValueError as e:
        return {"status": "skipped", "reason": str(e)}
    current, coach, captain = agent_mod._current_lineup(client, tid)
    res = execute_mod.apply_lineup(client, tid, best, current,
                                   dry_run=ctx.dry_run or not config.AUTO_LINEUP,
                                   current_coach=coach, current_captain=captain)
    return {"status": "ok", **res}


def _plan_bids(ctx, client, lid, team, report):
    """Turn profitable flips into SCHEDULED bids instead of immediate ones.

    Bidding on sight shows your hand: rivals see the listing is contested and
    outbid you. The original bot solved that with `bid-plan` + a cron, and this is
    the same idea with the plan living in PostgreSQL — one queued action per
    listing, fired seconds before that listing closes.
    """
    mode = setting("bid_mode")
    if not bids_allowed():
        # Still report what it WOULD have bid on, so the dashboard shows the
        # thinking while autonomy is off. Nothing is queued, so nothing can fire.
        return {"mode": "observe-only", "scheduled": [],
                "would_bid": execute_mod.plan_bids(client, lid, team),
                "reason": "FANTASYBOT_AUTO_EXECUTE/AUTO_BIDS is off"}
    if mode == "immediate":
        return {"mode": "immediate",
                **execute_mod.sync_bids(client, lid, team,
                                        dry_run=ctx.dry_run or not config.AUTO_EXECUTE)}

    plan = execute_mod.plan_bids(client, lid, team)
    scheduled, skipped = [], []
    # `plan_bids` already fits the targets inside the balance, cheapest commitment
    # first; we only add the timing.
    by_id = {str(o["market_id"]): o for o in report.get("flips") or []}
    for b in plan:
        mid = str(b["market_id"])
        close_at = (by_id.get(mid) or {}).get("expires_at")
        if not close_at:
            skipped.append({"market_id": mid, "reason": "no close time on the listing"})
            continue
        try:
            row = scheduler.schedule_bid(lid, mid, b["amount"], close_at,
                                         nombre=b.get("nombre"))
        except ValueError as e:
            skipped.append({"market_id": mid, "reason": str(e)})
            continue
        scheduled.append({"market_id": mid, "nombre": b.get("nombre"),
                          "max_bid": b["amount"], "close_at": to_iso(close_at),
                          "status": row.get("status")})
        events.emit("bid-plan", f"Last-minute bid scheduled: {b.get('nombre') or mid}",
                    detail={"max": f"{b['amount']:,}", "closes": to_iso(close_at)},
                    status="plan")
    return {"mode": "snipe", "scheduled": scheduled, "skipped": skipped}


def _queue_reminders(report):
    """Reminders become queued actions so a tick actually fires them on time.

    `state.save_reminders` still keeps the list (the CLI's `due` command reads it);
    this just gives each one a deadline the serverless scheduler understands.
    """
    queued = []
    for r in report.get("reminders") or []:
        fire_at = parse_iso(r.get("fire_at"))
        if fire_at is None:
            continue
        scheduler.schedule(REMINDER,
                           {"key": r.get("key"), "message": r.get("message"),
                            "event_at": r.get("event_at")},
                           execute_at=fire_at,
                           idempotency_key=f"reminder:{r.get('key')}",
                           expires_at=fire_at + timedelta(hours=6))
        queued.append(r.get("key"))
    return queued


def run_review(ctx, force=False):
    """The full human-like review, plus the autonomous actions it authorises."""
    store = get_storage()
    now = utcnow()
    last = parse_iso(store.get_doc("last_review_at"))
    interval = int(setting("review_interval") or 0)
    if not force and last is not None and (now - last).total_seconds() < interval:
        return {"status": "skipped", "reason": "not due",
                "next_at": to_iso(last + timedelta(seconds=interval))}

    holder = ctx.holder
    if not store.acquire_lock(REVIEW_LOCK, config.TICK_LOCK_SECONDS, holder):
        return {"status": "skipped", "reason": "another tick is reviewing"}
    try:
        client = ctx.get_client()
        report = agent_mod.review(client)
        lid, tid = client.default_ids()
        team = client.team(lid, tid)

        lineup_res = ({"status": "skipped", "reason": "autonomy off"}
                      if not (config.AUTO_EXECUTE and config.AUTO_LINEUP)
                      else _apply_best_lineup(ctx, client, lid, tid, team))
        bids_res = _plan_bids(ctx, client, lid, team, report)
        reminders = _queue_reminders(report)

        store.put_doc("last_review_at", to_iso(now))
        store.put_doc("last_report", _summarize(report, lineup_res, bids_res))
        events.emit("review", f"Review: balance {report['money']:,}",
                    detail={"flips": len(report.get("flips") or []),
                            "tasks": len(report.get("tasks") or []),
                            "scheduled_bids": len(bids_res.get("scheduled") or [])})
        return {"status": "ok", "money": report.get("money"),
                "lineup": lineup_res, "bids": bids_res,
                "reminders_queued": reminders,
                "tasks": len(report.get("tasks") or [])}
    finally:
        store.release_lock(REVIEW_LOCK, holder)


def _summarize(report, lineup_res, bids_res):
    """What the dashboard reads. Deliberately small: a full review payload is
    hundreds of KB of squad data and there is no reason to store it every hour."""
    lu = report.get("lineup") or {}
    return {
        "at": to_iso(utcnow()),
        "money": report.get("money"),
        "matchday": report.get("matchday"),
        "formation": lu.get("formation"),
        "lineup_changed": bool(lu.get("changed")),
        "lineup_result": lineup_res,
        "gaps": report.get("gaps"),
        "flips": (report.get("flips") or [])[:5],
        "sells": (report.get("sells") or [])[:5],
        "clause_targets": (report.get("clause_targets") or [])[:5],
        "tasks": report.get("tasks") or [],
        "bids": bids_res,
    }


# =============================================================================
# The LLM strategic pass
# =============================================================================
def run_llm_strategy(ctx, force=False):
    from .llm import strategy as llm_strategy
    if not llm_strategy.enabled():
        return {"status": "disabled"}
    store = get_storage()
    now = utcnow()
    last = parse_iso(store.get_doc("last_llm_at"))
    interval = int(setting("llm_interval") or 0)
    if not force and last is not None and (now - last).total_seconds() < interval:
        return {"status": "skipped", "reason": "not due"}
    try:
        res = llm_strategy.run(ctx)
    except Exception as e:                       # noqa: BLE001
        events.emit("error", f"LLM pass failed: {e}", status="error")
        return {"status": "error", "error": str(e)}
    store.put_doc("last_llm_at", to_iso(now))
    return res


# =============================================================================
# The tick itself
# =============================================================================
def run(mode="tick", dry_run=False, force_review=False, log=print,
        budget_seconds=None):
    """Run one tick. Always returns a JSON-serializable summary — including when
    it fails, because a tick that dies silently is a bot you cannot debug."""
    store = get_storage()
    started = time.monotonic()
    ctx = TickContext(
        budget_seconds=budget_seconds or (config.SNIPER_BUDGET_SECONDS
                                          if mode == "sniper"
                                          else config.TICK_BUDGET_SECONDS),
        dry_run=dry_run, log=log, mode=mode)
    execution_id = None
    summary = {"mode": mode, "started_at": to_iso(utcnow()), "dry_run": dry_run}

    try:
        execution_id = store.start_execution(mode)
    except Exception as e:                       # noqa: BLE001
        # No database, no tick. Say so loudly rather than pretending to work.
        return {"ok": False, "error": f"storage unavailable: {e}", **summary}

    try:
        summary["actions"] = scheduler.run_due(ctx, log=log)

        # A sniper tick exists only to hit a close; it must not spend its seconds
        # on a market review.
        if mode != "sniper" and not ctx.out_of_time(margin=15):
            summary["review"] = run_review(ctx, force=force_review)
            if not ctx.out_of_time(margin=10):
                summary["llm"] = run_llm_strategy(ctx)

        summary["next_deadline"] = to_iso(scheduler.next_deadline())
        summary["sleep_seconds"] = _sleep_hint()
        summary["pending"] = len(store.pending_actions(limit=50))
        summary["ok"] = True
        summary["duration_seconds"] = round(time.monotonic() - started, 2)
        store.finish_execution(execution_id, DONE, summary=summary)
        return summary
    except Exception as e:                       # noqa: BLE001
        summary.update({"ok": False, "error": f"{type(e).__name__}: {e}",
                        "traceback": traceback.format_exc()[-1500:],
                        "duration_seconds": round(time.monotonic() - started, 2)})
        try:
            store.finish_execution(execution_id, FAILED, summary=summary,
                                   error=summary["error"])
            events.emit("error", f"Tick failed: {e}", status="error")
        except Exception:
            pass
        return summary


def _sleep_hint():
    """How long the scheduler should wait before the next tick.

    None means "nothing queued, use your normal cron interval". A number means
    "something is due then — come back and hold for it", which is how a 5-minute
    cron still hits a deadline to the second.
    """
    secs = scheduler.seconds_to_next_deadline()
    if secs is None:
        return None
    return max(0, int(secs))

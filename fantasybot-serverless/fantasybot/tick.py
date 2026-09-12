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

import secrets
import time
import traceback
from datetime import date, timedelta

from . import agent as agent_mod
from . import bidding, config, events, explain, net, notify, scheduler
from . import execute as execute_mod
from . import state
from .scheduler import (BID, LINEUP, LLM_STRATEGY, REMINDER, REVIEW,
                        TickContext)
from .matching import num
from .storage import (DONE, FAILED, RUNNING, get_storage, parse_iso, to_iso,
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


def league_ids(client):
    """(league_id, team_id), cached.

    `client.default_ids()` resolves them by calling `leagues()` — a real request
    to an unofficial API, made on EVERY tick, for two values that do not change.
    At 288 ticks a day that was a third of our entire API footprint spent
    re-reading a constant.
    """
    store = get_storage()
    cached = store.get_doc("league_ids", None)
    if isinstance(cached, list) and len(cached) == 2 and all(cached):
        return cached[0], cached[1]
    lid, tid = client.default_ids()
    store.put_doc("league_ids", [lid, tid])
    return lid, tid


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
    # The lead is 60s and a tick can hold about 32 of them, so the watch used to
    # be handed back at ~25s to close and the next tick arrived after it. Telling
    # the bidder how long that gap is lets it bid instead of passing an auction
    # nobody else will attend.
    res = bidding.snipe(league_id, market_id, int(p.get("max_bid") or 0),
                        dry_run=dry, log=ctx.log,
                        client=ctx.get_client(), budget_seconds=budget,
                        last_call_seconds=config.CLOCK_INTERVAL_SECONDS + 10)
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


@scheduler.executor(scheduler.WARM)
def _execute_warm(ctx, action):
    """Refresh the scraped caches — and nothing else.

    Scraping futbolfantasy cold costs about 25 seconds, because being polite
    means a 1.2s floor between requests. Paying that inside a review left the
    review with nothing to decide with, and three of them ran past the 60s
    ceiling Vercel kills functions at (one reached 191s and one is still marked
    `running` because it was killed mid-write).

    So the scraping is its own job. It gets a whole tick to itself, writes to the
    shared Supabase cache, and every review afterwards reads that cache for free.
    """
    from .sources.lineups import probable_lineups
    from .sources.market_trends import trends_index
    from .sources import matchday

    net.set_deadline(time.monotonic() + max(5.0, ctx.remaining() - 3.0))
    got = {}
    try:
        for name, fn in (("trends", trends_index),
                         ("lineups", probable_lineups),
                         ("kickoff", matchday.next_kickoff),
                         ("gameweek", matchday.next_gameweek_kickoff)):
            if ctx.out_of_time(margin=4):
                got[name] = "sin tiempo"
                continue
            value = fn()
            got[name] = (len(value) if hasattr(value, "__len__")
                         else bool(value))
    finally:
        net.clear_deadline()
    get_storage().put_doc("last_warm_at", to_iso(utcnow()))
    return {"status": "ok", "warmed": got}


@scheduler.executor(scheduler.CLAUSE)
def _execute_clause(ctx, action):
    """Pay a rival's buyout clause the moment it unlocks.

    The only irreversible spend the bot makes, so every assumption made when this
    was planned is re-checked against the live API before a euro moves. Planning
    happened up to a day ago; any of these can have changed since:

      * we may already own him — somebody else's clause payment, or our own bid
      * the clause may have gone up (his owner raised it, or his value did)
      * the balance may have gone down (a bid we won in the meantime)

    Anything that no longer holds means we stand down and say why. A skipped
    clause costs nothing; an over-paid one cannot be undone.
    """
    p = action.get("payload") or {}
    if not config.AUTO_CLAUSES:
        return {"status": "skipped", "reason": "AUTO_CLAUSES is off"}
    if ctx.dry_run:
        return {"status": "skipped", "reason": "dry run"}

    client = ctx.get_client()
    lid, tid = league_ids(client)
    player_id, nombre = p.get("player_id"), p.get("nombre")
    max_pay = int(p.get("max_pay") or 0)

    team = client.team(lid, tid)
    owned = {str((pl.get("playerMaster") or {}).get("id"))
             for pl in team.get("players") or []}
    if str(player_id) in owned:
        return {"status": "already_owned", "nombre": nombre}

    # Re-read the clause from the live market rather than trusting the plan.
    current, unlock = None, None
    for row in client.market(lid) or []:
        if row.get("discr") != "marketPlayerTeam":
            continue
        if str((row.get("playerMaster") or {}).get("id")) != str(player_id):
            continue
        pt = row.get("playerTeam") or {}
        current = num(pt.get("buyoutClause")) or None
        unlock = pt.get("buyoutClauseLockedEndTime")
        break
    if current is None:
        return {"status": "gone", "reason": "not on the market any more",
                "nombre": nombre}

    current = int(current)
    if current > max_pay:
        return {"status": "too_expensive", "nombre": nombre,
                "clause": current, "max_pay": max_pay,
                "reason": f"clause rose to {current:,}, cap was {max_pay:,}"}

    unlock_at = parse_iso(unlock)
    if unlock_at is not None and unlock_at > utcnow():
        # Still locked. Not an error — come back when it opens.
        return {"retry": True, "status": "locked", "nombre": nombre,
                "unlocks_at": to_iso(unlock_at)}

    money = int(num(team.get("teamMoney")))
    if money - current < config.CASH_RESERVE:
        return {"status": "insufficient_funds", "nombre": nombre,
                "clause": current, "money": money,
                "reserve": config.CASH_RESERVE,
                "reason": f"{current:,} would leave less than the "
                          f"{config.CASH_RESERVE:,} reserve"}

    resp = client.pay_buyout_clause(lid, player_id, current)
    events.emit("clause", f"COMPRADO {nombre} por cláusula: {current:,} €",
                detail={"was_planned_at": p.get("planned_clause"),
                        "balance_after": money - current})
    notify.send(f"clause:{player_id}",
                f"Fichado {nombre} por cláusula: {current:,} €. "
                f"Saldo: {money - current:,} €", level="good")
    return {"status": "paid", "nombre": nombre, "amount": current,
            "response": resp}


@scheduler.executor(scheduler.SHIELD)
def _execute_shield(ctx, action):
    """Shield our most clause-vulnerable player. Free, and purely defensive."""
    p = action.get("payload") or {}
    if not config.AUTO_SHIELD:
        return {"status": "skipped", "reason": "AUTO_SHIELD is off"}
    if ctx.dry_run:
        return {"status": "skipped", "reason": "dry run"}
    client = ctx.get_client()
    lid, _ = league_ids(client)
    ptid = p.get("player_team_id")
    try:
        # A shield already in place makes this a no-op; ask before spending the
        # one-per-run shield on somebody who does not need it.
        if client.check_shield(lid, ptid):
            return {"status": "already_shielded", "nombre": p.get("nombre")}
    except Exception:
        pass          # the check is an optimisation, not a precondition
    resp = client.shield_player(lid, ptid)
    events.emit("note", f"Blindado {p.get('nombre')} "
                        f"(cláusula {int(p.get('clause') or 0):,} €)",
                detail={"value": p.get("value"), "reason": p.get("reason")})
    return {"status": "shielded", "nombre": p.get("nombre"), "response": resp}


@scheduler.executor(scheduler.LIST_SQUAD)
def _execute_listing(ctx, action):
    """Put one player on the market at his reserve price."""
    p = action.get("payload") or {}
    if ctx.dry_run or not config.AUTO_LIST:
        return {"status": "skipped", "reason": "listing is off"}
    client = ctx.get_client()
    # Re-read before listing: another tick (or you, from the app) may have listed
    # him already, and a second listing on the same player is at best noise.
    already = {str((r.get("playerMaster") or {}).get("id"))
               for r in client.market(p["league_id"]) or []
               if r.get("discr") == "marketPlayerTeam"}
    if str(p.get("player_id")) in already:
        return {"status": "already_listed", "nombre": p.get("nombre")}
    resp = client.sell_player(p["league_id"], p["player_team_id"], int(p["price"]))
    events.emit("sell", f"En venta: {p.get('nombre')} a {int(p['price']):,} €",
                detail={"reserve": p.get("price"), "value": p.get("value")},
                status="plan")
    return {"status": "listed", "nombre": p.get("nombre"),
            "price": p.get("price"), "response": resp}


@scheduler.executor(REMINDER)
def _execute_reminder(ctx, action):
    p = action.get("payload") or {}
    events.emit("note", p.get("message") or "Recordatorio",
                detail={"event_at": p.get("event_at")})
    key = p.get("key")
    if key:
        state.mark_reminder_fired(key)
    return {"status": "fired", "message": p.get("message")}


@scheduler.executor(LINEUP)
def _execute_lineup(ctx, action):
    client = ctx.get_client()
    lid, tid = league_ids(client)
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
    fixture_difficulty = agent_mod.captain_fixture_difficulty(client)
    try:
        best = lineup_opt.optimize(team, premium=premium,
                                   fixture_difficulty=fixture_difficulty)
    except ValueError as e:
        return {"status": "skipped", "reason": str(e)}
    current, coach, captain = agent_mod._current_lineup(client, tid)
    res = execute_mod.apply_lineup(client, tid, best, current,
                                   dry_run=ctx.dry_run or not config.AUTO_LINEUP,
                                   current_coach=coach, current_captain=captain)
    return {"status": "ok", "why": explain.lineup(res, best), **res}


def _plan_bids(ctx, client, lid, team, report):
    """Turn profitable flips into SCHEDULED bids instead of immediate ones.

    Bidding on sight shows your hand: rivals see the listing is contested and
    outbid you. The original bot solved that with `bid-plan` + a cron, and this is
    the same idea with the plan living in PostgreSQL — one queued action per
    listing, fired seconds before that listing closes.
    """
    mode = setting("bid_mode")
    if ctx.dry_run:
        # A dry run must not leave live orders behind. Queuing a bid IS acting —
        # just later — so it reports the plan and queues nothing.
        return {"mode": "dry-run", "scheduled": [],
                "would_bid": execute_mod.plan_bids(client, lid, team)}
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
    # Nobody in the league can outbid money they do not have. The richest rival's
    # estimated cash is the real ceiling on what any auction can cost us.
    rivals = report.get("rivals") or []
    # A cash estimate built from a partially backfilled history is not a ceiling,
    # it is a guess — and guessing LOW loses auctions. Until the history is
    # complete the cap stands as computed.
    reach = 0 if any(r.get("partial_history") for r in rivals) else max(
        ((r.get("cash") or 0) for r in rivals if not r.get("is_me")), default=0)
    scheduled, skipped = [], []
    # `plan_bids` already fits the targets inside the balance, cheapest commitment
    # first; we only add the timing.
    by_id = {str(o["market_id"]): o for o in report.get("flips") or []}
    for b in plan:
        mid = str(b["market_id"])
        close_at = (by_id.get(mid) or {}).get("expires_at")
        if not close_at:
            skipped.append({"market_id": mid, "nombre": b.get("nombre"),
                            "reason": "el anuncio no trae hora de cierre"})
            continue
        capped = bidding.cap_against_rivals(
            b["amount"], (by_id.get(mid) or {}).get("valor_actual"), reach)
        try:
            row = scheduler.schedule_bid(lid, mid, capped, close_at,
                                         nombre=b.get("nombre"))
        except ValueError as e:
            skipped.append({"market_id": mid, "nombre": b.get("nombre"),
                            "reason": f"no pude programarla: {e}"})
            continue
        state.complete_by_key(f"sell:{(by_id.get(mid) or {}).get('player_id')}")
        why = explain.bid(by_id.get(mid) or {"nombre": b.get("nombre")},
                          capped, reach if capped < b["amount"] else None)
        scheduled.append({"market_id": mid, "nombre": b.get("nombre"),
                          "max_bid": capped, "computed_cap": b["amount"],
                          "rival_reach": reach, "close_at": to_iso(close_at),
                          "why": why, "status": row.get("status")})
        events.emit("bid-plan", f"Puja programada para el cierre: {b.get('nombre') or mid}",
                    detail={"why": why, "closes": to_iso(close_at),
                            "capped_from": (f"{b['amount']:,}"
                                            if capped < b["amount"] else None)},
                    status="plan")
    return {"mode": "snipe", "scheduled": scheduled, "skipped": skipped}


# Don't spend on a signing who will not play. Same floor the clause hunter uses.
MIN_SIGNING_PROB = 40


def _points_per_euro(candidate, price):
    """Expected points per million, the quantity a budget should be spent on.

    A candidate with no probability at all still has to be comparable, or the
    ranking would silently prefer whoever happens to have data. He is valued at
    a replacement-level rate instead: the honest reading of "no idea" is "an
    ordinary player", not "the best available" and not "worthless".
    """
    from .strategy.points import DEFAULT_RATE

    expected = candidate.get("expected_points")
    if expected is None:
        expected = DEFAULT_RATE * (float(candidate.get("prob") or 50) / 100.0)
    return expected / max(1.0, price / 1_000_000.0)


def _plan_gap_signings(ctx, lid, team, report):
    """Buy a player for a position we have nobody in.

    This is the difference between a bot that trades well and one that wins. The
    flip engine only bids on PROFITABLE resales — so a squad missing a goalkeeper
    would sit there, correctly declining to overpay, fielding ten men and losing
    points every single gameweek. An empty slot costs more than a bad margin.

    Clauses for gap positions are already handled (`agent.clause_targets` filters
    on exactly these positions). What was missing is the bidding route.

    Returns the money committed, so the flip planner bids with what is left rather
    than promising the same euros twice.
    """
    needs = report.get("needs") or {}
    gaps = needs.get("gaps") or {}
    if not gaps:
        return {"mode": "on", "queued": [], "committed": 0}
    if not (config.AUTO_BIDS and config.AUTO_EXECUTE) or ctx.dry_run:
        return {"mode": "off", "queued": [], "committed": 0,
                "gaps": list(gaps)}

    budget = max(0, int(num(team.get("teamMoney"))) - config.CASH_RESERVE)
    queued, skipped, committed = [], [], 0
    for pos in gaps:
        # Every candidate that clears the bar, then the best POINTS PER EURO
        # among them. Filling the slot with the first affordable name spends the
        # whole budget on one player when two cheaper ones would have scored
        # more between them — this is a knapsack, and value per euro is the
        # greedy answer to a knapsack, not a tiebreak bolted on afterwards.
        eligible = []
        for c in (needs.get("suggestions") or {}).get(pos) or []:
            if c.get("via") not in ("SISTEMA", "PUJA"):
                continue          # the clause route is planned elsewhere
            if not c.get("disponible") or not c.get("expires"):
                continue
            prob = c.get("prob")
            if prob is not None and prob < MIN_SIGNING_PROB:
                continue          # a benchwarmer does not fill a gap
            cap = int(c.get("max_bid") or c.get("price") or 0)
            if not cap or committed + cap > budget:
                continue
            eligible.append((c, cap))
        pick = max(eligible, key=lambda e: _points_per_euro(e[0], e[1]),
                   default=None)
        if pick is None:
            skipped.append({"pos": pos,
                            "why": "no hay ningún titular que entre en la caja"})
            continue
        c, cap = pick
        try:
            scheduler.schedule_bid(lid, c["market_id"], cap, c["expires"],
                                   nombre=c.get("nombre"))
        except ValueError as e:
            skipped.append({"pos": pos, "why": f"no pude programarla: {e}"})
            continue
        committed += cap
        # The task existed to tell a human to go and sign somebody. The bot just
        # did it, so the task is done — leaving it up would make an autonomous
        # bot look like it was asking for help.
        state.complete_by_key(f"gap:{pos}")
        from .strategy.needs import MIN_SQUAD
        counts = ((report.get("squad") or {}).get("counts") or {})
        why = explain.gap_signing(pos, c, cap, have=counts.get(pos),
                                  want=MIN_SQUAD.get(pos))
        queued.append({"pos": pos, "nombre": c.get("nombre"),
                       "max_bid": cap, "prob": c.get("prob"),
                       "closes": c.get("expires"), "why": why})
        events.emit("bid-plan", f"Fichaje programado: {c.get('nombre')} "
                                f"para el hueco en {pos}",
                    detail={"why": why, "closes": c.get("expires")},
                    status="plan")
        notify.send(f"gap:{pos}:{date.today().isoformat()}",
                    f"Falta un {pos}: pujando por {c.get('nombre')} "
                    f"hasta {cap:,} €", level="info")
    return {"mode": "on", "queued": queued, "skipped": skipped,
            "committed": committed}


def _plan_clauses(ctx, lid, team, report):
    """Queue a buyout for every worthwhile target, timed to its unlock.

    Clauses are a race: the player is gone to whoever pays first, and the window
    opens at a known instant. Queuing the payment for that instant — rather than
    noticing it on the next hourly review — is the whole advantage.

    Targets the report says are cheaper to simply BID on are left alone: paying a
    ~1.67x clause premium for someone already listed at his value is burning money.
    """
    targets = report.get("clause_targets") or []
    if not targets:
        return {"mode": "on" if config.AUTO_CLAUSES else "off", "queued": []}
    money = int(team.get("teamMoney") or 0)
    spendable = max(0, money - config.CASH_RESERVE)
    if config.MAX_CLAUSE:
        spendable = min(spendable, config.MAX_CLAUSE)

    queued, skipped = [], []
    for t in targets:
        clause = int(t.get("clause") or 0)
        unlock = parse_iso(t.get("unlock"))
        if t.get("cheaper_via_bid"):
            skipped.append({**_target_brief(t), "why": "cheaper to bid for him"})
            continue
        if not clause or unlock is None:
            skipped.append({**_target_brief(t), "why": "no clause or no unlock time"})
            continue
        if clause > spendable:
            skipped.append({**_target_brief(t), "why":
                            f"{clause:,} is beyond the {spendable:,} we can spend"})
            continue
        if not (config.AUTO_CLAUSES and config.AUTO_EXECUTE):
            skipped.append({**_target_brief(t), "why": "AUTO_CLAUSES is off"})
            continue
        if ctx.dry_run:
            skipped.append({**_target_brief(t), "why": "dry run"})
            continue
        scheduler.schedule(
            scheduler.CLAUSE,
            {"league_id": lid, "player_id": t.get("player_id"),
             "nombre": t.get("nombre"), "planned_clause": clause,
             # A small allowance so a routine value bump between planning and
             # payment does not cost us the player — but never past what we can
             # actually afford.
             "max_pay": min(spendable, round(clause * 1.10))},
            execute_at=unlock,
            idempotency_key=f"clause:{lid}:{t.get('player_id')}:{to_iso(unlock)}",
            expires_at=unlock + timedelta(hours=6))
        state.complete_by_key(f"clause:{t.get('player_id')}")
        why = explain.clause(t, clause)
        queued.append({**_target_brief(t), "unlock": to_iso(unlock), "why": why})
        events.emit("bid-plan", f"Cláusula programada: {t.get('nombre')} "
                                f"por {clause:,} €",
                    detail={"why": why, "unlocks": to_iso(unlock)},
                    status="plan")
    return {"mode": "on" if config.AUTO_CLAUSES else "off",
            "queued": queued, "skipped": skipped}


def _target_brief(t):
    return {"player_id": t.get("player_id"), "nombre": t.get("nombre"),
            "pos": t.get("pos"), "clause": t.get("clause"),
            "prob": t.get("prob")}


def _plan_shield(ctx, lid, report):
    """Queue a shield for our most exposed player, if the report found one."""
    cand = report.get("shield")
    if not cand:
        return {"mode": "on" if config.AUTO_SHIELD else "off", "queued": None}
    if not (config.AUTO_SHIELD and config.AUTO_EXECUTE) or ctx.dry_run:
        return {"mode": "off", "would_shield": cand}
    scheduler.schedule(
        scheduler.SHIELD,
        {"league_id": lid, **cand},
        execute_at=utcnow(),
        # A blindaje lasts 48h, so at most one per player per day is ever useful.
        idempotency_key=f"shield:{lid}:{cand.get('player_team_id')}:"
                        f"{date.today().isoformat()}",
        expires_at=utcnow() + timedelta(hours=12))
    return {"mode": "on", "queued": {**cand, "why": explain.shield(cand)}}


def _days_listed(store, team, market):
    """How many days each squad player has sat on the market unsold.

    Tracked here rather than read from the API because the listing row carries no
    "first listed" time — and a player who was sold and re-signed should start
    the clock again, which only a record of our own can tell us.
    """
    listed = {str((r.get("playerMaster") or {}).get("id"))
              for r in market or [] if r.get("discr") == "marketPlayerTeam"}
    squad = {str((p.get("playerMaster") or {}).get("id"))
             for p in team.get("players") or []}
    since = store.get_doc("listed_since", {}) or {}
    now, out = utcnow(), {}
    for pid in squad & listed:
        first = parse_iso(since.get(pid))
        if first is None:
            since[pid] = to_iso(now)
            first = now
        out[pid] = (now - first).total_seconds() / 86400
    # Forget anyone no longer listed (sold, or we pulled him): his clock restarts.
    for pid in list(since):
        if pid not in out:
            since.pop(pid, None)
    store.put_doc("listed_since", since)
    return out


def _schedule_warm(store):
    """Queue the next cache refresh, if one is due."""
    try:
        last = parse_iso(store.get_doc("last_warm_at"))
        if last is not None and (utcnow() - last).total_seconds() < config.WARM_INTERVAL:
            return
        at = utcnow()
        scheduler.schedule(
            scheduler.WARM, {}, execute_at=at,
            idempotency_key=f"warm:{at.strftime('%Y%m%d%H')}",
            expires_at=at + timedelta(hours=2))
    except Exception:
        pass


def _check_sources(report):
    """Notice when a scraped source quietly stops working.

    futbolfantasy feeds the value trends and the probable lineups. If their HTML
    changes, the parsers return an empty index and everything DEGRADES rather than
    failing: flips stop being found, the optimiser falls back to priors, and the
    bot looks like it is working. A count that has collapsed is the only signal.
    """
    try:
        from .sources.lineups import probable_lineups
        from .sources.market_trends import trends_index
        # Reads only what the warm job already cached; the leash is still on, so
        # a cold cache reports zero instead of paying 25 seconds to find out.
        trends, lineups = len(trends_index() or {}), len(probable_lineups() or {})
    except Exception as e:                       # noqa: BLE001
        notify.send("scraper_degraded",
                    f"No se pudieron leer las fuentes externas: {e}", level="warn")
        return {"ok": False, "error": str(e)}
    # LaLiga has 20 clubs and ~500 players; a working scrape returns hundreds.
    ok = trends >= 100 and lineups >= 100
    if not ok:
        notify.send("scraper_degraded",
                    f"Fuentes externas degradadas: {trends} tendencias, "
                    f"{lineups} alineaciones probables. El bot sigue, pero con "
                    f"menos información.", level="warn")
    return {"ok": ok, "trends": trends, "lineups": lineups}


def _kickoffs(client):
    """Upcoming kickoff times this gameweek, as aware datetimes.

    Defensive on purpose: the calendar payload's date field has several plausible
    names and this is not worth crashing a review over. Anything unparseable is
    dropped rather than guessed at, and an empty list simply means the per-match
    lineup refresh does not run this week.
    """
    try:
        fixtures = client.calendar() or []
    except Exception:
        return []
    out = []
    for f in fixtures if isinstance(fixtures, list) else []:
        if not isinstance(f, dict):
            continue
        for key in ("date", "matchDate", "kickoff", "startDate", "matchDateTime"):
            dt = parse_iso(f.get(key))
            if dt is not None:
                out.append(dt)
                break
    return sorted(set(out))


def _plan_matchday_lineups(ctx, client, lid, tid):
    """Re-optimise the XI shortly before each kickoff.

    A player locks when HIS match starts, not when the gameweek does. A striker
    playing Sunday can still be swapped on Saturday night if he picks up a knock —
    and an hourly cadence catches that only by luck. These are free points.
    """
    if not (config.AUTO_MATCHDAY_LINEUP and config.AUTO_LINEUP
            and config.AUTO_EXECUTE) or ctx.dry_run:
        return {"mode": "off", "queued": []}
    now = utcnow()
    lead = timedelta(minutes=config.LINEUP_LEAD_MINUTES)
    queued = []
    for ko in _kickoffs(client):
        at = ko - lead
        if at <= now or at - now > timedelta(days=8):
            continue
        scheduler.schedule(
            scheduler.LINEUP, {"league_id": lid, "team_id": tid,
                               "kickoff": to_iso(ko)},
            execute_at=at,
            idempotency_key=f"lineup:{lid}:{to_iso(ko)}",
            expires_at=ko)
        queued.append(to_iso(at))
    return {"mode": "on", "queued": queued}


def _queue_reminders(report, dry_run=False):
    """Reminders become queued actions so a tick actually fires them on time.

    `state.save_reminders` still keeps the list (the CLI's `due` command reads it);
    this just gives each one a deadline the serverless scheduler understands.
    """
    queued = []
    if dry_run:
        return queued
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


def handle_offers(ctx):
    """Decide every open offer on our listed players. Runs on EVERY tick.

    Offers arrive and expire between reviews, so this cannot wait for the hourly
    cycle. It is deliberately cheap: one market read plus the reserve prices the
    last review cached, no lineup optimisation.
    """
    from .strategy import offers as offers_mod

    store = get_storage()
    reserves = store.get_doc("reserves", {}) or {}
    if not reserves:
        return {"status": "skipped", "reason": "no reserves cached yet"}
    if not (config.AUTO_SELLS or config.DECLINE_LOWBALLS):
        return {"status": "skipped", "reason": "offer handling is off"}

    client = ctx.get_client()
    lid, _tid = league_ids(client)
    # The squad is read from the cached reserves, not from a fresh team() call:
    # the reserves ARE the list of our players, and a market read alone is enough
    # to see the offers. One request per tick instead of three.
    decisions = offers_mod.evaluate_offers(None, client.market(lid),
                                           reserves=reserves)
    if not decisions:
        return {"status": "ok", "offers": 0}

    accepted, declined, skipped = [], [], []
    for d in decisions:
        d["why"] = explain.offer(d)
        if ctx.out_of_time():
            skipped.append(d)
            continue
        try:
            if d["action"] == offers_mod.ACCEPT:
                if not config.AUTO_SELLS or ctx.dry_run:
                    skipped.append({**d, "why": "AUTO_SELLS is off"})
                    continue
                client.accept_offer(lid, d["market_id"], d["offer_id"], d["amount"])
                events.emit("sell", f"VENDIDO {d['nombre']} por {d['amount']:,} €",
                            detail={"why": d["why"]})
                notify.send(f"sold:{d['player_id']}", d["why"], level="good")
                accepted.append(d)
            elif config.DECLINE_LOWBALLS and not ctx.dry_run:
                client.decline_offer(lid, d["market_id"], d["offer_id"])
                declined.append(d)
            else:
                skipped.append(d)
        except Exception as e:                   # noqa: BLE001
            # One bad offer must not stop the rest — the next one may be the good
            # one. Recorded, not swallowed.
            events.emit("error", f"Falló la oferta por {d.get('nombre')}: {e}",
                        status="error")
            skipped.append({**d, "error": str(e)})
    return {"status": "ok", "accepted": accepted, "declined": declined,
            "skipped": skipped, "offers": len(decisions)}


def _listing_skips(team, market, planned):
    """A count of the squad players NOT going up, by reason.

    Deliberately counts rather than names: this is a health check on the
    planner, not a second list of the squad.
    """
    from .strategy import offers as offers_mod

    already = offers_mod._listed_player_ids(market)
    going = {str(r.get("player_id")) for r in planned}
    reasons = {}
    for p in team.get("players") or []:
        pid = str((p.get("playerMaster") or {}).get("id"))
        if pid in going:
            continue
        if pid in already:
            key = "ya estaba en el mercado"
        elif not offers_mod._market_value(p):
            key = "sin valor de mercado en la ficha"
        else:
            key = "reserva por debajo del mínimo"
        reasons[key] = reasons.get(key, 0) + 1
    return reasons


def _store_reserves(client, lid, team, best, sells):
    """Work out what each player is worth to us, and write it down.

    Pulled out of `_plan_listings` because the offer handler runs on EVERY tick
    and reads exactly this — while listing is a time-boxed phase the review drops
    when it is running late. A shortened review therefore left the bot unable to
    judge a single offer until a full one came round, which is why a squad
    standing on the market sold nothing: the asks were out there and nobody was
    reading the replies.

    It is cheap enough to be unconditional: one market read and pure arithmetic.
    """
    from .strategy import offers as offers_mod

    store = get_storage()
    market = client.market(lid)
    days = _days_listed(store, team, market)
    reserves = offers_mod.reserve_map(team, best, sells, listed_since=days)
    store.put_doc("reserves", reserves)
    return market, days


def _plan_listings(ctx, client, lid, team, best, sells, market=None, days=None):
    """Queue a listing for every squad player not already on the market."""
    from .strategy import offers as offers_mod

    if market is None:
        market, days = _store_reserves(client, lid, team, best, sells)
    if not config.AUTO_LIST or ctx.dry_run:
        return {"mode": "off" if not config.AUTO_LIST else "dry-run",
                "listed": [], "would_list": offers_mod.plan_listings(
                    team, market, best, sells, listed_since=days)}

    planned = offers_mod.plan_listings(team, market, best, sells,
                                       listed_since=days)
    # Why the others were left out. "Listed 0" over a squad of fifteen with none
    # on the market is a silent refusal, and a silent refusal is indistinguishable
    # from a switch being off — which cost a day of guessing between the two.
    left_out = _listing_skips(team, market, planned)

    queued = []
    for row in planned:
        scheduler.schedule(
            scheduler.LIST_SQUAD,
            {"league_id": lid, **row},
            execute_at=utcnow(),
            # One listing attempt per player per day: if a listing lapses unsold,
            # tomorrow's review puts him back up at a freshly computed reserve.
            idempotency_key=f"list:{lid}:{row['player_team_id']}:"
                            f"{date.today().isoformat()}",
            expires_at=utcnow() + timedelta(hours=12))
        queued.append({**row, "why": explain.listing(row)})
    return {"mode": "on", "listed": queued, "left_out": left_out}


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
        # A short leash for the review's own fetching. A cold source gives up
        # quickly and the review proceeds with one signal fewer, rather than
        # spending the whole function on a scrape and being killed.
        net.set_deadline(time.monotonic() + config.REVIEW_FETCH_BUDGET)
        try:
            report = agent_mod.review(client)
        finally:
            net.set_deadline(time.monotonic() + ctx.remaining())
        lid, tid = league_ids(client)
        team = client.team(lid, tid)

        # Everything past this point is time-boxed, in order of what costs most
        # to skip. Vercel kills a function at 60 seconds with an HTML error page
        # and no chance to clean up — so the review would rather drop its last
        # phases and record why than be killed mid-write and leave the dashboard
        # showing an unparseable error.
        skipped = []

        def _afford(name, margin):
            if ctx.out_of_time(margin=margin):
                skipped.append(name)
                return False
            return True

        lineup_res = ({"status": "skipped", "reason": "autonomy off"}
                      if not (config.AUTO_EXECUTE and config.AUTO_LINEUP)
                      else _apply_best_lineup(ctx, client, lid, tid, team))
        # Gaps first: an empty slot costs points every gameweek, which beats any
        # flip margin. Whatever it commits is withheld from the flip budget so
        # the same euros are not promised twice.
        gaps_res = (_plan_gap_signings(ctx, lid, team, report)
                    if _afford("gap_signings", 12) else {"committed": 0})
        remaining = dict(team)
        remaining["teamMoney"] = max(0, int(num(team.get("teamMoney")))
                                     - gaps_res.get("committed", 0))
        bids_res = (_plan_bids(ctx, client, lid, remaining, report)
                    if _afford("bids", 10) else {"mode": "out of time"})
        best = None
        try:
            from .strategy import lineup as lineup_opt
            # Deliberately without the fixture: this XI answers "who are my
            # regulars" for the selling logic, and a regular with a hard match
            # this week is still a regular. Tilting it by the opponent would put
            # a starter on the market because he happens to visit the leaders.
            best = lineup_opt.optimize(team)
        except ValueError:
            pass          # incomplete squad: reserves fall back to squad premiums
        # Always, before anything that can be dropped for time: these are what
        # every tick uses to answer an offer.
        try:
            market, days_listed = _store_reserves(client, lid, team, best,
                                                  report.get("sells"))
        except Exception as e:                   # noqa: BLE001
            market, days_listed = None, None
            skipped.append(f"reserves ({e})")
        listings = (_plan_listings(ctx, client, lid, team, best,
                                   report.get("sells"), market, days_listed)
                    if _afford("listings", 8) else {"mode": "out of time"})
        clauses = (_plan_clauses(ctx, lid, team, report)
                   if _afford("clauses", 6) else {"queued": []})
        shield = (_plan_shield(ctx, lid, report)
                  if _afford("shield", 5) else {"queued": None})
        matchday = (_plan_matchday_lineups(ctx, client, lid, tid)
                    if _afford("matchday", 5) else {"queued": []})
        # Cheapest and least urgent, so it goes last: the scrapes it reads are
        # already warm from the review above.
        sources = _check_sources(report) if _afford("sources", 4) else {}
        _schedule_warm(store)
        reminders = _queue_reminders(report, dry_run=ctx.dry_run)

        store.put_doc("last_review_at", to_iso(now))
        store.put_doc("last_report",
                      _summarize(report, lineup_res, bids_res, listings,
                                 clauses, shield, sources, gaps_res, skipped))
        events.emit("review", f"Revisión: caja {report['money']:,} €",
                    detail={"flips": len(report.get("flips") or []),
                            "tasks": len(report.get("tasks") or []),
                            "scheduled_bids": len(bids_res.get("scheduled") or [])})
        _note_market_read(report)
        if skipped:
            events.emit("note", f"Revisión acortada por tiempo: "
                                f"{', '.join(skipped)}",
                        detail={"elapsed": round(ctx.elapsed(), 1)},
                        status="plan")
        return {"status": "ok", "money": report.get("money"),
                # The census travels with the answer, not only into the stored
                # report: the caller asking "why did it buy a keeper" is holding
                # this dict, and sending them to look somewhere else is how the
                # evidence gets lost between the two.
                "squad": report.get("squad"),
                "lineup": lineup_res, "bids": bids_res, "gaps": gaps_res,
                "listings": listings,
                "clauses": clauses, "shield": shield, "matchday": matchday,
                "sources": sources, "skipped_for_time": skipped,
                "elapsed": round(ctx.elapsed(), 1),
                "reminders_queued": reminders,
                "tasks": len(report.get("tasks") or [])}
    finally:
        store.release_lock(REVIEW_LOCK, holder)


def _note_market_read(report):
    """One line in the log for the whole market, winners and losers.

    An event per declined listing would bury the log forty rows deep every hour.
    One line naming the best of each side is what a person actually reads, and
    the scored table on the page is there for the rest.
    """
    market = report.get("market") or []
    if not market:
        return
    best = market[0]
    worst = next((o for o in reversed(market)
                  if o.get("verdict") in ("no vale la pena", "no alcanza")), None)
    title = (f"Miré {len(market)} jugadores del mercado. El mejor: "
             f"{best.get('nombre')} ({best.get('score')}/100, "
             f"{best.get('verdict')})")
    detail = {"why": best.get("headline") or (best.get("reasons") or [""])[0]}
    if worst is not None:
        detail["descartado"] = (f"{worst.get('nombre')} "
                                f"({worst.get('score')}/100): "
                                f"{worst.get('headline') or ''}")
    events.emit("note", title, detail=detail)


def _summarize(report, lineup_res, bids_res, listings=None, clauses=None,
               shield=None, sources=None, gaps_res=None, skipped_phases=None):
    """What the dashboard reads. Deliberately small: a full review payload is
    hundreds of KB of squad data and there is no reason to store it every hour."""
    lu = report.get("lineup") or {}
    return {
        "at": to_iso(utcnow()),
        "money": report.get("money"),
        "matchday": report.get("matchday"),
        "formation": lu.get("formation"),
        # Now that the XI is built from expected points, its total is a number
        # that means something on its own: what the eleven should score.
        "xi_points": lu.get("total"),
        "lineup_changed": bool(lu.get("changed")),
        "lineup_result": lineup_res,
        "gaps": report.get("gaps"),
        "squad": report.get("squad"),
        "flips": (report.get("flips") or [])[:5],
        # The whole market, scored — including everything declined. Trimmed to
        # what a phone can render, not to what the bot considered.
        "market": (report.get("market") or [])[:30],
        "sells": (report.get("sells") or [])[:5],
        "clause_targets": (report.get("clause_targets") or [])[:5],
        "tasks": report.get("tasks") or [],
        "bids": bids_res,
        "gap_signings": gaps_res or {},
        "listings": listings or {},
        "clauses": clauses or {},
        "shield": shield or {},
        "sources": sources or {},
        "skipped_phases": skipped_phases or [],
        # Is any of this actually making money? The rivals analysis already
        # computes our own purchases, sales and net P&L — surfacing it is the
        # difference between trusting the bot and hoping.
        "pnl": next(({"purchases": r.get("purchases"), "sales": r.get("sales"),
                      "net": r.get("net_profit"),
                      "team_value": r.get("team_value")}
                     for r in (report.get("rivals") or []) if r.get("is_me")),
                    None),
        "rivals": [{"position": r.get("position"),
                    "manager": r.get("manager_name"),
                    "points": r.get("points"),
                    "team_value": r.get("team_value"),
                    "cash": r.get("estimated_balance"),
                    "partial_history": r.get("partial_history"),
                    "is_me": r.get("is_me")}
                   for r in (report.get("rivals") or [])][:12],
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
        events.emit("error", f"Falló la pasada del LLM: {e}", status="error")
        return {"status": "error", "error": str(e)}
    store.put_doc("last_llm_at", to_iso(now))
    return res


# =============================================================================
# The tick itself
# =============================================================================
def run(mode="tick", dry_run=False, force_review=False, log=print,
        budget_seconds=None, source=None):
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
    failed = []          # phases that raised: contained, reported, never hidden
    summary = {"mode": mode, "started_at": to_iso(utcnow()), "dry_run": dry_run,
               "source": source or "unknown"}

    try:
        _close_stale_executions(store)
        execution_id = store.start_execution(
            f"{mode}:{source}" if source else mode)
    except Exception as e:                       # noqa: BLE001
        # No database, no tick. Say so loudly rather than pretending to work.
        return {"ok": False, "error": f"storage unavailable: {e}", **summary}

    # Every scrape in this process now shares the tick's clock. Without it a cold
    # cache could spend the whole budget being polite to futbolfantasy and get
    # the function killed mid-write; with it the scrapes give up and the run
    # finishes with fewer signals, which is the right trade.
    net.set_deadline(time.monotonic() + ctx.budget_seconds)

    try:
        summary["actions"] = scheduler.run_due(ctx, log=log)

        # Offers arrive and expire between reviews, so they are handled on every
        # tick — not on the hourly cycle.
        if not ctx.out_of_time(margin=8):
            try:
                summary["offers"] = handle_offers(ctx)
            except Exception as e:               # noqa: BLE001
                summary["offers"] = {"status": "error", "error": str(e)}

        # A sniper tick exists only to hit a close; it must not spend its seconds
        # on a market review.
        if mode != "sniper" and not ctx.out_of_time(margin=15):
            # Contained, like the offer handling above it. One unexpected row in
            # the market used to raise out of the review and take the whole tick
            # with it — the health note, the token check and the scheduler repair
            # all skipped, and the tick recorded as a failure. The review is the
            # biggest thing here and the likeliest to meet something new; it is
            # not a reason for everything else to stop.
            try:
                summary["review"] = run_review(ctx, force=force_review)
            except Exception as e:               # noqa: BLE001
                summary["review"] = {
                    "status": "error", "error": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc()[-1200:]}
                events.emit("error", f"Falló la revisión: {e}", status="error")
                # Contained, but NOT quiet. The tick finishes its other work
                # instead of dying halfway through it, and still reports failure
                # so the scheduler's job goes red and somebody finds out. A green
                # light over a broken mechanism is the thing that hides for days.
                failed.append(f"review: {type(e).__name__}: {e}")
            if not ctx.out_of_time(margin=10):
                summary["llm"] = run_llm_strategy(ctx)

        summary["next_deadline"] = to_iso(scheduler.next_deadline())
        summary["sleep_seconds"] = _sleep_hint()
        summary["pending"] = len(store.pending_actions(limit=50))
        net.clear_deadline()
        summary["ok"] = not failed
        if failed:
            # The cause, not just the phase: "review falló" sends you looking,
            # "review: KeyError: 'discr'" tells you where. The trace goes at the
            # top level as well as inside the phase, because that is where
            # anything looking at a failed tick looks first.
            summary["error"] = "; ".join(failed)
            summary["traceback"] = (summary.get("review") or {}).get("traceback")
        # Before the execution is filed, so the dashboard's own record of this
        # run carries it too — not only the caller that happened to ask.
        summary["clock"] = _clock_report(store, source)
        summary["duration_seconds"] = round(time.monotonic() - started, 2)
        store.finish_execution(execution_id, DONE if not failed else FAILED,
                               summary=summary)
        _note_health(store, ok=not failed, error=summary.get("error"))
        _check_token_expiry(store)
        _heal_scheduler_url(store)
        return summary
    except Exception as e:                       # noqa: BLE001
        net.clear_deadline()
        summary.update({"ok": False, "error": f"{type(e).__name__}: {e}",
                        "traceback": traceback.format_exc()[-1500:],
                        "duration_seconds": round(time.monotonic() - started, 2)})
        try:
            store.finish_execution(execution_id, FAILED, summary=summary,
                                   error=summary["error"])
            events.emit("error", f"Falló la ejecución: {e}", status="error")
            _note_health(store, ok=False, error=summary["error"])
        except Exception:
            pass
        return summary


def _note_health(store, ok, error=None):
    """Track consecutive failures and speak up once — then once on recovery.

    One failure is a hiccup (a 500 from LaLiga, a cold start that timed out) and
    is not worth a phone buzzing. Two in a row is a deployment that is broken and
    will stay broken until somebody looks.
    """
    try:
        streak = int(store.get_doc("fail_streak", 0) or 0)
    except Exception:
        return
    if ok:
        if streak >= 2:
            notify.clear("tick_failed")
            notify.send("tick_recovered", "El bot volvió a funcionar.",
                        level="good", force=True)
        if streak:
            store.put_doc("fail_streak", 0)
        _note_gap(store)
        return
    streak += 1
    store.put_doc("fail_streak", streak)
    if streak >= 2:
        notify.send("tick_failed",
                    f"El bot lleva {streak} ejecuciones fallando.\n{error or ''}",
                    level="error")


# GitHub disables scheduled workflows in a repository with no activity for 60
# days. Nothing fails when that happens — the bot simply stops being woken, which
# is invisible precisely because nothing is running to notice. The daily Vercel
# cron is the independent witness: when it fires and finds the last tick was
# hours rather than minutes ago, the scheduler is dead and we say so.
SCHEDULER_GAP_ALERT = 3600


PLACEHOLDER_HOSTS = ("tu-app", "your-app", "tu-dominio")


def _clock_report(store, source):
    """What the database's own clock is doing, folded into the tick's answer.

    The database schedules itself and reports its own success, so when its calls
    are being refused there is nowhere that failure shows up: pg_cron logs
    `succeeded` (net.http_post only queues the request), Vercel answers 401
    before our code runs, and the tick that never happened leaves no trace. The
    only place both halves are visible at once is a tick woken by something else,
    so that tick asks.

    Skipped when the database is what woke us — the answer is then obvious — and
    kept to counts, so it costs one small query on the GitHub-driven ticks only.
    """
    if source == "db" or store.kind != "supabase":
        return None

    # What the database SAYS it did comes from a function you have to install,
    # and this deployment spent an afternoon reading a clock whose reporting half
    # was never applied. What it actually DID is already here: every tick records
    # who woke it. Count those first, because they need nothing installed and
    # they are the thing that matters — a wake-up that landed beats a log line
    # saying a request was queued.
    report = {}
    try:
        rows = store.recent_executions(limit=20)
    except Exception:                            # noqa: BLE001
        rows = []
    wakes = [r for r in rows
             if str(r.get("trigger") or "").split(":")[-1] == "db"]
    report["db_wakes_recent"] = len(wakes)
    last = parse_iso(wakes[0].get("started_at")) if wakes else None
    report["last_db_wake"] = to_iso(last) if last else None
    if last:
        report["minutes_since_db_wake"] = round(
            (utcnow() - last).total_seconds() / 60, 1)

    try:
        report.update(store._request("POST", "rpc/fantasybot_clock_status") or {})
    except Exception as e:                       # noqa: BLE001
        # Said out loud rather than swallowed into a null. "No answer" and "the
        # answer is bad" look identical from the outside, and telling them apart
        # by redeploying twice is how an evening goes.
        report["status"] = "unavailable"
        report["error"] = f"{type(e).__name__}: {e}"[:200]
    return report


def _heal_scheduler_url(store):
    """Point the database's clock at this deployment, with this deployment's key.

    The scheduler migration ships a placeholder URL and asks you to paste a
    secret, and both fail in the worst possible way when they are wrong: pg_net
    dutifully fires every minute and Vercel dutifully refuses, or the request
    goes to a domain that does not resolve. No error surfaces anywhere a person
    looks — the clock is running, the bot simply never wakes. This deployment saw
    both: a placeholder URL for a night, then fifteen 401s in fifteen minutes.

    A running function knows its own address AND its own secret, so it can settle
    both. It writes the URL only over a placeholder or an empty value, never over
    one somebody chose — two deployments can share a database, and hijacking the
    other one's scheduler would be worse than the problem. The secret is
    different: a mismatched one is never intentional, it is always the reason
    every call is refused, so it is simply kept in sync.

    When the environment has no secret at all — added after the last build, set
    on the wrong environment, never set — there is nothing to sync and the clock
    would stay locked out forever. So one is minted here and stored, and the
    request guard accepts the stored value (see serverless/http.shared_secret).
    The deployment and its database end up agreeing on a key neither a person nor
    a crawler ever sees, which is the only state in which the bot can wake.
    """
    if store.kind != "supabase":
        return False
    url = config.self_url()
    secret = config.BOT_CRON_SECRET
    # Read the row the clock actually reads. `fantasybot_wake` selects id = 1, so
    # inspecting "the first row" and writing to id = 1 can be two different rows
    # the moment the table holds more than one — the repair then reports success
    # having changed nothing, which is the most expensive kind of green there is.
    try:
        rows = store._request("GET", "scheduler_config",
                              params={"select": "app_url,bot_secret",
                                      "id": "eq.1", "limit": "1"})
    except Exception:
        return False    # migration 0002 not applied; nothing to heal
    if not rows:
        return False

    patch = {}
    current_url = (rows[0].get("app_url") or "").strip()
    if url and (not current_url
                or any(h in current_url.lower() for h in PLACEHOLDER_HOSTS)):
        patch["app_url"] = url
    stored_secret = (rows[0].get("bot_secret") or "").strip()
    minted = False
    if secret:
        if stored_secret != secret:
            patch["bot_secret"] = secret
    elif not stored_secret:
        patch["bot_secret"] = secrets.token_urlsafe(32)
        minted = True
    if not patch:
        return False

    wanted = dict(patch)
    try:
        patch["updated_at"] = to_iso(utcnow())
        store._request("PATCH", "scheduler_config", params={"id": "eq.1"},
                       body=patch, prefer="return=minimal")
        # Read it back. A PATCH that matches no row succeeds — 200, empty, no
        # complaint — so "the write did not raise" is not evidence the value
        # changed. Announcing a repair that did not happen is worse than
        # announcing nothing: it sends you looking somewhere else.
        after = store._request("GET", "scheduler_config",
                               params={"select": "app_url,bot_secret",
                                       "id": "eq.1", "limit": "1"}) or [{}]
        landed = [k for k, v in wanted.items()
                  if str(after[0].get(k) or "").strip() == v]
    except Exception:
        return False
    if not landed:
        return False

    labels = {"app_url": f"la URL ({url})",
              "bot_secret": ("un secreto nuevo (Vercel no tiene "
                             "BOT_CRON_SECRET)" if minted else "el secreto")}
    fixed = " y ".join(labels[k] for k in landed)
    events.emit("note", f"Reloj de la base: corregí {fixed}")
    notify.send("scheduler_config_fixed",
                f"Corregí {fixed} en el reloj de Supabase. Estaba "
                f"desalineado, así que sus llamadas se rechazaban.",
                level="good")
    return True


# An execution left `running` was killed before it could write its own ending —
# a Vercel timeout, a runner going away. It is not in progress and never will be,
# but it sits at the top of the dashboard looking like it is, and it skews every
# "when did this last run" answer. Older than this, call it what it is.
STALE_EXECUTION_SECONDS = 300


def _close_stale_executions(store):
    try:
        rows = store.recent_executions(limit=10)
    except Exception:
        return          # the store is unreachable; the tick will say so itself
    for row in rows:
        try:
            if row.get("status") != RUNNING or row.get("finished_at"):
                continue
            started = parse_iso(row.get("started_at"))
            if started is None:
                continue
            age = (utcnow() - started).total_seconds()
            if age > STALE_EXECUTION_SECONDS:
                store.finish_execution(
                    row.get("id"), FAILED,
                    error=f"Sin final tras {age / 60:.0f} min: la función fue "
                          f"cortada antes de poder cerrarse (timeout de Vercel).")
        except Exception:
            continue    # one unclosable row must not stop the rest


def _note_gap(store):
    try:
        rows = store.recent_executions(limit=2)
        if len(rows) < 2:
            return
        now, prev = parse_iso(rows[0].get("started_at")), parse_iso(
            rows[1].get("started_at"))
        if not (now and prev):
            return
        gap = (now - prev).total_seconds()
        if gap > SCHEDULER_GAP_ALERT:
            who = (rows[0].get("trigger") or "?").split(":")[-1]
            hint = {
                "github": "Lo está despertando GitHub Actions, que throttlea "
                          "muchísimo los schedules. Aplicá "
                          "0003_clock_status.sql para que lo despierte Supabase "
                          "cada minuto.",
                "db": "Lo despierta Supabase, pero con huecos: mirá "
                      "`select public.fantasybot_clock_status();`",
            }.get(who, "Ningún reloj automático lo despertó; fue a mano.")
            notify.send("scheduler_gap",
                        f"El bot pasó {gap / 3600:.1f} h sin ejecutarse "
                        f"(último origen: {who}). {hint}",
                        level="warn")
    except Exception:
        pass


def _check_token_expiry(store):
    """Warn before the 90-day LaLiga refresh token runs out.

    It is the one thing only a human can fix, and it expires without warning: the
    bot simply stops being able to log in. Better to be told a week early than to
    discover it after missing four gameweeks.
    """
    try:
        from . import auth
        tokens = store.get_doc("tokens", None)
        if not tokens:
            return
        exp = auth.jwt_exp(tokens.get("refresh_token") or "")
        if not exp:
            return
        days = (exp - utcnow().timestamp()) / 86400
        if days <= config.TOKEN_WARN_DAYS:
            notify.send("token_expiring",
                        f"La sesión de LaLiga caduca en {days:.0f} días. "
                        f"Hay que repetir `fantasybot login`.",
                        level="warn" if days > 1 else "error")
    except Exception:
        pass


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

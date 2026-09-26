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
from . import bidding, config, events, explain, modes, net, notify, scheduler
from . import execute as execute_mod
from . import state
from .scheduler import (BID, LINEUP, LLM_STRATEGY, REMINDER, REVIEW,
                        TickContext)
from .matching import num
from .strategy import clausedefense as clausedef
from . import memory
from .strategy import timing
from .strategy import upgrades as upgrades_mod
from .storage import (DONE, FAILED, RUNNING, SKIPPED, StorageUnavailable,
                      get_storage, parse_iso, to_iso,
                      utcnow)

REVIEW_LOCK = "review"

# The phases that put money to work. Dropping any of these for time is not the
# same as dropping `sources` — it is a review that did all the thinking and none
# of the acting, so it earns itself a second attempt a couple of minutes later.
_SPENDING_PHASES = ("gap_signings", "listings", "bids", "clauses",
                    "clause_defense")

# How long a listing has to show up on the market before its absence counts as a
# refusal rather than as lag. Generous: calling a good listing refused is worse
# than noticing a bad one an hour later.
LISTING_GRACE_SECONDS = 900

# Past this age an attempt is INCONCLUSIVE, never a refusal.
#
# Absence from the market means "LaLiga refused it" only while the listing
# would still be running. Judge it an hour later and a listing that was
# genuinely live and then lapsed — the normal end of every listing — reads
# exactly like a rejection, and the backoff then benches a player who did
# nothing wrong. Reconciliation runs on every tick, so the verdict is reached
# minutes after the call and this ceiling is never a constraint in practice.
LISTING_VERDICT_MAX_SECONDS = 2700        # 45 min

# Longest we wait before trying a repeatedly refused listing again. Doubling
# from two hours, this is reached after about four consecutive refusals.
LISTING_BACKOFF_MAX_HOURS = 12

# How far past the biggest transfer the league has actually completed we still
# treat a rival as able to reach. Somebody can always spend more than they ever
# have — but not ten times more, and defending against a number nobody has come
# close to costs real money for nothing.
OBSERVED_SPEND_HEADROOM = 2.0

# How far the two independent readings of rival spending power may disagree
# before neither is trusted. They measure the same thing from the same feed, so
# a wide gap is not caution versus boldness — it is a feed we are misreading.
REACH_DISAGREEMENT = 5.0

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
                        last_call_seconds=config.CLOCK_INTERVAL_SECONDS + 10,
                        ceiling=p.get("ceiling"))
    if res.get("status") == "waiting":
        # Still early. Stay queued; the scheduler will wake us closer to the close.
        return {"retry": True, **res}
    # `guarding` and `raised` carry their own `retry`, which stays true until the
    # listing closes: a bid placed five minutes out has to be watched for those
    # five minutes, or bidding early is all cost and no cover.
    if res.get("status") in ("bid", "raised") and not dry:
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


def _clause_row(client, lid, p):
    """His current row: clause, lock, shield — and the slot to pay on.

    From his owner's squad when the plan knows the owner, which is every target
    now: a clause is payable on anybody in a rival squad, not only on the few a
    rival happens to list, so the market is not where he is. Plans queued before
    that carried no owner and are still read from the market.
    Returns None when he is not where the plan left him.
    """
    pid = str(p.get("player_id"))
    owner = p.get("owner_team_id")
    if owner:
        for row in (client.team(lid, owner) or {}).get("players") or []:
            if str((row.get("playerMaster") or {}).get("id")) == pid:
                return {"clause": num(row.get("buyoutClause")) or None,
                        "unlock": row.get("buyoutClauseLockedEndTime"),
                        "shielded_until": (row.get("shieldedEndDate")
                                           if row.get("isShielded") else None),
                        "slot": row.get("playerTeamId")
                        or p.get("player_team_id")}
        return None
    for row in client.market(lid) or []:
        if row.get("discr") != "marketPlayerTeam":
            continue
        if str((row.get("playerMaster") or {}).get("id")) != pid:
            continue
        pt = row.get("playerTeam") or {}
        return {"clause": num(pt.get("buyoutClause")) or None,
                "unlock": pt.get("buyoutClauseLockedEndTime"),
                "shielded_until": None,
                "slot": pt.get("playerTeamId") or p.get("player_team_id")
                or p.get("player_id")}
    return None


@scheduler.executor(scheduler.CLAUSE)
def _execute_clause(ctx, action):
    """Pay a rival's buyout clause the moment it can be paid.

    The only irreversible spend the bot makes, so every assumption made when this
    was planned is re-checked against the live API before a euro moves. Planning
    happened up to a day ago; any of these can have changed since:

      * we may already own him — somebody else's clause payment, or our own bid
      * the clause may have gone up (his owner raised it, or his value did)
      * his owner may have shielded him, or sold him to somebody else
      * the balance may have gone down (a bid we won in the meantime)

    Anything that no longer holds means we stand down and say why. A skipped
    clause costs nothing; an over-paid one cannot be undone. Waiting — a lock,
    a shield, LaLiga's pre-gameweek window — is not standing down: the action
    stays queued and fires when it opens.
    """
    p = action.get("payload") or {}
    if not config.AUTO_CLAUSES:
        return {"status": "skipped", "reason": "AUTO_CLAUSES is off"}
    if ctx.dry_run:
        return {"status": "skipped", "reason": "dry run"}
    nombre = p.get("nombre")
    # The window first, before a single request: LaLiga shuts clauses for the
    # 24 hours before a gameweek starts, and asking during them is a refusal.
    gw = (get_storage().get_doc("gameweek_start", {}) or {}).get("at")
    is_open, reopens = clause_window(gw)
    if not is_open:
        return {"retry": True, "status": "window_closed", "nombre": nombre,
                "reopens_at": to_iso(reopens)}

    client = ctx.get_client()
    lid, tid = league_ids(client)
    player_id = p.get("player_id")
    max_pay = int(p.get("max_pay") or 0)

    team = client.team(lid, tid)
    owned = {str((pl.get("playerMaster") or {}).get("id"))
             for pl in team.get("players") or []}
    if str(player_id) in owned:
        return {"status": "already_owned", "nombre": nombre}

    live = _clause_row(client, lid, p)
    if live is None or live.get("clause") is None:
        return {"status": "gone", "nombre": nombre,
                "reason": "ya no está donde estaba (lo vendieron o lo ficharon)"}

    current = int(live["clause"])
    if current > max_pay:
        return {"status": "too_expensive", "nombre": nombre,
                "clause": current, "max_pay": max_pay,
                "reason": f"clause rose to {current:,}, cap was {max_pay:,}"}

    for until, why in ((live.get("unlock"), "locked"),
                       (live.get("shielded_until"), "shielded")):
        at = parse_iso(until)
        if at is not None and at > utcnow():
            # Not an error — come back when it opens.
            return {"retry": True, "status": why, "nombre": nombre,
                    "unlocks_at": to_iso(at)}

    money = int(num(team.get("teamMoney")))
    if 0 < config.MAX_CLAUSE_SHARE < 1 and current > money * config.MAX_CLAUSE_SHARE:
        # Re-checked HERE, not only where it was queued. The balance moves
        # between the plan and the payment, and this is the last point at which
        # refusing still costs nothing.
        return {"status": "too_expensive", "nombre": nombre, "clause": current,
                "share_limit": int(money * config.MAX_CLAUSE_SHARE),
                "reason": f"{current:,} is over {config.MAX_CLAUSE_SHARE:.0%} "
                          f"of a {money:,} balance"}
    if money - current < modes.cash_floor():
        return {"status": "insufficient_funds", "nombre": nombre,
                "clause": current, "money": money,
                "reserve": modes.cash_floor(),
                "reason": f"{current:,} would leave less than the "
                          f"{modes.cash_floor():,} reserve"}

    try:
        resp = client.pay_buyout_clause(lid, live.get("slot"), current)
    except Exception as e:                       # noqa: BLE001
        if "030.01.17" in str(e):
            # The window, told to us by LaLiga because nothing else did (no
            # calendar yet). Wait for it; do not burn the action on it.
            return {"retry": True, "status": "window_closed", "nombre": nombre,
                    "reason": "LaLiga no deja pagar cláusulas 24h antes de "
                              "la jornada"}
        raise
    events.emit("clause", f"COMPRADO {nombre} por cláusula: {current:,} €",
                detail={"was_planned_at": p.get("planned_clause"),
                        "de": p.get("owner"),
                        "balance_after": money - current})
    notify.send(f"clause:{player_id}",
                f"Fichado {nombre} por cláusula: {current:,} €. "
                f"Saldo: {money - current:,} €", level="good")
    # The squad just changed: rethink the eleven, the sales and the next buy.
    _wake_review(f"clausulazo a {nombre}")
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


def _raise_brief(p):
    return {"nombre": p.get("nombre"), "target": p.get("target"),
            "clause": p.get("clause"), "why": p.get("why")}


def _clause_cost_ratio():
    """What a euro of clause costs us.

    Measured in production when a raise has landed; until then the game's own
    rule, which is known: pay X and the clause rises by CLAUSE_FACTOR * X. The
    defence used to wait for a measurement that could never come — it probed a
    route that does not exist, so no raise ever landed to measure.
    """
    from .api import FantasyClient
    known = 1.0 / FantasyClient.CLAUSE_FACTOR
    try:
        doc = get_storage().get_doc("clause_cost_ratio", {}) or {}
        ratio = doc.get("ratio")
        return float(ratio) if ratio else known
    except Exception:                            # noqa: BLE001
        return known


@scheduler.executor(scheduler.RAISE_CLAUSE)
def _execute_raise_clause(ctx, action):
    """Put one of ours permanently out of reach — and learn what that cost.

    The endpoint returns the new clause, never the bill, so the money before and
    after IS the measurement. It is banked the first time a raise lands, and
    every later decision is priced with it instead of the deliberately pessimistic
    assumption the first one had to use.
    """
    p = action.get("payload") or {}
    if not (config.AUTO_RAISE_CLAUSE and config.AUTO_EXECUTE):
        return {"status": "skipped", "reason": "AUTO_RAISE_CLAUSE is off"}
    if ctx.dry_run:
        return {"status": "skipped", "reason": "dry run", **_raise_brief(p)}
    client = ctx.get_client()
    lid, tid = league_ids(client)
    pid = p.get("player_id")
    # The ROSTER SLOT, not the footballer. Like sell_player and shield_player,
    # this endpoint acts on a slot in our squad: given the playerMaster id it
    # answers 404, which is exactly what it did the first time it ran live.
    ptid = p.get("player_team_id") or pid
    target = int(num(p.get("target")))

    # Re-read before spending: the clause rises on its own with the player's
    # value, so the target we computed an hour ago may already be met, and the
    # balance may have gone into a bid since.
    team = client.team(lid, tid)
    before_money = int(num(team.get("teamMoney")))
    live = None
    for row in team.get("players") or []:
        if str((row.get("playerMaster") or {}).get("id")) == str(pid):
            live = row
            break
    if live is None:
        return {"status": "gone", "nombre": p.get("nombre"),
                "reason": "ya no está en la plantilla"}
    before_clause = int(num(live.get("buyoutClause")))
    if before_clause >= target:
        return {"status": "already_safe", "nombre": p.get("nombre"),
                "clause": before_clause, "target": target}

    # What to pay, not where to land: the clause rises by CLAUSE_FACTOR times
    # the amount sent.
    factor = getattr(client, "CLAUSE_FACTOR", 2) or 2
    cost = -(-(target - before_clause) // factor)          # ceil
    if cost > before_money - modes.cash_floor():
        return {"status": "too_expensive", "nombre": p.get("nombre"),
                "cost": cost, "money": before_money}

    resp = client.increase_buyout_clause(lid, ptid, cost)

    # What it actually cost. Read from the account, not from the response.
    after = client.team(lid, tid)
    after_money = int(num(after.get("teamMoney")))
    after_clause = before_clause
    for row in after.get("players") or []:
        if str((row.get("playerMaster") or {}).get("id")) == str(pid):
            after_clause = int(num(row.get("buyoutClause")))
            break
    measured = clausedef.measure_ratio(before_money, after_money,
                                       before_clause, after_clause)
    stored = (get_storage().get_doc("clause_cost_ratio", {}) or {}).get("ratio")
    if measured is not None and not stored:
        get_storage().put_doc("clause_cost_ratio", {"ratio": measured,
                                                    "at": to_iso(utcnow()),
                                                    "from": p.get("nombre")})
    paid = before_money - after_money
    events.emit("note", f"Cláusula de {p.get('nombre')} subida a {after_clause:,} €",
                detail={"antes": f"{before_clause:,} €",
                        "pagado": f"{paid:,} €",
                        "why": p.get("why"),
                        "coste por € de cláusula": measured or "no medible"})
    return {"status": "raised", "nombre": p.get("nombre"),
            "clause": after_clause, "previous": before_clause,
            "paid": paid, "measured_ratio": measured, "response": resp}


# The market, read ONCE per tick for listing purposes.
#
# `_execute_listing` used to read it twice per player: once to check he was not
# already up, and again afterwards to confirm the listing landed. At two calls a
# head that is 2.1 reads per listing — thirty-seven calls to put eighteen players
# on the market, about fifty-six seconds of network against a budget that ends at
# fifty. The queue therefore died half-drained every tick, and since LaLiga's
# listings lapse daily it never caught up: the same few players went up each
# morning and the rest never did.
#
# Cleared at the start of every tick by `run`, because a warm Vercel container
# reuses the process and a snapshot that outlived its tick would decide the next
# one's listings from stale data. Within a single tick the only thing that lists
# OUR players is us, so adding each id as we go keeps it honest without
# re-reading.
_MARKET_IDS: dict = {}


def _listed_now(client, lid):
    """playerMaster ids currently on the market as somebody's squad player."""
    if lid not in _MARKET_IDS:
        _MARKET_IDS[lid] = {
            str((r.get("playerMaster") or {}).get("id"))
            for r in client.market(lid) or []
            if r.get("discr") == "marketPlayerTeam"}
    return _MARKET_IDS[lid]


def forget_market():
    """Drop the per-tick market snapshot. For tests."""
    _MARKET_IDS.clear()


@scheduler.executor(scheduler.LIST_SQUAD)
def _execute_listing(ctx, action):
    """Put one player on the market at his reserve price."""
    p = action.get("payload") or {}
    if ctx.dry_run or not config.AUTO_LIST:
        return {"status": "skipped", "reason": "listing is off"}
    client = ctx.get_client()
    lid, pid = p["league_id"], str(p.get("player_id"))
    # Another tick (or you, from the app) may have listed him already, and a
    # second listing on the same player is at best noise. One shared snapshot
    # answers that for every listing in this tick.
    if pid in _listed_now(client, lid):
        return {"status": "already_listed", "nombre": p.get("nombre")}
    resp = client.sell_player(lid, p["player_team_id"], int(p["price"]))
    # He is up as far as we know, so the snapshot says so for the rest of this
    # tick without another call.
    _listed_now(client, lid).add(pid)
    # Whether it actually LANDED is checked against the next market read rather
    # than by making one now. A silent refusal still gets caught — the review
    # reads the market anyway, and an attempt that is not there by then is
    # reported — but it costs nothing per player instead of a full read each.
    # It also catches a listing that vanishes an hour later, which a check made
    # four seconds after the call never could.
    try:
        store = get_storage()
        attempts = store.get_doc("listing_attempts", {}) or {}
        attempts[pid] = {"at": to_iso(utcnow()), "nombre": p.get("nombre"),
                         "price": int(p["price"])}
        store.put_doc("listing_attempts", attempts)
    except Exception:                            # noqa: BLE001
        pass          # bookkeeping; never fail a listing that went through
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


def _rival_reach(report):
    """The richest rival's estimated cash, and why it is zero when it is.

    The key is `estimated_balance`. It was read as `cash` in two places, which
    is not a name that exists anywhere in the rivals analysis, so both of them
    quietly saw a league with no money in it: the bid cap never capped anything
    and the clause defence found nobody exposed, both while reporting normally.
    A wrong key fails exactly like a quiet league, which is why this returns the
    reason alongside the number instead of just the number.
    """
    rivals = report.get("rivals") or []
    if not rivals:
        return 0, "todavía no leí la actividad de la liga"
    if any(r.get("partial_history") for r in rivals):
        # An estimate built from a history we have not finished reading is not a
        # ceiling, it is a guess that is still climbing.
        return 0, "todavía estoy reconstruyendo la caja de los rivales"
    others = [r for r in rivals if not r.get("is_me")]
    if not others:
        return 0, "no veo rivales en la liga"
    # What the league has SHOWN it will pay, not what we think it is holding.
    #
    # The estimate was the only signal here and it does not survive contact with
    # the data: one manager came out at 159M holding the SMALLEST squad in the
    # league, while a rival with three times his squad estimated at 4.9M. The
    # ordering does not correlate with anything, because the error is unread
    # purchases and that varies per manager rather than cancelling out.
    #
    # The biggest transfer a rival has actually completed is a price somebody
    # paid, in the league's own activity feed. It cannot be wrong, only stale,
    # and stale in the safe direction: it grows the moment anyone spends more.
    # So it leads, with room for a rival to go further than he ever has, and the
    # estimate is only allowed to raise the bar within reach of what the league
    # has demonstrated. A number nobody has come close to spending is not a
    # threat to defend a squad against — it is a reason to spend our own money
    # on nothing.
    # PER MANAGER, not across the league. Comparing the biggest observed spend
    # to the biggest estimate takes them from different people — they can agree
    # by coincidence while every individual pair disagrees wildly, which is what
    # the live league does. The aggregate check passed and told me nothing.
    #
    # The two managers who shared a largest purchase of exactly 141,030,000 were
    # NOT a bug: the activity rows show the same player (3104) moving twice —
    # bought from the market on 10 August, then taken on his clause from that
    # owner on 8 September, for the same figure. Both buyers are credited, which
    # is correct. The number is real and so is the scale: this league trades at
    # a hundred million and up. Calling it impossible was an assumption about
    # the game asserted as a reading of the data, and it was wrong.
    reachable = []
    unreadable = 0
    for r in others:
        if r.get("estimate_suspect"):
            continue
        spent = int(num(r.get("max_purchase")))
        holds = int(num(r.get("estimated_balance")))
        if spent > 0 and holds > 0:
            spread = max(spent, holds) / min(spent, holds)
            if spread > REACH_DISAGREEMENT:
                # His two readings describe different people. Skip him rather
                # than pick the scarier one: defending against a number we
                # cannot corroborate spends real money on a guess.
                unreadable += 1
                continue
            reachable.append(min(max(spent, holds),
                                 round(spent * OBSERVED_SPEND_HEADROOM)))
        elif spent > 0:
            reachable.append(round(spent * OBSERVED_SPEND_HEADROOM))
        elif holds > 0:
            # Never recorded buying anything. Whether that is believable depends
            # entirely on whether he HAS a squad: nobody assembles ninety-three
            # million of footballers for free, so a squad with no purchases
            # behind it is a history we are missing rather than a manager who
            # never bought. With no squad either, it is week one and the
            # estimate — everyone's identical starting budget — is simply true.
            if int(num(r.get("team_value"))) > 0:
                unreadable += 1
            else:
                reachable.append(holds)
    if not reachable:
        if unreadable:
            return 0, ("lo que gastaron y lo que estimo que tienen no se "
                       "parecen; no gasto contra un número que no entiendo")
        return 0, "todavía no vi a ningún rival gastar"
    return max(reachable), None


def _cancel_offside_bids(client, lid, log=print):
    """Drop queued bids on players we are no longer allowed to bid for.

    Filtering the PLANNER only stops new ones. A bid already in the queue fires
    on its own schedule, hours later, against a rule that changed after it was
    written — and three of them were sitting there aimed at players other
    managers had parked on the market, which is exactly the transaction this bot
    does not do any more. A rival's player is reached by his clause.

    It also covers the case that motivated the rule: friends who list a squad the
    way we do, as a standing ask, and never accept. Money committed to those
    auctions is money not bidding on what LaLiga actually put up for sale.
    """
    try:
        rows = {str(r.get("id")): r for r in (client.market(lid) or [])}
    except Exception as e:                       # noqa: BLE001
        return {"checked": 0, "cancelled": [], "error": str(e)}
    cancelled = []
    for action in scheduler.pending(80):
        if action.get("type") != BID:
            continue
        payload = action.get("payload") or {}
        row = rows.get(str(payload.get("market_id")))
        # Gone from the market is not our business here: the bid will find it
        # missing and stand down on its own. Only a listing we can SEE and that
        # is not LaLiga's gets pulled.
        if row is None or bidding._is_laliga_listing(row):
            continue
        key = action.get("idempotency_key")
        try:
            scheduler.cancel(key)
        except Exception:                        # noqa: BLE001
            continue
        nombre = (payload.get("nombre")
                  or ((row.get("playerMaster") or {}).get("nickname")) or key)
        cancelled.append({"nombre": nombre, "key": key,
                          "why": "es de otro manager: se ficha por cláusula"})
        log(f"[tick] cancelled bid on {nombre}: not a LaLiga listing")
    if cancelled:
        events.emit("note", f"Cancelé {len(cancelled)} puja(s) por jugadores "
                            f"de otros managers",
                    detail={"jugadores": ", ".join(c["nombre"]
                                                   for c in cancelled),
                            "why": "a un rival se lo ficha por cláusula, "
                                   "no pujando por su anuncio"},
                    status="plan")
    return {"checked": len(rows), "cancelled": cancelled}


def _score_the_model(report):
    """Bank this gameweek's prediction, and settle any that results have landed for.

    The optimiser has always said what it expects the eleven to score and that
    number went nowhere, so the model was never wrong about anything — and a
    model nobody scores cannot improve. Both halves are cheap: the prediction is
    already computed, and the results come from the same `weekPoints` read the
    form model already makes.
    """
    week = ((report.get("matchday") or {}).get("week")
            if isinstance(report.get("matchday"), dict) else None)
    lineup = report.get("lineup") or {}
    out = {}
    # The eleven ACTUALLY fielded, tilted by this week's fixtures — not the
    # untilted one the selling logic builds.
    if week and lineup.get("xi"):
        got = memory.predict(week, {
            "formation": lineup.get("formation"),
            "total": lineup.get("total"),
            "goalkeeper": None, "defender": [], "midfield": [],
            "striker": [e for e in lineup["xi"] if e]})
        if got:
            out["predicted"] = {"week": week, "expected": got.get("expected"),
                                "formation": got.get("formation")}
    # Settle every week we are still waiting on, not only the last one: a
    # gameweek can finish while the bot is asleep, and a prediction nobody ever
    # settles is the same as never having made one.
    form_index = report.get("form_index") or {}
    if form_index:
        by_player = {pid: hist[0]["points"] for pid, hist in form_index.items()
                     if hist and hist[0].get("points") is not None}
        if by_player and week:
            settled = memory.settle(int(week) - 1, by_player)
            if settled and settled.get("actual") is not None:
                out["settled"] = {"week": int(week) - 1,
                                  "expected": settled.get("expected"),
                                  "actual": settled.get("actual"),
                                  "error": settled.get("error")}
                events.emit("note",
                            f"Jornada {int(week) - 1}: esperaba "
                            f"{settled.get('expected')} pts y saqué "
                            f"{settled.get('actual')}",
                            detail={"diferencia": settled.get("error"),
                                    "jugadores medidos":
                                        settled.get("settled_players")},
                            status="plan")
    out["accuracy"] = memory.accuracy()
    return out


def _react_to_squad_changes(report, team):
    """Notice that the squad changed, say so, and remember it.

    `state.diff_snapshots` has computed this on every review since the beginning,
    put it in the report under "events", and nothing ever read it. A rival paid
    the clause on one of ours and the run that watched it happen carried on as
    though nothing had. Losing a player is the biggest thing that can happen
    between two reviews — the money lands, a hole opens in the eleven, and every
    plan made an hour ago was made for a different squad.

    This runs BEFORE the phases that buy and sell, so the same review that
    notices the loss is the one that answers it.
    """
    changes = report.get("events") or {}
    line = memory.describe(changes)
    if not line:
        return {"changed": False}
    entries = memory.record(changes, money_after=num(team.get("teamMoney")))
    removed = list(changes.get("removed") or [])
    events.emit("note" if not removed else "error",
                f"Cambió la plantilla: {line}",
                detail={"entraron": ", ".join(changes.get("added") or []) or "—",
                        "salieron": ", ".join(removed) or "—",
                        "caja": f"{int(num(team.get('teamMoney'))):,} €"},
                status="error" if removed else "plan")
    # A player leaving is worth a phone buzzing; one arriving is the bot doing
    # what it was told to. The subject carries the names so two different losses
    # are two different messages rather than one muted by the other.
    if removed:
        notify.send(f"squad_out:{'-'.join(sorted(removed))}",
                    f"Perdí a {', '.join(removed)}. {line}", level="warn")
    return {"changed": True, "line": line, "entries": entries,
            "removed": removed, "added": list(changes.get("added") or [])}


def _committed_bids(market=None):
    """Money already promised to auctions that have not closed yet.

    Two places hold it: bids queued for a close, and bids already standing on
    the market. Counted once per listing — a queued bid that has fired and is
    now guarding its auction is in both. Returns (total, listing ids), and the
    ids are how a later review knows not to plan the same signing twice.
    """
    covered, total = set(), 0
    try:
        queued = scheduler.pending(80)
    except Exception:                            # noqa: BLE001
        queued = []
    for a in queued:
        if a.get("type") != BID:
            continue
        p = a.get("payload") or {}
        mid = str(p.get("market_id"))
        if mid in covered:
            continue
        covered.add(mid)
        total += int(num(p.get("max_bid")))
    for row in market or []:
        mine = bidding._our_bid(row)
        mid = str(row.get("id"))
        if mine and mid not in covered:
            covered.add(mid)
            total += int(num(bidding._bid_amount(mine)))
    return total, covered


def _team_value(team):
    value = num(team.get("teamValue"))
    if value:
        return value
    return sum(num((p.get("playerMaster") or {}).get("marketValue"))
               for p in team.get("players") or [])


def _buying_power(team, market=None):
    """What the bot can spend right now: cash, credit it can repay, minus promises."""
    from .strategy import finance
    store = get_storage()
    floors = store.get_doc("sale_floors", {}) or {}
    gw = (store.get_doc("gameweek_start", {}) or {}).get("at")
    committed, covered = _committed_bids(market)
    power = finance.buying_power(
        num(team.get("teamMoney")), _team_value(team),
        finance.liquid_value(floors),
        credit_use=modes.knob("credit_use") if config.AUTO_CREDIT else 0,
        offers_proven=bool(store.get_doc("offers_seen", None)),
        gameweek_start=gw, committed=committed, floor=modes.cash_floor())
    return {**power, "gameweek_start": gw, "covered": sorted(covered)}


def _plan_bids(ctx, client, lid, team, report, power=None):
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

    # Ordered by what each signing ADDS to the eleven per euro, not by resale
    # margin. Ranking the market by projected profit is a trader's question, and
    # a trader finishes the season rich and second; the league is scored on
    # points. `execute.plan_bids` remains the CLI's margin-ordered plan and the
    # fallback for a review that produced no ranking.
    # Only what we can actually BID on, BEFORE the budget picks its three.
    #
    # The filter used to sit after `best_plan`, which is a different thing
    # entirely: the three best points-per-euro in the whole market were chosen
    # first — rival-owned players included — and only then thrown out for being
    # unbiddable. Three slots spent on players we were never going to bid for,
    # and the LaLiga listings behind them never even considered. The run that
    # found this had eighteen candidates worth signing and scheduled nothing.
    #
    # A rival's player is not lost by this: he goes to the clause pipeline,
    # ranked by the same points-per-euro measure.
    from .strategy import finance
    power = power or _buying_power(team)
    gw = power.get("gameweek_start")
    covered = set(power.get("covered") or ())
    # Affordability is decided HERE, against what can really be spent — cash,
    # plus LaLiga's credit where the debt could be repaid before the next
    # gameweek — and not against the cash alone that the review priced the
    # market with. That is the difference between "no me alcanza" on every
    # listing and a bot that bids with a 240M squad behind it.
    candidates = []
    for r in (report.get("upgrades") or []):
        if r.get("via") != SYSTEM_LISTING:
            continue
        if str(r.get("market_id")) in covered:
            continue          # already bid on, or already queued: money counted
        price = int(num(r.get("buy_price")))
        credit_ok = bool(power.get("credit")) and finance.credit_ok_for(
            r.get("expires_at"), gw)
        limit = power["spend_total"] if credit_ok else power["spend_cash"]
        candidates.append({**r, "credit_ok": credit_ok,
                           "affordable": 0 < price <= limit})
    ranked = [r for r in candidates if upgrades_mod.worth_signing(r)]
    budget = power["spend_total"]
    # Where the candidates went. "scheduled_bids: 0" with forty-four million in
    # the bank is a claim with nothing beside it: it reads the same whether the
    # market was empty, everyone was too expensive, or nobody was worth the
    # points. Each of those is a different problem and two of them are bugs.
    funnel = {
        "anuncios de LaLiga": len(candidates),
        "de otros managers (van por cláusula)": len(
            [r for r in (report.get("upgrades") or [])
             if r.get("via") != SYSTEM_LISTING]),
        "ya pujados o en cola": len(covered),
        "no me alcanza": sum(1 for r in candidates if not r.get("affordable")),
        "suman muy poco": sum(1 for r in candidates
                              if r.get("affordable")
                              and (r.get("gain") or 0) < modes.knob("min_gain")),
        "valen la pena": len(ranked),
        "mejor gana": max((r.get("gain") or 0 for r in candidates), default=0),
        "hace falta ganar": modes.knob("min_gain"),
        "modo": modes.active(),
        "caja": power["cash"],
        "crédito usable": power["credit"],
        "comprometido": power["committed"],
        "poder de compra": power["spend_total"],
        "sin crédito porque": power.get("why_no_credit"),
    }
    if ranked:
        plan = [{"market_id": r["market_id"], "nombre": r.get("nombre"),
                 "amount": int(num(r.get("buy_price"))),
                 "margin_pct": r.get("margin_pct"), "gain": r.get("gain"),
                 "gain_per_million": r.get("gain_per_million"),
                 "credit_ok": r.get("credit_ok"),
                 "running_total": r.get("running_total")}
                for r in upgrades_mod.best_plan(ranked, budget,
                                                cash_budget=power["spend_cash"])]
        funnel["entran en la caja"] = len(plan)
    else:
        plan = [b for b in execute_mod.plan_bids(client, lid, team)
                if str(b.get("market_id")) not in covered]
        funnel["por margen (plan B)"] = len(plan)
    # Nobody in the league can outbid money they do not have. The richest rival's
    # estimated cash is the real ceiling on what any auction can cost us.
    # A cash estimate built from a partially backfilled history is not a ceiling,
    # it is a guess — and guessing LOW loses auctions. Until the history is
    # complete the cap stands as computed.
    reach, _ = _rival_reach(report)
    scheduled, skipped = [], []
    # `plan_bids` already fits the targets inside the balance, cheapest commitment
    # first; we only add the timing.
    # The close time and the current value come from whichever list the plan was
    # built from. Reading them off `flips` alone left an upgrade that was not
    # also a profitable flip with no close time — and therefore silently unbid.
    by_id = {str(o["market_id"]): o
             for o in (report.get("flips") or []) + (report.get("upgrades") or [])
             if o.get("market_id") is not None}
    for b in plan:
        mid = str(b["market_id"])
        row = by_id.get(mid) or {}
        # Only what LaLiga itself put up for sale.
        #
        # A rival's player reaches us by ONE route: his buyout clause. Bidding on
        # another manager's listing is a different transaction with a different
        # price, and the planner was doing it — a rival row is priced at his
        # CLAUSE here, so every such "bid" offered a ~1.67x premium for a player
        # the clause pipeline was already tracking properly.
        if row.get("via") != SYSTEM_LISTING:
            skipped.append({"market_id": mid, "nombre": b.get("nombre"),
                            "reason": "es de otro manager: va por cláusula, "
                                      "no por puja"})
            continue
        close_at = row.get("expires_at")
        if not close_at:
            skipped.append({"market_id": mid, "nombre": b.get("nombre"),
                            "reason": "el anuncio no trae hora de cierre"})
            continue
        # The asking price is not negotiable. A system listing cannot be bought
        # for less than it costs, so the rival cap has nothing to lower here —
        # cutting the BID down to what a poor league could counter just sends a
        # number LaLiga refuses. What the field does bound is how far ABOVE the
        # price we chase: the value drifts during the day and auctions get
        # contested, and outbidding money nobody has is money that does not buy
        # the next player. So the price is the bid, and the field caps the roof.
        price = int(num(b["amount"]))
        # How far above the asking price we are willing to chase is the mode's
        # call, and it is the honest place for "risk": losing an auction by a
        # hundred thousand costs the whole player, while overpaying for a trade
        # costs exactly the profit. The rival cap still applies on top — paying
        # over money nobody in the league has is never the aggressive move, just
        # the expensive one.
        roof = round(price * (1 + bidding.VALUE_DRIFT) * modes.knob("bid_ceiling"))
        ceiling = bidding.cap_against_rivals(max(price, roof), price, reach)
        # Never past what can actually be spent on him, counting the signings
        # planned ahead of him in this same pass: a raise into money that is
        # promised elsewhere is a bid LaLiga refuses or a debt nobody planned.
        limit = (power["spend_total"] if b.get("credit_ok")
                 else power["spend_cash"])
        before = int(num(b.get("running_total") or price)) - price
        ceiling = max(price, min(ceiling, limit - before))
        try:
            row = scheduler.schedule_bid(lid, mid, price, close_at,
                                         nombre=b.get("nombre"),
                                         ceiling=ceiling)
        except ValueError as e:
            skipped.append({"market_id": mid, "nombre": b.get("nombre"),
                            "reason": f"no pude programarla: {e}"})
            continue
        state.complete_by_key(f"sell:{(by_id.get(mid) or {}).get('player_id')}")
        why = explain.bid(by_id.get(mid) or {"nombre": b.get("nombre")},
                          price, reach if ceiling < roof
                          else None)
        scheduled.append({"market_id": mid, "nombre": b.get("nombre"),
                          "max_bid": price, "ceiling": ceiling,
                          "computed_cap": b["amount"],
                          "rival_reach": reach, "close_at": to_iso(close_at),
                          "why": why, "status": row.get("status")})
        events.emit("bid-plan", f"Puja programada para el cierre: {b.get('nombre') or mid}",
                    detail={"why": why, "closes": to_iso(close_at),
                            "precio": f"{price:,}", "techo": f"{ceiling:,}"},
                    status="plan")
    return {"mode": "snipe", "scheduled": scheduled, "skipped": skipped,
            "funnel": funnel, "budget": budget, "power": power}


# Don't spend on a signing who will not play. Same floor the clause hunter uses.
MIN_SIGNING_PROB = 40

# The only route a BID may take: a listing LaLiga itself published. Another
# manager's player is reached by paying his clause, never by bidding on his
# listing — a different transaction, at a different price, that the clause
# pipeline already handles.
SYSTEM_LISTING = "SISTEMA"


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


def _plan_gap_signings(ctx, lid, team, report, power=None):
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
    # Only the positions where no legal eleven can be fielded. Below that floor
    # the slot scores nothing every week and is worth almost any price; above it
    # you have a starter and no cover, and whether cover is worth buying is a
    # question with an actual number behind it — the upgrade engine answers it,
    # priced as the insurance it is, instead of a squad-size rule of thumb
    # spending real money on a substitute who would score 0.4 when he plays.
    gaps = report.get("blocking_gaps") or {}
    if not gaps:
        return {"mode": "on", "queued": [], "committed": 0}
    if not (config.AUTO_BIDS and config.AUTO_EXECUTE) or ctx.dry_run:
        return {"mode": "off", "queued": [], "committed": 0,
                "gaps": list(gaps)}

    from .strategy import finance
    power = power or _buying_power(team)
    gw = power.get("gameweek_start")
    covered = set(power.get("covered") or ())
    queued, skipped, committed = [], [], 0
    for pos in gaps:
        # Every candidate that clears the bar, then the best POINTS PER EURO
        # among them. Filling the slot with the first affordable name spends the
        # whole budget on one player when two cheaper ones would have scored
        # more between them — this is a knapsack, and value per euro is the
        # greedy answer to a knapsack, not a tiebreak bolted on afterwards.
        eligible = []
        for c in (needs.get("suggestions") or {}).get(pos) or []:
            if c.get("via") != SYSTEM_LISTING:
                continue          # a rival's player goes through his clause
            if not c.get("disponible") or not c.get("expires"):
                continue
            prob = c.get("prob")
            if prob is not None and prob < MIN_SIGNING_PROB:
                continue          # a benchwarmer does not fill a gap
            if str(c.get("market_id")) in covered:
                continue          # already bid on or queued
            cap = int(c.get("max_bid") or c.get("price") or 0)
            # Borrowed money only where it can be repaid before the gameweek.
            budget = (power["spend_total"]
                      if power.get("credit") and finance.credit_ok_for(
                          c.get("expires"), gw)
                      else power["spend_cash"])
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
                       "market_id": c.get("market_id"),
                       "max_bid": cap, "prob": c.get("prob"),
                       "closes": c.get("expires"), "why": why})
        events.emit("bid-plan", f"Fichaje programado: {c.get('nombre')} "
                                f"para el hueco en {pos}",
                    detail={"why": why, "closes": c.get("expires")},
                    status="plan")
        # Say what the hole actually is. "Falta un POR" next to three keepers
        # in the squad reads as a bot that cannot count; "1 de 2" reads as what
        # it is — a starter with no cover. Same alert, and you can act on it.
        have, want = counts.get(pos), MIN_SQUAD.get(pos)
        short = f" ({have} de {want})" if have is not None and want else ""
        notify.send(f"gap:{pos}:{date.today().isoformat()}",
                    f"Me falta {pos}{short}: pujando por {c.get('nombre')} "
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
    # The best thing we could sign RIGHT NOW, which is what waiting is measured
    # against. Only LaLiga's own listings: a rival's player is itself a clause.
    best_now_gain = max(
        ((u.get("gain") or 0) for u in (report.get("upgrades") or [])
         if u.get("via") == SYSTEM_LISTING and upgrades_mod.worth_signing(u)),
        default=0.0)
    kickoff = ((report.get("matchday") or {}).get("kickoff")
               if isinstance(report.get("matchday"), dict) else None)
    money = int(num(team.get("teamMoney")))
    spendable = max(0, money - modes.cash_floor())
    if config.MAX_CLAUSE:
        spendable = min(spendable, config.MAX_CLAUSE)
    # The fence that does not go stale. One player may never take more than this
    # share of the balance: a clause big enough to leave us unable to answer the
    # next one has cost us two players, not bought one.
    # The mode decides how much of the bank one player may take. "Todo a
    # puntos" lets an extraordinary signing be most of it; "hacer caja" barely
    # uses clauses at all, because a ~1.67x premium is a terrible entry price
    # for a trade. Never wider than the configured ceiling either way.
    share = min(config.MAX_CLAUSE_SHARE, modes.knob("clause_share"))
    if 0 < share < 1:
        spendable = min(spendable, int(money * share))

    gw = (get_storage().get_doc("gameweek_start", {}) or {}).get("at")
    # Who already has a payment waiting. Re-queuing him would pay nobody twice —
    # the executor re-reads ownership — but it would announce a new plan every
    # review for a player the bot is already about to take.
    try:
        waiting = {str((a.get("payload") or {}).get("player_id"))
                   for a in scheduler.pending(80)
                   if a.get("type") == scheduler.CLAUSE}
    except Exception:                            # noqa: BLE001
        waiting = set()
    queued, skipped = [], []
    for t in targets:
        clause = int(t.get("clause") or 0)
        unlock = parse_iso(t.get("unlock"))
        # Position is no longer the filter, so the BAR has to be. A clause is
        # this bot's only irreversible spend and it carries a ~1.67x premium:
        # paying one for a player who barely improves the eleven is the most
        # expensive way there is to stand still.
        gain = t.get("gain")
        if gain is not None and gain < modes.knob("min_gain"):
            skipped.append({**_target_brief(t),
                            "why": f"solo suma {gain} pts/jornada, no paga "
                                   f"la prima de la cláusula"})
            continue
        # Is he worth WAITING for? The unlock instant is LaLiga's, not ours, so
        # the real question is whether to hold the money for him or spend it
        # today on the best thing actually available. A gameweek played with a
        # worse eleven is not refunded when the signing finally lands. A clause
        # that is already open involves no waiting, so there is nothing to weigh.
        opens = parse_iso(t.get("unlock"))
        if (gain is not None and t.get("unlock") and opens is not None
                and opens > utcnow() + timedelta(minutes=5)):
            verdict = timing.worth_waiting(gain, best_now_gain, t["unlock"],
                                           kickoff)
            t = {**t, "timing": verdict}
            if not verdict["wait"]:
                skipped.append({**_target_brief(t), "why": verdict["why"]})
                continue
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
        if str(t.get("player_id")) in waiting:
            skipped.append({**_target_brief(t), "why": "ya está en cola"})
            continue
        # When it can actually be paid: his unlock, or now if that has passed,
        # pushed past LaLiga's pre-gameweek window when it lands inside it. The
        # best target goes first by a second per rank: they are paid one after
        # another from the same balance, and the first one paid is the one that
        # is sure to fit.
        fire = max(unlock, utcnow())
        is_open, reopens = clause_window(gw, now=fire)
        if not is_open and reopens is not None:
            fire = reopens + timedelta(seconds=5)
        fire = fire + timedelta(seconds=len(queued))
        # A clause that opens later is one plan, keyed on when it opens. One
        # already open has no such instant — its "unlock" is now, which moves —
        # so it is keyed on what LaLiga sent plus the hour: one attempt an hour
        # at most, and a stand-down (a raised clause, a short bank) is retried
        # an hour later instead of never.
        if unlock > utcnow():
            key = f"clause:{lid}:{t.get('player_id')}:{to_iso(unlock)}"
        else:
            key = (f"clause:{lid}:{t.get('player_id')}:"
                   f"{t.get('lock_key') or t.get('unlock')}:"
                   f"{utcnow().strftime('%Y%m%d%H')}")
        row = scheduler.schedule(
            scheduler.CLAUSE,
            {"league_id": lid, "player_id": t.get("player_id"),
             # The slot the payment is keyed on and whose squad he is in, so
             # the executor re-reads him where he actually is.
             "player_team_id": t.get("player_team_id"),
             "owner_team_id": t.get("owner_team_id"),
             "owner": t.get("owner"),
             "nombre": t.get("nombre"), "planned_clause": clause,
             # A small allowance so a routine value bump between planning and
             # payment does not cost us the player — but never past what we can
             # actually afford.
             "max_pay": min(spendable, round(clause * 1.10))},
            execute_at=fire,
            idempotency_key=key,
            expires_at=fire + timedelta(hours=6))
        if (row or {}).get("status") not in (None, "pending"):
            skipped.append({**_target_brief(t),
                            "why": "ya lo intenté esta hora"})
            continue
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
            "prob": t.get("prob"), "owner": t.get("owner"),
            "gain": t.get("gain")}


def _plan_clause_defense(ctx, lid, team, report):
    """Raise the clauses that a rival could actually pay.

    The shield patches one weekend; this is what stops the problem coming back
    every week until somebody finally takes the player. It spends real money, so
    it is fenced three ways: only players whose loss costs real points, only a
    share of free cash, and only at a price we have MEASURED rather than guessed.
    """
    if not (config.AUTO_RAISE_CLAUSE and config.AUTO_EXECUTE) or ctx.dry_run:
        return {"mode": "off", "raises": []}
    reach, blocked = _rival_reach(report)
    if blocked:
        # Defending against a number we cannot read means either paying twice
        # for the same protection or, as happened here, never defending at all
        # while reporting that nobody is exposed.
        return {"mode": "waiting", "raises": [], "why": blocked}
    got = clausedef.plan(team, reach, num(team.get("teamMoney")),
                         report.get("points_at_risk") or {},
                         cost_ratio=_clause_cost_ratio(),
                         reserve=modes.cash_floor())
    # What the reach was built from. A single number is not enough to tell a
    # rich league from one manager whose history we have misread, and that
    # distinction decides whether the whole squad looks reachable.
    got["reach_detail"] = [
        {"manager": r.get("manager_name"),
         "mayor_compra": int(num(r.get("max_purchase"))),
         "estimado": int(num(r.get("estimated_balance"))),
         "valor_plantel": int(num(r.get("team_value"))),
         "dudoso": bool(r.get("estimate_suspect"))}
        for r in (report.get("rivals") or []) if not r.get("is_me")][:6]
    queued = []
    for r in got.get("raises") or []:
        try:
            scheduler.schedule(
                scheduler.RAISE_CLAUSE, {"league_id": lid, **r},
                execute_at=utcnow(),
                # Once per player per day: the clause only needs raising again
                # when his value has moved, which is a daily event at most.
                idempotency_key=f"raise:{lid}:{r.get('player_id')}:"
                                f"{date.today().isoformat()}",
                expires_at=utcnow() + timedelta(hours=6))
        except ValueError as e:
            continue_reason = f"no pude programarla: {e}"
            got.setdefault("skipped", []).append({"nombre": r.get("nombre"),
                                                  "why": continue_reason})
            continue
        queued.append(r)
    return {**got, "raises": queued, "rival_reach": reach,
            "cost_ratio": _clause_cost_ratio(),
            # What an activity row actually looks like, while the two readings
            # of it still disagree. Disappears once one is recorded.
            # Recorded once and carried until the readings stop
            # contradicting each other. Gating it on `blocked` hid it exactly
            # when the aggregate check wrongly passed and I needed it most.
            "activity_shape": get_storage().get_doc("activity_shape", None)}


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

    # The clock survives a lapsed listing, and this is the whole point.
    #
    # LaLiga's market closes every day, so a player we are still trying to sell
    # is ABSENT from the market for part of every single day — the listing
    # phase re-posts him each morning, as its own idempotency key says. The
    # previous version forgot the clock for anyone not on the market at that
    # instant, so every re-listing started again at day zero.
    #
    # `days_listed` therefore never once got past zero, the premium never
    # decayed, and the asking price sat at value +15% forever. Nobody pays that,
    # so nothing sold, so the bank only ever drained. Six simulated days of the
    # real cycle asked 5,750,000 on day one and 5,750,000 on day six.
    #
    # What the clock is actually measuring is "how long have we been trying to
    # sell this player", and that is not interrupted by the market closing
    # overnight. Only leaving the squad ends it: sold, or taken by a clause.
    for pid in list(since):
        if pid not in squad:
            since.pop(pid, None)
    for pid in squad & listed:
        since.setdefault(pid, to_iso(now))
    for pid, first_iso in since.items():
        first = parse_iso(first_iso)
        if first is not None:
            out[pid] = max(0.0, (now - first).total_seconds() / 86400)
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


def _schedule_catchup(skipped, now):
    """Re-run the review shortly when the clock cost it a phase that SPENDS.

    A review that drops `gap_signings` and `bids` has done the whole expensive
    part — reading the market, pricing every candidate, ranking the upgrades —
    and then stopped one step before the only step that puts money to work. The
    next one is an hour away, and in that hour the listings it was going to bid
    on close. Forty-five million in the bank and zero bids scheduled is not a
    cautious bot, it is a bot that ran out of seconds.

    Two minutes later the scraped caches are still warm, so the re-run reaches
    those phases with time to spare. Keyed by the hour, so a review that keeps
    running long retries once and then waits for its next turn rather than
    looping on itself.
    """
    spending = [n for n in skipped if n in _SPENDING_PHASES]
    if not spending:
        return None
    try:
        at = now + timedelta(seconds=120)
        scheduler.schedule(
            scheduler.REVIEW, {"reason": f"catch-up: {', '.join(spending)}"},
            execute_at=at,
            idempotency_key=f"review-catchup:{now.strftime('%Y%m%d%H')}",
            expires_at=at + timedelta(minutes=20))
        return {"at": to_iso(at), "phases": spending}
    except Exception:                            # noqa: BLE001
        return None


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
    # The per-gameweek stats come from LaLiga, not from a scrape, which is
    # exactly why they are worth reporting next to it: when the scrape is the
    # half that broke, this is the half still telling us who is playing.
    form_stat = report.get("form") or {}
    if not ok:
        cover = ("Los datos oficiales por jornada siguen bien, así que aún sé "
                 "quién está jugando." if form_stat.get("ok") else
                 "Los datos oficiales por jornada tampoco están llegando.")
        notify.send("scraper_degraded",
                    f"Fuentes externas degradadas: {trends} tendencias, "
                    f"{lineups} alineaciones probables. {cover}", level="warn")
    return {"ok": ok, "trends": trends, "lineups": lineups,
            "form": form_stat}


def _kickoffs(client, week=None):
    """Kickoff times of a gameweek (the current one by default), as aware datetimes.

    Defensive on purpose: the calendar payload's date field has several plausible
    names and this is not worth crashing a review over. Anything unparseable is
    dropped rather than guessed at, and an empty list simply means the per-match
    lineup refresh does not run this week.
    """
    try:
        fixtures = (client.calendar(week) if week is not None
                    else client.calendar()) or []
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


def _gameweek_start(client, report=None, now=None):
    """The first kick-off of the next gameweek that has not started, as ISO.

    The instant that decides two things at once: a balance still negative then
    scores zero for the whole gameweek, and buyout clauses cannot be paid in the
    24 hours before it. Read from LaLiga's own calendar first — the current
    gameweek if it has not begun, the next one if it has — and from the scraped
    fixture list only when the calendar says nothing.
    """
    now = now or utcnow()
    try:
        week = (client.current_week() or {}).get("weekNumber")
    except Exception:                            # noqa: BLE001
        week = None
    if week is not None:
        try:
            week = int(week)
        except (TypeError, ValueError):
            week = None
    for w in ((week, week + 1) if week is not None else ()):
        kos = _kickoffs(client, w)
        if kos and kos[0] > now:
            return to_iso(kos[0]), "calendario"
    iso = ((report or {}).get("matchday") or {}).get("gameweek_kickoff") \
        if isinstance((report or {}).get("matchday"), dict) else None
    at = parse_iso(iso)
    if at is not None and at > now:
        return to_iso(at), "futbolfantasy"
    return None, None


def clause_window(gameweek_start, now=None):
    """Whether a buyout clause can be paid right now, and when that changes.

    LaLiga shuts the window in the 24 hours before a gameweek's first kick-off
    and reopens it at that kick-off (030.01.17 on the way in). Returns
    (open, reopens_at): reopens_at is the kick-off while shut, None while open.
    With no known kick-off there is no opinion: open.
    """
    start = parse_iso(gameweek_start)
    now = now or utcnow()
    if start is None:
        return True, None
    if start - timedelta(hours=24) <= now < start:
        return False, start
    return True, None


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


def _wake_review(reason, now=None):
    """Ask for a review NOW, because something just changed the answer.

    The cadence is a floor on how often the bot thinks, not the only time it
    does. A sale puts money in the bank that should be bidding before the next
    close; a new LaLiga listing is a player nobody has scored yet. Waiting for
    the clock to come round wastes exactly the minutes that decide auctions.

    Keyed to the five-minute slot, so a burst of triggers is one review.
    """
    now = now or utcnow()
    slot = now.replace(minute=now.minute - now.minute % 5, second=0,
                       microsecond=0)
    try:
        scheduler.schedule(
            REVIEW, {"reason": reason}, execute_at=now,
            idempotency_key=f"review-wake:{slot.strftime('%Y%m%d%H%M')}",
            expires_at=now + timedelta(minutes=15))
        return True
    except Exception:                            # noqa: BLE001
        return False


def _watch_new_listings(store, market):
    """Notice when LaLiga puts new players up, and wake a review for them.

    Returns the ids that were not there last time. The first read of a fresh
    install has nothing to compare against, so it only remembers.
    """
    ids = sorted(str(r.get("id")) for r in market or []
                 if r.get("id") is not None and bidding._is_laliga_listing(r))
    before = store.get_doc("seen_system_listings", None)
    if before is None or sorted(before) != ids:
        store.put_doc("seen_system_listings", ids)
    if before is None:
        return []
    fresh = sorted(set(ids) - set(before))
    if fresh:
        _wake_review(f"{len(fresh)} jugador(es) nuevo(s) de LaLiga")
    return fresh


def _live_money(client, tid, market=None):
    """The balance right now, as cheaply as possible.

    Our own listing rows carry `sellerTeam.teamMoney`, so a tick that already
    read the market usually has it for free; otherwise one small read.
    """
    for row in market or []:
        seller = row.get("sellerTeam") or {}
        if seller.get("teamMoney") is not None and str(seller.get("id")) == str(tid):
            return int(num(seller.get("teamMoney")))
    try:
        return int(num((client.team_money(tid) or {}).get("teamMoney")))
    except Exception:                            # noqa: BLE001
        return None


def handle_offers(ctx):
    """Sell. Runs on EVERY tick.

    LaLiga's market row says how many offers a listing has and nothing more;
    the offers themselves are read from the player's slot. Then each is decided
    against what we TAKE for that player — not what we ask, which LaLiga forces
    above value — and, when the bank is negative with a gameweek coming, against
    the one rule that matters more than any price: a gameweek that starts in the
    red scores zero.

    Cheap on purpose: one market read, one request per listing that actually
    has offers, and the prices the last review cached.
    """
    from .strategy import finance
    from .strategy import offers as offers_mod

    store = get_storage()
    reserves = store.get_doc("reserves", {}) or {}
    floors = store.get_doc("sale_floors", {}) or {}
    if not reserves and not floors:
        return {"status": "skipped", "reason": "no reserves cached yet"}
    if not (config.AUTO_SELLS or config.DECLINE_LOWBALLS):
        return {"status": "skipped", "reason": "offer handling is off"}

    client = ctx.get_client()
    lid, tid = league_ids(client)
    market = client.market(lid)
    # Free: the read is already here, and reconciling every tick rather than
    # every review is what keeps a verdict minutes old instead of an hour old —
    # which is the difference between spotting a refusal and mistaking a lapsed
    # listing for one.
    try:
        _reconcile_listings(store, market)
    except Exception:                            # noqa: BLE001
        pass          # bookkeeping never breaks the offer handling
    try:
        fresh = _watch_new_listings(store, market)
    except Exception:                            # noqa: BLE001
        fresh = []
    # The offers, from where LaLiga actually keeps them.
    polled = store.get_doc("offers_polled", {}) or {}
    fetch = offers_mod.attach_offers(
        client, lid, market, set(reserves) | set(floors),
        slots={pid: (meta or {}).get("ptid") for pid, meta in floors.items()},
        polled=polled, now_ts=time.time(),
        out_of_time=lambda: ctx.out_of_time(margin=10))
    if fetch.get("consultados"):
        store.put_doc("offers_polled", polled)
    # What our own listings look like coming back. "Nobody sold" has three
    # causes that render identically — nothing of ours is up, our listings are
    # up and nobody bid, or offers exist and we are not reading them.
    shape = offers_mod.inspect_our_listings(market, reserves=reserves or floors)
    shape["lectura_de_ofertas"] = fetch
    store.put_doc("listing_shape", shape)
    decisions = offers_mod.evaluate_offers(None, market,
                                           reserves=reserves or None,
                                           floors=floors or None)
    if not decisions:
        return {"status": "ok", "offers": 0, "listings_seen": shape,
                "new_listings": len(fresh)}
    # Proof that the sale channel works. The credit line waits for this: the
    # bot does not borrow against a buyer it has never seen turn up — and the
    # buyer a debt is repaid with is LaLiga's daily offer, not a friend's bid.
    from_laliga = sum(1 for d in decisions if d.get("de_laliga"))
    if from_laliga:
        store.put_doc("offers_seen", {"at": to_iso(utcnow()),
                                      "offers": len(decisions),
                                      "de_laliga": from_laliga})

    money = _live_money(client, tid, market)
    gw = (store.get_doc("gameweek_start", {}) or {}).get("at")
    level = finance.pressure(money if money is not None else 0, gw)
    if money is not None and level is not finance.CALM:
        finance.settle_debt(decisions, money, level)

    accepted, declined, held, skipped = [], [], [], []
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
                            detail={"why": d["why"],
                                    "comprador": ("LaLiga" if d.get("de_laliga")
                                                  else d.get("comprador") or "rival"),
                                    "mínimo": f"{int(d.get('floor') or 0):,} €",
                                    "valor": f"{int(d.get('value') or 0):,} €"})
                notify.send(f"sold:{d['player_id']}", d["why"], level="good")
                accepted.append(d)
            elif d["action"] == offers_mod.HOLD:
                held.append(d)
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
    if accepted:
        # Money just landed. It should be bidding before the next close, not
        # waiting for the clock to come round.
        _wake_review(f"vendí {len(accepted)} jugador(es)")
    after = None if money is None else money + sum(d["amount"] for d in accepted)
    if level is not finance.CALM and after is not None and after < 0:
        notify.send(f"debt:{(gw or '')[:10]}",
                    f"Sigo en negativo ({after:,} €) y la jornada empieza "
                    f"{('el ' + gw[:16].replace('T', ' ')) if gw else 'pronto'}. "
                    f"Vendo en cuanto entren ofertas.", level="error")
    return {"status": "ok", "accepted": accepted, "declined": declined,
            "held": held, "skipped": skipped, "offers": len(decisions),
            "money_before": money, "money_after": after, "pressure": level,
            "new_listings": len(fresh), "read": fetch}


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


def _expected_points(team, report):
    """Expected points per gameweek for every player we own, by roster slot.

    Pricing a squad without this is how two reserve keepers ended up listed at
    market value PLUS fifteen per cent — asked at a premium, all season, for
    footballers who were never going to take the field. Nobody pays that, so
    nothing sold, so the money stayed in them.
    """
    from .strategy import lineup as lineup_opt
    prob_index = (report or {}).get("prob_index")
    fixture = (report or {}).get("fixture_difficulty")
    form_index = (report or {}).get("form_index")
    out = {}
    for p in team.get("players") or []:
        pm = p.get("playerMaster") or {}
        ptid = str(p.get("playerTeamId") or pm.get("id") or "")
        if not ptid:
            continue
        try:
            score, _prob, _disp, _tag = lineup_opt.player_score(
                p, prob_index, fixture, form_index)
        except Exception:                        # noqa: BLE001
            continue
        out[ptid] = round(float(score), 2)
    return out


def _paid_by_id(lid, team):
    """What we paid for each player we still hold, keyed by playerMaster id.

    Only a profit-taking mode needs this, so every other mode pays nothing for
    it — the activity feed is already in storage, but walking it is work, and
    this runs inside the listing phase's slice of a sixty-second budget.

    Failing is fine and returns {}. Without an entry price a holding is simply
    priced on its merits, which is what every mode did before this existed.
    """
    if not modes.knob("flip_target"):
        return {}
    try:
        from .strategy import history as history_mod
        mid = team.get("managerId") or (team.get("manager") or {}).get("id")
        return history_mod.paid_for_squad(state.load_activity_history(lid), mid)
    except Exception:
        return {}


def _listing_backoff(store):
    """playerMaster ids we should stop trying to list for now, and until when.

    Re-listing every review is right when a listing merely lapsed, and wrong
    when LaLiga is refusing the player — a cap on simultaneous listings, say.
    Without this the hourly retry would hammer a refusal forever.

    The wait doubles per consecutive refusal and is capped, and one confirmed
    listing clears the record entirely.
    """
    strikes = store.get_doc("listing_strikes", {}) or {}
    if not strikes:
        return {}
    now, out = utcnow(), {}
    for pid, row in strikes.items():
        last = parse_iso((row or {}).get("last"))
        count = int((row or {}).get("count") or 0)
        if last is None or count <= 0:
            continue
        wait = min(2 ** count, LISTING_BACKOFF_MAX_HOURS)
        until = last + timedelta(hours=wait)
        if until > now:
            out[pid] = {"until": to_iso(until), "veces": count}
    return out


def _reconcile_listings(store, market):
    """Did the listings we sent actually land? Answered by a read we already made.

    The executor no longer verifies each listing with its own market read — that
    was the cost that stopped the queue draining. It records the attempt instead,
    and this checks them all against the market the review reads anyway.

    Strictly more informative than the old per-player check, which ran four
    seconds after the call: a listing that LaLiga accepted and then dropped an
    hour later was invisible to it and is caught here.

    Attempts younger than the grace period are left alone — the row may simply
    not have appeared yet.
    """
    attempts = store.get_doc("listing_attempts", {}) or {}
    if not attempts:
        return {}
    live = {str((r.get("playerMaster") or {}).get("id"))
            for r in market or [] if r.get("discr") == "marketPlayerTeam"}
    now, refused, confirmed = utcnow(), [], 0
    strikes = store.get_doc("listing_strikes", {}) or {}
    for pid, row in list(attempts.items()):
        if pid in live:
            attempts.pop(pid, None)
            strikes.pop(pid, None)      # it worked; the slate is clean
            confirmed += 1
            continue
        at = parse_iso((row or {}).get("at"))
        age = None if at is None else (now - at).total_seconds()
        if age is not None and age < LISTING_GRACE_SECONDS:
            continue          # too early to call it a refusal
        attempts.pop(pid, None)
        if age is None or age > LISTING_VERDICT_MAX_SECONDS:
            # Too old to tell a refusal from a listing that simply ran its
            # course. Forget it rather than blame anybody.
            continue
        count = int((strikes.get(pid) or {}).get("count") or 0) + 1
        strikes[pid] = {"count": count, "last": to_iso(now)}
        refused.append({"player_id": pid, "nombre": (row or {}).get("nombre"),
                        "price": (row or {}).get("price"),
                        "veces": count})
    store.put_doc("listing_attempts", attempts)
    store.put_doc("listing_strikes", strikes)
    if refused:
        events.emit("error",
                    f"LaLiga no aceptó {len(refused)} anuncio(s)",
                    detail={"jugadores": ", ".join(str(r.get("nombre"))
                                                   for r in refused),
                            "why": "se enviaron a vender y no aparecen en el "
                                   "mercado"},
                    status="error")
    return {"confirmed": confirmed, "refused": refused,
            "pending": len(attempts)}


def _store_reserves(client, lid, team, best, sells, report=None):
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
    expected = _expected_points(team, report)
    # The offer handler reads this map on every tick. It has to agree with what
    # the listing phase asks for, or the bot lists a holding at its profit-taking
    # price and then judges the offer that meets it against the old premium —
    # refusing the very sale it advertised.
    # The market read is already in hand, so answering "did yesterday's
    # listings land" costs nothing extra here.
    listing_check = _reconcile_listings(store, market)
    paid = _paid_by_id(lid, team)
    reserves = offers_mod.reserve_map(team, best, sells, listed_since=days,
                                      expected=expected, paid_by_id=paid)
    store.put_doc("reserves", reserves)
    # What we TAKE for each one, beside what we ask. Written together so the
    # offer handler never judges an offer with one review's ask and another's
    # floor.
    store.put_doc("sale_floors", offers_mod.sale_floors(
        team, best, sells, listed_since=days, expected=expected,
        paid_by_id=paid, fund_ids=_fund_ids(team, report, best, expected)))
    if listing_check.get("refused"):
        store.put_doc("last_listing_refusals", listing_check["refused"])
    return market, days, expected, paid


def _fund_ids(team, report, best=None, expected=None):
    """playerMaster ids whose sale would pay for a signing that adds more.

    `transfers` pairs every signing the bank cannot reach with the cheapest
    sale that covers it, net of the points that sale costs. Those players are
    for sale for a reason beyond themselves, so their floor drops: LaLiga's
    offer at about value is a yes, because the signing it funds is worth more.

    The best clause target the cash cannot reach is funded the same way, from
    the bench: a clause needs cash in hand — LaLiga lends nothing for one — so
    the bench players who score least are sold until it is there.
    """
    by_slot = {str(p.get("playerTeamId")): str((p.get("playerMaster") or {}).get("id"))
               for p in (team or {}).get("players") or []}
    out = set()
    for t in ((report or {}).get("transfers") or [])[:3]:
        pid = by_slot.get(str(t.get("player_team_id")))
        if pid:
            out.add(pid)
    return out | _clause_funding(team, report, best, expected)


def _clause_funding(team, report, best=None, expected=None):
    """The bench players whose sale would pay the best clause the cash cannot."""
    from .strategy import offers as offers_mod
    from .strategy.lineup import payload_ids

    if not (config.AUTO_CLAUSES and config.AUTO_EXECUTE):
        return set()
    cash = int(num((team or {}).get("teamMoney"))) - modes.cash_floor()
    share = min(config.MAX_CLAUSE_SHARE, modes.knob("clause_share"))
    bar = 2 * modes.knob("min_gain")
    worth = [t for t in (report or {}).get("clause_targets") or []
             if (t.get("gain") or 0) >= max(bar, 0.5)
             and int(num(t.get("clause"))) > max(0, cash)]
    if not worth:
        return set()
    t = max(worth, key=lambda r: (r.get("gain_per_million") or 0))
    clause = int(num(t.get("clause")))
    # The share fence is re-checked at payment, so the cash has to clear it too.
    needed = max(clause, round(clause / share) if 0 < share < 1 else clause)
    short = needed - max(0, cash)
    xi = {str(i) for i in (payload_ids(best) if best else set())}
    bench = []
    for p in (team or {}).get("players") or []:
        pm = p.get("playerMaster") or {}
        ptid = str(p.get("playerTeamId") or pm.get("id"))
        if ptid in xi:
            continue
        value = int(num(pm.get("marketValue")))
        pts = (expected or {}).get(ptid) or 0
        bench.append((pts, -value, str(pm.get("id")), value))
    bench.sort()
    out, raised = set(), 0
    for _pts, _neg, pid, value in bench:
        if raised >= short:
            break
        out.add(pid)
        raised += round(value * offers_mod.ACCEPT_FUNDING)
    return out if raised >= short else set()


def _plan_listings(ctx, client, lid, team, best, sells, market=None, days=None,
                   expected=None, paid=None):
    """Queue a listing for every squad player not already on the market."""
    from .strategy import offers as offers_mod

    if market is None:
        market, days, expected, paid = _store_reserves(client, lid, team, best,
                                                       sells)
    # AUTO_EXECUTE is the master switch, and this phase was the one that did not
    # ask. Observe-only mode meant "touch nothing", and listing the whole squad
    # is touching something: it puts every player of yours in front of the league
    # at a price. It only stayed hidden because the review often ran out of time
    # before reaching this phase.
    if not (config.AUTO_LIST and config.AUTO_EXECUTE) or ctx.dry_run:
        return {"mode": ("dry-run" if ctx.dry_run
                         else "off" if not config.AUTO_LIST
                         else "observe-only"),
                "listed": [], "would_list": offers_mod.plan_listings(
                    team, market, best, sells, listed_since=days,
                    expected=expected, paid_by_id=paid)}

    planned = offers_mod.plan_listings(team, market, best, sells,
                                       listed_since=days, expected=expected,
                                       paid_by_id=paid)
    # Anyone LaLiga keeps refusing waits instead of being retried every hour.
    backoff = _listing_backoff(get_storage())
    if backoff:
        planned = [r for r in planned if str(r.get("player_id")) not in backoff]
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
            # One attempt per REVIEW, not per day.
            #
            # The key used to carry the calendar date, and a finished action
            # keyed the same way is a permanent no-op — that is the anti-double
            # -bid rule doing its job on the wrong thing. LaLiga's listings lapse
            # when the market closes, so from that moment the squad was off the
            # market and every hourly review that tried to put it back was
            # silently swallowed until midnight. Simulated over one day: the
            # whole squad listed at 00h, the listings lapsed at 14h, and the
            # planner correctly queued all of them at 15h, 16h, 17h and every
            # hour after — and not one reached the market. Ten hours a day with
            # nothing for sale.
            #
            # Double-listing is already prevented twice and does not need this
            # key to do it: the planner skips anyone currently on the market,
            # and the executor re-checks against the tick's market snapshot.
            idempotency_key=f"list:{lid}:{row['player_team_id']}:"
                            f"{utcnow().strftime('%Y-%m-%dT%H')}",
            # Short: if this attempt does not run within the hour, the next
            # review plans a fresh one at a freshly computed reserve. A stale
            # listing action firing hours late would use yesterday's price.
            expires_at=utcnow() + timedelta(hours=1))
        queued.append({**row, "why": explain.listing(row)})
    return {"mode": "on", "listed": queued, "left_out": left_out,
            "en_espera": backoff}


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
        # What the thinking cost, before any of the acting starts. When phases
        # get dropped for time this is the number that explains it, and every
        # time it mattered it was missing: "shortened for time" with no idea
        # whether the review took eight seconds or thirty-eight is a symptom
        # report, not a diagnosis.
        think_seconds = round(ctx.elapsed(), 1)
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

        # First, before anything is planned: did the squad change under us?
        squad_change = _react_to_squad_changes(report, team)
        # And how did the last one actually go? Cheap, and it is the only thing
        # that tells us whether any of the rest of this works.
        scoring_report = _score_the_model(report)

        lineup_res = ({"status": "skipped", "reason": "autonomy off"}
                      if not (config.AUTO_EXECUTE and config.AUTO_LINEUP)
                      else _apply_best_lineup(ctx, client, lid, tid, team))
        # The gameweek clock, read once and written down: the offer handler runs
        # every minute and needs it to know how hard to sell when the bank is
        # negative, and the clause planner needs it to know when LaLiga shuts
        # the clause window.
        try:
            gw_at, gw_src = _gameweek_start(client, report, now=now)
            store.put_doc("gameweek_start", {"at": gw_at, "source": gw_src,
                                             "computed": to_iso(now)})
        except Exception:                        # noqa: BLE001
            pass
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
        # every tick uses to answer an offer — and what the credit line is
        # measured against, so they come before anything that spends.
        try:
            market, days_listed, expected_pts, paid_for = _store_reserves(
                client, lid, team, best, report.get("sells"), report)
        except Exception as e:                   # noqa: BLE001
            market, days_listed, expected_pts, paid_for = None, None, None, None
            skipped.append(f"reserves ({e})")
        # What can really be spent: cash, the part of LaLiga's credit the bench
        # can repay before the gameweek, minus bids already promised.
        power = _buying_power(team, market)
        # Gaps first: an empty slot costs points every gameweek, which beats any
        # flip margin. Whatever it commits is withheld from the flip budget so
        # the same euros are not promised twice.
        gaps_res = (_plan_gap_signings(ctx, lid, team, report, power=power)
                    if _afford("gap_signings", 12) else {"committed": 0})
        remaining = dict(team)
        remaining["teamMoney"] = max(0, int(num(team.get("teamMoney")))
                                     - gaps_res.get("committed", 0))
        gap_spend = int(gaps_res.get("committed", 0) or 0)
        bid_power = {**power,
                     "committed": power["committed"] + gap_spend,
                     "spend_cash": max(0, power["spend_cash"] - gap_spend),
                     "spend_total": max(0, power["spend_total"] - gap_spend),
                     "covered": sorted(set(power.get("covered") or ()) | {
                         str(q.get("market_id")) for q in
                         gaps_res.get("queued") or [] if q.get("market_id")})}
        # Listing comes BEFORE the phases that spend. It is the one that brings
        # money in, it is the cheapest of them, and it is the one that had never
        # run: the review kept reaching its budget among the phases that buy and
        # dropping the phase that sells. Selling first also funds the buying.
        listings = (_plan_listings(ctx, client, lid, team, best,
                                   report.get("sells"), market, days_listed,
                                   expected=expected_pts, paid=paid_for)
                    if _afford("listings", 8) else {"mode": "out of time"})
        # Before planning anything new, retire what the rules no longer allow.
        offside = _cancel_offside_bids(client, lid, log=ctx.log)
        bids_res = (_plan_bids(ctx, client, lid, remaining, report,
                               power=bid_power)
                    if _afford("bids", 10) else {"mode": "out of time"})
        if offside.get("cancelled"):
            bids_res = {**bids_res, "cancelled": offside["cancelled"]}
        clauses = (_plan_clauses(ctx, lid, team, report)
                   if _afford("clauses", 6) else {"queued": []})
        shield = (_plan_shield(ctx, lid, report)
                  if _afford("shield", 5) else {"queued": None})
        # Defence goes after the buying phases on purpose: a squad you cannot
        # improve is not worth defending, and the raises spend from what is left.
        defense = (_plan_clause_defense(ctx, lid, team, report)
                   if _afford("clause_defense", 5) else {"raises": []})
        matchday = (_plan_matchday_lineups(ctx, client, lid, tid)
                    if _afford("matchday", 5) else {"queued": []})
        # Cheapest and least urgent, so it goes last: the scrapes it reads are
        # already warm from the review above.
        sources = _check_sources(report) if _afford("sources", 4) else {}
        _schedule_warm(store)
        catchup = _schedule_catchup(skipped, now)
        reminders = _queue_reminders(report, dry_run=ctx.dry_run)

        # The REPORT first, the clock second. They were the other way round, and
        # the order is the whole difference between two failures:
        #
        #   report stored, clock not  -> the next tick reviews again. Harmless.
        #   clock stored, report not  -> the review is "done" for an hour and
        #                                the page keeps yesterday's market,
        #                                presented as if it were today's.
        #
        # The second is what happened: a review that stamped itself complete and
        # then lost the write it existed to make. Anything that throws between
        # these two lines now costs a repeated review instead of a blind hour.
        store.put_doc("last_report",
                      _summarize(report, lineup_res, bids_res, listings,
                                 clauses, shield, sources, gaps_res, skipped,
                                 think_seconds=think_seconds, catchup=catchup,
                                 defense=defense, squad_change=squad_change,
                                 scoring_report=scoring_report))
        store.put_doc("last_review_at", to_iso(now))
        events.emit("review", f"Revisión: caja {report['money']:,} €",
                    detail={"flips": len(report.get("flips") or []),
                            "tasks": len(report.get("tasks") or []),
                            "scheduled_bids": len(bids_res.get("scheduled") or [])})
        _note_market_read(report)
        if skipped:
            events.emit("note", f"Revisión acortada por tiempo: "
                                f"{', '.join(skipped)}",
                        detail={"elapsed": round(ctx.elapsed(), 1),
                                "el análisis": f"{think_seconds}s",
                                "reintento": (catchup or {}).get("at")
                                or "no hace falta"},
                        status="plan")
        return {"status": "ok", "money": report.get("money"),
                # Which phases the clock cost us, and how long the whole thing
                # took. Every time this mattered it was missing: a review that
                # drops its phases looks identical to one that had nothing to do.
                "skipped": skipped,
                "seconds": round(ctx.elapsed(), 1),
                "think_seconds": think_seconds,
                # The census travels with the answer, not only into the stored
                # report: the caller asking "why did it buy a keeper" is holding
                # this dict, and sending them to look somewhere else is how the
                # evidence gets lost between the two.
                "squad": report.get("squad"),
                "lineup": lineup_res, "bids": bids_res, "gaps": gaps_res,
                "listings": listings,
                "clauses": clauses, "shield": shield, "matchday": matchday,
                # The two newest signals, in the answer the deploy check reads.
                # A phase that runs and reports nothing looks exactly like one
                # that was never wired up, and that has cost a cycle twice now.
                "defense": defense,
                # When the per-gameweek stats did not parse, the payload's own
                # shape travels with the answer. It is the only way to write the
                # parser from here: the endpoint cannot be called from a laptop,
                # so the first live run IS the documentation. Keys only, and it
                # disappears from the response the moment it starts working.
                "form": (report.get("form") if (report.get("form") or {}).get("ok")
                         else {**(report.get("form") or {}),
                               "shape": store.get_doc("all_players_shape", {})}),
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


def _finance_brief(bids_res):
    store = get_storage()
    power = dict((bids_res or {}).get("power") or {})
    power.pop("covered", None)
    try:
        power["ofertas_vistas"] = store.get_doc("offers_seen", None)
        power["recompensa_diaria"] = store.get_doc("daily_reward", None)
        power["jornada"] = store.get_doc("gameweek_start", None)
    except Exception:                            # noqa: BLE001
        pass
    return power


def _summarize(report, lineup_res, bids_res, listings=None, clauses=None,
               shield=None, sources=None, gaps_res=None, skipped_phases=None,
               think_seconds=None, catchup=None, defense=None,
               squad_change=None, scoring_report=None):
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
        # Which posture every decision below was taken under. Without it the
        # page explains WHAT the bot did and never why it was willing to.
        "mode": modes.describe(),
        # Evidence for the one question the page could never answer: are we on
        # the market at all, and is anybody bidding?
        "listing_shape": get_storage().get_doc("listing_shape", {}) or {},
        "xi_points": lu.get("total"),
        "lineup_changed": bool(lu.get("changed")),
        "lineup_result": lineup_res,
        "gaps": report.get("gaps"),
        "squad": report.get("squad"),
        "flips": (report.get("flips") or [])[:5],
        # The whole market, scored — including everything declined. Trimmed to
        # what a phone can render, not to what the bot considered.
        # Every listing, not the top thirty. This is a single document rewritten
        # once an hour — not the executions table, which is what actually had to
        # be slimmed — and a market runs to a few dozen rows.
        "market": report.get("market") or [],
        # How many listings there were and how many were not ours, so an empty
        # market list can say which of the two things happened.
        "market_census": report.get("market_census") or {},
        "upgrades": (report.get("upgrades") or [])[:10],
        "transfers": report.get("transfers") or [],
        # The whole-squad moves: sell these, buy those, and what the eleven is
        # worth afterwards. Capped because each one carries its own reasoning.
        "rebuild": (report.get("rebuild") or [])[:3],
        "leaks": (report.get("leaks") or [])[:6],
        "sells": (report.get("sells") or [])[:5],
        "clause_targets": (report.get("clause_targets") or [])[:5],
        "tasks": report.get("tasks") or [],
        "bids": bids_res,
        # The money, whole: what is in the bank, what LaLiga would lend and how
        # much of it the bot will use, what is already promised, and the clock
        # the debt has to be repaid by.
        "finance": _finance_brief(bids_res),
        "gap_signings": gaps_res or {},
        "listings": listings or {},
        "clauses": clauses or {},
        "shield": shield or {},
        # Who a rival could take off us right now, and what we did about it.
        "defense": defense or {},
        "sources": sources or {},
        "skipped_phases": skipped_phases or [],
        "squad_change": squad_change or {},
        "ledger": memory.recent(12),
        # What it predicted, what happened, and how wrong it has been.
        "scoring": scoring_report or {},
        # Next to the list of what was dropped, the two numbers that say why and
        # what happens about it: how long the analysis took before any of the
        # acting started, and when the re-run is due.
        "think_seconds": think_seconds,
        "catchup_at": (catchup or {}).get("at"),
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
    # The market snapshot belongs to THIS tick, not to the process.
    #
    # Vercel reuses a warm container across invocations, so "a tick is a fresh
    # process" is true on a cold start and false the rest of the time. A market
    # read cached past the end of a tick would decide the next tick's listings
    # from stale data — skipping players who had since lapsed off the market,
    # which is the exact failure this cache was added to fix.
    forget_market()
    ctx = TickContext(
        budget_seconds=budget_seconds or (config.SNIPER_BUDGET_SECONDS
                                          if mode == "sniper"
                                          else config.TICK_BUDGET_SECONDS),
        dry_run=dry_run, log=log, mode=mode)
    execution_id = None
    failed = []          # phases that raised: contained, reported, never hidden
    summary = {"mode": mode, "started_at": to_iso(utcnow()), "dry_run": dry_run,
               "source": source or "unknown",
               # So a caller can tell whether it is looking at the build it just
               # deployed, instead of assuming.
               "deploy": config.DEPLOY_SHA[:7] or None}

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
        # A human who pressed "analyse the market now" wants the analysis, not
        # the queue drained. The review used to come LAST, after every due action
        # and every open offer — so with a dozen actions waiting, the tick spent
        # its whole budget before reaching it and the review never ran. Worse, it
        # said nothing: the branch below simply did not execute and `review` was
        # absent from the answer, which reads exactly like a review that found
        # nothing. On a forced run the order is inverted.
        if force_review and mode != "sniper":
            try:
                summary["review"] = run_review(ctx, force=True)
            except Exception as e:               # noqa: BLE001
                summary["review"] = {
                    "status": "error", "error": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc()[-1200:]}
                events.emit("error", f"Falló la revisión: {e}", status="error")
                failed.append(f"review: {type(e).__name__}: {e}")

        summary["actions"] = scheduler.run_due(ctx, log=log)

        # Offers arrive and expire between reviews, so they are handled on every
        # tick — not on the hourly cycle.
        if not ctx.out_of_time(margin=8):
            try:
                summary["offers"] = handle_offers(ctx)
            except Exception as e:               # noqa: BLE001
                summary["offers"] = {"status": "error", "error": str(e)}

        # Free money, once a day. Costs nothing on every other tick of the day.
        if mode != "sniper" and not ctx.out_of_time(margin=12):
            try:
                reward = claim_daily_reward(ctx)
            except Exception as e:               # noqa: BLE001
                reward = {"status": "error", "error": str(e)[:160]}
            if reward:
                summary["reward"] = reward

        # A sniper tick exists only to hit a close; it must not spend its seconds
        # on a market review.
        if "review" in summary:
            pass          # a forced run already did it, first
        elif mode != "sniper" and ctx.out_of_time(margin=15):
            # Say so. An absent `review` key and a review that found nothing
            # render identically, and the difference is the whole answer to
            # "why did nothing happen when I pressed the button".
            summary["review"] = {"status": "skipped",
                                 "reason": "no quedó tiempo en este tick"}
        elif mode != "sniper":
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
                               summary=_slim(summary))
        _note_health(store, ok=not failed, error=summary.get("error"))
        _check_token_expiry(store)
        _heal_scheduler_url(store)
        return summary
    except StorageUnavailable as e:
        # The database was busy, not wrong. On a free tier woken sixty times an
        # hour this is a minute that happens, and the next tick is a minute
        # away — so it is a note, not a red event, and it does not touch the
        # fail streak that puts a "your bot is broken" message on a phone.
        # Calling a slow minute a broken deployment is how a real one stops
        # being believed.
        net.clear_deadline()
        summary.update({"ok": True, "degraded": "storage",
                        "note": f"{type(e).__name__}: {e}",
                        "duration_seconds": round(time.monotonic() - started, 2)})
        try:
            store.finish_execution(execution_id, SKIPPED, summary=_slim(summary))
            events.emit("note", "La base tardó de más; lo reintento en el "
                                "próximo minuto",
                        detail={"detalle": str(e)[:200]}, status="plan")
        except Exception:
            pass
        return summary
    except Exception as e:                       # noqa: BLE001
        net.clear_deadline()
        summary.update({"ok": False, "error": f"{type(e).__name__}: {e}",
                        "traceback": traceback.format_exc()[-1500:],
                        "duration_seconds": round(time.monotonic() - started, 2)})
        try:
            store.finish_execution(execution_id, FAILED, summary=_slim(summary),
                                   error=summary["error"])
            events.emit("error", f"Falló la ejecución: {e}", status="error")
            _note_health(store, ok=False, error=summary["error"])
        except Exception:
            pass
        return summary


def _madrid_today(now=None):
    """The date in Madrid, which is when LaLiga's daily counters reset."""
    from .sources.matchday import SPAIN_TZ
    return (now or utcnow()).astimezone(SPAIN_TZ).date().isoformat()


# A reward that could not be claimed is asked about again after this long, not
# on every tick: a broken route answering sixty times an hour helps nobody.
REWARD_RETRY_SECONDS = 3600


def claim_daily_reward(ctx, now=None):
    """Claim LaLiga's daily reward once per Madrid day. None when nothing to do.

    Check, claim, stamp. The stamp short-circuits the rest of the day at zero
    requests, and a check that answers "already taken" (from the phone, say)
    stamps too, so the bot never asks twice.
    """
    if not (config.AUTO_DAILY_REWARD and config.AUTO_EXECUTE) or ctx.dry_run:
        return None
    store = get_storage()
    now = now or utcnow()
    today = _madrid_today(now)
    doc = store.get_doc("daily_reward", {}) or {}
    if doc.get("day") == today and doc.get("status") in ("claimed", "already"):
        return None
    failed = parse_iso(doc.get("failed_at"))
    if failed is not None and (now - failed).total_seconds() < REWARD_RETRY_SECONDS:
        return None
    client = ctx.get_client()
    lid, tid = league_ids(client)
    try:
        chk = client.check_daily_reward(lid, tid) or {}
    except Exception as e:                       # noqa: BLE001
        if "050.01.04" in str(e) or getattr(e, "status", None) == 400:
            store.put_doc("daily_reward", {"day": today, "status": "already"})
            return {"status": "already_claimed"}
        store.put_doc("daily_reward", {**doc, "failed_at": to_iso(now),
                                       "error": str(e)[:160]})
        return {"status": "error", "error": str(e)[:160]}
    if int(num(chk.get("dailyRewardsRedeemed"))) > 0:
        store.put_doc("daily_reward", {"day": today, "status": "already"})
        return {"status": "already_claimed"}
    before = _live_money(client, tid)
    try:
        client.claim_daily_reward(lid, tid)
    except Exception as e:                       # noqa: BLE001
        store.put_doc("daily_reward", {**doc, "failed_at": to_iso(now),
                                       "error": str(e)[:160]})
        return {"status": "error", "error": str(e)[:160]}
    after = _live_money(client, tid)
    gained = (after - before) if (before is not None and after is not None) else None
    store.put_doc("daily_reward", {"day": today, "status": "claimed",
                                   "amount": gained, "at": to_iso(now)})
    events.emit("note", "Cobré la recompensa diaria"
                        + (f": +{gained:,} €" if gained else ""),
                detail={"caja": f"{after:,} €" if after is not None else "—"})
    return {"status": "claimed", "amount": gained}


def _slim_offers(offers):
    """What a history needs from a tick's offer handling: who went, for what."""
    if not isinstance(offers, dict):
        return offers
    keep = ("nombre", "amount", "floor", "reserve", "value", "de_laliga",
            "comprador", "why", "in_xi")
    out = {k: offers[k] for k in ("status", "reason", "offers", "pressure",
                                  "money_before", "money_after",
                                  "new_listings", "error") if k in offers}
    for bucket in ("accepted", "declined", "held"):
        rows = offers.get(bucket) or []
        if rows:
            out[bucket] = [{k: r.get(k) for k in keep} for r in rows[:10]]
    read = offers.get("read")
    if isinstance(read, dict) and read.get("consultados"):
        out["read"] = {k: read.get(k) for k in ("consultados", "ofertas")}
    return out


# What is worth keeping in the executions table, per tick, forever.
#
# A tick runs sixty times an hour and each one writes a row here. The review's
# analysis -- the scored market, the ranked upgrades, the rival breakdown, the
# recorded payload shapes -- is hundreds of kilobytes, it already lives in
# `last_report`, and only the newest copy of it is ever worth anything. Stored
# on every row it made the dashboard's own `select *` over ten rows time out.
#
# So the row keeps what a HISTORY needs: did it work, how long, what did it do,
# what broke. The analysis is a snapshot, not a log.
_EXECUTION_KEEP = ("ok", "mode", "deploy", "duration_seconds", "error",
                   "traceback", "note", "degraded", "actions", "pending",
                   "next_deadline", "sleep_seconds", "clock", "source",
                   "reward")
_REVIEW_KEEP = ("status", "reason", "seconds", "think_seconds", "skipped",
                "money", "scheduled_bids")


def _slim(summary):
    """The execution row's summary: small, and the same shape every time."""
    out = {k: summary[k] for k in _EXECUTION_KEEP if k in summary}
    # The page's "Ofertas recibidas" reads the last execution's offers, and they
    # were never kept — the section could only ever be empty.
    if summary.get("offers") is not None:
        out["offers"] = _slim_offers(summary["offers"])
    review = summary.get("review")
    if isinstance(review, dict):
        kept = {k: review[k] for k in _REVIEW_KEEP if k in review}
        # The one analysis number a history actually wants: what it bought.
        bids = review.get("bids") or {}
        if isinstance(bids, dict):
            kept["scheduled_bids"] = len(bids.get("scheduled") or [])
        out["review"] = kept
    elif review is not None:
        out["review"] = review
    return out


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

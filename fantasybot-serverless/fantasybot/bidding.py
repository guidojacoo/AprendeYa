"""Last-minute bidding: bid right at the close, not before.

Idea: don't reveal your bid early. A cron job launches this ~5 min before market
close and decides the amount based on the competition, at two checkpoints:

  - If there are bids from others → bid a competitive amount (value + margin),
    without exceeding your cap (`max_bid`). Don't wait: competition forces a move.
  - If there are NO bids with ~15s left → bid value + a touch (cushion) and done.

The timing is deterministic: it spends no LLM tokens. The agent only picks which
players and with what cap; this runs the finish.

Two ways to run the finish, one decision function:

  `snipe()`      bounded. Polls until its budget runs out and then RETURNS,
                 saying whether it bid, or that it is still waiting. That is the
                 serverless shape: a Vercel function has ~60s to live, so it
                 holds the line for as long as it safely can and hands the watch
                 back to the next tick.
  `last_minute_bid()`  unbounded. The original CLI behaviour — sit on the market
                 until the close — now just `snipe()` with no budget.

Both ask `decide()` the same question, so the CLI and the cloud bid identically.
"""

import threading
import time
from datetime import datetime, timezone

from .api import FantasyClient
from . import config, events, state
from .matching import num

CONTESTED_MARGIN_PCT = 0.03   # how far above value to bid if there's competition
UNCONTESTED_CUSHION = 10      # minimum cushion if nobody else bids
DEFAULT_FINAL = config.BID_FINAL_SECONDS   # seconds before close for the finish
DEFAULT_POLL = 3             # how often, in seconds, to poll in the final minute


# How far above the richest rival's estimated cash we still allow ourselves to
# go. The estimate is derived from public transfer activity, not read off their
# account, so it can be low — this margin is the price of being wrong.
RIVAL_CASH_MARGIN = 0.10

# How far above the planned cap a bid may still climb when the player's LIVE
# value has risen past it between the plan and the close. LaLiga re-values
# players during the day, and the cap was sized off the value we read an hour
# ago: without this, a 2% drift turns a planned signing into a refused bid. It
# is bounded because the cap is also an affordability statement — a player who
# has run 10% away from us is a player we plan for again tomorrow, not one we
# chase with money earmarked for somebody else.
VALUE_DRIFT = 0.10


def cap_against_rivals(computed_cap, value, richest_rival_cash):
    """Lower a bid cap to what the competition could actually counter.

    Winning an auction by ten million when nobody in the league could have paid
    more than eight is ten million that does not buy the next player. The cap
    only ever comes DOWN — and never below what the listing itself requires, or
    LaLiga rejects the bid outright ("not a valid money quantity").

    `value` MUST be the price the listing actually requires, not a scraped
    estimate of what the player is worth. Passing the low one is how a cap ends
    up under the asking price: the rival ceiling wins the `min`, the floor is
    too low to pull it back, and the bid is sent to be refused with 030.01.01.

    An unknown or zero reach means we know nothing about the field, and the
    computed cap stands: guessing low there loses players for no reason. An
    unknown VALUE means the same thing about the floor — with no idea what the
    listing requires, there is nothing to stop the rival ceiling cutting the cap
    below it, so the cap stands there too. Capping a bid against a guess is how
    a signing turns into `"8754920" is not a valid money quantity for this
    player`, and a player we had the money for is lost to a cheaper number.
    """
    if not richest_rival_cash or richest_rival_cash <= 0:
        return computed_cap
    if not value:
        return computed_cap
    floor = value + UNCONTESTED_CUSHION
    ceiling = round(richest_rival_cash * (1 + RIVAL_CASH_MARGIN))
    return max(floor, min(computed_cap, ceiling))


def decide(value, other_bids, seconds_left, max_bid, final=DEFAULT_FINAL):
    """How much to bid NOW, or None to wait — never an amount LaLiga refuses.

    - other_bids > 0  → competition: value + margin, capped at max_bid.
    - no competition and <= final s left → value + minimum cushion.
    - otherwise, wait.

    The cap can only ever pull a bid down TO the player's value, never through
    it. Below it there is no legal bid at all: LaLiga answers a bid under the
    current value with 400 `"8754920" is not a valid money quantity for this
    player` (030.01.01), the listing closes, and the player is gone. So a cap
    under the value is not a cheaper bid, it is no bid — say so with None
    instead of sending a request that cannot be accepted.
    """
    if max_bid < value:
        return None
    if other_bids > 0:
        competitive = value + max(UNCONTESTED_CUSHION, round(value * CONTESTED_MARGIN_PCT))
        return max(value, min(max_bid, competitive))
    if seconds_left <= final:
        return max(value, min(max_bid, value + UNCONTESTED_CUSHION))
    return None


def _seconds_left(close_iso):
    # A malformed/missing close time must never crash the bid loop. Unknown -> treat as
    # "plenty of time left" (non-urgent), so decide() won't fire the final-window bid.
    try:
        close = datetime.fromisoformat(close_iso)
    except (TypeError, ValueError):
        return 3600.0
    if close.tzinfo is None:  # naive timestamp -> assume UTC to avoid a TypeError subtract
        close = close.replace(tzinfo=timezone.utc)
    return (close - datetime.now(timezone.utc)).total_seconds()


def _is_laliga_listing(row):
    """Whether LaLiga put this up for sale, rather than another manager.

    Not a single field: LaLiga ships market rows WITHOUT `discr` (flip.evaluate
    carries a comment about a KeyError that killed a whole review over exactly
    that), so reading one key and refusing everything else would block
    legitimate bids the day the field goes missing. The real tell is ownership —
    a rival's listing carries the team that owns him, with his clause on it.
    """
    discr = row.get("discr")
    if discr == "marketPlayerLeague":
        return True
    if discr == "marketPlayerTeam":
        return False
    return not (row.get("playerTeam") or row.get("sellerTeam"))


def _find(market, market_id):
    for e in market:
        if e.get("id") == market_id:
            return e
    return None


def _our_bid(row):
    """Whether THIS account already has money on the listing.

    The market row carries our own bid/offer back to us, which makes it the one
    piece of state a duplicate run cannot lie about: a tick that placed a bid and
    then died before recording it will see the bid here and stand down instead of
    bidding twice. Local files can be lost; LaLiga's view of the auction cannot.
    """
    return row.get("bid") or row.get("offer") or None


def _bid_amount(mine):
    """What our standing bid is worth, whatever LaLiga called the field."""
    if not isinstance(mine, dict):
        return num(mine)
    for key in ("money", "amount", "bid", "value"):
        got = num(mine.get(key))
        if got:
            return got
    return 0


def _rivals_on(row, mine):
    """How many bids on this listing are NOT ours.

    `numberOfBids` counts the whole auction, ours included. Reading it as the
    competition once we have bid is how a bot talks itself into outbidding
    itself: one bid on the listing, it is mine, and the "contested" price fires.
    """
    total = int(num(row.get("numberOfBids", 0)))
    return max(0, total - (1 if mine else 0))


def guard_bid(row, mine, ceiling, value=None):
    """How much to RAISE a standing bid to, or None to leave it alone.

    Bidding five minutes out buys room to retry a refused bid, and costs the
    sealed timing: rivals watch the bid count rise and still have five minutes
    to answer. This is what pays that back. We keep the watch after placing the
    bid, and when somebody else joins the auction we raise to the contested
    price — the same number `decide` would have chosen had we still been
    waiting — instead of standing down and losing to a later bid.

    Only ever upward, never past the ceiling, and never when we are already at
    or above the contested price. Nothing here re-enters an auction we are not
    already in: no standing bid means nothing to guard.
    """
    if not mine:
        return None
    rivals = _rivals_on(row, mine)
    if rivals <= 0:
        return None            # still the only bid; raising would bid against ourselves
    if value is None:
        value = max(num(row.get("salePrice")),
                    num((row.get("playerMaster") or {}).get("marketValue")))
    if not value:
        return None
    target = decide(value, rivals, 0, ceiling)
    if target is None:
        return None            # the ceiling is under the value: no legal raise exists
    current = _bid_amount(mine)
    return target if target > current else None


def _guard(fc, league_id, market_id, nombre, row, mine, roof, close_iso,
           dry_run, log):
    """Hold a bid we already placed: raise it if the auction turned contested.

    Returns `guarding` while the bid is still the right one, and asks to be
    called again until the listing closes — that watch is the whole point of
    bidding early. `raised` when it moved, which is the case the old
    stand-down lost outright.
    """
    left = _seconds_left(close_iso) if close_iso else 0
    target = guard_bid(row, mine, roof)
    if target is None:
        log(f"[bid] {nombre}: our bid stands "
            f"({_bid_amount(mine):,} of a {roof:,} ceiling, {int(left)}s left).")
        return {"status": "guarding", "market_id": market_id, "nombre": nombre,
                "bid": mine, "amount": _bid_amount(mine),
                "rivals": _rivals_on(row, mine), "seconds_left": int(left),
                "retry": left > 0}
    bid_id = mine.get("id") if isinstance(mine, dict) else None
    if not bid_id:
        # Without an id there is nothing to modify, and cancel-then-rebid would
        # drop us out of the auction for however long the second call takes.
        # Keeping the lower bid beats risking no bid at all.
        log(f"[bid] {nombre}: a rival joined but our bid carries no id; "
            f"leaving it at {_bid_amount(mine):,}.")
        return {"status": "guarding", "market_id": market_id, "nombre": nombre,
                "bid": mine, "amount": _bid_amount(mine),
                "why": "sin id de puja para modificarla", "retry": left > 0}
    if dry_run:
        log(f"[bid] {nombre}: WOULD RAISE to {target:,} ({int(left)}s left)")
        return {"status": "raised", "dry_run": True, "amount": target,
                "market_id": market_id, "nombre": nombre, "retry": left > 0}
    resp = fc.modify_bid(league_id, market_id, bid_id, target)
    log(f"[bid] {nombre}: RAISED {_bid_amount(mine):,} -> {target:,} "
        f"({int(left)}s left)")
    events.emit("bid", f"Subo la puja por {nombre}: {target:,} €",
                detail={"antes": f"{_bid_amount(mine):,} €",
                        "rivales": _rivals_on(row, mine),
                        "quedaban": f"{int(left)}s",
                        "why": "entró otro postor y mi puja se quedaba corta"})
    return {"status": "raised", "amount": target, "market_id": market_id,
            "nombre": nombre, "previous": _bid_amount(mine),
            "rivals": _rivals_on(row, mine), "bid_id": bid_id,
            "response": resp, "retry": left > 0}


def snipe(league_id, market_id, max_bid, value=None, final=DEFAULT_FINAL,
          poll=DEFAULT_POLL, dry_run=False, log=print, client=None,
          budget_seconds=None, last_call_seconds=0, ceiling=None):
    """Watch a listing and bid at the optimal moment, within a time budget.

    Returns a dict whose "status" is one of:
      bid       we placed it (amount / bid_id in the dict)
      gone      the listing is no longer in the market (closed or bought)
      not_ours  another manager's listing — his clause is the only route in
      closed    the close time passed without the conditions to bid
      unpriced  no usable value, so no bid could be sized
      over_cap  the live value is above what we may pay — no legal bid exists
      raised    we already had a bid, a rival joined, and we raised it
      guarding  we already have a bid and it is still the best move; watch on
      waiting   still too early AND the budget ran out; call again later
    `budget_seconds=None` means "no budget": poll until the close (CLI behaviour).

    `last_call_seconds` is how long the caller expects to wait before it can run
    again. When the budget runs out and the close is nearer than that, handing
    the watch back means nobody is left to bid — so it bids NOW, at the same
    price the final window would have produced. That costs the sniping edge
    (rivals see the bid count rise sooner) and it is not close: a bid placed
    forty seconds early beats a bid never placed. Two signings were lost to this.

    `ceiling` is the hard limit the bid may climb to when the player's live value
    has passed `max_bid` since the plan was made. It defaults to `max_bid` plus
    `VALUE_DRIFT`. Past it there is no legal bid to place, and this returns
    `over_cap` rather than sending one LaLiga will refuse.
    """
    fc = client or FantasyClient()
    started = time.monotonic()
    fixed_value = value
    el = _find(fc.market(league_id), market_id)
    if not el:
        log(f"[bid] marketId {market_id} is not in the market (already closed?).")
        return {"status": "gone", "market_id": market_id}
    # The last gate, at the point money moves. The planner filters and the queue
    # is swept, but a bid queued before either existed still arrives here — and
    # this is the only place that reads the listing as it is RIGHT NOW. A rival's
    # player is signed by paying his clause; his listing is not ours to bid on,
    # however it got into the queue.
    if not _is_laliga_listing(el):
        nombre = (el.get("playerMaster") or {}).get("nickname", market_id)
        log(f"[bid] {nombre}: another manager's listing. Not bidding; "
            f"he is reached by his clause.")
        return {"status": "not_ours", "market_id": market_id, "nombre": nombre,
                "why": "es de otro manager: se ficha por cláusula"}
    close_iso = el.get("expirationDate")
    nombre = el["playerMaster"].get("nickname", market_id)
    roof = ceiling if ceiling is not None else round(max_bid * (1 + VALUE_DRIFT))
    mine = _our_bid(el)
    if mine:
        # We are already in this auction. Standing down was right when the bid
        # went in at fifteen seconds — nobody could answer it. Bidding five
        # minutes out, they can, so this is where that is paid back.
        return _guard(fc, league_id, market_id, nombre, el, mine, roof,
                      close_iso, dry_run, log)
    if not close_iso:
        log(f"[bid] {nombre}: no close date; can't time it. Done.")
        return {"status": "closed", "market_id": market_id, "nombre": nombre}

    def _current_value(row):
        # Bid at LEAST the player's CURRENT value, RE-READ on every poll. A system auction
        # keeps its listing `salePrice` FROZEN, but the value is re-valued (daily / during
        # the day); if it climbs while we wait for the close, a bid sized off the value we
        # read MINUTES AGO is below the new value and LaLiga rejects it ("... is not a valid
        # money quantity for this player", 030.01.01). So recompute from the fresh row — the
        # HIGHER of salePrice/marketValue — never from a stale first read.
        if fixed_value is not None:
            return fixed_value
        # Coerced: LaLiga sends these as strings on some endpoints, and
        # max("2683751", 0) raises rather than comparing. A TypeError here is a
        # bid that never leaves.
        sale = num(row.get("salePrice"))
        mval = num((row.get("playerMaster") or {}).get("marketValue"))
        return max(sale, mval) or None

    def _spent():
        return time.monotonic() - started

    while True:
        el = _find(fc.market(league_id), market_id)
        if not el:
            log(f"[bid] {nombre}: no longer in the market. Done.")
            return {"status": "gone", "market_id": market_id, "nombre": nombre}
        mine = _our_bid(el)
        if mine:
            # A bid appeared while we were polling — another tick placed it, or
            # a send we thought had failed actually landed. Same situation as
            # finding one on entry, so the same answer: guard it, don't walk
            # away from an auction we are now in.
            return _guard(fc, league_id, market_id, nombre, el, mine, roof,
                          close_iso, dry_run, log)
        value = _current_value(el)
        if not value:  # no usable price -> can't size a bid (and would crash the f-string)
            log(f"[bid] {nombre}: no market value; can't price a bid.")
            return {"status": "unpriced", "market_id": market_id, "nombre": nombre}
        # The live value is the legal minimum, and it moves. When it has moved
        # past our cap, lift the cap to meet it — but only as far as the ceiling,
        # and say plainly when the player has priced himself out. Both outcomes
        # used to look the same from outside: a bid that never appeared.
        cap = max_bid
        if value > cap:
            room = ceiling if ceiling is not None else round(max_bid * (1 + VALUE_DRIFT))
            needed = value + UNCONTESTED_CUSHION
            if needed <= room:
                log(f"[bid] {nombre}: value rose to {value:,} (cap was "
                    f"{max_bid:,}); lifting to {needed:,}.")
                cap = needed
            else:
                log(f"[bid] {nombre}: value {value:,} is above the ceiling "
                    f"{room:,}. No legal bid; standing down.")
                events.emit("bid", f"Sin puja por {nombre}: vale {value:,} € y mi "
                                   f"techo era {room:,} €",
                            detail={"valor": value, "tope": max_bid,
                                    "techo": room,
                                    "why": "LaLiga rechaza cualquier puja por "
                                           "debajo del valor actual"},
                            status="skip")
                return {"status": "over_cap", "market_id": market_id,
                        "nombre": nombre, "value": value, "max_bid": max_bid,
                        "ceiling": room}
        left = _seconds_left(close_iso)
        other_bids = el.get("numberOfBids", 0)
        amount = decide(value, other_bids, left, cap, final)
        if amount is not None:
            if dry_run:
                log(f"[bid] {nombre}: WOULD BID {amount:,} "
                    f"(other_bids={other_bids}, {int(left)}s left)")
                return {"status": "bid", "dry_run": True, "amount": amount,
                        "market_id": market_id, "nombre": nombre,
                        "other_bids": other_bids}
            resp = fc.make_bid(league_id, market_id, amount)
            log(f"[bid] {nombre}: BID {amount:,} placed "
                f"(value {value:,}, other_bids={other_bids}, {int(left)}s left)")
            events.emit("bid", f"Puja al cierre: {amount:,} € por {nombre}",
                        detail={"pujas rivales": other_bids, "quedaban": f"{int(left)}s"})
            return {"status": "bid", "amount": amount, "market_id": market_id,
                    "nombre": nombre, "other_bids": other_bids,
                    "bid_id": resp.get("id") if isinstance(resp, dict) else None,
                    "response": resp}
        if left <= 0:
            log(f"[bid] {nombre}: market closed without bidding.")
            return {"status": "closed", "market_id": market_id, "nombre": nombre}
        # adaptive polling: long wait far from close, short in the final minute
        if left > 60:
            wait = min(30, left - 60)
        else:
            wait = min(poll, max(1, left - final))
        if budget_seconds is not None and _spent() + wait >= budget_seconds:
            if left <= last_call_seconds:
                # No later call arrives before the close, so the watch cannot be
                # handed back. Bid at the price the final window would have set.
                amount = decide(value, other_bids, 0, cap, final)
                if amount is None:
                    return {"status": "closed", "market_id": market_id,
                            "nombre": nombre}
                if dry_run:
                    log(f"[bid] {nombre}: WOULD BID {amount:,} as last call "
                        f"({int(left)}s left)")
                    return {"status": "bid", "dry_run": True, "amount": amount,
                            "market_id": market_id, "nombre": nombre,
                            "other_bids": other_bids, "last_call": True}
                resp = fc.make_bid(league_id, market_id, amount)
                log(f"[bid] {nombre}: BID {amount:,} placed as LAST CALL "
                    f"({int(left)}s left, no later tick before the close)")
                events.emit("bid", f"Puja de último recurso: {amount:,} € por {nombre}",
                            detail={"pujas rivales": other_bids,
                                    "quedaban": f"{int(left)}s",
                                    "why": "no quedaba otra ejecución antes "
                                           "del cierre"})
                return {"status": "bid", "amount": amount, "last_call": True,
                        "market_id": market_id, "nombre": nombre,
                        "other_bids": other_bids,
                        "bid_id": resp.get("id") if isinstance(resp, dict) else None,
                        "response": resp}
            # Out of time before anything is due. Nothing was sent, so handing the
            # watch back to the next tick is always safe.
            return {"status": "waiting", "market_id": market_id, "nombre": nombre,
                    "seconds_left": int(left), "close_at": close_iso}
        time.sleep(wait)


def last_minute_bid(league_id, market_id, max_bid, value=None, final=DEFAULT_FINAL,
                    poll=DEFAULT_POLL, dry_run=False, log=print):
    """Watches until close and places the bid at the optimal moment.

    The original, unbounded form (CLI / `bid-now`). Returns the API response when a
    bid was placed and None otherwise, exactly as before.
    """
    res = snipe(league_id, market_id, max_bid, value=value, final=final, poll=poll,
                dry_run=dry_run, log=log, budget_seconds=None)
    if res.get("status") != "bid":
        return None
    return {"dry_run": True, "amount": res["amount"],
            "other_bids": res["other_bids"]} if res.get("dry_run") else res.get("response")


def run_bid_plan(league_id, dry_run=False, log=print):
    """Runs the saved bid plan: one thread per target (simultaneous closes)."""
    plan = state.load_bid_plan()
    if not plan:
        return  # nothing to do; silent for the cron job
    log(f"[bid] running plan: {len(plan)} targets")
    threads = []
    for t in plan:
        th = threading.Thread(target=last_minute_bid, kwargs={
            "league_id": league_id, "market_id": t["market_id"],
            "max_bid": t["max_bid"], "dry_run": dry_run, "log": log})
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    if not dry_run:
        state.clear_bid_plan()  # plan consumed

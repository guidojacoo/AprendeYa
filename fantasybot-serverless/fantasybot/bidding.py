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
from . import events, state
from .matching import num

CONTESTED_MARGIN_PCT = 0.03   # how far above value to bid if there's competition
UNCONTESTED_CUSHION = 10      # minimum cushion if nobody else bids
DEFAULT_FINAL = 15           # seconds before close for the finish
DEFAULT_POLL = 3             # how often, in seconds, to poll in the final minute


# How far above the richest rival's estimated cash we still allow ourselves to
# go. The estimate is derived from public transfer activity, not read off their
# account, so it can be low — this margin is the price of being wrong.
RIVAL_CASH_MARGIN = 0.10


def cap_against_rivals(computed_cap, value, richest_rival_cash):
    """Lower a bid cap to what the competition could actually counter.

    Winning an auction by ten million when nobody in the league could have paid
    more than eight is ten million that does not buy the next player. The cap
    only ever comes DOWN — and never below what the listing itself requires, or
    LaLiga rejects the bid outright ("not a valid money quantity").

    An unknown or zero reach means we know nothing about the field, and the
    computed cap stands: guessing low there loses players for no reason.
    """
    if not richest_rival_cash or richest_rival_cash <= 0:
        return computed_cap
    floor = (value or 0) + UNCONTESTED_CUSHION
    ceiling = round(richest_rival_cash * (1 + RIVAL_CASH_MARGIN))
    return max(floor, min(computed_cap, ceiling))


def decide(value, other_bids, seconds_left, max_bid, final=DEFAULT_FINAL):
    """How much to bid NOW, or None to wait.

    - other_bids > 0  → competition: value + margin, capped at max_bid.
    - no competition and <= final s left → value + minimum cushion.
    - otherwise, wait.
    """
    if other_bids > 0:
        competitive = value + max(UNCONTESTED_CUSHION, round(value * CONTESTED_MARGIN_PCT))
        return min(max_bid, competitive)
    if seconds_left <= final:
        return min(max_bid, value + UNCONTESTED_CUSHION)
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


def snipe(league_id, market_id, max_bid, value=None, final=DEFAULT_FINAL,
          poll=DEFAULT_POLL, dry_run=False, log=print, client=None,
          budget_seconds=None, last_call_seconds=0):
    """Watch a listing and bid at the optimal moment, within a time budget.

    Returns a dict whose "status" is one of:
      bid       we placed it (amount / bid_id in the dict)
      already   we already had a bid on this listing — nothing to do
      gone      the listing is no longer in the market (closed or bought)
      closed    the close time passed without the conditions to bid
      unpriced  no usable value, so no bid could be sized
      waiting   still too early AND the budget ran out; call again later
    `budget_seconds=None` means "no budget": poll until the close (CLI behaviour).

    `last_call_seconds` is how long the caller expects to wait before it can run
    again. When the budget runs out and the close is nearer than that, handing
    the watch back means nobody is left to bid — so it bids NOW, at the same
    price the final window would have produced. That costs the sniping edge
    (rivals see the bid count rise sooner) and it is not close: a bid placed
    forty seconds early beats a bid never placed. Two signings were lost to this.
    """
    fc = client or FantasyClient()
    started = time.monotonic()
    fixed_value = value
    el = _find(fc.market(league_id), market_id)
    if not el:
        log(f"[bid] marketId {market_id} is not in the market (already closed?).")
        return {"status": "gone", "market_id": market_id}
    if _our_bid(el):
        log(f"[bid] {market_id}: we already have a bid on this listing. Standing down.")
        return {"status": "already", "market_id": market_id, "bid": _our_bid(el)}
    close_iso = el.get("expirationDate")
    nombre = el["playerMaster"].get("nickname", market_id)
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
        if _our_bid(el):
            return {"status": "already", "market_id": market_id, "nombre": nombre,
                    "bid": _our_bid(el)}
        value = _current_value(el)
        if not value:  # no usable price -> can't size a bid (and would crash the f-string)
            log(f"[bid] {nombre}: no market value; can't price a bid.")
            return {"status": "unpriced", "market_id": market_id, "nombre": nombre}
        left = _seconds_left(close_iso)
        other_bids = el.get("numberOfBids", 0)
        amount = decide(value, other_bids, left, max_bid, final)
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
                amount = decide(value, other_bids, 0, max_bid, final)
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

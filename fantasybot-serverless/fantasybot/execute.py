"""EXECUTION layer: turns decisions into real actions.

Autonomy authorized by the user:
  - Lineup: applies the best lineup (reversible, no spending).
  - Bid/cancel in market: places bids on profitable flips and pulls those that no
    longer apply (reversible until market close). May use the whole balance.
  - Buyouts: NOT automatic (irreversible spending) → left as an alert/task.

Everything runs through `dry_run`: if True, it only returns the PLAN without
touching anything.
"""

from . import events, state
from .strategy import flip
from .strategy.lineup import payload_ids

# Position name (as used in `best["missing"]`) → Spanish abbreviation for the copy.
_POS_ABBREV = {"goalkeeper": "POR", "defender": "DEF", "midfield": "MED", "striker": "DEL"}


def _norm_id(x):
    """Compare ids across int/str/None uniformly (the captain is a string in the payload,
    but the current lineup may hand it back as an int, or None when unset)."""
    return None if x is None else str(x)


def describe_missing(missing):
    """Format a `best["missing"]` dict ({"midfield": 1, ...}) as Spanish slot text.

    e.g. {"midfield": 1} -> "1 MED"; {"defender": 2, "midfield": 1} -> "2 DEF, 1 MED".
    Ordered POR/DEF/MED/DEL so the phrasing is stable regardless of dict order.
    """
    order = ["goalkeeper", "defender", "midfield", "striker"]
    parts = [f"{missing[pos]} {_POS_ABBREV[pos]}" for pos in order if missing.get(pos)]
    return ", ".join(parts)


def apply_lineup(client, team_id, best, current_ids, dry_run=True,
                 current_coach=None, current_captain=None):
    """Applies the optimal lineup if it differs from the current one.

    Fielding the best available XI beats leaving a worse one, so a partial (incomplete)
    lineup is still applied — but when `best["incomplete"]`, the emitted event flags it as
    a problem (which line is short) instead of reporting a clean "lineup applied", so the
    user is told to sign a player for the empty slot.

    PREMIUM: the payload may also carry a `coach`/`captain` (see lineup.optimize). A captain
    (or coach) change with the SAME XI must still count as "changed", or we would never PUT
    the new captain. So the comparison also checks coach/captain against the current lineup's
    (`current_coach`/`current_captain`, from agent._current_lineup) — but ONLY when the payload
    actually carries that field (non-premium payloads have neither, so this is a no-op there
    and behaviour stays byte-identical).
    """
    new_ids = payload_ids(best)
    incomplete = bool(best.get("incomplete"))
    missing = best.get("missing") or {}
    payload = best["payload"]
    # Premium extras only participate when present in the payload (gated on premium upstream).
    coach_same = ("coach" not in payload) or (
        _norm_id(payload.get("coach")) == _norm_id(current_coach))
    captain_same = ("captain" not in payload) or (
        _norm_id(payload.get("captain")) == _norm_id(current_captain))
    if new_ids == current_ids and coach_same and captain_same:
        return {"action": "lineup", "changed": False,
                "incomplete": incomplete, "missing": missing}
    has_extras = any(k in payload for k in ("coach", "captain", "bench"))
    premium_stripped = False
    if not dry_run:
        # PREMIUM extras (coach/captain/bench) use a to-be-live-confirmed PUT format. If the
        # API rejects it, a working XI still beats a rejected PUT — retry WITHOUT the extras.
        # A non-premium payload (no extras) has nothing to strip, so its failure re-raises as
        # before (a real error we must not swallow).
        try:
            client.update_lineup(team_id, payload)
        except Exception:
            base = {k: v for k, v in payload.items()
                    if k not in ("coach", "captain", "bench")}
            if base != payload:
                client.update_lineup(team_id, base)   # premium extras dropped, XI still set
                premium_stripped = True               # ...so we must NOT claim we set them
            else:
                raise
        d, m, f = best["formation"]
        if incomplete:
            desc = describe_missing(missing)
            title = f"⚠️ Alineación INCOMPLETA {d}-{m}-{f}: falta(n) {desc} sin cubrir"
        else:
            title = f"Lineup {d}-{m}-{f} applied"
        events.emit("lineup", title, detail={"score": best.get("total")})
    # premium_applied tells the caller whether the coach/captain ACTUALLY made it to LaLiga,
    # so it never reports a captain/coach it silently had to drop (honest paid-user panels).
    return {"action": "lineup", "changed": True, "applied": not dry_run,
            "formation": best["formation"], "incomplete": incomplete, "missing": missing,
            "premium_applied": has_extras and not premium_stripped}


def _system_flips(client, league_id):
    """Profitable SYSTEM flips (a single pass over market + trends)."""
    return [o for o in flip.opportunities(client, league_id)
            if o["via"] == "SISTEMA" and o["margin_pct"] > 0]


def plan_bids(client, league_id, team, ops=None):
    """What to bid on: profitable SYSTEM flips that fit the balance, by margin.

    SYSTEM only (auction). Buyouts are outside the scope of autonomy.
    """
    money = team["teamMoney"]
    if ops is None:
        ops = _system_flips(client, league_id)
    # The live market is the truth about what already has money on it, not the local
    # file. The file only remembers THIS bot's bids: anything placed from the app or
    # by hand is invisible to it, and the bot re-proposes players that already carry
    # a bid. Reading `bid`/`offer` off each listing catches them all.
    already = set(state.load_bids())
    try:
        for el in client.market(league_id):
            if el.get("bid") or el.get("offer"):
                already.add(str(el.get("id")))
    except Exception:
        pass          # without the market, the local file is still better than nothing
    plan, committed = [], 0
    for o in ops:
        if str(o["market_id"]) in already:
            continue  # money is already on that player
        if committed + o["buy_price"] > money:
            continue  # doesn't fit in the balance
        plan.append({"market_id": o["market_id"], "nombre": o["nombre"],
                     "amount": o["buy_price"], "margin_pct": o["margin_pct"]})
        committed += o["buy_price"]
    return plan


def sync_bids(client, league_id, team, dry_run=True):
    """Places new bids from the plan and cancels those that no longer apply."""
    ops = _system_flips(client, league_id)   # a single pass, reused below
    plan = plan_bids(client, league_id, team, ops)
    bids = state.load_bids()
    valid_ids = {o["market_id"] for o in ops}

    placed, cancelled = [], []
    # cancel bids whose target is no longer profitable
    for mid, info in list(bids.items()):
        if mid not in valid_ids:
            if not dry_run:
                try:
                    client.cancel_bid(league_id, mid, info["bid_id"])
                except Exception:
                    pass
                bids.pop(mid, None)
                events.emit("cancel", f"Bid cancelled: {info.get('nombre', mid)}",
                            detail="no longer profitable")
            cancelled.append(info.get("nombre", mid))
    # place new bids
    for b in plan:
        if not dry_run:
            resp = client.make_bid(league_id, b["market_id"], b["amount"])
            bid_id = resp.get("id") if isinstance(resp, dict) else None
            bids[b["market_id"]] = {"bid_id": bid_id, "amount": b["amount"],
                                    "nombre": b["nombre"]}
            events.emit("bid", f"Bid {b['amount']:,} for {b['nombre']}",
                        detail={"margin": f"{b['margin_pct']}%"})
        placed.append(b)

    if not dry_run:
        state.save_bids(bids)
    return {"action": "bids", "placed": placed, "cancelled": cancelled,
            "applied": not dry_run}


def act(client, league_id, team_id, team, best, current_ids, dry_run=True,
        current_coach=None, current_captain=None):
    """Executes (or plans) the autonomous actions: set lineup + bid.

    `current_coach`/`current_captain` (premium) let apply_lineup detect a captain/coach
    change that leaves the XI unchanged; None (non-premium/default) keeps today's behaviour.
    """
    return {
        "lineup": apply_lineup(client, team_id, best, current_ids, dry_run,
                               current_coach=current_coach, current_captain=current_captain),
        "bids": sync_bids(client, league_id, team, dry_run),
    }

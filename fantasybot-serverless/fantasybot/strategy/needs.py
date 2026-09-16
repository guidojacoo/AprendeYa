"""Squad needs + signings advisor.

Detects positions where you're short (e.g. a single goalkeeper) and looks for
candidates in the market to sign, prioritizing those who will start for their team.

Includes URGENCY logic: the fewer days left until the matchday, the more willing
to pay a little above the ideal price (if you wait, another one may not show up in
time). With several days ahead, there's no rush.
"""

from ..matching import match_name, num, POS, position_id, position_of
from ..sources.lineups import probable_lineups
from . import points as points_mod

# Recommended minimum per position (1 starter + rotation/injury margin).
MIN_SQUAD = {"POR": 2, "DEF": 5, "MED": 5, "DEL": 3}


def xi_minimum(premium=False):
    """The fewest players per line that can still fill a LEGAL formation.

    Different question from MIN_SQUAD, and the difference decides money. Below
    this the eleven cannot be completed at all and the empty slot scores nothing
    every single week, so it is worth almost any price. Between this and
    MIN_SQUAD you have a starter and no cover — worth buying only if the cover
    pays for itself, which is a question the upgrade engine answers with actual
    numbers instead of a rule of thumb.

    Derived from the formations LaLiga accepts rather than written down, so it
    cannot drift away from them.
    """
    from .lineup import FORMATIONS, PREMIUM_FORMATIONS

    shapes = list(FORMATIONS) + (list(PREMIUM_FORMATIONS) if premium else [])
    return {"POR": 1,
            "DEF": min(d for d, _m, _f in shapes),
            "MED": min(m for _d, m, _f in shapes),
            "DEL": min(f for _d, _m, f in shapes)}


def blocking_gaps(team, premium=False):
    """Positions so short that no legal XI can be fielded. These are urgent."""
    counts = squad_counts(team)
    floor = xi_minimum(premium)
    return {pos: floor[pos] - counts[pos]
            for pos in floor if counts[pos] < floor[pos]}


def squad_counts(team):
    counts = {"POR": 0, "DEF": 0, "MED": 0, "DEL": 0}
    for p in team["players"]:
        pos = position_of(p["playerMaster"])
        if pos in counts:   # only the 4 outfield lines; a coach ("ENT", positionId 5) is skipped
            counts[pos] += 1
    return counts


def gaps(team):
    """Positions below the recommended minimum, with how many are missing."""
    counts = squad_counts(team)
    return {pos: MIN_SQUAD[pos] - counts[pos]
            for pos in counts if counts[pos] < MIN_SQUAD[pos]}


def urgency_multiplier(days_to_matchday):
    """How much extra over the ideal price we accept based on how close the matchday is.

    Far (>=5 days): 1.0 (no rush). Close: up to ~1.15 on matchday.
    None = unknown → 1.0 (cautious).
    """
    if days_to_matchday is None:
        return 1.0
    if days_to_matchday >= 5:
        return 1.0
    # from 5 days (1.0) to 0 days (1.15), linear
    return round(1.0 + (5 - max(0, days_to_matchday)) * 0.03, 3)


def candidates(client, league_id, position, prob_index=None, money=None, owned=None):
    """Market candidates in a position, sorted by starting probability.

    Returns dicts with buy price, route (system/buyout) and starting prob.
    Excludes players you already own (`owned` = set of playerMaster.id).
    """
    if prob_index is None:
        prob_index = probable_lineups()
    owned = owned or set()
    pos_id = {v: k for k, v in POS.items()}[position]

    out = []
    for el in client.market(league_id):
        pm = el["playerMaster"]
        if position_id(pm) != pos_id:
            continue
        if pm.get("id") in owned:
            continue  # already yours
        clause = sale = None
        if el.get("discr") == "marketPlayerLeague":
            via, price = "SISTEMA", (num(el.get("salePrice"))
                                     or num(pm.get("marketValue")))
        else:
            clause = num(el.get("playerTeam", {}).get("buyoutClause")) or None
            # A player another manager has listed could be bid for at his sale
            # price, which is usually cheaper than his ~1.67x clause. That route
            # is deliberately not taken: a rival's player is signed by paying his
            # clause, and nothing else. `sale` stays as context for the page.
            sale = (num(el.get("salePrice")) or None) \
                if el.get("status") == "on_sale" else None
            via, price = "CLAUSULA", clause
        if not price:
            continue
        info = match_name(pm.get("nickname", ""), pm.get("name", ""), prob_index)
        prob = info.get("prob") if info else None
        disponible = pm.get("playerStatus", "ok") == "ok"
        if info and (info.get("lesionado") or not info.get("disponible", True)):
            disponible = False
        out.append({
            "nombre": pm.get("nickname") or pm.get("name"),
            "market_id": el["id"],
            "player_id": pm.get("id"),
            "via": via,
            "price": price,
            "prob": prob,
            "disponible": disponible,
            "valor": num(pm.get("marketValue")),
            # What he would actually add to the XI. Ranking gap signings by
            # market value picked the most expensive man available, which is a
            # different question from the one being asked.
            "expected_points": points_mod.expected(pm, prob),
            "affordable": (money is None or price <= money),
            # both routes, so the caller can see what the alternative would have cost
            "clause": clause,
            "sale_price": sale,
            "expires": el.get("expirationDate"),
        })
    # Available first, then by the points he is expected to add — not by what he
    # costs. Value survives only as the last tiebreak, for the players LaLiga
    # gives no probability for at all.
    out.sort(key=lambda c: (c["disponible"], c["expected_points"] or 0,
                            c["prob"] or 0, c["valor"] or 0),
             reverse=True)
    return out


def advise(client, league_id, team, days_to_matchday=None):
    """Report: squad gaps and the best candidates to fill them."""
    prob_index = probable_lineups()
    mult = urgency_multiplier(days_to_matchday)
    money = team["teamMoney"]
    owned = {p["playerMaster"].get("id") for p in team["players"]}
    report = {"gaps": gaps(team), "urgency_multiplier": mult, "suggestions": {}}
    for pos in report["gaps"]:
        cands = candidates(client, league_id, pos, prob_index, money, owned)
        for c in cands:
            # recommended cap: the price, raised by urgency when there is bidding
            # involved. A clause is a fixed amount — urgency cannot change it.
            c["max_bid"] = (round(c["price"] * mult)
                            if c["via"] in ("SISTEMA", "PUJA") else c["price"])
        report["suggestions"][pos] = cands
    return report

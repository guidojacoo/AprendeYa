"""Putting your best players out of reach, permanently.

The shield is free and lasts 48 hours. Raising a clause costs money and lasts
until the player's value moves. They are not alternatives — the shield is a
patch over one weekend, and this is the thing that stops the problem recurring
every week for the rest of the season.

This is the move that decides leagues. A rival does not need to outplay you to
beat you; he needs to pay the clause on your best forward once, and you lose the
points AND he gains them, which is a two-sided swing on the only scoreboard that
matters. Any manager paying attention raises the clauses on his stars in the
first week. A bot that never does it is playing a different, easier game than
everyone else at the table.

WHO to protect is not "the most valuable". It is the one whose loss costs the
most POINTS PER WEEK and whose clause a rival can actually afford — a 12M
defender nobody can reach is safe, and a 4M starter with a 5M clause in a league
holding 8M is the one who disappears on Friday.

HOW MUCH the raise costs is the one thing the API will not tell us in advance,
so this does not guess it. It probes: the first raise is a deliberately small
one on the cheapest exposed player, the money before and after is recorded, and
every later decision is priced with the ratio that measurement produced. Until
then it assumes the most expensive plausible rule — that a raise costs the full
increase — so a wrong guess makes it too cautious rather than broke.
"""

from ..matching import num, position_of

# How far above the richest rival's reach a clause has to land to count as safe.
# Their cash moves: they sell a player on Saturday and the clause that was out of
# reach on Friday is not. The margin is what stops us paying twice for the same
# protection.
SAFETY_MARGIN = 0.35

# Points per gameweek a player must be worth to the XI before spending real money
# defending him. Below it, losing him costs about what a replacement costs.
MIN_POINTS_AT_RISK = 0.8

# The assumed cost of raising a clause by 1 EUR, before we have measured the real
# one. Deliberately the most expensive plausible rule.
ASSUMED_COST_RATIO = 1.0

# What the probe raise is allowed to cost, worst case. Small enough to be worth
# paying for the answer, large enough that the money delta is unambiguous.
PROBE_BUDGET = 100_000

# Never spend more than this share of free cash on defence in one pass. Defending
# a squad you can no longer improve is how you finish fourth with everyone intact.
MAX_CASH_SHARE = 0.30


def cost_of(current_clause, target_clause, cost_ratio=None):
    """What raising this clause should cost us, at the ratio we believe."""
    ratio = ASSUMED_COST_RATIO if cost_ratio is None else float(cost_ratio)
    return max(0, int(round((target_clause - current_clause) * ratio)))


def target_for(clause, value, rivals_reach):
    """The clause that puts a player out of reach, or None if he already is.

    Out of reach is measured against the richest rival plus a margin, never
    against the player's value: what stops a buyout is the price of the buyout,
    and a rival with 20M does not care that our striker is "only" worth 6M.
    """
    reach = int(num(rivals_reach))
    if reach <= 0:
        return None                       # no idea what the field holds: don't spend
    safe = int(round(reach * (1 + SAFETY_MARGIN)))
    clause = int(num(clause))
    if clause >= safe:
        return None                       # already beyond them
    # Never propose a clause below the player's own value — LaLiga would not
    # accept it, and it would not protect him from anyone either.
    return max(safe, int(num(value)))


def exposed(team, rivals_reach, points_at_risk, now=None):
    """Our players a rival could take right now, worst loss first.

    `points_at_risk` maps playerTeamId to what the XI loses per gameweek without
    him — the same measure the upgrade engine uses to price a signing, so buying
    and defending are finally denominated in the same currency.
    """
    out = []
    for p in team.get("players") or []:
        pm = p.get("playerMaster") or {}
        clause = num(p.get("buyoutClause"))
        if not clause:
            continue                      # no clause: nothing to raise
        value = num(pm.get("marketValue"))
        ptid = str(p.get("playerTeamId") or pm.get("id") or "")
        at_risk = float(points_at_risk.get(ptid, 0) or 0)
        target = target_for(clause, value, rivals_reach)
        if target is None:
            continue                      # already out of reach
        out.append({
            "nombre": pm.get("nickname") or pm.get("name"),
            "player_id": pm.get("id"),
            "player_team_id": ptid,
            "pos": position_of(pm, "?"),
            "value": int(value),
            "clause": int(clause),
            "target": int(target),
            "points_at_risk": round(at_risk, 2),
            "shielded": bool(p.get("isShielded")),
        })
    out.sort(key=lambda r: (-r["points_at_risk"], -r["value"]))
    return out


def probe(rows, money, cost_ratio=None):
    """A small raise to learn the real price with, or None.

    The probe exists to MEASURE, not to protect, so it does not raise anyone to
    his safe target — that costs millions and the whole point is to find out
    what a million buys before spending it. It nudges one clause by the probe
    budget and reads the bill.

    Which player barely matters, so it picks the one it hurts least to be wrong
    about: the most exposed one, who was getting a raise anyway.
    """
    if cost_ratio is not None:
        return None
    budget = min(PROBE_BUDGET, int(num(money)))
    if budget <= 0 or not rows:
        return None
    pick = rows[0]
    # At the pessimistic ratio a raise of `budget` costs at most `budget`, so
    # this cannot overspend whatever the real rule turns out to be.
    target = min(pick["target"], pick["clause"] + budget)
    if target <= pick["clause"]:
        return None
    return {**pick, "target": int(target),
            "cost": cost_of(pick["clause"], target),
            "why": "subida pequeña a propósito: primero mido cuánto cuesta "
                   "subir una cláusula, después defiendo en serio"}


def plan(team, rivals_reach, money, points_at_risk, cost_ratio=None,
         reserve=0, max_raises=2):
    """Which clauses to raise now, and what each should cost.

    Greedy by points at risk: the player it hurts most to lose is defended
    first, because the budget runs out before the squad does.
    """
    rows = exposed(team, rivals_reach, points_at_risk)
    free = max(0, int(num(money)) - int(num(reserve)))
    budget = int(free * MAX_CASH_SHARE)
    if cost_ratio is None:
        # Nothing is priced yet. Buy the price first, act on it next pass.
        one = probe(rows, budget)
        return {"mode": "probe", "raises": [one] if one else [],
                "exposed": rows, "budget": budget,
                "why": "todavía no sé cuánto cuesta subir una cláusula"}
    picked, spent = [], 0
    for r in rows:
        if len(picked) >= max_raises:
            break
        if r["points_at_risk"] < MIN_POINTS_AT_RISK:
            continue
        cost = cost_of(r["clause"], r["target"], cost_ratio)
        if not cost or spent + cost > budget:
            continue
        picked.append({**r, "cost": cost,
                       "why": f"pierdo {r['points_at_risk']} pts/jornada si me lo "
                              f"clausulan, y su cláusula ({r['clause']:,} €) está "
                              f"al alcance de la liga"})
        spent += cost
    return {"mode": "on", "raises": picked, "exposed": rows,
            "budget": budget, "spent": spent}


def measure_ratio(before_money, after_money, before_clause, after_clause):
    """What the raise actually cost, per euro of clause. None when unreadable.

    The money delta is the only honest source: the endpoint returns the new
    clause, not the bill. A raise that appears free, or one that appears to have
    cost more than it raised, is refused rather than banked — a wrong ratio here
    silently mis-prices every later decision.
    """
    paid = num(before_money) - num(after_money)
    raised = num(after_clause) - num(before_clause)
    if raised <= 0 or paid <= 0:
        return None
    ratio = paid / raised
    if not (0.01 <= ratio <= 2.0):
        return None
    return round(ratio, 4)

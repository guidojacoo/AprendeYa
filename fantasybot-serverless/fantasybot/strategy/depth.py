"""Eleven starters beat three stars.

Fantasy scores an ELEVEN. Every slot pays the same, so a squad with three
brilliant players and eight who do not start is a squad fielding eight zeros —
and it loses to eleven solid ones bought with the same money. That is a
different question from the one the upgrade engine answers, and nothing was
asking it:

  * `transfers` pairs ONE sale with ONE buy. It could never sell a twenty-
    million forward to buy two ten-million starters, which is the move.
  * `best_plan` picks several signings and ADDS UP gains that were each computed
    against the current squad, one at a time. Two players who would fill the
    same weak slot are counted as if they filled two — so the arithmetic that
    should favour breadth quietly favoured stacking.

Both are fixed by measuring the RESULTING ELEVEN instead of adding marginal
gains. A combination is worth what the team is worth after it, minus what the
team is worth now, minus what leaving costs the players sold. There is no other
honest way to compare "one star" against "three starters".

The search is deliberately small. This runs inside a function Vercel kills at
sixty seconds, and each evaluation re-solves the lineup.
"""

from itertools import combinations

from .. import modes
from ..matching import num
from .upgrades import SALE_HAIRCUT, _as_squad_member, squad_points

# How wide to search. Small on purpose: the candidates are already ranked by
# points per euro, so the good combinations live near the top — and every
# evaluation re-solves the lineup, inside a function Vercel kills at sixty
# seconds. Seven buys by three, against five sells by two, is a thousand solves
# and about twenty seconds. These numbers are the budget, not a preference.
TOP_BUYS = 6
TOP_SELLS = 4
MAX_BUYS = 3
MAX_SELLS = 1

# A hard ceiling on the search, whatever the numbers above allow. Reached only
# when the pruning below fails to bite, and better than being killed mid-write.
MAX_EVALUATIONS = 90

# A slot is a LEAK when the man in it is expected to score about nothing. This
# is the number the whole idea turns on: filling a leak is worth several times
# what upgrading a working slot is worth, and only a whole-eleven measurement
# sees that.
LEAK_POINTS = 1.5


def leaks(best):
    """XI slots scoring near nothing, worst first.

    `best` is a `lineup.optimize` result. An incomplete line counts too: an
    empty slot is the most expensive leak there is, and it scores exactly zero.
    """
    if not best:
        return []
    out = []
    for line in ("goalkeeper", "defender", "midfield", "striker"):
        entries = best.get(line)
        if entries is None:
            continue
        for e in ([entries] if isinstance(entries, dict) else entries):
            if not e:
                continue
            score = float(e.get("score") or 0)
            if score <= LEAK_POINTS:
                out.append({"player_team_id": str(e.get("playerTeamId")),
                            "nombre": e.get("nombre"), "line": line,
                            "expected": round(score, 2), "tag": e.get("tag")})
    missing = best.get("missing") or {}
    for line, holes in missing.items():
        for _ in range(int(holes or 0)):
            out.append({"player_team_id": None, "nombre": "(hueco vacío)",
                        "line": line, "expected": 0.0, "tag": "empty"})
    out.sort(key=lambda r: r["expected"])
    return out


def _without(team, ptids):
    drop = {str(p) for p in ptids}
    return {**team, "players": [
        p for p in team.get("players") or []
        if str(p.get("playerTeamId")
               or (p.get("playerMaster") or {}).get("id")) not in drop]}


def _with(team, pms):
    return {**team, "players": list(team.get("players") or [])
            + [_as_squad_member(pm) for pm in pms]}


def combo_gain(team, pms, sell_ptids=(), prob_index=None,
               fixture_difficulty=None, form_index=None, base=None):
    """What the ELEVEN is worth after this whole move, minus what it is worth now.

    Not a sum of marginal gains. Two signings who would fill the same slot add
    one slot's worth between them, and adding their separate gains says two —
    which is precisely the arithmetic that makes stacking stars look good.
    """
    if base is None:
        base = squad_points(team, prob_index, fixture_difficulty, form_index)
    after = _with(_without(team, sell_ptids), pms)
    return round(squad_points(after, prob_index, fixture_difficulty,
                              form_index) - base, 2)


def rebuild(team, ranked, cards, sellable_rows, money=0, reserve=0,
            prob_index=None, fixture_difficulty=None, form_index=None,
            max_buys=MAX_BUYS, max_sells=MAX_SELLS, limit=3):
    """The best whole move available: sell these, buy those.

    Returns plans sorted by what they add to the eleven, each carrying the
    arithmetic that justifies it. A plan that needs no sale is included — this
    is not a bias towards churn, it is a search that happens to allow it.
    """
    spare = max(0, int(num(money)) - int(num(reserve)))
    min_gain = modes.knob("min_gain")
    base = squad_points(team, prob_index, fixture_difficulty, form_index)
    # A price of zero is not a free footballer, it is a price we failed to read
    # — `num` turns a missing or malformed field into 0, and three of those
    # would sail through the budget check and propose an eighty-million move
    # against an empty account. `rank` already drops them; this does not depend
    # on that, because the promise "a plan can pay for itself" has to hold for
    # whoever calls this, not just for the one caller that filters first.
    buys = [r for r in (ranked or [])
            if r.get("market_id") is not None
            and int(num(r.get("buy_price"))) > 0][:TOP_BUYS]
    # Cheapest to give up first: sellable is already ordered by what leaving costs.
    sells = [r for r in (sellable_rows or []) if r.get("raises")][:TOP_SELLS]

    plans = []
    evaluations = 0
    sale_sets = [()]
    for n in range(1, max_sells + 1):
        sale_sets.extend(combinations(sells, n))

    for sale in sale_sets:
        raised = sum(int(num(s.get("raises"))) * SALE_HAIRCUT for s in sale)
        lost = sum(float(s.get("loss") or 0) for s in sale)
        budget = spare + int(raised)
        gone = [str(s.get("player_team_id")) for s in sale]
        for n in range(1, max_buys + 1):
            for pick in combinations(buys, n):
                price = sum(int(num(b.get("buy_price"))) for b in pick)
                if price > budget:
                    continue
                # Independent gains are an UPPER BOUND on what a combination can
                # be worth: two players competing for one slot deliver less
                # together than apart, never more. So a combination whose
                # optimistic total cannot clear the bar cannot clear it honestly
                # either, and is skipped without paying for a lineup solve.
                # Only when every row actually carries a gain. An absent one
                # would read as zero and prune the whole search away in
                # silence — the exact shape of failure this bot keeps finding.
                if all(b.get("gain") is not None for b in pick):
                    if sum(float(b["gain"]) for b in pick) - lost < min_gain:
                        continue
                # A sale has to be NEEDED. The search happily attaches a
                # harmless one to a purchase that was already affordable —
                # "sell the 500k reserve keeper to buy a 22M midfielder" — which
                # is not a plan, it is the same plan with a pointless disposal
                # stapled to it. If the cash covers the buys, there is nothing
                # to fund.
                if sale and price <= spare:
                    continue
                pms = [cards.get(str(b.get("player_id"))) for b in pick]
                if not all(pms):
                    continue
                if evaluations >= MAX_EVALUATIONS:
                    break
                evaluations += 1
                gained = combo_gain(team, pms, gone, prob_index,
                                    fixture_difficulty, form_index, base=base)
                net = round(gained - lost, 2)
                if net < min_gain:
                    continue
                plans.append({
                    "net_gain": net,
                    "gain": gained,
                    "loss": round(lost, 2),
                    "buy": [{"nombre": b.get("nombre"),
                             "market_id": b.get("market_id"),
                             "player_id": b.get("player_id"),
                             "price": int(num(b.get("buy_price"))),
                             "via": b.get("via")} for b in pick],
                    "sell": [{"nombre": s.get("nombre"),
                              "player_team_id": s.get("player_team_id"),
                              "raises": int(num(s.get("raises")))}
                             for s in sale],
                    "spend": price,
                    "left_over": budget - price,
                    "why": _why(pick, sale, gained, lost, net),
                })
    # One entry per set of signings. Two plans that buy the same players are the
    # same plan; keeping both spends the caller's three slots showing one idea
    # twice, and pushes a genuinely different move off the list.
    plans.sort(key=lambda p: (-p["net_gain"], len(p["sell"]), p["spend"]))
    seen, unique = set(), []
    for p in plans:
        key = tuple(sorted(b["market_id"] for b in p["buy"]))
        if key in seen:
            continue
        seen.add(key)
        p["evaluations"] = evaluations
        # The money, spelled out. A plan the reader cannot check is a plan the
        # reader has to trust, and this one asks to sell a footballer.
        p["cash"] = spare
        p["from_sales"] = int(sum(int(num(x["raises"])) * SALE_HAIRCUT
                                  for x in p["sell"]))
        p["affordable"] = p["spend"] <= p["cash"] + p["from_sales"]
        unique.append(p)
    return unique[:limit]


def _why(pick, sale, gained, lost, net):
    buys = ", ".join(b.get("nombre") or "?" for b in pick)
    if not sale:
        return f"Fichar {buys} suma {gained} pts/jornada al once."
    sells = ", ".join(s.get("nombre") or "?" for s in sale)
    if len(pick) > len(sale):
        return (f"Vender {sells} para fichar {buys}: {len(pick)} titulares por "
                f"{len(sale)}. Suma {gained} y cuesta {lost} — neto {net} "
                f"pts/jornada.")
    return (f"Vender {sells} y fichar {buys}: neto {net} pts/jornada "
            f"({gained} menos {lost}).")

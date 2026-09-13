"""Which signing actually improves the team.

The bot bought by resale margin: it ranked the market by projected profit and
never asked whether the player would take the field. That is a trader, and a
trader finishes the season rich and second. The league is scored on points.

So every candidate is answered with one question — how many points would my XI
score with him in it, against what it scores now? Put him in the squad,
re-optimise the eleven, take the difference. A signing that does not make the XI
is worth zero points however cheap he is, and a goalkeeper for a squad with one
keeper is worth whatever the bench was giving, which is nearly nothing.

That single measure replaces three separate rules: filling a gap is just a very
large gain, a flip is a gain of zero with a resale margin attached, and "he is a
good player" is a gain of zero when your own line is already better. The margin
survives as a tiebreak between signings that help the XI equally, because money
is what funds the next one.

Cost: one lineup optimisation per candidate. It is pure arithmetic over a squad
of twenty — no API calls, no scraping — so forty candidates cost milliseconds.
"""

from ..matching import num, position_of
from . import lineup as lineup_opt

# A signing has to beat this before it is worth doing at all, in expected points
# per gameweek. Below it the difference is noise in the starting probabilities,
# and acting on noise spends real money.
MIN_GAIN = 0.25


def squad_points(team, prob_index=None, fixture_difficulty=None):
    """Expected points of the best XI this squad can field. 0 if it cannot."""
    try:
        best = lineup_opt.optimize(team, prob_index,
                                   fixture_difficulty=fixture_difficulty)
    except ValueError:
        return 0.0          # no goalkeeper: cannot field an XI at all
    return float(best.get("total") or 0.0)


def _as_squad_member(pm):
    """A market row's player, shaped like one of ours.

    The id is prefixed so a simulated signing can never be confused with a real
    roster slot if one of these dicts escapes into a payload.
    """
    return {"playerTeamId": f"sim:{pm.get('id')}", "playerMaster": pm}


def gain_from(team, pm, prob_index=None, fixture_difficulty=None, base=None):
    """Points per gameweek the XI gains by owning this player."""
    if base is None:
        base = squad_points(team, prob_index, fixture_difficulty)
    trial = {**team, "players": list(team.get("players") or [])
             + [_as_squad_member(pm)]}
    return round(squad_points(trial, prob_index, fixture_difficulty) - base, 2)


def players_by_id(market):
    """{playerMaster id: the card}, for scoring a candidate in our own XI.

    Kept beside the evaluated rows rather than inside them: the rows are stored
    in the dashboard's report, and embedding a full player card in each would
    multiply that payload for data only the optimiser needs.
    """
    out = {}
    for row in market or []:
        pm = row.get("playerMaster") or {}
        if pm.get("id") is not None:
            out[str(pm["id"])] = pm
    return out


def rank(ops, team, cards=None, money=None, prob_index=None,
         fixture_difficulty=None, limit=None):
    """Every candidate, ranked by the points he adds to the XI per euro.

    `ops` are market rows already evaluated by strategy.flip — they carry the
    price and the resale margin — and `cards` maps player id to the raw card, so
    the optimiser can score him in our shirt.

    Points per euro, not points: two signings that each add a point beat one that
    adds one and a half for twice the price, and the budget is finite. Absolute
    gain breaks ties, because a squad has only so many slots.
    """
    base = squad_points(team, prob_index, fixture_difficulty)
    owned = {str((p.get("playerMaster") or {}).get("id"))
             for p in team.get("players") or []}
    cards = cards or {}
    out = []
    for op in ops or []:
        pid = str(op.get("player_id") or "")
        pm = cards.get(pid) or {}
        if not pid or not pm or pid in owned:
            continue
        price = int(num(op.get("buy_price")))
        if price <= 0:
            continue
        gain = gain_from(team, pm, prob_index, fixture_difficulty, base=base)
        out.append({
            **op,
            "gain": gain,
            "gain_per_million": round(gain / max(1.0, price / 1_000_000.0), 3),
            "affordable": money is None or price <= int(num(money)),
            "pos": op.get("pos") or position_of(pm, "?"),
        })
    out.sort(key=lambda r: (-r["gain_per_million"], -r["gain"],
                            r.get("buy_price") or 0))
    return out[:limit] if limit else out


def worth_signing(row):
    """Whether a ranked candidate clears the bar for spending real money."""
    return bool(row.get("affordable")) and row.get("gain", 0) >= MIN_GAIN


def best_plan(ranked, money, reserve=0, max_signings=3):
    """Greedy pick under the budget: best value per euro first.

    Greedy is the right answer here and not a shortcut — this is a knapsack, and
    value-per-euro-first is its standard approximation. Exactness would buy
    nothing: the inputs are probabilities, not prices.
    """
    budget = max(0, int(num(money)) - int(num(reserve)))
    picked, spent = [], 0
    for row in ranked:
        if len(picked) >= max_signings:
            break
        price = int(num(row.get("buy_price")))
        if not worth_signing(row) or spent + price > budget:
            continue
        picked.append({**row, "running_total": spent + price})
        spent += price
    return picked

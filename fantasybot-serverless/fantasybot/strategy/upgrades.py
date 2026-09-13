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

# How often a given starter misses a gameweek: injury, suspension, rotation, a
# knock on the Friday. Roughly one week in seven across a season.
#
# This is what makes a substitute worth anything at all. A backup keeper adds
# exactly zero points while the first choice is fit, so a measure that only asks
# "what does he add this week" says never buy one — and then the week the keeper
# is out there is no legal XI and the gameweek is lost outright. He is not points,
# he is insurance, and insurance has a price: the loss he prevents times the
# chance of needing him.
#
# It also settles a disagreement between two of this bot's own rules. The squad
# minimums said buy a second keeper; the upgrade measure said he is worthless.
# Both were describing the same player badly.
UNAVAILABILITY = 0.15


# A player who cannot score, used to measure a squad that has no goalkeeper.
_EMPTY_KEEPER = {"playerTeamId": "sim:no-keeper",
                 "playerMaster": {"id": "sim:no-keeper", "nickname": "—",
                                  "name": "—", "positionId": 1,
                                  "marketValue": 0, "playerStatus": "injured",
                                  "averagePoints": 0, "points": 0,
                                  "lastSeasonPoints": 0}}


def squad_points(team, prob_index=None, fixture_difficulty=None,
                 form_index=None):
    """Expected points of the best XI this squad can field.

    A squad with no goalkeeper does NOT score zero: the ten outfielders still
    play and still score, LaLiga simply leaves the slot empty. Returning zero
    here priced a backup keeper at 2.6 points a week — as if his absence cost the
    whole team — when what he actually prevents is one empty slot.

    So the keeperless world is measured with a keeper who cannot score, which is
    exactly what an empty slot is.
    """
    try:
        best = lineup_opt.optimize(team, prob_index,
                                   fixture_difficulty=fixture_difficulty,
                                   form_index=form_index)
    except ValueError:
        try:
            best = lineup_opt.optimize(
                {**team, "players": list(team.get("players") or [])
                 + [_EMPTY_KEEPER]},
                prob_index, fixture_difficulty=fixture_difficulty,
                form_index=form_index)
        except ValueError:
            return 0.0      # not even ten outfielders: nothing to field
    return float(best.get("total") or 0.0)


def _as_squad_member(pm):
    """A market row's player, shaped like one of ours.

    The id is prefixed so a simulated signing can never be confused with a real
    roster slot if one of these dicts escapes into a payload.
    """
    return {"playerTeamId": f"sim:{pm.get('id')}", "playerMaster": pm}


def _without_best_in_line(team, pos, prob_index=None, fixture_difficulty=None,
                          form_index=None):
    """The squad minus its strongest player in one position.

    Not a hypothetical: it is the ordinary state of a squad about one week in
    seven, and it is the only world in which a substitute is worth anything.
    """
    players = team.get("players") or []
    line = [p for p in players if position_of(p.get("playerMaster")) == pos]
    if not line:
        return team
    best = max(line, key=lambda p: num((p.get("playerMaster") or {})
                                       .get("marketValue")))
    return {**team, "players": [p for p in players if p is not best]}


def gain_from(team, pm, prob_index=None, fixture_difficulty=None, base=None,
              form_index=None,
              with_insurance=True):
    """Points per gameweek the XI gains by owning this player.

    Two worlds, weighted: the ordinary one where everyone ahead of him is fit,
    and the one where the best man in his position is missing. A first-choice
    signing is worth almost all of the first; a substitute is worth only the
    second, which is small but is not zero — and "not zero" is the difference
    between fielding ten men and eleven on the week it happens.
    """
    if base is None:
        base = squad_points(team, prob_index, fixture_difficulty, form_index)
    trial = {**team, "players": list(team.get("players") or [])
             + [_as_squad_member(pm)]}
    now = squad_points(trial, prob_index, fixture_difficulty, form_index) - base
    if not with_insurance:
        return round(now, 2)

    pos = position_of(pm)
    thin = _without_best_in_line(team, pos, prob_index, fixture_difficulty,
                                 form_index)
    thin_base = squad_points(thin, prob_index, fixture_difficulty, form_index)
    thin_with = squad_points(
        {**thin, "players": list(thin.get("players") or [])
         + [_as_squad_member(pm)]}, prob_index, fixture_difficulty,
        form_index)
    cover = max(0.0, (thin_with - thin_base) - max(0.0, now))
    return round(now + UNAVAILABILITY * cover, 2)


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
         fixture_difficulty=None, limit=None, form_index=None):
    """Every candidate, ranked by the points he adds to the XI per euro.

    `ops` are market rows already evaluated by strategy.flip — they carry the
    price and the resale margin — and `cards` maps player id to the raw card, so
    the optimiser can score him in our shirt.

    Points per euro, not points: two signings that each add a point beat one that
    adds one and a half for twice the price, and the budget is finite. Absolute
    gain breaks ties, because a squad has only so many slots.
    """
    base = squad_points(team, prob_index, fixture_difficulty, form_index)
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
        gain = gain_from(team, pm, prob_index, fixture_difficulty, base=base,
                         form_index=form_index)
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


def loss_from_selling(team, player_team_id, prob_index=None,
                      fixture_difficulty=None, base=None, form_index=None):
    """Points per gameweek the XI loses if this player leaves.

    Zero for most of a squad: a bench player who never makes the eleven costs
    nothing to sell, however much he is worth. That is exactly the money a better
    starter should be bought with.
    """
    if base is None:
        base = squad_points(team, prob_index, fixture_difficulty, form_index)
    without = [p for p in team.get("players") or []
               if str(p.get("playerTeamId")) != str(player_team_id)]
    trial = {**team, "players": without}
    return round(base - squad_points(trial, prob_index, fixture_difficulty, form_index), 2)


# How much of a player's reserve price we assume we would actually get. A
# reserve is an ASK, and a transfer plan built on the ask is a plan that funds
# itself with money nobody has offered. Selling to the system is the floor that
# always exists, and it pays around the market value.
SALE_HAIRCUT = 0.90


def sellable(team, reserves=None, prob_index=None, fixture_difficulty=None,
             form_index=None):
    """Every owned player, with what leaving would cost and what he would raise.

    Sorted by the cheapest thing to give up first: no points lost, most money
    raised. That is the order a transfer should eat through a squad in.
    """
    base = squad_points(team, prob_index, fixture_difficulty, form_index)
    reserves = reserves or {}
    out = []
    for p in team.get("players") or []:
        pm = p.get("playerMaster") or {}
        pid = str(pm.get("id"))
        ptid = p.get("playerTeamId") or pid
        value = num(pm.get("marketValue"))
        raise_ = int(num(reserves.get(pid)) or value * SALE_HAIRCUT)
        out.append({
            "player_id": pid, "player_team_id": ptid,
            "nombre": pm.get("nickname") or pm.get("name"),
            "pos": position_of(pm, "?"),
            "value": int(value), "raises": raise_,
            "loss": loss_from_selling(team, ptid, prob_index,
                                      fixture_difficulty, base=base,
                                      form_index=form_index),
        })
    out.sort(key=lambda r: (r["loss"], -r["raises"]))
    return out


def transfers(ranked, team, money=0, reserves=None, prob_index=None,
              fixture_difficulty=None, reserve_cash=0, limit=5,
              form_index=None, give_up=None):
    """Signings the cash cannot reach, paired with the player who funds them.

    The bot could only ever buy what its balance covered, so a squad holding a
    nine-million bench player who scores nothing was locked out of every real
    starter on the market. Selling him IS the transfer; treating the two halves
    as separate decisions is what kept them from ever happening.

    Net points, never gross: a sale that costs more than the signing adds is not
    a transfer, it is a downgrade with extra steps.

    `give_up` is `sellable(team, ...)` when the caller already has it. It is not
    a micro-optimisation: sellable re-optimises the whole eleven ONCE PER PLAYER
    to price what losing him costs, so computing it twice in one review is
    sixteen extra lineup solves and roughly ten seconds of a sixty-second
    function — time the review then takes out of the phases that spend money.
    """
    spare = max(0, int(num(money)) - int(num(reserve_cash)))
    if give_up is None:
        give_up = sellable(team, reserves, prob_index, fixture_difficulty,
                           form_index=form_index)
    owned_ids = {r["player_id"] for r in give_up}
    out = []
    for buy in ranked:
        if buy.get("gain", 0) < MIN_GAIN:
            continue
        price = int(num(buy.get("buy_price")))
        if price <= spare:
            continue          # affordable already; not a transfer
        for sell in give_up:
            if sell["player_id"] in owned_ids and sell["raises"] + spare < price:
                continue      # still does not cover it
            net = round(buy["gain"] - sell["loss"], 2)
            if net < MIN_GAIN:
                continue      # the sale costs more than the signing adds
            out.append({
                "buy": buy.get("nombre"), "market_id": buy.get("market_id"),
                "price": price, "gain": buy.get("gain"),
                "sell": sell["nombre"], "player_team_id": sell["player_team_id"],
                "raises": sell["raises"], "loss": sell["loss"],
                "net_gain": net,
                "left_over": sell["raises"] + spare - price,
            })
            break             # the cheapest sale that covers it, and no more
    out.sort(key=lambda r: -r["net_gain"])
    return out[:limit] if limit else out

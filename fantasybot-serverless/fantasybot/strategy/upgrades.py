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

from .. import modes
from ..matching import num, position_of
from . import lineup as lineup_opt

# A signing has to beat this before it is worth doing at all, in expected points
# per gameweek. Below it the difference is noise in the starting probabilities,
# and acting on noise spends real money.
MIN_GAIN = 0.25
# The projected resale profit a purchase must show before "hacer caja" will make
# it. Below this the spread does not cover being wrong about the trend.
MIN_MARGIN_PCT = 5.0

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


def freezes_capital(team, pm, expected=None, premium=False):
    """What buying this player turns into dead money, and who.

    `gain_from` measures what a signing ADDS to the eleven and is right about
    it. What it cannot see is what the signing PUSHES OUT. Buying an eleventh
    defender when a formation fields five does not merely fail to help — it
    converts another defender into capital that can never score, and that cost
    belongs to this decision.

    Measured on the reported squad, this is not a hypothetical: an eleventh
    defender scored a 1.60 gain against a fourth striker's 1.43, so the ranking
    actively preferred the line that was already six players over its ceiling.
    Ten defenders and three million euros is what that produces.

    Returns (frozen_value, displaced_player) — the market value of the man who
    newly falls outside what his line can field, and who he is. (0, None) when
    the line has room, which is the ordinary case.
    """
    from .offers import SPARE_PER_LINE, _max_fieldable

    pos = position_of(pm)
    if pos is None or pos == "ENT":
        return 0, None
    keep = _max_fieldable(premium).get(pos, 99) + SPARE_PER_LINE
    line = [p for p in (team.get("players") or [])
            if position_of(p.get("playerMaster")) == pos]
    if len(line) < keep:
        return 0, None          # room in the line: nothing is pushed out

    def _rank(p):
        """Best first. Expected points if we have them, scoring record if not.

        The fallback matters more than it looks. Ranking a full line by MARKET
        VALUE would make an expensive player who scores nothing outrank the
        squad — so an overpriced dud would appear to displace somebody good
        instead of being the one who does not fit, and the charge would land on
        the wrong player. Price is what the market thinks; points are what the
        line is for.
        """
        m = p.get("playerMaster") or {}
        ptid = str(p.get("playerTeamId") or m.get("id"))
        pts = (expected or {}).get(ptid)
        if pts is None:
            pts = num(m.get("averagePoints"), None)
        return (pts is None, -(pts or 0), -num(m.get("marketValue")))

    # Who is NEWLY frozen, which is not the same as who is outside the window.
    #
    # With ten defenders and room for six, four are already dead capital before
    # we buy anybody; the eleventh signing makes it five. So the cost of this
    # purchase is the one player who crosses the line because of it — the last
    # man once the newcomer is ranked in — and not the best of the players who
    # were already stranded, who is what the first version charged.
    #
    # Ranking the newcomer with the rest rather than assuming he is the best is
    # what keeps this honest for a mediocre signing: if HE ends up last, the
    # frozen capital is his own price, which is exactly right.
    arriving = {"playerTeamId": None, "playerMaster": pm}
    order = sorted(line + [arriving], key=_rank)
    loser = order[-1]
    lm = loser.get("playerMaster") or {}
    return int(num(lm.get("marketValue"))), {
        "player_id": lm.get("id"),
        "player_team_id": loser.get("playerTeamId"),
        "nombre": lm.get("nickname") or lm.get("name"),
        "value": int(num(lm.get("marketValue"))),
        "es_el_que_ficho": loser is arriving}


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
         fixture_difficulty=None, limit=None, form_index=None, expected=None):
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
        # A signing costs its price PLUS whatever it freezes. Buying into a
        # line that is already full converts somebody into capital that can
        # never score, and charging the purchase for it is the difference
        # between rotating a squad and hoarding one.
        frozen, displaced = freezes_capital(team, pm, expected)
        committed = price + frozen
        out.append({
            **op,
            "gain": gain,
            "gain_per_million": round(gain / max(1.0, price / 1_000_000.0), 3),
            # Ranked on this: points per euro actually tied up, not per euro
            # handed over. They are the same number until a line is full.
            "gain_per_million_neto": round(
                gain / max(1.0, committed / 1_000_000.0), 3),
            "congela": frozen,
            "desplaza": displaced,
            "affordable": money is None or price <= int(num(money)),
            "pos": op.get("pos") or position_of(pm, "?"),
        })
    # What "best" means is the mode's call. Points per euro is the default and
    # the right one for winning gameweeks; a trading posture ranks the same rows
    # by the profit it expects to take out of them instead.
    if modes.knob("rank_by") == "margin":
        out.sort(key=lambda r: (-(r.get("margin_pct") or 0),
                                -r["gain_per_million_neto"],
                                r.get("buy_price") or 0))
    else:
        out.sort(key=lambda r: (-r["gain_per_million_neto"], -r["gain"],
                                r.get("buy_price") or 0))
    return out[:limit] if limit else out


def worth_signing(row):
    """Whether a ranked candidate clears the bar for spending real money.

    The bar is the active mode's, not this module's constant. In "hacer caja"
    the points bar is zero — which does NOT mean buy anyone: it means points are
    no longer what is being tested, and the margin test below is. In "todo a
    puntos" it drops, because a smaller edge is still an edge when points are
    the only currency.
    """
    if not row.get("affordable"):
        return False
    if modes.knob("rank_by") == "margin":
        # Trading: the purchase has to show a real projected profit. A player
        # who adds nothing and gains nothing is not a trade, he is a donation.
        return (row.get("margin_pct") or 0) >= MIN_MARGIN_PCT
    return row.get("gain", 0) >= modes.knob("min_gain")


def best_plan(ranked, money, reserve=0, max_signings=3, cash_budget=None):
    """Greedy pick under the budget: best value per euro first.

    Greedy is the right answer here and not a shortcut — this is a knapsack, and
    value-per-euro-first is its standard approximation. Exactness would buy
    nothing: the inputs are probabilities, not prices.

    `cash_budget`, when given, is the part of `money` that is not borrowed. A
    row marked `credit_ok: False` — an auction whose debt could not be repaid
    before the next gameweek — must fit inside it, counting everything already
    picked, because only cash is safe for it.
    """
    budget = max(0, int(num(money)) - int(num(reserve)))
    picked, spent = [], 0
    for row in ranked:
        if len(picked) >= max_signings:
            break
        price = int(num(row.get("buy_price")))
        if not worth_signing(row) or spent + price > budget:
            continue
        if (cash_budget is not None and row.get("credit_ok") is False
                and spent + price > int(num(cash_budget))):
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

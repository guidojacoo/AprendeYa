"""Standing listings, and what to do with the offers they attract.

A different way to sell. Instead of the bot picking who to sell and dumping them
on the market, the WHOLE squad sits listed permanently and the market tells us
what each player is worth. Nothing is sold until somebody offers enough.

Listing is not selling. A listing is an ask; the sale only happens if we accept
an offer. So keeping everyone listed costs nothing and buys information: a rival
desperate for a goalkeeper will pay over the odds for ours, and we will never
learn that if we never listed him.

What the ask price means here: it is our RESERVE, not a wish. Each player is
listed at the least we would genuinely accept for him, which makes the rule for
incoming offers trivial — at or above the ask, sell; below it, decline. The ask
is computed from what the player is worth TO US, not just his market value:

  in the optimal XI      value + 40%   prising away a starter should hurt
  useful squad player    value + 15%   a real asset; sell at a profit, not at par
  already a sell target  value          the advisor wants him gone anyway
  out of LaLiga          value - 30%   his value is collapsing; take what you can

Everything here is pure: squad and market data in, decisions out. No I/O, no
clock, no API calls — so the thresholds can be tested exhaustively, which matters
for code that decides when to part with a player.
"""

from .. import modes
from ..matching import num, position_of
from .lineup import FORMATIONS, PREMIUM_FORMATIONS, payload_ids

# Premium over market value required to part with a player, by how much we need him.
XI_PREMIUM = 0.40          # a starter in the optimal XI
SQUAD_PREMIUM = 0.15       # a bench player who is still an asset
SELLABLE_PREMIUM = 0.0     # the sell advisor already flagged him
DUMP_DISCOUNT = -0.30      # out of LaLiga: his value is going to zero, take the cash

# A player who does not play for his REAL club.
#
# Two reserve keepers sat in this squad scoring nothing, priced as "bench player
# who is still an asset" — market value plus fifteen per cent. Nobody pays a
# premium for a man who will not take the field, so the listing never sold and
# the money stayed locked in him all season.
#
# He is not an asset. He is cash we have not collected yet, and the reasoning is
# the same one DUMP_DISCOUNT already applies to a player who left the league:
# waiting does not make him worth more. So he is asked slightly UNDER his value,
# which is what actually moves a player nobody wants at par.
BENCH_DISCOUNT = -0.08

# Expected points per gameweek below which a player is not an asset at all.
# A genuine substitute keeper projects around 0.4; a starter, several times that.
MIN_USEFUL_POINTS = 1.0

# Below this, a listing is not worth the slot.
MIN_LISTING_PRICE = 100_000

ACCEPT = "accept"
DECLINE = "decline"


# --- positional surplus -------------------------------------------------------
# Ten defenders when a formation fields five.
#
# The pricing above asks what a player is worth to US, and answered it one
# player at a time: a seventh defender has perfectly good expected points in
# isolation, so he was "a bench player who is still an asset" at market value
# plus fifteen per cent, and he sat there all season. He is not an asset. His
# POSITION cannot field him — there is no arrangement of the other nine in which
# he takes the pitch — so the points he might score are unreachable, and the
# money in him is frozen.
#
# That is a property of the squad's shape, not of the player, and only a
# position-wide view can see it.
#
# The ceilings come from the formation tables rather than from numbers typed
# here, so they stay true if LaLiga changes them: the most any shape fields.
# One spare per line on top, because an injury or a rotation needs cover and
# selling down to exactly the eleven is its own kind of broken.
SPARE_PER_LINE = 1


def _max_fieldable(premium=False):
    shapes = FORMATIONS + (PREMIUM_FORMATIONS if premium else [])
    return {"POR": 1,
            "DEF": max(d for d, _m, _f in shapes),
            "MED": max(m for _d, m, _f in shapes),
            "DEL": max(f for _d, _m, f in shapes)}


def surplus_ids(team, expected=None, premium=False):
    """playerMaster ids of players their own position can never field.

    Ranked within each position by expected points, best first, so the ones cut
    are the ones we would field last. Without `expected` the ranking falls back
    to market value, which is a weaker proxy but never a crash.
    """
    ceiling = _max_fieldable(premium)
    by_pos: dict = {}
    for p in team.get("players") or []:
        pm = p.get("playerMaster") or {}
        pos = position_of(pm)
        if pos is None or pos == "ENT":
            continue          # a coach occupies no outfield slot
        ptid = str(p.get("playerTeamId") or pm.get("id"))
        rank = ((expected or {}).get(ptid), num(pm.get("marketValue")))
        by_pos.setdefault(pos, []).append((rank, str(pm.get("id"))))
    out = set()
    for pos, rows in by_pos.items():
        keep = ceiling.get(pos, 99) + SPARE_PER_LINE
        rows.sort(key=lambda r: (r[0][0] is None, -(r[0][0] or 0), -r[0][1]))
        out.update(pid for _rank, pid in rows[keep:])
    return out


def _market_value(player):
    """His price, as a number whatever LaLiga sent.

    It arrives as the string "2683751" from the squad endpoint, and
    `round(value * 1.15)` on a string raises. That is what stopped every listing:
    no reserve could be computed, so nobody went on the market, so no offer ever
    came back, so nothing ever sold.
    """
    return num((player.get("playerMaster") or {}).get("marketValue"))


def _is_out_of_league(player):
    return (player.get("playerMaster") or {}).get("playerStatus") == "out_of_league"


def premium_for(player, xi_ids, sell_ids, expected=None, surplus=()):
    """How much over market value this player must fetch before we let him go.

    `expected` maps playerTeamId to his expected points per gameweek. Without it
    everything behaves as before; with it, a player who does not play is priced
    to actually leave instead of sitting at a premium nobody will pay.

    The active mode scales the result, and scales the eleven separately from
    everyone else. That separation is the whole point: "move the stock I do not
    field" and "sell the man who starts every week" are different instructions,
    and a single knob for both would quietly mean the second. So "hacer caja"
    discounts the bench to turn it over and leaves the eleven exactly where it
    was, while "todo a puntos" makes a starter effectively unbuyable.
    """
    pm = player.get("playerMaster") or {}
    ptid = str(player.get("playerTeamId") or pm.get("id"))
    if _is_out_of_league(player):
        return DUMP_DISCOUNT
    if ptid in {str(i) for i in xi_ids}:
        return XI_PREMIUM * modes.knob("xi_premium")
    # Checked BEFORE the sell list and before the squad default: a man who does
    # not play is the clearest sell there is, whether or not an advisor flagged
    # him, and he is certainly not worth a premium.
    if expected is not None:
        pts = expected.get(ptid)
        if pts is not None and pts < MIN_USEFUL_POINTS:
            return BENCH_DISCOUNT
    # His position cannot field him, however good he looks on his own. Priced
    # like the man who does not play, because in practice he is that man: the
    # points are unreachable and the money is frozen until somebody buys him.
    if str(pm.get("id")) in {str(i) for i in surplus}:
        return BENCH_DISCOUNT
    if str(pm.get("id")) in {str(i) for i in sell_ids}:
        return SELLABLE_PREMIUM
    return SQUAD_PREMIUM * modes.knob("bench_premium")


def take_profit(player, xi_ids, paid):
    """Whether this holding has run far enough to cash in.

    The trade the points engine cannot express: bought at ten, worth fifteen,
    sell. `paid` is what we actually paid, read off LaLiga's own activity feed —
    not a number this bot stores and could get wrong.

    Never a starter. A player who appreciated is still a player who plays, and
    selling the eleven to bank a paper gain is how a trading mode relegates you.
    In practice a mode that buys for margin parks those players on the bench
    anyway, so this costs the strategy nothing.
    """
    target = modes.knob("flip_target")
    if not target or not paid:
        return False
    pm = player.get("playerMaster") or {}
    ptid = str(player.get("playerTeamId") or pm.get("id"))
    if ptid in {str(i) for i in xi_ids}:
        return False
    value = _market_value(player)
    return bool(value) and value >= num(paid) * (1 + target)


def _in_xi(player, xi_ids):
    pm = player.get("playerMaster") or {}
    ptid = str(player.get("playerTeamId") or pm.get("id"))
    return ptid in {str(i) for i in xi_ids}


# The ask for a player we are willing to move, as a share of his market value.
#
# The reserve is a THRESHOLD, not a price we receive: `accept_offer` is called
# with the offered amount, so a lower reserve never earns us less — it only
# decides yes or no. Asking a premium from a player we want gone therefore buys
# nothing and costs the sale.
#
# And the buyer is LaLiga. The market makes a standing offer on every listing
# roughly once a day, often above market value, and in a league of friends that
# is the only reliable bid there is. A bench player asked at value +15% against
# a buyer who offers +10% is a player who never sells, all season, for the sake
# of a premium we would not have collected anyway.
#
# So anyone outside the eleven is asked market value, and we take whatever
# LaLiga puts on the table above it. The eleven keeps its premium: nobody is
# selling a starter to the market for par.
MOVABLE_ASK = 1.0


def reserve_price(player, xi_ids, sell_ids, days_listed=0, expected=None,
                  paid=None, surplus=()):
    """The least we would accept — and therefore what we list him at.

    `days_listed` is accepted and no longer changes the price. It used to drive
    a premium that decayed over unsold days — a fix for the same underlying
    problem MOVABLE_ASK now solves directly and from day one, which makes
    waiting for a decay redundant: a non-XI player already asks no more than
    market value on the day he is listed, so there is nothing left to walk
    down. Kept as a parameter because callers report it on the page (how long
    a listing has sat), which is still useful information on its own.
    """
    value = _market_value(player)
    if not value:
        return 0
    # A holding that hit its target is priced to LEAVE, at market, with no
    # premium on top. The decision was "take the profit"; haggling over the last
    # few per cent is how a taken profit turns back into a holding.
    if take_profit(player, xi_ids, paid):
        return max(0, round(value))
    premium = premium_for(player, xi_ids, sell_ids, expected, surplus)
    # Outside the eleven, never ask more than the market pays. See MOVABLE_ASK:
    # the threshold does not change what we collect, so a premium here is a
    # refusal dressed up as a price. The eleven keeps whatever premium_for gave
    # it — a starter is never discounted just because nobody has bid yet.
    if not _in_xi(player, xi_ids):
        premium = min(premium, MOVABLE_ASK - 1.0)
    return max(0, round(value * (1 + premium)))


def _listed_player_ids(market):
    """playerMaster ids currently sitting on the market as a team's player.

    A player belongs to exactly one squad, so a `marketPlayerTeam` row for one of
    ours can only be OUR listing — which makes this a reliable "already listed"
    check without having to match team ids across two payload shapes.
    """
    out = set()
    for row in market or []:
        if row.get("discr") != "marketPlayerTeam":
            continue
        pid = (row.get("playerMaster") or {}).get("id")
        if pid is not None:
            out.add(str(pid))
    return out


def plan_listings(team, market, best=None, sells=None, min_price=MIN_LISTING_PRICE,
                  listed_since=None, expected=None, paid_by_id=None):
    """Squad players that should be put on the market, and at what price.

    Everyone not already listed goes up, each at his own reserve. Starters
    included — their reserve is high enough that only a genuinely good offer gets
    them, and a listing nobody meets costs us nothing.
    """
    xi_ids = payload_ids(best) if best else set()
    sell_ids = {s.get("player_id") for s in (sells or [])}
    surplus = surplus_ids(team, expected)
    already = _listed_player_ids(market)

    out = []
    for p in team.get("players") or []:
        pm = p.get("playerMaster") or {}
        pid = str(pm.get("id"))
        if pid in already:
            continue
        days = (listed_since or {}).get(pid, 0)
        paid = (paid_by_id or {}).get(pid)
        price = reserve_price(p, xi_ids, sell_ids, days_listed=days,
                              expected=expected, paid=paid, surplus=surplus)
        if price < min_price:
            continue      # not worth a listing slot
        out.append({
            "player_id": pm.get("id"),
            # The sell endpoint keys on the roster-slot id, not the master id.
            "player_team_id": p.get("playerTeamId") or pm.get("id"),
            "nombre": pm.get("nickname") or pm.get("name"),
            "value": _market_value(p),
            "price": price,
            "premium_pct": round(100 * (
                (price / _market_value(p) - 1.0) if _market_value(p) else 0)),
            # Why he is priced to leave: his line is full, not his form is bad.
            "sobra_en_su_posicion": str(pm.get("id")) in surplus,
            "expected_points": (expected or {}).get(
                str(p.get("playerTeamId") or pm.get("id"))),
            "days_listed": days,
            # What we paid, and whether this listing is a profit being taken.
            # On the page this is the difference between "lo vendo porque no me
            # sirve" and "lo vendo porque ya ganó lo que tenía que ganar".
            "paid": int(num(paid)) if paid else None,
            "taking_profit": take_profit(p, xi_ids, paid),
            "in_xi": str(p.get("playerTeamId") or pm.get("id")) in
                     {str(i) for i in xi_ids},
        })
    out.sort(key=lambda r: -r["value"])
    return out


# Fields that identify a HUMAN bidder on an offer. LaLiga's own standing offer
# carries none of them — it is not a manager — so their absence is what marks
# an offer as coming from the market itself.
_BIDDER_KEYS = ("user", "userId", "manager", "managerId", "team", "teamId",
                "buyerTeam", "sellerTeam", "userTeam")


def is_system_offer(raw):
    """True when LaLiga itself is the bidder, not a league manager.

    The market makes a standing offer on every listed player roughly once a
    day, often ABOVE his market value, and that — not a rival's bid — is how a
    player in this league actually sells. Nothing here knew that channel
    existed.

    Decided by the ABSENCE of a bidder rather than by a magic id, because an id
    would be a guess about an undocumented API and an absence is observable. A
    payload that does name its bidder is treated as human, which is the safe
    way round: mistaking a rival for the system would price our squad against
    the wrong buyer.
    """
    if not isinstance(raw, dict):
        return False
    for key in _BIDDER_KEYS:
        got = raw.get(key)
        if got not in (None, "", 0, "0", {}, []):
            return False
    return True


def _offers_on(row):
    """Normalise however the API hands offers back.

    Seen as a list under `offers` and as a single object under `offer`; a row with
    neither simply has no offers. Anything without an id is skipped rather than
    guessed at — acting on an offer we cannot identify is how you accept the
    wrong one.
    """
    raw = row.get("offers")
    if raw is None:
        single = row.get("offer")
        raw = [single] if isinstance(single, dict) else []
    if not isinstance(raw, list):
        return []
    out = []
    for o in raw:
        if not isinstance(o, dict):
            continue
        oid = o.get("id")
        money = o.get("money", o.get("amount"))
        if oid is None or money is None:
            continue
        try:
            out.append({"id": oid, "money": int(money),
                        "de_laliga": is_system_offer(o)})
        except (TypeError, ValueError):
            continue
    return out


def reserve_map(team, best=None, sells=None, listed_since=None,
                expected=None, paid_by_id=None):
    """{playerMaster id: reserve price} for the whole squad.

    Computed once per review and cached, because working it out needs the optimal
    XI — and re-optimising the lineup on every five-minute tick just to price an
    incoming offer would burn the function's whole time budget for nothing.
    """
    xi_ids = payload_ids(best) if best else set()
    sell_ids = {s.get("player_id") for s in (sells or [])}
    surplus = surplus_ids(team, expected)
    out = {}
    for p in team.get("players") or []:
        pid = (p.get("playerMaster") or {}).get("id")
        if pid is not None:
            out[str(pid)] = reserve_price(
                p, xi_ids, sell_ids,
                days_listed=(listed_since or {}).get(str(pid), 0),
                expected=expected, paid=(paid_by_id or {}).get(str(pid)),
                surplus=surplus)
    return out


def inspect_our_listings(market, reserves=None, team=None):
    """What our own listings actually look like coming back from LaLiga.

    "Nobody sold" has three completely different causes that render
    identically: nothing of ours is on the market, our listings are there and
    nobody bid, or offers exist and we are not reading them. The third is a bug
    and the other two are not, and until this existed there was no way to tell
    them apart — the offer handler simply found no offers and said nothing.

    The offer path assumes every bid arrives embedded in the market row, under
    `offers` or `offer`. Nothing ever checked that assumption against a real
    payload. If LaLiga puts them somewhere else, every listing reads as "no
    offers" forever, in silence, which is exactly what a season of never selling
    looks like.

    So this records what came back: how many of our listings there are, which
    keys they carry, and how many carried an offers structure of any kind.
    Cheap, and it turns a guess into a reading.
    """
    ours = set(reserves or {}) | {
        str((p.get("playerMaster") or {}).get("id"))
        for p in (team or {}).get("players") or []}
    mine, with_offers, offer_keys, row_keys = 0, 0, set(), set()
    total_offers = 0
    for row in market or []:
        if row.get("discr") != "marketPlayerTeam":
            continue
        pm = row.get("playerMaster") or {}
        if ours and str(pm.get("id")) not in ours:
            continue
        mine += 1
        row_keys.update(k for k in row if not k.startswith("_"))
        found = _offers_on(row)
        if found:
            with_offers += 1
            total_offers += len(found)
        for key in ("offers", "offer", "bids", "bid", "offersReceived"):
            if row.get(key) is not None:
                offer_keys.add(key)
    return {
        "anuncios_nuestros": mine,
        "con_ofertas": with_offers,
        "ofertas_totales": total_offers,
        # The keys LaLiga actually sends on our own listing rows. If none of the
        # offer-shaped names appear on any of them, offers do not travel with
        # this payload and the handler is reading the wrong place.
        "claves_de_oferta_vistas": sorted(offer_keys),
        "claves_de_la_fila": sorted(row_keys)[:40],
    }


def evaluate_offers(team, market, best=None, sells=None, reserves=None):
    """Decide every open offer on our listed players.

    Returns one decision per offer: accept it, or decline it. There is no
    "leave it and see" — an offer left sitting is an offer whose fate is decided
    by the platform's expiry rules rather than by us, and the whole point of a
    reserve price is that we choose.

    Only the BEST offer per player can be accepted; the rest are declined, so we
    never sell the same player twice in one pass.
    """
    xi_ids = payload_ids(best) if best else set()
    sell_ids = {s.get("player_id") for s in (sells or [])}
    squad = {str((p.get("playerMaster") or {}).get("id")): p
             for p in (team or {}).get("players") or []}
    # With cached reserves in hand, the reserves ARE the squad: their keys are
    # exactly our players. That lets a tick decide offers from one market read,
    # without also fetching the full team payload every five minutes.
    ours = set(squad) | set(reserves or {})

    decisions = []
    for row in market or []:
        if row.get("discr") != "marketPlayerTeam":
            continue
        pm = row.get("playerMaster") or {}
        pid = str(pm.get("id"))
        if pid not in ours:
            continue          # a rival's listing, not ours
        player = squad.get(pid) or {"playerMaster": pm}
        offers = _offers_on(row)
        if not offers:
            continue
        # A cached reserve (from the last review) wins: it was priced against a
        # freshly optimised XI, which this tick has not paid for.
        if reserves is not None:
            reserve = int(reserves.get(str(pm.get("id")), 0) or 0)
        else:
            reserve = reserve_price(player, xi_ids, sell_ids)
        offers.sort(key=lambda o: -o["money"])
        best_offer = offers[0]
        for i, offer in enumerate(offers):
            good = i == 0 and reserve > 0 and offer["money"] >= reserve
            decisions.append({
                "action": ACCEPT if good else DECLINE,
                "market_id": row.get("id"),
                "offer_id": offer["id"],
                "amount": offer["money"],
                "player_id": pm.get("id"),
                "nombre": pm.get("nickname") or pm.get("name"),
                "value": _market_value(player),
                "reserve": reserve,
                "in_xi": str(player.get("playerTeamId") or pm.get("id")) in
                         {str(i2) for i2 in xi_ids},
                # Who is paying. LaLiga's own standing offer, roughly once a
                # day and often above value, is the main channel a player in
                # this league actually sells through — worth knowing apart
                # from a rival manager's bid, which behaves completely
                # differently.
                "de_laliga": offer.get("de_laliga", False),
                "reason": (f"{offer['money']:,} >= reserve {reserve:,}" if good
                           else (f"{offer['money']:,} < reserve {reserve:,}"
                                 if i == 0 else
                                 f"outbid by {best_offer['money']:,}")),
            })
    return decisions

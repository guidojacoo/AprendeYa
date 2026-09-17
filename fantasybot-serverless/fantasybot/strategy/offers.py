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

from ..matching import num
from .lineup import payload_ids

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

# A price nobody meets is a price that is wrong. After a grace period, an unsold
# listing gives back some of its premium each day, so the reserve walks down
# towards what the market will actually pay — but never below market value,
# because selling an asset at a discount is a different decision entirely.
STALE_AFTER_DAYS = 2
PREMIUM_DECAY_PER_DAY = 0.25   # a quarter of the ORIGINAL premium, per day

ACCEPT = "accept"
DECLINE = "decline"


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


def premium_for(player, xi_ids, sell_ids, expected=None):
    """How much over market value this player must fetch before we let him go.

    `expected` maps playerTeamId to his expected points per gameweek. Without it
    everything behaves as before; with it, a player who does not play is priced
    to actually leave instead of sitting at a premium nobody will pay.
    """
    pm = player.get("playerMaster") or {}
    ptid = str(player.get("playerTeamId") or pm.get("id"))
    if _is_out_of_league(player):
        return DUMP_DISCOUNT
    if ptid in {str(i) for i in xi_ids}:
        return XI_PREMIUM
    # Checked BEFORE the sell list and before the squad default: a man who does
    # not play is the clearest sell there is, whether or not an advisor flagged
    # him, and he is certainly not worth a premium.
    if expected is not None:
        pts = expected.get(ptid)
        if pts is not None and pts < MIN_USEFUL_POINTS:
            return BENCH_DISCOUNT
    if str(pm.get("id")) in {str(i) for i in sell_ids}:
        return SELLABLE_PREMIUM
    return SQUAD_PREMIUM


def decayed_premium(premium, days_listed):
    """The premium after `days_listed` days without a taker.

    Only positive premiums decay. The out-of-league discount is not an asking
    price we are being stubborn about — it is a judgement that the player is
    losing value, and waiting should make us MORE willing to sell, not less.
    """
    if premium <= 0 or not days_listed or days_listed <= STALE_AFTER_DAYS:
        return premium
    stale_days = days_listed - STALE_AFTER_DAYS
    return max(0.0, premium - premium * PREMIUM_DECAY_PER_DAY * stale_days)


def reserve_price(player, xi_ids, sell_ids, days_listed=0, expected=None):
    """The least we would accept — and therefore what we list him at."""
    value = _market_value(player)
    if not value:
        return 0
    premium = decayed_premium(
        premium_for(player, xi_ids, sell_ids, expected), days_listed)
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
                  listed_since=None, expected=None):
    """Squad players that should be put on the market, and at what price.

    Everyone not already listed goes up, each at his own reserve. Starters
    included — their reserve is high enough that only a genuinely good offer gets
    them, and a listing nobody meets costs us nothing.
    """
    xi_ids = payload_ids(best) if best else set()
    sell_ids = {s.get("player_id") for s in (sells or [])}
    already = _listed_player_ids(market)

    out = []
    for p in team.get("players") or []:
        pm = p.get("playerMaster") or {}
        pid = str(pm.get("id"))
        if pid in already:
            continue
        days = (listed_since or {}).get(pid, 0)
        price = reserve_price(p, xi_ids, sell_ids, days_listed=days,
                              expected=expected)
        if price < min_price:
            continue      # not worth a listing slot
        out.append({
            "player_id": pm.get("id"),
            # The sell endpoint keys on the roster-slot id, not the master id.
            "player_team_id": p.get("playerTeamId") or pm.get("id"),
            "nombre": pm.get("nickname") or pm.get("name"),
            "value": _market_value(p),
            "price": price,
            "premium_pct": round(100 * decayed_premium(
                premium_for(p, xi_ids, sell_ids, expected), days)),
            "expected_points": (expected or {}).get(
                str(p.get("playerTeamId") or pm.get("id"))),
            "days_listed": days,
            "in_xi": str(p.get("playerTeamId") or pm.get("id")) in
                     {str(i) for i in xi_ids},
        })
    out.sort(key=lambda r: -r["value"])
    return out


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
            out.append({"id": oid, "money": int(money)})
        except (TypeError, ValueError):
            continue
    return out


def reserve_map(team, best=None, sells=None, listed_since=None,
                expected=None):
    """{playerMaster id: reserve price} for the whole squad.

    Computed once per review and cached, because working it out needs the optimal
    XI — and re-optimising the lineup on every five-minute tick just to price an
    incoming offer would burn the function's whole time budget for nothing.
    """
    xi_ids = payload_ids(best) if best else set()
    sell_ids = {s.get("player_id") for s in (sells or [])}
    out = {}
    for p in team.get("players") or []:
        pid = (p.get("playerMaster") or {}).get("id")
        if pid is not None:
            out[str(pid)] = reserve_price(
                p, xi_ids, sell_ids,
                days_listed=(listed_since or {}).get(str(pid), 0),
                expected=expected)
    return out


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
                "reason": (f"{offer['money']:,} >= reserve {reserve:,}" if good
                           else (f"{offer['money']:,} < reserve {reserve:,}"
                                 if i == 0 else
                                 f"outbid by {best_offer['money']:,}")),
            })
    return decisions

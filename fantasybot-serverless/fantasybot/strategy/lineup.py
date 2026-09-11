"""Lineup optimizer.

Picks the best XI and the best formation among several valid ones, based on the
probability of each player starting (futbolfantasy) and their availability
(injury/suspension, from the API and from futbolfantasy). Returns the proposal and the
body ready for `FantasyClient.update_lineup`. It does NOT apply anything by itself.
"""

from ..matching import match_name
from ..sources.lineups import probable_lineups
from .captain import form as _form, pick_captain

# positionId -> XI line. 5 ("Entrenador"/coach) is a premium slot that is NOT one of the
# four XI lines, so it maps to a label ("ENT") that is deliberately absent from `by_pos`:
# the grouping loop skips any positionId whose line isn't a real XI bucket, so a coach is
# never mixed into an outfield line (see `optimize`). The coach is selected separately.
POS = {1: "goalkeeper", 2: "defender", 3: "midfield", 4: "striker", 5: "ENT"}

# LaLiga positionId for the coach ("Entrenador"): owned like a normal player, only fieldable
# in a premium league, and scores like a player.
COACH_POSITION_ID = 5


def payload_ids(best) -> set:
    """Set of playerTeamIds of the XI in a lineup payload (`optimize`)."""
    p = best["payload"]
    return {p["goalkeeper"], *p["defender"], *p["midfield"], *p["striker"]}

# Valid formations as (defenders, midfielders, forwards). The goalkeeper is fixed (1).
FORMATIONS = [
    (3, 4, 3), (3, 5, 2), (4, 3, 3), (4, 4, 2),
    (4, 5, 1), (5, 3, 2), (5, 4, 1),
]

# The premium-only formation set LaLiga unlocks in premium leagues
# (config.premiumFeatures.formations). Each sums to 10 outfield players and includes unusual
# shapes: 4-6-0 fields ZERO forwards and 3-6-1 packs 6 midfielders. A midfield-short squad can
# field a complete XI via 5-2-3 (verified live against the API).
PREMIUM_FORMATIONS = [(5, 2, 3), (4, 6, 0), (4, 2, 4), (3, 6, 1), (3, 3, 4)]

def caliber_prior(market_value):
    """Prior for 'probability of starting' when futbolfantasy gives no data.

    Relies on market value: an expensive player is almost certainly a starter; a
    cheap one is a benchwarmer. Prevents an unmatched cast-off (Purić, 0.4M) from being
    worth the same as an unmatched starter (Etta, 25M). Scaled 0-100 like the probability.
    """
    v = market_value or 0
    if v >= 15_000_000:
        return 62
    if v >= 8_000_000:
        return 52
    if v >= 4_000_000:
        return 42
    if v >= 2_000_000:
        return 28
    return 12


NOT_IN_XI_SCORE = 5.0  # on his team but futbolfantasy doesn't list him as a starter → ~0%
DOUBTFUL_DISCOUNT = 0.6  # a 'duda' usually plays: rank below a fit player, ABOVE an injured one

# LaLiga playerStatus values that mean "probably plays, but with risk". Everything else
# that isn't "ok" is treated as won't-play (score 0) — conservative for unknown statuses.
_DOUBTFUL_STATUS = ("doubtful", "duda", "warned")


def player_score(player, prob_index):
    """Scores a player for the XI. Returns (score, prob, disponible, tag).

    tag distinguishes the source of the score:
      - 'in_xi'     : futbolfantasy gives him a starting probability (strong signal).
      - 'not_in_xi' : on his team but NOT in the probable lineup → ~0% (real bench;
                      in transfers, a signal to watch).
      - 'unknown'   : doesn't appear on futbolfantasy → we estimate from his value.
      - 'doubtful'  : a 'duda' — probably plays; scored at a risk discount so he beats
                      an injured player for a slot but sits below a fit one.
      - 'out'       : injured/suspended/unavailable → won't play, scores 0.
    """
    pm = player["playerMaster"]
    info = match_name(pm.get("nickname", ""), pm.get("name", ""), prob_index)
    prob = info.get("prob") if info else None

    status = (pm.get("playerStatus") or "ok").lower()
    doubtful = status in _DOUBTFUL_STATUS
    disponible = status == "ok" or doubtful   # a doubtful player is still fieldable & may score
    # futbolfantasy is a second injury source; it only ever makes things worse (out).
    if info and (info.get("lesionado") or not info.get("disponible", True)):
        disponible, doubtful = False, False

    if not disponible:
        base, tag = 0.0, "out"
    elif info is None:
        base, tag = float(caliber_prior(pm.get("marketValue"))), "unknown"
    elif prob is not None:
        base, tag = float(prob), "in_xi"
    else:
        base, tag = NOT_IN_XI_SCORE, "not_in_xi"
    if doubtful:
        base *= DOUBTFUL_DISCOUNT   # risk: below a fit player of the same prob, above injured (0)
        tag = "doubtful"
    base += (pm.get("lastSeasonPoints") or 0) * 0.001  # tiebreaker
    return base, prob, disponible, tag


def _pid(player):
    return player.get("playerTeamId") or player["playerMaster"]["id"]


def _entry(player, score, prob, disponible, tag):
    pm = player["playerMaster"]
    return {
        "playerTeamId": _pid(player),
        "nombre": pm.get("nickname") or pm.get("name"),
        "score": round(score, 1),
        "prob": prob,
        "disponible": disponible,
        "tag": tag,
        "valor": pm.get("marketValue"),
    }


def _fill(need, by_pos, gk):
    """Fill a formation line by line, ONLY from each line's own-position players.

    LaLiga silently DROPS any player fielded out of position (a midfielder sent as a
    defender comes straight back off the lineup, leaving the slot empty), but it KEEPS an
    injured/suspended player who is IN position — he just scores 0. So each line is filled
    exclusively from its own players: the available ones first, then that line's
    injured/unavailable players (a position-valid slot that happens to score 0). Nothing
    is ever borrowed across lines — a healthy player smuggled into the wrong slot would be
    dropped by LaLiga and change nothing but the lie in the payload.

    When a line owns fewer players than the formation asks for, it stays SHORT (fewer
    picks) — an honest empty hole the caller can surface and raise as an urgent problem,
    never a fake fill. `_fill` therefore always produces a (possibly partial) XI; it never
    returns None.

    Returns (picks, unavailable, missing):
      - picks:       {position: [entries]} of the real, position-valid players fielded.
      - unavailable: how many injured/suspended-yet-in-position players were fielded
                     (they count against the shape but are legal slots, NOT holes).
      - missing:     {position: count_short} for every line that couldn't be filled
                     (only positions with count > 0); empty dict when the XI is complete.
    """
    used = {gk["playerTeamId"]}
    picks = {}
    missing = {}
    for pos, n in need.items():
        # own players of this line, available first then injured (all position-valid)
        own = [e for e in by_pos[pos] if e["playerTeamId"] not in used]
        own.sort(key=lambda e: (not e["disponible"], -e["score"]))
        take = own[:n]
        for e in take:
            used.add(e["playerTeamId"])
        picks[pos] = take
        if len(take) < n:
            missing[pos] = n - len(take)  # honest hole: no own player left to fill it
    unavailable = sum(1 for pos in picks for e in picks[pos] if not e["disponible"])
    return picks, unavailable, missing


def _select_coach(players):
    """Best owned coach (positionId 5), or None if the squad owns none.

    A coach scores like a player, so we prefer an AVAILABLE one (an injured coach scores
    0), then the best recent form, then market value as a tiebreaker. Returns the raw
    squad-player dict (caller pulls its playerTeamId), or None — no coach owned is a gap
    the market side can flag, never an error here.
    """
    coaches = [p for p in players
               if (p.get("playerMaster") or {}).get("positionId") == COACH_POSITION_ID]
    if not coaches:
        return None

    def key(p):
        pm = p["playerMaster"]
        available = (pm.get("playerStatus") or "ok").lower() == "ok"
        return (available, _form(pm), pm.get("marketValue") or 0)

    return max(coaches, key=key)


def optimize(team, prob_index=None, premium=False, fixture_difficulty=None):
    """Computes the best XI + formation. Returns a dict with the proposal and the body.

    Every fielded slot is POSITION-VALID: each line is filled only from its own players,
    preferring available ones and using a line's own injured players to backfill that same
    line (see `_fill`). LaLiga drops a player fielded out of position but keeps an injured
    one who is in position (he scores 0), so a position-valid XI is what actually reaches
    the pitch — nothing is ever borrowed across lines.

    A squad may not own enough players of a line to fill ANY legal shape (LaLiga's 7 standard
    shapes all need >= 3 midfielders, so a 2-midfielder squad can't field 11). When that
    happens the optimizer does NOT paper over it: it fields only the real players it has,
    picks the shape with the FEWEST empty holes, and flags the result — `best["incomplete"]`
    True and `best["missing"]` a {position: count_short} dict — so the copy can raise it as an
    urgent "sign a player" problem instead of reporting a clean lineup. `payload_ids` then
    returns fewer than 11 for such an XI, which is correct and intended.

    `premium=True` (a premium league — config.premiumFeatures.formations) additionally unlocks
    the 2-midfielder shapes in PREMIUM_FORMATIONS, so a midfield-short squad CAN complete its
    XI (e.g. a 5-def, 2-mid, 3-str squad fields a full 5-2-3 instead of leaving a hole).
    """
    if prob_index is None:
        prob_index = probable_lineups()

    # group and score the squad by position
    by_pos = {"goalkeeper": [], "defender": [], "midfield": [], "striker": []}
    watch = []  # signals to watch (expensive players outside the probable lineup)
    for p in team["players"]:
        pos = POS.get(p["playerMaster"]["positionId"])
        if pos not in by_pos:   # unknown position OR a coach ("ENT"): never an XI line
            continue
        score, prob, disp, tag = player_score(p, prob_index)
        entry = _entry(p, score, prob, disp, tag)
        by_pos[pos].append(entry)
        if tag == "not_in_xi" and (entry["valor"] or 0) >= 8_000_000:
            watch.append(entry)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda e: -e["score"])

    if not by_pos["goalkeeper"]:
        raise ValueError("No goalkeeper in the squad.")
    # only AVAILABLE players are candidates for a slot; injured/suspended never fielded
    avail = {pos: [e for e in by_pos[pos] if e["disponible"]] for pos in by_pos}
    gk = (avail["goalkeeper"] or by_pos["goalkeeper"])[0]

    best = None
    for d, m, f in FORMATIONS + (PREMIUM_FORMATIONS if premium else []):
        need = {"defender": d, "midfield": m, "striker": f}
        # prefer formations we can fill entirely from AVAILABLE players of each position
        full_available = (len(avail["defender"]) >= d and len(avail["midfield"]) >= m
                          and len(avail["striker"]) >= f)
        picks, unavailable, missing = _fill(need, by_pos, gk)
        total_holes = sum(missing.values())
        # Only the players actually fielded score; a short line simply contributes fewer.
        total = gk["score"] + sum(e["score"] for pos in picks for e in picks[pos])
        cand = {
            "formation": (d, m, f),
            "goalkeeper": gk,
            "defender": picks["defender"],
            "midfield": picks["midfield"],
            "striker": picks["striker"],
            "total": round(total, 1),
            "incomplete": bool(total_holes > 0),
            "missing": missing,
            # rank: FEWEST empty holes first, then fewest injured fielded, then prefer a
            # shape filled entirely from available players, then score.
            "_rank": (-total_holes, -unavailable,
                      1 if full_available else 0, round(total, 1)),
        }
        if best is None or cand["_rank"] > best["_rank"]:
            best = cand

    best.pop("_rank", None)

    best["payload"] = {
        "goalkeeper": best["goalkeeper"]["playerTeamId"],
        "defender": [e["playerTeamId"] for e in best["defender"]],
        "midfield": [e["playerTeamId"] for e in best["midfield"]],
        "striker": [e["playerTeamId"] for e in best["striker"]],
        "tactical_formation": list(best["formation"]),
    }
    # PREMIUM ONLY: also set the coach, captain and bench. Gated strictly on `premium` so a
    # non-premium payload stays byte-identical (no coach/captain/bench keys). Building these
    # never raises: any failure just falls back to the XI-only payload already built above
    # (a working XI beats a rejected PUT). See `_premium_extras`.
    if premium:
        _premium_extras(team, best, fixture_difficulty)
    best["watch"] = watch
    return best


# --- PREMIUM payload field names: a TO-BE-LIVE-CONFIRMED assumption -----------------
# The GET /teams/{tid}/lineup response for a premium team carries, inside `formation`:
#   coach:[player], captain:"<playerTeamId>", bench:{}, tacticalFormation:[D,M,F].
# Our PUT payload uses SINGLE ids and snake_case (goalkeeper:<id>, tactical_formation:[...]),
# unlike the GET's lists/camelCase. Following those two conventions, the PUT premium fields
# are assumed to be:
#   coach:   the coach's playerTeamId (a single id, like `goalkeeper`)
#   captain: the captain's playerTeamId as a STRING (matches the GET's string captain)
#   bench:   {} (empty — every premium team we inspected had an empty bench; {} is valid and
#            preserves current behaviour, so we do NOT guess a populated structure)
# These three field names/formats are NOT 100% confirmed and are validated live before deploy.
def _premium_extras(team, best, fixture_difficulty=None):
    """Add coach + captain + bench to `best["payload"]` (premium only). Never raises.

    `fixture_difficulty` (optional, see strategy.captain) nudges the captain pick away
    from a tough fixture; omitted -> today's form-only pick, unchanged.
    """
    try:
        players = team.get("players", [])
        # COACH: the best owned positionId-5 player, if any (a single playerTeamId).
        coach = _select_coach(players)
        best["coach"] = coach["playerTeamId"] if coach else None
        if coach is not None:
            best["payload"]["coach"] = coach["playerTeamId"]

        # CAPTAIN: the top expected scorer among the chosen XI (doubles points). We map each
        # XI slot back to its raw squad player so the captain scorer sees real form data.
        by_id = {_pid(p): p for p in players}
        xi = [best["goalkeeper"], *best["defender"], *best["midfield"], *best["striker"]]
        candidates = []
        for e in xi:
            raw = by_id.get(e["playerTeamId"])
            if raw:
                candidates.append({"playerTeamId": e["playerTeamId"],
                                   "playerMaster": raw["playerMaster"],
                                   "prob": e.get("prob"), "disponible": e.get("disponible")})
        captain = pick_captain(candidates, fixture_difficulty)
        best["captain"] = str(captain) if captain is not None else None
        if captain is not None:
            best["payload"]["captain"] = str(captain)   # GET stores captain as a string id

        # BENCH: empty by default (see the field-name note above).
        best["payload"]["bench"] = {}
    except Exception:
        # A working XI-only payload beats a rejected PUT: drop any half-built premium fields.
        for k in ("coach", "captain", "bench"):
            best["payload"].pop(k, None)

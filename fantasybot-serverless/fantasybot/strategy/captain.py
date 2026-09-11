"""Captain (and coach form) scoring for PREMIUM leagues.

In a premium league one starter can be named CAPTAIN and scores DOUBLE this
gameweek. That makes the pick pure upside — name the XI player most likely to
rack up the most points — and it is deterministic MATH, not an LLM decision:
recent form, tilted by how surely the player starts. The lineup optimizer calls
`pick_captain` while building a premium payload; nothing here has side effects.

The same `form` signal (a player's recent scoring rate) also ranks owned coaches,
so the helper lives here and the optimizer imports it for both.

Real user question: "if my best-form player faces Real Madrid/Barcelona, does the
captain pick account for that?" It didn't — form/probability/value never looked at
who the opponent is. `fixture_difficulty_by_team` + the optional `fixture_difficulty`
argument add that as a NUDGE (a tiebreaker among comparable options), never a veto: a
clearly-better captain still wins even against the toughest possible fixture. There is
no official LaLiga strength/standings endpoint (checked live: /v1/teams and friends all
404), so difficulty is proxied by a team's aggregate squad market value — Real Madrid
and Barcelona sit far above the rest of the league on that measure, which is the
defensible part; it is NOT the same as their real table position.
"""

RIVAL_DIFFICULTY_WEIGHT = 0.40  # cap on the penalty: the toughest possible rival costs
                                # at most 40% of the captain value -- a nudge, not a veto


def form(pm):
    """A player's recent scoring form, read from the LaLiga playerMaster.

    Prefers this season's average points per game (`averagePoints`), then total
    `points`, then last season's points as a cold-start fallback. 0 when nothing
    is known — an unscored player simply ranks last, never crashes the pick.
    """
    for key in ("averagePoints", "points", "lastSeasonPoints"):
        v = pm.get(key)
        if v:
            return float(v)
    return 0.0


def team_strength_index(all_players) -> dict:
    """{team_id: percentile 0..1} by each team's AGGREGATE squad market value (0 = the
    league's cheapest squad, 1 = the priciest). A proxy for footballing difficulty in
    the absence of any official standings/strength endpoint. Pure, deterministic.
    """
    totals = {}
    for p in all_players or []:
        tid = p.get("teamId")
        if not tid:
            continue
        tid = str(tid)
        # all_players() returns marketValue as a STRING (LaLiga's API, always -- verified
        # live), never an int; a bare `+` here TypeErrors on the very first player.
        try:
            value = float(p.get("marketValue") or 0)
        except (TypeError, ValueError):
            value = 0
        totals[tid] = totals.get(tid, 0) + value
    if not totals:
        return {}
    ranked = sorted(totals, key=lambda tid: totals[tid])  # weakest squad first
    n = len(ranked)
    if n == 1:
        return {ranked[0]: 0.5}  # a single known team -> neutral, no basis to rank it
    return {tid: i / (n - 1) for i, tid in enumerate(ranked)}


def fixture_difficulty_by_team(all_players, fixtures) -> dict:
    """{team_id: difficulty 0..1 of the RIVAL this team faces this gameweek}, from
    `strength = team_strength_index(all_players)` and a `fixtures` list shaped like
    `client.calendar()` (`[{localId, visitorId, ...}]`). A team with no match this week
    (bye, unmatched id) is simply absent from the result — callers must treat absence
    as neutral (see `captain_value`), never crash on it. Pure.
    """
    strength = team_strength_index(all_players)
    out = {}
    for m in fixtures or []:
        local_id = str(m.get("localId")) if m.get("localId") is not None else None
        visitor_id = str(m.get("visitorId")) if m.get("visitorId") is not None else None
        if local_id and visitor_id in strength:
            out[local_id] = strength[visitor_id]
        if visitor_id and local_id in strength:
            out[visitor_id] = strength[local_id]
    return out


def captain_value(entry, fixture_difficulty=None):
    """Expected captain payoff for one XI entry — higher is a better captain.

    = recent `form`, tilted by start-probability (a nailed starter beats a
    rotation risk of equal form) with market value as a faint tiebreaker (the
    pricier player is the safer captain when form and probability are level).
    `prob` is 0-100 (futbolfantasy) or None; unknown -> a neutral 0.5 weight, so
    a player with no probable-lineup data still ranks on form, just without the
    boost.

    `fixture_difficulty` (optional, from `fixture_difficulty_by_team`) tilts the value
    DOWN by up to `RIVAL_DIFFICULTY_WEIGHT` for the toughest possible rival — a nudge
    among comparable options, never enough to override a clearly better captain.
    Omitted, or the player's team missing from the dict -> no penalty, identical to the
    signal not existing at all. Pure function.
    """
    pm = entry.get("playerMaster") or {}
    prob = entry.get("prob")
    prob_w = (prob / 100.0) if isinstance(prob, (int, float)) and not isinstance(prob, bool) else 0.5
    value = pm.get("marketValue") or 0
    base = form(pm) * (0.5 + 0.5 * prob_w) + value * 1e-9
    team_id = (pm.get("team") or {}).get("id")
    difficulty = (fixture_difficulty or {}).get(str(team_id)) if team_id is not None else None
    if isinstance(difficulty, (int, float)) and not isinstance(difficulty, bool):
        base *= 1 - RIVAL_DIFFICULTY_WEIGHT * difficulty
    return base


def pick_captain(xi, fixture_difficulty=None):
    """Choose the captain among the chosen XI.

    `xi` is a list of dicts, each with a `playerTeamId`, a `playerMaster`, and the
    optimizer's `prob`/`disponible`. Only AVAILABLE starters are eligible — an
    injured in-position filler scores 0, so captaining him would waste the double.
    `fixture_difficulty` is optional (see `captain_value`); omitted -> today's
    form-only behaviour, unchanged. Returns the best starter's playerTeamId, or None
    when the XI is empty / nobody is eligible (the caller then simply omits the
    captain).
    """
    eligible = [e for e in xi if e.get("disponible", True)]
    if not eligible:
        return None
    best = max(eligible, key=lambda e: captain_value(e, fixture_difficulty))
    return best.get("playerTeamId") or best["playerMaster"]["id"]

"""How well each club is ACTUALLY playing, from results.

Opponent strength was estimated from the market value of a club's squad. That is
defensible — Madrid and Barcelona really do sit far above the rest on it — and it
is not the question. An expensive squad losing every week is an easy fixture, and
a cheap one conceding nothing is not.

The real answer was already in reach and being thrown away. `/stats/week/{n}`
turned out to be the FIXTURE LIST, not player stats — ten rows of
{date, id, local, localScore, matchState, visitor, visitorScore} — which is a
results table waiting to be added up. It is fetched and cached already.

So: recent results become a strength index, and squad value stays as the prior
it always was. Four gameweeks is a small sample and a club can be beaten twice
by the two best teams in Spain without becoming bad, which is exactly what a
prior is for. Early in a season, with nothing played, the value prior is all
there is and the answer is what it was before.
"""

from . import form as form_src

# How many finished gameweeks to read. Enough to mean something, recent enough
# that a September collapse does not still be judging a club in December.
WEEKS = 6

# How much the table is trusted against the squad-value prior, once there is a
# full sample. Results win, because they are the thing being asked about.
RESULTS_WEIGHT = 0.65

# Sample at which results carry their full weight; below it they carry less,
# proportionally.
FULL_SAMPLE = 4

_FINISHED = {"finished", "ended", "played", "7", 7}


def _team_id(side):
    """A fixture's side, whether it arrived as an id or as an object."""
    if isinstance(side, dict):
        for key in ("id", "teamId", "team_id"):
            if side.get(key) is not None:
                return str(side[key])
        return None
    return str(side) if side is not None else None


def _score(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _is_finished(row):
    """A match with two scores is played, whatever the state field calls it."""
    state = row.get("matchState")
    if state is not None and str(state).lower() in {str(s).lower()
                                                    for s in _FINISHED}:
        return True
    return (_score(row.get("localScore")) is not None
            and _score(row.get("visitorScore")) is not None)


def tally(weeks):
    """{team_id: {points, played, gf, ga}} from a list of gameweek fixture lists."""
    out = {}

    def _slot(tid):
        return out.setdefault(tid, {"points": 0, "played": 0, "gf": 0, "ga": 0})

    for rows in weeks or []:
        for row in rows or []:
            if not isinstance(row, dict) or not _is_finished(row):
                continue
            home, away = _team_id(row.get("local")), _team_id(row.get("visitor"))
            hs, as_ = _score(row.get("localScore")), _score(row.get("visitorScore"))
            if not home or not away or hs is None or as_ is None:
                continue
            h, a = _slot(home), _slot(away)
            h["played"] += 1
            a["played"] += 1
            h["gf"] += hs
            h["ga"] += as_
            a["gf"] += as_
            a["ga"] += hs
            if hs > as_:
                h["points"] += 3
            elif as_ > hs:
                a["points"] += 3
            else:
                h["points"] += 1
                a["points"] += 1
    return out


def strength_from(table):
    """{team_id: 0..1} — 1 is the strongest club in the sample.

    Points per game carries it, with goal difference per game as the tiebreak
    that separates a club grinding out 1-0s from one winning 4-0. Both are
    normalised across the league rather than against an absolute, because what
    matters is who is harder than whom.
    """
    scored = {}
    for tid, row in (table or {}).items():
        played = row.get("played") or 0
        if played <= 0:
            continue
        ppg = row["points"] / played
        gdpg = (row["gf"] - row["ga"]) / played
        scored[tid] = ppg + 0.25 * gdpg
    if not scored:
        return {}
    lo, hi = min(scored.values()), max(scored.values())
    if hi <= lo:
        return dict.fromkeys(scored, 0.5)
    return {tid: round((v - lo) / (hi - lo), 3) for tid, v in scored.items()}


def blend(results, prior, played_by_team=None):
    """Results tilted onto the squad-value prior, by how much has been played.

    A club with one game played is mostly its prior; with four or more it is
    mostly its record. A club the table does not know at all keeps its prior
    untouched, which is what happens in week one for everybody.
    """
    out = dict(prior or {})
    for tid, value in (results or {}).items():
        played = (played_by_team or {}).get(tid, FULL_SAMPLE)
        trust = RESULTS_WEIGHT * min(1.0, played / float(FULL_SAMPLE))
        base = out.get(tid, 0.5)
        out[tid] = round(base * (1 - trust) + value * trust, 3)
    return out


def team_form(client, current_week, weeks=WEEKS):
    """(strength 0..1 by team, matches played by team). {} when nothing is read."""
    try:
        first = max(1, int(current_week) - weeks)
    except (TypeError, ValueError):
        return {}, {}
    fetched = []
    for w in range(int(current_week) - 1, first - 1, -1):
        try:
            payload = form_src.raw_week(client, w)
        except Exception:                        # noqa: BLE001
            continue
        if payload:
            fetched.append(payload)
    table = tally(fetched)
    played = {tid: row.get("played", 0) for tid, row in table.items()}
    return strength_from(table), played

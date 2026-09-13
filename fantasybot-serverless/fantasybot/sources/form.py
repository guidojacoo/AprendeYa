"""Recent form and rotation risk, from LaLiga's own per-gameweek stats.

Two things the bot could not see, both of them decisive.

FORM. `averagePoints` is a season average, which by November is mostly history:
a player who scored 12, 11 and 9 in the last three weeks and a player who scored
those in September have the same average and are not the same bet. The season
rate is still the honest prior — three weeks is a small sample and chasing it is
how you sell a good player after two quiet games — so recent form TILTS the rate
rather than replacing it.

MINUTES. The probability of starting comes from a scrape of futbolfantasy, and
that scrape is the single most fragile input in the whole system: when their HTML
changes it returns nothing and everything silently degrades. How often a player
ACTUALLY appeared over the last few gameweeks is the same question answered by
LaLiga's own API, which makes it both a cross-check and a fallback. A player the
scrape calls an 80% starter who has played one of the last four is a rotation
risk the scrape cannot see; a scrape that has broken entirely leaves this
signal standing.

The payload shape is not documented and cannot be confirmed from here, so the
parser recognises the plausible layouts rather than one of them, and an
unrecognised payload RECORDS ITS SHAPE instead of silently returning nothing.
A source that fails loudly gets fixed; one that returns {} looks like a quiet
week forever.
"""

from .. import cache, config
from ..storage import get_storage, to_iso, utcnow

CACHE_TTL = 21600          # 6h: per-gameweek stats change once a week
WEEKS = 4                  # how many gameweeks back "recent" means

_ID_KEYS = ("playerId", "player_id", "id", "playerMasterId")
_POINT_KEYS = ("points", "totalPoints", "weekPoints", "score", "totalPoint")
_MINUTE_KEYS = ("mins_played", "minutesPlayed", "minutes", "min")


def _first(d, keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _week_key(k):
    """Sort gameweek keys numerically: "10" must come after "9", not before."""
    try:
        return (0, int(str(k).strip()))
    except (TypeError, ValueError):
        return (1, str(k))


def _entry(row):
    """One player's line for a gameweek: (id, points, minutes) or None."""
    if not isinstance(row, dict):
        return None
    pid = _first(row, _ID_KEYS)
    if pid is None:
        pid = _first(row.get("playerMaster") or {}, _ID_KEYS)
    if pid is None:
        return None
    stats = row.get("stats") if isinstance(row.get("stats"), dict) else row
    pts = _num(_first(stats, _POINT_KEYS))
    if pts is None:
        pts = _num(_first(row, _POINT_KEYS))
    mins = _num(_first(stats, _MINUTE_KEYS))
    if mins is None:
        mins = _num(_first(row, _MINUTE_KEYS))
    if pts is None and mins is None:
        return None
    return str(pid), pts, mins


def _rows(payload):
    """The per-player list, wherever this payload happens to keep it."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("players", "data", "items", "results", "stats"):
            got = payload.get(key)
            if isinstance(got, list):
                return got
    return []


def parse_week(payload):
    """{player_id: {"points": float|None, "minutes": float|None}} for one week."""
    out = {}
    for row in _rows(payload):
        got = _entry(row)
        if got is None:
            continue
        pid, pts, mins = got
        out[pid] = {"points": pts, "minutes": mins}
    return out


def _record_shape(week, payload):
    """Bank what an unrecognised payload looks like, so it can be fixed.

    Keys only, and only the outer two levels: enough to write the parser
    against, and nothing that could turn a diagnostic into a data dump.
    """
    try:
        shape = {"type": type(payload).__name__}
        if isinstance(payload, dict):
            shape["keys"] = sorted(payload.keys())[:20]
        rows = _rows(payload)
        shape["rows"] = len(rows)
        if rows and isinstance(rows[0], dict):
            shape["row_keys"] = sorted(rows[0].keys())[:25]
            inner = rows[0].get("stats")
            if isinstance(inner, dict):
                shape["stats_keys"] = sorted(inner.keys())[:25]
        get_storage().put_doc("week_stats_shape",
                              {"week": week, "at": to_iso(utcnow()),
                               "shape": shape})
    except Exception:                            # noqa: BLE001
        pass


# `/stats/week/{n}` is NOT per-player stats. The first live run recorded what it
# actually returns: ten rows of {date, id, local, localScore, matchState,
# visitor, visitorScore} — the week's FIXTURES. Useful, and not this.
#
# So the per-player history comes from `all_players()`, the competition-wide read
# the review already makes once a day to bank market values. Whether those rows
# carry a per-week breakdown is the one thing left to confirm, and the same
# recorder answers it: `player_shape` banks the keys of one row so the next live
# run says what is there, instead of another round of guessing from here.
def week(client, week_number):
    """One gameweek's stats, cached. {} when the shape is not recognised."""
    def _fetch():
        payload = client.week_stats(week_number)
        parsed = parse_week(payload)
        if not parsed:
            _record_shape(week_number, payload)
        return parsed
    return cache.cached(f"week_stats_{week_number}", CACHE_TTL, _fetch,
                        default={}) or {}


# `weekPoints` is the one the live payload actually carries, next to the season
# total in `points` and the average in `averagePoints`. The row recorder found
# it: [averagePoints, id, image, lastSeasonPoints, marketValue, nickname,
# playerStatus, points, positionId, teamId, weekPoints].
_STAT_LIST_KEYS = ("weekPoints", "playerStats", "stats", "weeks", "weekStats")


def from_player_rows(players):
    """{player_id: [most recent first]} from an `all_players()` payload.

    A row that carries its own per-gameweek breakdown gives us form and minutes
    for the whole competition in a read we already make. A row that does not
    contributes nothing, which is the honest answer rather than a zero.
    """
    out = {}
    for row in players or []:
        pid = row.get("id")
        if pid is None:
            continue
        series = None
        for key in _STAT_LIST_KEYS:
            got = row.get(key)
            if isinstance(got, list) and got:
                series = got
                break
            if isinstance(got, dict) and got:
                # Keyed by gameweek ({"1": 4, "2": 11}), so the ORDER has to come
                # from the keys rather than from insertion: a JSON object makes
                # no promise about that, and reading it in the wrong order turns
                # a player finding form into one losing it.
                series = [got[k] for k in sorted(got, key=_week_key)]
                break
        if not series:
            continue
        lines = []
        for item in series:
            if isinstance(item, dict):
                pts = _num(_first(item, _POINT_KEYS))
                mins = _num(_first(item, _MINUTE_KEYS))
                stats = item.get("stats")
                if isinstance(stats, dict):
                    pts = pts if pts is not None else _num(_first(stats, _POINT_KEYS))
                    mins = mins if mins is not None else _num(_first(stats, _MINUTE_KEYS))
            else:
                pts, mins = _num(item), None
            if pts is None and mins is None:
                continue
            lines.append({"points": pts, "minutes": mins})
        if lines:
            out[str(pid)] = list(reversed(lines))[:WEEKS]
    return out


def record_player_shape(players):
    """Bank the keys of one `all_players()` row, so the parser can be aimed.

    Keys only, two levels deep. It runs once and only while the history is still
    empty — a diagnostic that keeps firing after it has been answered is just
    another thing writing to storage every hour.
    """
    try:
        row = (players or [None])[0]
        if not isinstance(row, dict):
            return
        shape = {"row_keys": sorted(row.keys())[:40]}
        for key in _STAT_LIST_KEYS:
            got = row.get(key)
            if isinstance(got, list) and got:
                shape[f"{key}[0]"] = (sorted(got[0].keys())[:30]
                                      if isinstance(got[0], dict)
                                      else type(got[0]).__name__)
                shape[f"{key}_len"] = len(got)
            elif isinstance(got, dict) and got:
                shape[f"{key}_keys"] = sorted(map(str, got))[:20]
            elif got is not None:
                shape[key] = type(got).__name__
        get_storage().put_doc("all_players_shape",
                              {"at": to_iso(utcnow()), "shape": shape})
    except Exception:                            # noqa: BLE001
        pass


def history(client, current_week, weeks=WEEKS):
    """{player_id: [most recent first]} of {"points", "minutes"} per gameweek.

    Weeks that fail or come back unrecognised are skipped, not zero-filled: a
    missing gameweek is no evidence, and counting it as a blank is how a data
    outage turns into "everybody lost form".
    """
    out = {}
    try:
        first = max(1, int(current_week) - weeks)
    except (TypeError, ValueError):
        return out
    for w in range(int(current_week) - 1, first - 1, -1):
        try:
            got = week(client, w)
        except Exception:                        # noqa: BLE001
            continue
        for pid, line in got.items():
            out.setdefault(pid, []).append(line)
    return out


def describe(index):
    """Whether this source is actually working, for the sources check.

    `minutes` is reported separately because the two halves can arrive apart:
    the live payload carries `weekPoints` and no minutes field, so form works
    and the rotation cross-check does not. A player who did not play scores
    zero, so his absence still shows up in the form factor — but a zero is not
    the same statement as "did not take the field", and reporting the two as one
    number would hide which of them we actually have.
    """
    index = index or {}
    players = len(index)
    with_minutes = sum(1 for v in index.values()
                       if any(h.get("minutes") is not None for h in v))
    return {"ok": players > 0, "players": players,
            "weeks": max((len(v) for v in index.values()), default=0),
            "minutes": with_minutes}


__all__ = ["history", "week", "parse_week", "describe", "WEEKS",
           "config"]

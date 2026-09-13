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
    """Whether this source is actually working, for the sources check."""
    players = len(index or {})
    return {"ok": players > 0, "players": players,
            "weeks": max((len(v) for v in (index or {}).values()), default=0)}


__all__ = ["history", "week", "parse_week", "describe", "WEEKS",
           "config"]

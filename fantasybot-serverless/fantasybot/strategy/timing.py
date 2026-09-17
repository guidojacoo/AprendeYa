"""Is it worth waiting for him?

A buyout clause opens at an instant LaLiga decides, and "in five days" is not a
delay the bot chose — it cannot pay one second earlier. So the question is never
"wait or not". It is:

    take the best player available TODAY, or hold the money for the better one
    who becomes reachable on Friday?

Nobody was asking it. A clause was queued for its unlock and that was the end of
the thought, even when a gameweek fell in between and the squad would field a
worse eleven through it. Points scored in a gameweek you played badly are not
refunded when the signing finally lands.

The comparison is a small piece of arithmetic over a planning horizon:

    waiting   = gain_later  x  (gameweeks in the horizon - gameweeks missed)
    acting    = gain_now     x  gameweeks in the horizon

Whichever is larger. A clause that is one gameweek away and clearly better still
wins; one that is three gameweeks away and barely better does not.
"""

from datetime import datetime, timezone

# How far ahead to compare. Long enough that a genuinely better player earns his
# wait, short enough that it is a decision about this month rather than a
# prophecy about May.
HORIZON_WEEKS = 5

# Days between gameweeks. LaLiga plays weekly outside cup weeks; using the real
# calendar here would be more precise and is not worth an extra call, because the
# answer only changes when the two options are nearly equal anyway.
DAYS_PER_WEEK = 7.0


def _parse(iso):
    if isinstance(iso, datetime):
        return iso if iso.tzinfo else iso.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def days_until(iso, now=None):
    """Days from now until `iso`, or None if it cannot be read."""
    dt = _parse(iso)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (dt - now).total_seconds() / 86400.0)


def gameweeks_missed(unlock_iso, next_kickoff_iso=None, now=None):
    """How many gameweeks start before we could pay this clause.

    Counted from the NEXT kickoff, because that is the first one the signing
    could have affected. Without a kickoff to anchor on it falls back to whole
    weeks of waiting, which is the same answer in an ordinary month.
    """
    wait = days_until(unlock_iso, now)
    if wait is None:
        return 0
    first = days_until(next_kickoff_iso, now) if next_kickoff_iso else None
    if first is None:
        return int(wait // DAYS_PER_WEEK)
    if wait <= first:
        return 0            # reachable before the gameweek starts: nothing lost
    return 1 + int((wait - first) // DAYS_PER_WEEK)


def worth_waiting(gain_later, gain_now, unlock_iso, next_kickoff_iso=None,
                  horizon=HORIZON_WEEKS, now=None):
    """Whether holding out for the clause beats signing what is available today.

    Returns a dict rather than a bool, because the caller has to be able to say
    WHY on a page: the two totals, the gameweeks given up, and the verdict.
    """
    later = float(gain_later or 0)
    now_gain = float(gain_now or 0)
    missed = gameweeks_missed(unlock_iso, next_kickoff_iso, now)
    playable = max(0, horizon - missed)
    waiting_total = round(later * playable, 2)
    acting_total = round(now_gain * horizon, 2)
    return {
        "gameweeks_missed": missed,
        "horizon": horizon,
        "waiting_total": waiting_total,
        "acting_total": acting_total,
        "wait": waiting_total >= acting_total,
        "cost_of_waiting": round(later * missed, 2),
        "why": _why(later, now_gain, missed, waiting_total, acting_total),
    }


def _why(later, now_gain, missed, waiting_total, acting_total):
    if missed == 0:
        return ("Su cláusula abre antes de la próxima jornada: no me pierdo "
                "ninguna esperándolo.")
    if waiting_total >= acting_total:
        return (f"Me pierdo {missed} jornada(s) esperándolo, pero aun así suma "
                f"{waiting_total} pts en el horizonte contra {acting_total} "
                f"del mejor disponible hoy.")
    return (f"Esperarlo cuesta {missed} jornada(s): {waiting_total} pts contra "
            f"{acting_total} si ficho ahora al mejor que puedo. No lo espero.")

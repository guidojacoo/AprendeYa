"""SHIELD advisor (blindaje): which of OUR players is most exposed to a rival's buyout.

The defensive mirror of `agent.clause_targets` (which hunts OTHER managers' players to buy
via their clause). Here we look over OUR OWN squad and flag the player a rich rival could
snatch by paying his buyout clause — the VALUABLE, UNSHIELDED one whose clause is both
within a rival's reach AND already open (or opening soon). Shielding him is FREE (a
rewarded-ad flow), so protecting the single most exposed asset is a clean defensive win.

Pure + testable: the caller passes in `rivals_max_money` (the reach signal) and, for
determinism, `now`. No network here.
"""

from datetime import datetime, timedelta, timezone

from ..matching import POS

# A player worth shielding at all: below this, losing him to a clause barely hurts, so we
# don't bother (keeps the agent from "protecting" near-worthless bench filler).
MIN_VALUE = 1_000_000

# Official statuses that mean the player WON'T take the field. The shield is one-per-run,
# so don't spend it protecting someone who's out — shield a healthy asset instead (a rival
# is far likelier to clause a fit player, and it's what the user expects). "doubtful" is
# kept eligible: he may still play. (This is why the bot used to shield an injured Isi.)
UNAVAILABLE_STATUS = {"injured", "suspended", "out_of_league"}

# Shield timing. A rival would clause your player to field (and deny) him right before the
# gameweek — but clauses are LOCKED in the final 24h before it, so the real theft window is
# 72h..24h before kickoff. The blindaje lasts 48h, so applying it once the gameweek is
# within 72h (48h shield + 24h lockout) covers that whole window down to the lockout. A
# shield applied earlier just lapses before it protects anything.
SHIELD_DURATION_HOURS = 48   # a blindaje lasts 48h
CLAUSE_LOCKOUT_HOURS = 24    # clauses can't be paid in the final 24h before the gameweek
SHIELD_LEAD_HOURS = SHIELD_DURATION_HOURS + CLAUSE_LOCKOUT_HOURS  # 72: when to start shielding


def _parse(iso):
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    # Normalise to aware so comparisons with an aware `now` never raise.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clause_unlocked(unlock_iso, now):
    """True when the clause is ALREADY payable (unlocked) right now.

    A LOCKED clause means the player is temporarily protected, and the shield API rejects
    him with 400 "Player team protected" (confirmed live) — you can only shield a player
    who is CURRENTLY vulnerable. Missing / unparseable lock time -> treat as unlocked (an
    absent lock means nothing stops a rival, exactly the case we want to shield).
    """
    if not unlock_iso:
        return True
    dt = _parse(unlock_iso)
    return dt is None or dt <= now


def shield_candidate(team, rivals_max_money, now=None, min_value=MIN_VALUE,
                     gameweek_kickoff=None):
    """The single most clause-vulnerable valuable player worth shielding, or None.

    A player qualifies when ALL hold:
      * NOT already shielded (`isShielded` is false),
      * VALUABLE — `marketValue` >= `min_value`,
      * his `buyoutClause` is within a rich rival's reach (<= `rivals_max_money`), and
      * his clause is ALREADY UNLOCKED (`buyoutClauseLockedEndTime` in the past/absent) —
        a locked clause is already protected and the shield API rejects it.
    Among the qualifiers we return the MOST valuable (the one it hurts most to lose).
    `rivals_max_money` is the reach signal the caller supplies (e.g. the richest rival's
    cash / squad value); `now` defaults to the current UTC time.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    # Only shield once the gameweek is close enough that the 48h blindaje still covers the
    # theft window (72h..24h before kickoff). Too early -> the shield would lapse before it
    # matters, so hold off. Unknown kickoff -> don't gate (protect regardless).
    if gameweek_kickoff:
        gk = _parse(gameweek_kickoff)
        if gk is not None and gk - now > timedelta(hours=SHIELD_LEAD_HOURS):
            return None
    reach = rivals_max_money or 0
    best = None
    for p in team.get("players", []):
        if p.get("isShielded"):
            continue  # already protected
        pm = p.get("playerMaster") or {}
        if (pm.get("playerStatus") or "").lower() in UNAVAILABLE_STATUS:
            continue  # injured/suspended/gone -> won't play; don't waste the shield on him
        value = pm.get("marketValue") or 0
        if value < min_value:
            continue  # not valuable enough to bother shielding
        clause = p.get("buyoutClause") or 0
        if not clause or clause > reach:
            continue  # no clause, or beyond any rival's reach -> not vulnerable
        unlock = p.get("buyoutClauseLockedEndTime")
        if not _clause_unlocked(unlock, now):
            continue  # clause still locked -> already protected, can't/needn't shield
        cand = {
            "nombre": pm.get("nickname") or pm.get("name"),
            "player_id": pm.get("id"),
            "player_team_id": p.get("playerTeamId") or pm.get("id"),
            "pos": POS.get(pm.get("positionId"), "?"),
            "value": value,
            "clause": clause,
            "unlock": unlock,
            "reason": "clause within a rival's reach and unshielded",
        }
        if best is None or value > best["value"]:
            best = cand
    return best

"""Clausulazos on every rival squad, not only on what rivals happen to list.

A buyout clause can be paid on ANY player in a rival's squad once his lock has
run out. The bot only ever looked for clause targets among the market rows —
the handful of players a rival had put up for sale — so the whole of every
rival squad, which is where the clausulazos in this league actually happen,
was invisible to it. A bot with clauses switched on that never sees the
players it could take is a bot that never takes one.

So the squads are read (cached: they change a few times a day, not a minute),
every player whose clause we could ever pay becomes a candidate, and the
candidates are valued exactly the way a market signing is — what he adds to
OUR eleven per euro of clause.

Three things decide WHEN a clause can be paid, and each is on the row:

  * his lock   `buyoutClauseLockedEndTime` — 14 days after he was signed
  * a shield   `isShielded` / `shieldedEndDate` — 24h nobody can pay it
  * the window LaLiga shuts clauses for the 24h before a gameweek's first
               kick-off (see tick.clause_window) — the planner applies that one
"""

from datetime import datetime, timezone

from ..matching import match_name, num, position_of
from ..storage import get_storage, parse_iso, to_iso, utcnow

# How long a read of the rival squads is good for. Squads change when somebody
# signs or sells, a handful of times a day; clauses and locks are on the rows.
SQUADS_TTL = 1800

# How many candidates get the full valuation (one lineup solve each, times
# three). Chosen by a cheap proxy first, so the expensive part stays bounded
# however large the league.
MAX_EVALUATED = 30


def _slim(row):
    """A squad row with only what a clause decision reads."""
    pm = dict(row.get("playerMaster") or {})
    pm.pop("images", None)
    return {"playerMaster": pm,
            "playerTeamId": row.get("playerTeamId"),
            "buyoutClause": row.get("buyoutClause"),
            "buyoutClauseLockedEndTime": row.get("buyoutClauseLockedEndTime"),
            "isShielded": row.get("isShielded"),
            "shieldedEndDate": row.get("shieldedEndDate")}


def _usable_rows(rows):
    """Whether a league_teams row already carries the squad with its clauses."""
    return bool(rows) and all(
        isinstance(r, dict) and r.get("playerTeamId") is not None
        and "buyoutClause" in r for r in rows)


def fetch_rival_squads(client, league_id, my_team_id, ttl=SQUADS_TTL,
                       now=None, force=False):
    """[{team_id, manager, players: [slim rows]}] for every rival, cached.

    One read of the league, then one squad read per rival unless the league
    read already carried the squads. Any single rival that fails to read is
    skipped rather than failing the lot: a clause on four squads beats none.
    """
    store = get_storage()
    now = now or utcnow()
    cached = store.get_doc("rival_squads", None)
    if not force and isinstance(cached, dict):
        at = parse_iso(cached.get("at"))
        if at is not None and (now - at).total_seconds() < ttl:
            return cached.get("teams") or []
    teams = client.league_teams(league_id) or []
    out = []
    for t in teams if isinstance(teams, list) else []:
        tid = str(t.get("id") or "")
        if not tid or tid == str(my_team_id) or t.get("teamMoney") is not None:
            continue          # our own team: the only one whose money we see
        manager = ((t.get("manager") or {}).get("managerName")
                   or t.get("managerName"))
        rows = t.get("players") or []
        if not _usable_rows(rows):
            try:
                rows = (client.team(league_id, tid) or {}).get("players") or []
            except Exception:                    # noqa: BLE001
                continue
        out.append({"team_id": tid, "manager": manager,
                    "players": [_slim(r) for r in rows if isinstance(r, dict)]})
    store.put_doc("rival_squads", {"at": to_iso(now), "teams": out})
    return out


def payable_from(row, now=None):
    """The first instant his clause can be paid, ignoring the gameweek window.

    The later of his lock and his shield; now, if neither is running.
    """
    now = now or datetime.now(timezone.utc)
    at = now
    lock = parse_iso(row.get("buyoutClauseLockedEndTime"))
    if lock is not None and lock > at:
        at = lock
    if row.get("isShielded"):
        shield = parse_iso(row.get("shieldedEndDate"))
        if shield is not None and shield > at:
            at = shield
    return at


def candidates(teams, owned_ids, prob_index=None, max_clause=None, now=None):
    """Every rival player whose clause we could ever pay, cheapest proxy first.

    `max_clause` bounds what is worth looking at — the most we could raise,
    cash plus what the bench would sell for. Anyone above it is out of reach
    whatever he scores.
    """
    owned = {str(i) for i in owned_ids or ()}
    out = []
    for team in teams or []:
        for row in team.get("players") or []:
            pm = row.get("playerMaster") or {}
            pid = str(pm.get("id"))
            if not pm or pid in owned:
                continue
            clause = int(num(row.get("buyoutClause")))
            if clause <= 0:
                continue
            if max_clause is not None and clause > max_clause:
                continue
            pos = position_of(pm)
            if pos is None or pos == "ENT":
                continue
            info = match_name(pm.get("nickname", ""), pm.get("name", ""),
                              prob_index) if prob_index else None
            prob = info.get("prob") if info else None
            avg = num(pm.get("averagePoints"), 0) or 0
            out.append({
                "nombre": pm.get("nickname") or pm.get("name"),
                "player_id": pm.get("id"),
                "player_team_id": row.get("playerTeamId"),
                "owner_team_id": team.get("team_id"),
                "owner": team.get("manager"),
                "pos": pos,
                "clause": clause,
                "value": int(num(pm.get("marketValue"))),
                "unlock": to_iso(payable_from(row, now)),
                # What the unlock is made of, as LaLiga sent it. `unlock` moves
                # with the clock once he is payable; this does not, so a plan
                # keyed on it is the same plan on the next review.
                "lock_key": "|".join(str(x) for x in (
                    row.get("buyoutClauseLockedEndTime"),
                    row.get("shieldedEndDate") if row.get("isShielded") else None)),
                "shielded": bool(row.get("isShielded")),
                "prob": prob,
                "card": pm,
                # The cheap proxy that picks who gets the full valuation.
                "_proxy": avg / max(1.0, clause / 1_000_000.0),
            })
    out.sort(key=lambda c: -c["_proxy"])
    return out

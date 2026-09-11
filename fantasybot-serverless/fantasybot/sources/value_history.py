"""Daily snapshots of LaLiga's OFFICIAL market values (from api.all_players()).

futbolfantasy (scraped) is the agent's value-trend source today — fragile, and it costs an
IP footprint. LaLiga returns EVERY player's current marketValue + playerStatus in ONE authed
GET (`all_players()`). Banking those values once a day lets us compute trends OURSELVES
(today's value vs N days ago) from our own history, no scraping involved.

This module is the collection + a pure trend helper:
  - snapshot_from_players / trend are PURE + deterministic: values and date strings go in,
    no datetime.now() inside, so they're fully unit-testable.
  - The only I/O is reading/writing snapshot JSON files ("YYYY-MM-DD.json") under a dir.
  - The caller passes api.all_players() output in; no network here.

`agent.review()` collects a snapshot on every run (best-effort, never blocks the review),
so the history starts accumulating from day one. `strategy.flip` reads it back as an
"oficial_trend_pct" field on each opportunity — informational, alongside the futbolfantasy
trend, not yet driving the buy/sell math (it earns that once enough days have banked).
"""

import glob
import json
import os
from datetime import date, timedelta

KEEP_DAYS = 40  # prune snapshots older than this (days) whenever we save a new one


def snapshot_from_players(players):
    """Compact api.all_players() output into {str(id): {"v": int(marketValue), "s": playerStatus}}.

    Skips entries without an id or without a usable value. Coerces marketValue (which the
    API sends as a STRING) to int. PURE — does not mutate the input."""
    snap = {}
    for p in (players or []):
        pid = p.get("id")
        if pid is None or pid == "":
            continue
        raw = p.get("marketValue")
        if raw is None or raw == "":
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        snap[str(pid)] = {"v": value, "s": p.get("playerStatus")}
    return snap


def _snapshot_path(dir_path, day_iso):
    return os.path.join(dir_path, f"{day_iso}.json")


def _parse_day(name):
    """Parse a snapshot filename's date ('YYYY-MM-DD.json' -> date), or None if it doesn't
    match the snapshot pattern. Guards prune so it never touches non-snapshot files."""
    if not name.endswith(".json"):
        return None
    stem = name[:-len(".json")]
    try:
        return date.fromisoformat(stem)
    except ValueError:
        return None


def save_snapshot(dir_path, day_iso, snapshot):
    """Write {dir_path}/{day_iso}.json (creating the dir if needed) and prune snapshots
    older than KEEP_DAYS. Returns the path written."""
    os.makedirs(dir_path, exist_ok=True)
    path = _snapshot_path(dir_path, day_iso)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False)
    _prune(dir_path, day_iso)
    return path


def _prune(dir_path, day_iso):
    """Delete snapshot files whose date is more than KEEP_DAYS before day_iso. Only touches
    files matching the 'YYYY-MM-DD.json' pattern — never anything else in the dir."""
    try:
        today = date.fromisoformat(day_iso)
    except ValueError:
        return
    cutoff = today - timedelta(days=KEEP_DAYS)
    for path in glob.glob(os.path.join(dir_path, "*.json")):
        d = _parse_day(os.path.basename(path))
        if d is not None and d < cutoff:
            try:
                os.remove(path)
            except OSError:
                pass


def load_snapshot(dir_path, day_iso):
    """Load one day's snapshot dict, or {} if missing/unreadable."""
    path = _snapshot_path(dir_path, day_iso)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def shift_day(day_iso, days_back):
    return (date.fromisoformat(day_iso) - timedelta(days=days_back)).isoformat()


_shift_day = shift_day   # the original private name, kept for callers that use it


def trend_between(today_snapshot, prev_snapshot, player_id):
    """The trend maths on two snapshots already in hand — FULLY pure, no I/O.

    Split out so a database-backed store can hand in the two days it fetched and
    reuse exactly the same arithmetic the file-backed one uses. `trend()` below is
    now just "load two days, then call this".
    """
    pid = str(player_id)
    today = today_snapshot or {}
    prev = prev_snapshot or {}
    if pid not in today or pid not in prev:
        return None
    today_v = today[pid].get("v")
    prev_v = prev[pid].get("v")
    if today_v is None or prev_v is None or prev_v == 0:
        return None
    delta = today_v - prev_v
    return {"value": today_v, "prev": prev_v, "delta": delta,
            "pct": round(100 * delta / prev_v, 1)}


def trend(dir_path, player_id, today_iso, days_back, now_snapshot=None):
    """Compare a player's value today vs days_back days earlier.

    Returns {"value", "prev", "delta", "pct"} or None if either snapshot/player is missing
    or the previous value is 0. PURE-ish: reads snapshot files but takes date strings in
    (no datetime.now()). `now_snapshot` lets the caller pass today's freshly-fetched values
    instead of reloading them from disk."""
    today = now_snapshot if now_snapshot is not None else load_snapshot(dir_path, today_iso)
    prev = load_snapshot(dir_path, shift_day(today_iso, days_back))
    return trend_between(today, prev, player_id)

#!/usr/bin/env python3
"""Copy an existing local `.state/` (and tokens.json) into Supabase.

Read-only on the source. Nothing under `.state/` is touched, renamed or deleted —
verify the migration first, keep the folder as your rollback, and remove it by
hand when you are satisfied.

    python scripts/migrate-state.py --dry-run    # show what would move
    python scripts/migrate-state.py              # move it
    python scripts/migrate-state.py --tokens     # include tokens.json

Tokens are opt-in because they are the one genuinely sensitive thing here: a
refresh_token is a 90-day key to your LaLiga account. Passing --tokens sends it
to YOUR Supabase project over TLS, where RLS keeps it away from the anon key.
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fantasybot import config                                # noqa: E402
from fantasybot.storage import StorageError                  # noqa: E402
from fantasybot.storage.local import LocalStorage, STATE_DIR  # noqa: E402
from fantasybot.storage.supabase import SupabaseStorage      # noqa: E402

# The documents worth carrying over. Deliberately not everything: `.cache/` is
# regenerable scrape data and `run.current` is a 20-minute grouping marker, so
# copying either would just import staleness.
DOCUMENTS = ["snapshot", "tasks", "reminders", "bids", "bid_plan",
             "rivals_snapshot", "activity_history", "squad_history",
             "players_cache", "settings"]


def _local_docs():
    found = {}
    for name in DOCUMENTS:
        value = LocalStorage().get_doc(name, None)
        if value not in (None, {}, []):
            found[name] = value
    return found


def _value_history():
    out = {}
    vh = pathlib.Path(STATE_DIR) / "value_history"
    for path in sorted(vh.glob("*.json")) if vh.is_dir() else []:
        try:
            out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    # The newer flat layout, written by the storage-backed local backend.
    for path in sorted(pathlib.Path(STATE_DIR).glob("value_history_*.json")):
        day = path.stem[len("value_history_"):]
        try:
            out[day] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return out


def _events():
    path = pathlib.Path(STATE_DIR) / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[-500:]:
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tokens", action="store_true",
                    help="also migrate tokens.json (your LaLiga session)")
    ap.add_argument("--events", type=int, default=200,
                    help="how many recent events to carry over (0 = none)")
    args = ap.parse_args()

    if not pathlib.Path(STATE_DIR).is_dir():
        print(f"No local state at {STATE_DIR} — nothing to migrate.")
        return 0

    docs = _local_docs()
    history = _value_history()
    events = _events()[-args.events:] if args.events else []
    tokens_path = pathlib.Path(config.TOKENS_PATH)
    tokens = None
    if args.tokens and tokens_path.exists():
        tokens = json.loads(tokens_path.read_text(encoding="utf-8"))

    print(f"Source : {STATE_DIR}")
    for name, value in docs.items():
        size = len(value) if isinstance(value, (list, dict)) else 1
        print(f"  doc    {name:<20} {size} entr{'y' if size == 1 else 'ies'}")
    for day in sorted(history):
        print(f"  values {day:<20} {len(history[day])} players")
    if events:
        print(f"  events {'events.jsonl':<20} {len(events)} lines")
    if tokens:
        print(f"  tokens {'tokens.json':<20} "
              f"refresh_token {'present' if tokens.get('refresh_token') else 'MISSING'}")
    if not (docs or history or events or tokens):
        print("Nothing to migrate.")
        return 0

    if args.dry_run:
        print("\n--dry-run: nothing was written.")
        return 0

    try:
        remote = SupabaseStorage()
    except StorageError as e:
        print(f"\n[X] {e}")
        return 2

    print(f"\nTarget : {config.SUPABASE_URL}  (scope {remote.scope})")
    for name, value in docs.items():
        remote.put_doc(name, value)
        print(f"  -> agent_state/{name}")
    for day, values in history.items():
        remote.put_market_snapshot(day, values)
        print(f"  -> market_snapshots/{day}")
    for ev in events:
        remote.emit_event(ev)
    if events:
        print(f"  -> events ({len(events)})")
    if tokens:
        remote.put_doc("tokens", tokens)
        print("  -> agent_state/tokens")

    print("\nDone. `.state/` was NOT modified — verify with:\n"
          "  FANTASYBOT_STORAGE=supabase python -m fantasybot tasks\n"
          "and delete the folder yourself once you're happy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

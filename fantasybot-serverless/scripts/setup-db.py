#!/usr/bin/env python3
"""Create (or verify) the Supabase schema.

Supabase's REST API cannot run DDL — that is a deliberate restriction on their
side, not an oversight — so this script does the honest thing: it VERIFIES the
schema over REST and, when something is missing, prints the exact SQL to paste
into the SQL Editor. No hidden magic, no half-applied migration.

    python scripts/setup-db.py            # check what exists
    python scripts/setup-db.py --print    # print the SQL to run
    python scripts/setup-db.py --seed     # write default settings rows
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fantasybot import config                                 # noqa: E402
from fantasybot.storage import StorageError                   # noqa: E402
from fantasybot.storage.supabase import SupabaseStorage       # noqa: E402

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "supabase" / "migrations"
TABLES = ["agent_state", "cache_entries", "events", "market_snapshots",
          "scheduled_actions", "executions", "locks", "settings",
          "agent_decisions"]
DEFAULT_SETTINGS = {
    "review_interval": config.REVIEW_INTERVAL,
    "llm_interval": config.LLM_INTERVAL,
    "bid_mode": "snipe",
    "auto_execute": config.AUTO_EXECUTE,
}


def sql():
    return "\n\n".join(p.read_text(encoding="utf-8")
                       for p in sorted(MIGRATIONS.glob("*.sql")))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", dest="dump", action="store_true",
                    help="print the migration SQL and exit")
    ap.add_argument("--seed", action="store_true",
                    help="write the default settings rows")
    args = ap.parse_args()

    if args.dump:
        print(sql())
        return 0

    try:
        store = SupabaseStorage()
    except StorageError as e:
        print(f"[X] {e}")
        return 2

    print(f"Project : {store.url}")
    print(f"Endpoint: {store.rest}")
    print(f"Scope   : {store.scope}\n")

    missing = []
    for table in TABLES:
        try:
            store._request("GET", table, params={"select": "*", "limit": "1"})
            print(f"  [ok]      {table}")
        except StorageError as e:
            missing.append(table)
            detail = str(e)
            hint = "missing" if "does not exist" in detail or "PGRST205" in detail else detail[:70]
            print(f"  [MISSING] {table}  ({hint})")

    if missing:
        if len(missing) == len(TABLES):
            print("\nEVERY table is missing, which usually means one of two things:\n"
                  "  a) the migration has not been run yet (most likely), or\n"
                  "  b) SUPABASE_URL points somewhere else — check the 'Endpoint'\n"
                  "     line above reads https://<ref>.supabase.co/rest/v1 exactly once.")
        print(f"\n{len(missing)} table(s) missing. Run the migration:\n"
              f"  1. Supabase dashboard > SQL Editor > New query\n"
              f"  2. Paste the output of:  python scripts/setup-db.py --print\n"
              f"  3. Run it, then re-run this script.\n"
              f"  (or: supabase db push, if you use their CLI)")
        return 1

    print("\nSchema is complete.")
    if args.seed:
        for key, value in DEFAULT_SETTINGS.items():
            store.set_setting(key, value)
        print(f"Seeded {len(DEFAULT_SETTINGS)} settings: "
              f"{', '.join(DEFAULT_SETTINGS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

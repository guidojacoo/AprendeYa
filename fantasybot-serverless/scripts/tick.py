#!/usr/bin/env python3
"""Run one tick locally — the same code path Vercel runs.

The fastest way to find out whether a deployment will work before deploying it:
point the env at Supabase and run this. What happens here is what happens there.

    python scripts/tick.py --dry-run        # decide everything, send nothing
    python scripts/tick.py --force          # review even if not due
    python scripts/tick.py --mode sniper    # bids only, skip the review
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fantasybot import tick                                   # noqa: E402
from fantasybot.storage import get_storage                    # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("tick", "sniper"), default="tick")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="run the review even if it is not due by cadence")
    ap.add_argument("--json", action="store_true", help="print the raw summary")
    args = ap.parse_args()

    print(f"storage: {get_storage().kind}")
    result = tick.run(mode=args.mode, dry_run=args.dry_run,
                      force_review=args.force, log=lambda m: print(f"  {m}"))
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"\nok={result.get('ok')}  "
              f"actions={len(result.get('actions') or [])}  "
              f"pending={result.get('pending')}  "
              f"next={result.get('next_deadline')}")
        if result.get("review"):
            print(f"review: {result['review'].get('status')}")
        if result.get("error"):
            print(f"error: {result['error']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

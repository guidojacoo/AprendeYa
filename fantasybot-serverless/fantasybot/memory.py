"""What happened to this squad, and what it cost.

The bot already noticed players arriving and leaving — `state.diff_snapshots`
has computed it on every review since the beginning, put it in the report under
"events", and nothing ever read it. A rival paid the clause on one of ours and
the run that saw it happen carried on as though the squad were unchanged.

Two things were missing and they are the same thing:

REACTING. Losing a player is the single biggest change that can happen between
two reviews. The money lands, a hole opens in the eleven, and the plan built
sixty minutes ago was built for a different squad. That has to be noticed in the
run that sees it, not in whatever run happens to look next.

REMEMBERING. A diff is only true once — the next snapshot overwrites it, and
then nothing anywhere knows the player ever existed. A season is a sequence of
these, and "what have I been doing" is a question the page could not answer at
all. So every arrival and departure is appended to a ledger that survives.

The ledger is deliberately small and append-only. It is a log of facts, not a
model of anything: who, when, in or out, and what the balance did.
"""

from .storage import get_storage, to_iso, utcnow

LEDGER = "squad_ledger"

# How many movements to keep. A season is a few dozen; this is generous and
# still bounded, because a document that grows forever eventually stops loading.
MAX_ENTRIES = 300

IN, OUT = "in", "out"


def _load(store):
    got = store.get_doc(LEDGER, None)
    return got if isinstance(got, list) else []


def record(changes, money_after=None, store=None):
    """Append what changed to the ledger. Returns the entries it added.

    `changes` is a `state.diff_snapshots` result. A first run adds nothing:
    every player looks new, and a squad appearing all at once is not news.
    """
    if not changes or changes.get("first_run"):
        return []
    added = list(changes.get("added") or [])
    removed = list(changes.get("removed") or [])
    if not added and not removed:
        return []
    store = store or get_storage()
    now = to_iso(utcnow())
    delta = int(changes.get("money_delta") or 0)
    entries = ([{"at": now, "how": OUT, "nombre": n, "money_delta": delta}
                for n in removed]
               + [{"at": now, "how": IN, "nombre": n, "money_delta": delta}
                  for n in added])
    for e in entries:
        if money_after is not None:
            e["balance"] = int(money_after)
    try:
        ledger = _load(store) + entries
        store.put_doc(LEDGER, ledger[-MAX_ENTRIES:])
    except Exception:                            # noqa: BLE001
        # Losing the record must never cost the reaction it is attached to.
        pass
    return entries


def recent(limit=20, store=None):
    """The last movements, newest first."""
    try:
        return list(reversed(_load(store or get_storage())))[:limit]
    except Exception:                            # noqa: BLE001
        return []


def describe(changes):
    """One line a human reads on a phone, or None when nothing moved."""
    if not changes or changes.get("first_run"):
        return None
    added = list(changes.get("added") or [])
    removed = list(changes.get("removed") or [])
    if not (added or removed):
        return None
    parts = []
    if removed:
        parts.append("me quitaron a " + ", ".join(removed)
                     if len(removed) > 1 else f"me quitaron a {removed[0]}")
    if added:
        parts.append("llegó " + ", ".join(added)
                     if len(added) > 1 else f"llegó {added[0]}")
    delta = int(changes.get("money_delta") or 0)
    line = " · ".join(parts)
    if delta:
        line += f" · caja {'+' if delta > 0 else ''}{delta:,} €"
    return line

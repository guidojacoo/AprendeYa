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


# --- did it work? -----------------------------------------------------------
#
# The ledger above records what CHANGED. This records what the bot BELIEVED, and
# then what actually happened, which is the only way it gets better at believing.
#
# Every gameweek the optimiser fields an eleven and says what it expects them to
# score. That number was never written down anywhere, so it was never wrong
# about anything: a model nobody scores is a model that cannot improve. Here the
# prediction is banked when the XI is set, and settled against `weekPoints` once
# LaLiga has published them.

PREDICTIONS = "predictions"
MAX_WEEKS = 20


def _predictions(store):
    got = store.get_doc(PREDICTIONS, None)
    return got if isinstance(got, dict) else {}


def predict(week, best, store=None):
    """Bank what we expect this XI to score. Overwrites until the week starts.

    Re-recording is correct: the XI is re-optimised before kickoff, and the
    prediction that matters is the one attached to the team actually fielded.
    A settled week is never overwritten — its result is history.
    """
    if not week or not best:
        return None
    store = store or get_storage()
    try:
        book = _predictions(store)
        key = str(week)
        if (book.get(key) or {}).get("actual") is not None:
            return book[key]
        players = [best.get("goalkeeper")] + [
            e for line in ("defender", "midfield", "striker")
            for e in (best.get(line) or [])]
        entry = {
            "at": to_iso(utcnow()),
            "formation": "-".join(str(n) for n in (best.get("formation") or ())),
            "expected": float(best.get("total") or 0),
            "xi": [{"id": str(e.get("playerTeamId")), "nombre": e.get("nombre"),
                    "expected": float(e.get("score") or 0)}
                   for e in players if e],
            "actual": None,
        }
        book[key] = entry
        for old in sorted(book, key=lambda k: int(k) if k.isdigit() else 0)[:-MAX_WEEKS]:
            book.pop(old, None)
        store.put_doc(PREDICTIONS, book)
        return entry
    except Exception:                            # noqa: BLE001
        return None


def settle(week, points_by_player, store=None):
    """Fill in what the fielded XI actually scored. Returns the settled entry.

    `points_by_player` maps playerMaster id to that week's points. The XI is
    keyed by roster slot, so the caller passes whichever mapping it has — an id
    we cannot find contributes nothing rather than a zero, because "did not
    play" and "we lost track of him" are different and only one is his fault.
    """
    if not week or not points_by_player:
        return None
    store = store or get_storage()
    try:
        book = _predictions(store)
        entry = book.get(str(week))
        if not entry or entry.get("actual") is not None:
            return entry
        total, known = 0.0, 0
        for row in entry.get("xi") or []:
            got = points_by_player.get(str(row.get("id")))
            if got is None:
                continue
            row["actual"] = float(got)
            total += float(got)
            known += 1
        if not known:
            return entry            # nothing published yet; try again next tick
        entry["actual"] = round(total, 1)
        entry["settled_players"] = known
        entry["error"] = round(entry["actual"] - float(entry.get("expected") or 0), 1)
        book[str(week)] = entry
        store.put_doc(PREDICTIONS, book)
        return entry
    except Exception:                            # noqa: BLE001
        return None


def accuracy(store=None):
    """How well the model has been predicting, over the settled weeks.

    `bias` is the part that is actionable: consistently positive means the
    estimates are too shy, consistently negative means too generous. One week is
    noise; a run of them is a number to correct by.
    """
    try:
        book = _predictions(store or get_storage())
    except Exception:                            # noqa: BLE001
        return {}
    done = [e for e in book.values() if isinstance(e, dict)
            and e.get("actual") is not None]
    if not done:
        return {"weeks": 0}
    errors = [float(e.get("error") or 0) for e in done]
    return {
        "weeks": len(done),
        "bias": round(sum(errors) / len(errors), 2),
        "mean_abs_error": round(sum(abs(x) for x in errors) / len(errors), 2),
        "last": sorted(done, key=lambda e: e.get("at") or "")[-1],
    }

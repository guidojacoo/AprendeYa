"""The strategic pass: Hermes' judgement, minus Hermes.

What Hermes actually contributed was not execution — the CLI already executed —
but three things a deterministic optimiser has no opinion on: which targets are
worth stretching for, what to remember between runs, and when to walk away. So
that is all this reproduces.

The hard rule, and the reason this file is short: **the model never acts.** It
reads a summary and returns a small JSON object of adjustments. Deterministic
code then applies them, each one clamped:

  * a bid cap may be nudged within a band around the calculated one, never past
    the balance, and only on a bid that is ALREADY queued
  * a target may be dropped
  * a sale becomes a task for you, never an API call

So the worst a confused (or prompt-injected) model can do is bid slightly wrong
on something the optimiser had already chosen, or queue a note. It cannot invent
a purchase, spend a clause, or cause an action to run twice — those paths are not
reachable from here at all.
"""

import json

from .. import events, scheduler
from ..storage import get_storage, to_iso, utcnow
from . import client as llm_client

# How far the model may move a cap the optimiser computed. Wide enough to matter
# in an auction, tight enough that a hallucinated number cannot drain the bank.
CAP_MIN_FACTOR = 0.7
CAP_MAX_FACTOR = 1.3

SYSTEM = """You are the strategist for an autonomous LALIGA Fantasy manager whose
goal is to finish the season FIRST in his league.

You do NOT execute anything. The deterministic engine below you already optimises
the lineup, prices flips, snipes bids at the close and pays buyout clauses on
time. What it cannot do is read the SEASON: it has no opinion on whether this is
a moment to protect a lead or to gamble for one.

That is your job. Judge from `standing`:

- Comfortably ahead, season late  -> protect. Favour certainty over upside: keep
  proven starters, do not stretch caps, avoid tying up cash in speculation.
- Close race                      -> maximise expected points. Normal caps, take
  the flips with real margin, prefer players who start every week.
- Behind, and the season is short -> variance is your friend. A safe second place
  is worth the same as last. Stretch for the differentials, accept the risk.
- Early season                    -> build value. Trade aggressively; points lost
  now matter less than a bigger squad later.

Reply with ONE JSON object and nothing else:
{
  "summary": "2-3 sentences: where we stand and what you are playing for",
  "stance":  "protect" | "balanced" | "chase" | "build",
  "bid_caps":  [{"market_id": "<id already in scheduled_bids>", "max_bid": <int>, "reason": "..."}],
  "avoid":     ["<market_id to drop from the plan>"],
  "sell":      [{"player_id": "<id>", "price": <int>, "reason": "..."}],
  "memory":    "what the next run should remember (<= 600 chars)"
}

Rules:
- Only use market_ids that appear in scheduled_bids. Anything else is ignored.
- Caps are advisory; they get clamped to +-30% of the computed cap and to the balance.
- "sell" produces a task for the human, never a sale.
- `rivals` shows each opponent's estimated cash — that is who can outbid us.
- Omit a key rather than inventing entries. An empty plan is a valid answer, and a
  season going well rarely needs adjusting."""


def enabled():
    return llm_client.enabled()


def _context(ctx):
    """The compact snapshot the model reasons over.

    Small on purpose: every field here is tokens on every strategic pass, and the
    squad payload alone would be tens of thousands of them for no added judgement.
    """
    store = get_storage()
    report = store.get_doc("last_report", {}) or {}
    memory = store.get_doc("agent_memory", "") or ""
    queued = [a for a in store.pending_actions(limit=25)
              if a.get("type") == scheduler.BID]
    rivals = report.get("rivals") or []
    me = next((r for r in rivals if r.get("is_me")), None)
    leader = rivals[0] if rivals else None
    standing = None
    if me:
        standing = {
            "position": me.get("position"),
            "teams": len(rivals),
            "points": me.get("points"),
            "points_behind_leader": ((leader or {}).get("points") or 0)
                                    - (me.get("points") or 0),
            "leader": (leader or {}).get("manager"),
        }
    return {
        "now": to_iso(utcnow()),
        "memory": memory,
        "standing": standing,
        "rivals": rivals,
        "queued_clauses": (report.get("clauses") or {}).get("queued") or [],
        "shield": (report.get("shield") or {}).get("queued"),
        "balance": report.get("money"),
        "matchday": report.get("matchday"),
        "formation": report.get("formation"),
        "squad_gaps": report.get("gaps"),
        "top_flips": report.get("flips") or [],
        "sell_candidates": report.get("sells") or [],
        "clause_targets": report.get("clause_targets") or [],
        "open_tasks": [t.get("text") for t in (report.get("tasks") or [])][:10],
        "scheduled_bids": [
            {"market_id": (a.get("payload") or {}).get("market_id"),
             "nombre": (a.get("payload") or {}).get("nombre"),
             "max_bid": (a.get("payload") or {}).get("max_bid"),
             "close_at": (a.get("payload") or {}).get("close_at")}
            for a in queued],
        "recent_events": [
            {"at": e.get("iso"), "kind": e.get("kind"), "title": e.get("title")}
            for e in store.load_events(limit=25)],
    }


def run(ctx):
    """One strategic pass: think, persist the decision, apply the safe parts."""
    context = _context(ctx)
    decision = llm_client.complete_json(
        SYSTEM, json.dumps(context, ensure_ascii=False, default=str))
    store = get_storage()
    info = llm_client.describe()
    store.save_decision("llm_strategy", info.get("model"), decision)

    applied = apply_decision(decision, context)
    if decision.get("stance"):
        # Recorded, not obeyed: the stance explains the adjustments it made, and
        # gives the next run something to be consistent with.
        store.put_doc("stance", {"stance": str(decision["stance"])[:40],
                                 "at": to_iso(utcnow()),
                                 "why": (decision.get("summary") or "")[:400]})
    if decision.get("memory"):
        store.put_doc("agent_memory", str(decision["memory"])[:2000])
    events.emit("note", "Pasada estratégica",
                detail={"summary": (decision.get("summary") or "")[:300],
                        "applied": applied})
    return {"status": "ok", "model": info.get("model"),
            "stance": decision.get("stance"),
            "summary": decision.get("summary"), "applied": applied}


def apply_decision(decision, context):
    """Carry out the parts of a decision that are safe to carry out.

    Every branch here either edits something already queued or writes a note.
    Nothing in this function can create a new irreversible action.
    """
    from .. import state

    balance = context.get("balance") or 0
    queued = {str(b["market_id"]): b for b in context.get("scheduled_bids") or []
              if b.get("market_id")}
    applied = {"caps": [], "dropped": [], "sell_tasks": [], "ignored": []}

    for item in decision.get("bid_caps") or []:
        mid = str(item.get("market_id") or "")
        row = queued.get(mid)
        if row is None:
            applied["ignored"].append({"market_id": mid, "why": "not scheduled"})
            continue
        try:
            wanted = int(item.get("max_bid"))
        except (TypeError, ValueError):
            applied["ignored"].append({"market_id": mid, "why": "cap not a number"})
            continue
        base = int(row.get("max_bid") or 0)
        low, high = int(base * CAP_MIN_FACTOR), int(base * CAP_MAX_FACTOR)
        capped = max(low, min(high, wanted))
        if balance:
            capped = min(capped, int(balance))
        if capped == base:
            continue
        scheduler.schedule_bid(
            (context.get("league_id") or row.get("league_id")
             or _league_of(mid) or ""),
            mid, capped, row.get("close_at"), nombre=row.get("nombre"))
        applied["caps"].append({"market_id": mid, "from": base, "to": capped,
                                "requested": wanted})

    for mid in decision.get("avoid") or []:
        row = queued.get(str(mid))
        if row is None:
            continue
        key = scheduler.bid_key(_league_of(str(mid)) or "", str(mid),
                                row.get("close_at"))
        if scheduler.cancel(key):
            applied["dropped"].append(str(mid))

    for s in decision.get("sell") or []:
        pid = s.get("player_id")
        if not pid:
            continue
        price = s.get("price")
        price_txt = f" (~{int(price):,})" if isinstance(price, (int, float)) else ""
        state.add_task(f"Sell {pid}{price_txt}: {s.get('reason') or 'strategy call'}.",
                       key=f"llm-rec:sell:{pid}")
        applied["sell_tasks"].append(str(pid))

    # llm-rec tasks have no natural completion signal, so they expire themselves.
    state.expire_tasks("llm-rec:", 14)
    return applied


def _league_of(market_id):
    """The league a queued bid belongs to, read back from its stored action.

    Kept out of the model's reach on purpose: the league is ours to know, not
    something a reply gets to choose.
    """
    for a in get_storage().pending_actions(limit=50):
        p = a.get("payload") or {}
        if str(p.get("market_id")) == str(market_id):
            return p.get("league_id")
    return None

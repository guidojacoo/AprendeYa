"""Strategic modes: the posture you choose, from the phone.

The engine has always had exactly one opinion — maximise expected points per
gameweek, subject to the budget. That is the right default and it is not the
right answer all season. Twenty points clear in May you want certainty. Last in
November you want variance. And sometimes the move is not points at all: it is
buying a player at ten million because he will be worth fifteen in a week, which
the points engine cannot even express — it would score that purchase on what he
adds to the eleven and refuse it.

So a mode is a small set of knobs, chosen by the user and stored in the
database, that the decision code reads instead of its own constants.

Three of them, because three is what is actually different:

  equilibrio  what the bot has always done. Points per euro, nothing stretched.
  dinero      make the bank grow. Rank by resale margin rather than points, sell
              a holding once it has run, and never overpay — in trading the
              price you pay IS the profit.
  puntos      win the gameweek. Pay over the odds to actually land the player,
              refuse to sell anyone who starts, and let one clause take most of
              the bank.

WHAT A MODE MAY NOT DO. It tilts judgement; it never removes a safety rail. The
autonomy switches, the legal-bid floor, the affordability checks and the
idempotency keys are all outside this file and stay exactly as they are. The
worst a wrong mode can do is make choices you disagree with — not unsafe ones.

Resolved ONCE per tick and cached for the life of the process. A tick is a fresh
process, so there is no staleness to worry about, and it means the fifty places
that ask "what mode are we in" cost one storage round trip between them rather
than fifty.
"""

from .storage import get_storage

SETTING = "mode"
DEFAULT = "equilibrio"

MODES = {
    "equilibrio": {
        "label": "Equilibrado",
        "blurb": "Puntos por euro. Lo de siempre.",
        # The bar a signing must clear, in expected points per gameweek.
        "min_gain": 0.25,
        # What to rank the market by: "points" (gain per million) or "margin"
        # (projected resale profit).
        "rank_by": "points",
        # Multipliers on how hard we hold on. Separate for the eleven and the
        # bench on purpose: "move the stock I don't field" and "sell the man who
        # starts every week" are not the same instruction, and one knob for both
        # would quietly mean the second.
        "xi_premium": 1.0,
        "bench_premium": 1.0,
        # Profit over the purchase price at which a holding is cashed in. None
        # disables profit-taking: a player is sold on his merits, not his chart.
        "flip_target": None,
        # How far past the computed cap a bid may go. Above 1.0 is deliberately
        # paying over the odds.
        "bid_ceiling": 1.0,
        # The most of the bank one buyout clause may take.
        "clause_share": 0.60,
        # Cash that is never spent, in euros.
        #
        # This existed as FANTASYBOT_CASH_RESERVE and defaulted to ZERO, which
        # meant the only fence on spending was `clause_share` — a share of
        # whatever is LEFT. That is not a floor: at 60% each clause leaves 40%,
        # so four of them take eighty million down to two. Which is exactly what
        # happened, while the selling side was broken and nothing came back in.
        #
        # A share cannot bound a sequence. Only an absolute number can, and its
        # job is concrete: always be able to answer the next market close.
        "cash_floor": 10_000_000,
    },
    "dinero": {
        "label": "Hacer caja",
        "blurb": "Compra barato, vende caro. Los puntos son secundarios.",
        # Points are not the bar in this mode; the margin is. Zero does not mean
        # "buy anyone" — a purchase still has to clear the margin test and the
        # budget. It means points are no longer the thing being tested.
        "min_gain": 0.0,
        "rank_by": "margin",
        # The eleven is NOT for sale even here. A trading mode that liquidates
        # your starters is not a trading mode, it is a relegation.
        "xi_premium": 1.0,
        # Everyone else is stock, and stock that does not move is dead money.
        "bench_premium": 0.5,
        "flip_target": 0.25,
        # Never pay over the odds: in trading the price you pay IS the profit.
        "bid_ceiling": 0.95,
        # A clause costs roughly 1.67x market value. That is a terrible entry
        # price for a trade, so this mode may barely use them.
        "clause_share": 0.30,
        # Trading wants its capital working, not idle — but a trader with no
        # cash cannot take the next opportunity either, which is the whole game
        # here.
        "cash_floor": 5_000_000,
    },
    "puntos": {
        "label": "Todo a puntos",
        "blurb": "Máximo de puntos posible. Paga de más y no vende titulares.",
        # A smaller edge still justifies a move when points are the only thing
        # that counts.
        "min_gain": 0.10,
        "rank_by": "points",
        # Nobody who starts is for sale, at any sane price.
        "xi_premium": 2.0,
        "bench_premium": 1.0,
        "flip_target": None,
        # Pay over the odds. Losing an auction by a hundred thousand euros costs
        # the whole player.
        "bid_ceiling": 1.20,
        # One extraordinary player can be most of the bank.
        "clause_share": 0.85,
        # Spend aggressively, but never literally broke: an account at zero
        # cannot bid at a close, and missing the close costs the whole player.
        "cash_floor": 3_000_000,
    },
}

_ACTIVE = None


def names():
    return list(MODES)


def catalogue():
    """Every mode, for the page. Safe to show: no secrets, no live state."""
    return [{"name": n, **spec} for n, spec in MODES.items()]


def active():
    """The mode in force, resolved once per process.

    A mode nobody recognises falls back to the default rather than raising: a
    typo in the settings table must not take the tick down, and the default is
    the behaviour that was there before modes existed.
    """
    global _ACTIVE
    if _ACTIVE is None:
        name = DEFAULT
        try:
            stored = get_storage().get_settings().get(SETTING)
            if stored and str(stored).strip().lower() in MODES:
                name = str(stored).strip().lower()
        except Exception:
            pass          # storage down: play the default, do not crash
        _ACTIVE = name
    return _ACTIVE


def cash_floor():
    """Cash the bot may never spend, in euros.

    The configured reserve wins when it is higher: the mode provides a sane
    floor, the env var is how you raise it, and neither can lower the other.
    """
    from . import config
    return max(int(config.CASH_RESERVE or 0), int(knob("cash_floor") or 0))


def knob(name, default=None):
    """One knob of the active mode."""
    spec = MODES.get(active()) or MODES[DEFAULT]
    value = spec.get(name, default)
    # A mode that omits a knob inherits it rather than getting None, so adding a
    # knob here can never silently disable something in the other two modes.
    if value is None and name not in spec:
        return MODES[DEFAULT].get(name, default)
    return value


def describe():
    """What the dashboard shows."""
    name = active()
    spec = MODES.get(name) or MODES[DEFAULT]
    return {"mode": name, "label": spec["label"], "blurb": spec["blurb"],
            "knobs": {k: v for k, v in spec.items()
                      if k not in ("label", "blurb")}}


def set_mode(name):
    """Switch modes. Returns the name actually stored."""
    name = (name or "").strip().lower()
    if name not in MODES:
        raise ValueError(f"unknown mode {name!r}; known: {', '.join(MODES)}")
    get_storage().set_setting(SETTING, name)
    forget()
    return name


def forget():
    """Drop the cached mode. For tests, and right after a switch."""
    global _ACTIVE
    _ACTIVE = None

"""The money the bot can really use, and staying above zero when it counts.

Two rules of the game decide this, and the bot knew neither.

LaLiga lets you bid past your cash. The limit is your cash PLUS 20% of your
squad's value — a squad worth 240M can bid about 48M with nothing in the bank.
The bot only ever spent the cash it had, minus a ten-million floor, so with 1.3M
in the account it could not bid on anybody while holding 240M of players.

And the catch: a team that starts a gameweek with a negative balance scores
ZERO that gameweek, even if it is back above zero an hour later. Once the
gameweek is under way, negative is harmless. So borrowing is exactly as safe as
the plan to repay it before the next first kick-off, and no safer.

The plan to repay is selling, and selling here means LaLiga's own daily offer on
every listed player. So the credit is only ever as large as the bench that can
pay it back, it is only used when the money lands with at least two of those
daily offers left before the gameweek starts, and it is not used at all until
the bot has actually SEEN one of those offers arrive — borrowing against a sale
channel nobody has observed working is how you score zero on a Friday.

Everything here is pure: numbers and timestamps in, decisions out.
"""

from datetime import datetime, timedelta, timezone

from .. import config
from ..matching import num

# LaLiga's own rule: the most you can owe is this share of your squad's value.
DEBT_SHARE = config.DEBT_SHARE

# How long before a gameweek's first kick-off borrowed money has to have landed.
# LaLiga makes its offer on a listed player once a day, so this is two of those
# offers with a couple of hours to spare: one to sell, one in case the first
# falls short.
CREDIT_MIN_HOURS = config.CREDIT_MIN_HOURS

# What a bench player raises when the bank needs him to, as a share of his value.
# The debt is planned against this, not against the value itself.
LIQUIDITY_HAIRCUT = 0.85

# Negative with this long to go: sell the bench at a small discount.
DEBT_HOURS = 60
# Negative with this long to go: sell whatever it takes, the bench first.
PANIC_HOURS = 30

# Accepting under pressure, as a share of value. In panic there is no floor for
# the bench — a sale at 60% loses money; a gameweek at zero loses the league.
ACCEPT_IN_DEBT = 0.90
ACCEPT_URGENT = 0.80

# Land a little above zero rather than on it: values move between the sale and
# the kick-off, and a balance of -3 € scores exactly as much as one of -3M.
SAFETY_BUFFER = 150_000

CALM, IN_DEBT, URGENT, PANIC = None, "deuda", "urgente", "panico"


def _parse(iso):
    if isinstance(iso, datetime):
        return iso if iso.tzinfo else iso.replace(tzinfo=timezone.utc)
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hours_until(iso, now=None):
    """Hours from now until `iso`; None when it cannot be read."""
    dt = _parse(iso)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (dt - now).total_seconds() / 3600.0


def credit_line(team_value):
    """The most LaLiga lets this squad owe."""
    return int(round(num(team_value) * DEBT_SHARE))


def liquid_value(floors):
    """What the players outside the eleven are worth, from the cached floors.

    These are the ones a debt is repaid with. The eleven is not counted: a plan
    that repays a signing by selling a starter is a plan to stand still.
    """
    return int(sum(num((meta or {}).get("value"))
                   for meta in (floors or {}).values()
                   if not (meta or {}).get("xi")))


def credit_ok_for(close_at, gameweek_start, now=None,
                  min_hours=CREDIT_MIN_HOURS):
    """Whether money borrowed when `close_at` resolves can be repaid in time.

    A listing that closes AFTER the next gameweek has begun lands its debt
    while negative is harmless, with a whole week to the one after: fine. One
    that closes before needs `min_hours` of daily offers between the two.
    """
    close, start = _parse(close_at), _parse(gameweek_start)
    if close is None or start is None:
        return False
    if close >= start:
        return True
    return (start - close) >= timedelta(hours=min_hours)


def buying_power(cash, team_value, liquid, *, credit_use, offers_proven,
                 gameweek_start, committed=0, floor=0, now=None):
    """What can be spent right now, split into cash and borrowed money.

    `credit_use` is the share of LaLiga's credit line the mode allows;
    `committed` is money already promised to bids that have not resolved.
    The result names why no credit is available when there is none, because
    "0 pujas" beside a 240M squad has to be able to explain itself.
    """
    cash = int(num(cash))
    line = credit_line(team_value)
    credit, why = 0, None
    hours = hours_until(gameweek_start, now)
    if not credit_use or credit_use <= 0:
        why = "el modo no usa crédito"
    elif not offers_proven:
        why = ("todavía no vi llegar ninguna oferta de LaLiga: no me endeudo "
               "sin saber que puedo vender para devolverlo")
    elif hours is None:
        why = "no sé cuándo empieza la próxima jornada"
    else:
        # What the bench can repay, minus what we already owe.
        repayable = int(num(liquid) * LIQUIDITY_HAIRCUT) - max(0, -cash)
        credit = max(0, min(int(line * credit_use), repayable))
        if credit <= 0:
            why = ("no tengo suplentes que valgan lo suficiente para devolver "
                   "una deuda")
    spend_cash = max(0, cash - int(floor) - int(committed))
    spend_total = max(0, cash - int(floor) + credit - int(committed))
    return {"cash": cash, "credit_line": line, "credit": credit,
            "committed": int(committed), "floor": int(floor),
            "spend_cash": spend_cash, "spend_total": spend_total,
            "hours_to_gameweek": None if hours is None else round(hours, 1),
            "why_no_credit": why}


def pressure(cash, gameweek_start, now=None):
    """How hard the bank has to sell right now.

    None while the balance is not negative. Otherwise it tightens with the
    clock: a gameweek that starts in the red scores zero, and there is nothing
    to be bought with the money saved by refusing a slightly low offer.
    """
    if num(cash) >= 0:
        return CALM
    hours = hours_until(gameweek_start, now)
    if hours is None:
        return URGENT               # no clock: act as if it were close
    if hours < 0:
        # The start we know of has passed: that gameweek is under way, when
        # negative is harmless, and the next start is days off. Sell, but do
        # not fire-sale on a clock that has already run out.
        return IN_DEBT
    if hours <= PANIC_HOURS:
        return PANIC
    if hours <= DEBT_HOURS:
        return URGENT
    return IN_DEBT


def _willing(decision, level):
    """Whether this offer may be taken at this level of pressure."""
    value = num(decision.get("value"))
    amount = num(decision.get("amount"))
    if level == PANIC:
        return True
    if decision.get("in_xi"):
        return False
    share = ACCEPT_IN_DEBT if level == IN_DEBT else ACCEPT_URGENT
    return not value or amount >= value * share


def settle_debt(decisions, cash, level):
    """Turn enough standing offers into sales to finish above zero.

    Mutates and returns `decisions`. Only each player's BEST offer is ever
    considered. The bench goes first, the cheapest to lose first within it; the
    eleven is touched only in panic, and then in order of fewest points lost.
    Stops the moment the balance, counting every sale already decided, clears
    zero with the safety buffer on top.
    """
    if level is CALM:
        return decisions
    need = -int(num(cash)) + SAFETY_BUFFER - sum(
        int(num(d.get("amount"))) for d in decisions if d.get("action") == "accept")
    if need <= 0:
        return decisions
    pool = [d for d in decisions
            if d.get("best", True) and d.get("action") != "accept"
            and _willing(d, level)]
    pool.sort(key=lambda d: (bool(d.get("in_xi")),
                             num(d.get("pts"), 0) or 0,
                             -num(d.get("amount"))))
    for d in pool:
        if need <= 0:
            break
        d["action"] = "accept"
        d["por_deuda"] = (
            f"Estoy en negativo y la jornada empieza pronto: una jornada que "
            f"arranca en rojo puntúa cero. Nivel: {level}.")
        need -= int(num(d.get("amount")))
    return decisions

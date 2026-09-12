"""A number for "is he worth buying", and the sentences behind it.

The bot already knew all of this — projected value, trend, whether the man even
starts — but it only ever published the five listings it liked. Everything it
looked at and declined was invisible, so "why didn't you buy him?" had no answer
anywhere on the page.

So every listing gets a score out of 100 and, more importantly, the reasons that
produced it. The score is a summary you can sort by; the reasons are the part you
actually read. Both come from the same numbers the bidder uses, never from a
second opinion invented for display — a dashboard that scores a player 80 while
the bot declines to bid on him is a dashboard that lies.
"""

from . import points as points_mod

# The league is won on points, so points lead the score. A signing that adds two
# expected points a week over a replacement-level starter moves it by twelve; a
# listing projecting +10% resale moves it by ten. Both are real — the margin is
# what funds the next signing — but only one of them is what the season is
# scored on, and the old weighting had it the other way round.
POINTS_WEIGHT = 6.0
MARGIN_WEIGHT = 1.0
BASE = 50.0

# What an ordinary starter gives you per gameweek. The question a signing has to
# answer is not "does he score points" but "does he score more than whoever
# would play instead", so the expected points are measured against this.
REPLACEMENT_RATE = 3.0

VERDICTS = ((70, "comprar"), (58, "interesante"), (45, "regular"))


def _clamp(n, low=0.0, high=100.0):
    return max(low, min(high, n))


def score(op, prob=None, money=None):
    """Score one evaluated listing. `op` is a row from strategy.flip.evaluate.

    Affordability deliberately does NOT move the score: a good player you cannot
    pay for is still a good player, and pretending otherwise would hide him again
    the moment the balance dips. It changes the verdict instead.
    """
    margin_pct = float(op.get("margin_pct") or 0)
    # Each reason carries the weight it pulled, so the page can quote the one
    # that DECIDED rather than the first one written. "Descarté a Lewandowski
    # porque proyecta +11%" is a sentence that makes no sense to read.
    weighed = []

    if margin_pct > 0:
        weighed.append((margin_pct * MARGIN_WEIGHT,
                        f"Proyección +{margin_pct:.1f}% sobre lo que cuesta "
                        f"(vale {_money(op.get('buy_price'))}, lo proyecto en "
                        f"{_money(op.get('proyeccion'))} tras comisión)."))
    else:
        weighed.append((margin_pct * MARGIN_WEIGHT,
                        f"Proyección {margin_pct:.1f}%: lo que pide "
                        f"({_money(op.get('buy_price'))}) es más de lo que "
                        f"creo que va a valer "
                        f"({_money(op.get('proyeccion'))})."))

    # What he is expected to SCORE, which is what the league counts. The
    # probability lives inside this number rather than beside it: a suplente is
    # penalised because 20% of a good rate is a bad rate, not by a separate rule
    # that would count the same fact twice.
    rate = points_mod.per_start({"averagePoints": op.get("avg_points"),
                                 "points": op.get("season_points"),
                                 "lastSeasonPoints": op.get("last_season_points")})
    if prob is not None:
        exp = prob / 100.0 * rate
        weighed.append(((exp - REPLACEMENT_RATE) * POINTS_WEIGHT,
                        f"Esperaría {exp:.1f} puntos por jornada de él "
                        f"({prob:.0f}% de titularidad × {rate:.1f} puntos por "
                        f"partido jugado); un titular corriente da "
                        f"{REPLACEMENT_RATE:.0f}."))
    else:
        weighed.append((0, f"Sin dato de alineación probable: hace "
                           f"{rate:.1f} puntos por partido cuando juega, pero "
                           f"no sé si va a jugar."))

    tend = op.get("tendencia")
    if tend is not None and tend > 0:
        weighed.append((5, "Su valor viene subiendo."))
    elif tend is not None and tend < 0:
        weighed.append((-5, "Su valor viene bajando: comprarlo es atrapar un "
                            "cuchillo cayendo."))

    pts = int(op.get("last_season_points") or 0)
    if pts >= 100:
        weighed.append((5, f"Hizo {pts} puntos la temporada pasada."))

    total = round(_clamp(BASE + sum(w for w, _ in weighed)))
    price = int(op.get("buy_price") or 0)
    affordable = money is None or price <= int(money)
    reasons = [text for _, text in weighed]
    if not affordable:
        reasons.append(f"No alcanza la caja: pide {_money(price)} y hay "
                       f"{_money(money)}.")

    verdict = _verdict(total, affordable)
    return {**op, "score": total, "affordable": affordable,
            "verdict": verdict, "prob": prob, "reasons": reasons,
            "headline": _headline(weighed, verdict, reasons[-1], affordable)}


def _headline(weighed, verdict, money_reason, affordable):
    """The one sentence that decided it.

    For anything turned down that is the heaviest thing against it — the money,
    the bench, the falling value — never the first line written.
    """
    if not affordable:
        return money_reason
    if verdict in ("comprar", "interesante"):
        return max(weighed, key=lambda r: r[0])[1]
    return min(weighed, key=lambda r: r[0])[1]


def _verdict(total, affordable):
    if not affordable:
        return "no alcanza"
    for floor, label in VERDICTS:
        if total >= floor:
            return label
    return "no vale la pena"


def _money(n):
    try:
        return f"{int(n):,} €".replace(",", ".")
    except (TypeError, ValueError):
        return "?"


def rank(ops, prob_index=None, money=None, limit=None):
    """Score every listing, best first. Ties break on the cheaper one."""
    from ..matching import match_name

    out = []
    for op in ops or []:
        prob = None
        if prob_index:
            entry = match_name(op.get("nombre") or "", op.get("nombre") or "",
                               prob_index)
            if entry:
                prob = entry.get("prob")
        out.append(score(op, prob=prob, money=money))
    out.sort(key=lambda r: (-r["score"], r.get("buy_price") or 0))
    return out[:limit] if limit else out

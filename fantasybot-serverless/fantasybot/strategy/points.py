"""How many points a player is actually expected to score.

The bot ranked footballers by two questions — "¿va a jugar?" and "¿cuánto
vale?" — and almost never by the one the league is scored on. The XI was built
from `probabilidad + 0.5 × media`, which on a 0-100 probability scale makes form
a rounding error: a 95%-probable player averaging 2.5 outranked a 60%-probable
one averaging 11, and the second scores nearly three times as many points.

The right quantity is a product, not a sum:

    E[puntos] = P(juega) × puntos por partido jugado

Both halves already existed. Nothing new is fetched; they were just never
multiplied.

`averagePoints` is points per appearance, so early in a season it is a loud,
unreliable number — two good games make a 10.0 average. It is therefore shrunk
toward last season's rate until enough matches have been played, which is the
difference between spotting form and chasing noise.
"""

# Appearances at which this season's average is trusted on its own. Below it the
# estimate leans on last season in proportion to how little we have seen.
SAMPLE_FULL = 8
SEASON_MATCHES = 38

# What an unknown player is assumed to score per appearance. Not cosmetic: with a
# rate of zero, expected points is zero for everyone the league has no history
# for, every one of them ties, and the XI is picked by list order — a 95%
# starter and an unmatched reserve become the same player. An average rate keeps
# the score proportional to the probability, which is the best the evidence
# supports when there is none about the scoring itself.
DEFAULT_RATE = 3.0


def games_played(pm):
    """Appearances, derived rather than requested.

    LaLiga does not publish a match count on the player payload, but it
    publishes both the total and the per-appearance average — and one divided by
    the other is the count. Nonsense results (a stale total, a zero average) are
    refused rather than rounded into a number that looks real.
    """
    avg = float(pm.get("averagePoints") or 0)
    total = float(pm.get("points") or 0)
    if avg <= 0 or total <= 0:
        return 0
    games = round(total / avg)
    return games if 1 <= games <= SEASON_MATCHES else 0


def per_start(pm):
    """Points he scores in a match he plays.

    Last season's rate is the prior: it is a full season of evidence about the
    same player, and ignoring it means every August the bot believes whoever
    happened to score in week one is the best footballer alive.
    """
    avg = float(pm.get("averagePoints") or 0)
    last = float(pm.get("lastSeasonPoints") or 0) / SEASON_MATCHES
    games = games_played(pm)
    if avg <= 0:
        return last or DEFAULT_RATE
    if games <= 0:
        return avg
    weight = min(1.0, games / SAMPLE_FULL)
    return avg * weight + last * (1 - weight)


def expected(pm, prob_pct):
    """Expected points for one gameweek. `prob_pct` is 0-100, or None for "no idea".

    None means no data, not zero: treating an unknown as a certainty in either
    direction is how a diagnosis becomes a guess. The caller decides the prior
    (see lineup.caliber_prior) and passes it in.
    """
    if prob_pct is None:
        return None
    return max(0.0, float(prob_pct)) / 100.0 * per_start(pm)

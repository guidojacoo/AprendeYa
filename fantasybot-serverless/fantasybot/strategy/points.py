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

from ..matching import position_id

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

# How much the rival he faces this week moves his points, by position.
#
# A goalkeeper's afternoon is decided almost entirely by whether his team keeps a
# clean sheet, and that depends far more on who they are playing than on him. A
# striker facing the same opponent still takes his chances. So the opponent is
# worth more to a keeper than to a forward, and one flat adjustment for everyone
# would be wrong in both directions at once.
#
# This is the clean-sheet effect, reached through the door the data actually
# opens: his own team's defensive quality is already inside his points-per-match
# rate, so all that is missing is who he is up against this week.
FIXTURE_WEIGHT = {1: 0.40, 2: 0.35, 3: 0.25, 4: 0.18}
DEFAULT_FIXTURE_WEIGHT = 0.25


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


def fixture_factor(pm, difficulty):
    """How much easier or harder than average this week's opponent makes it.

    `difficulty` is 0..1 — 0 the league's weakest squad, 1 the strongest (see
    captain.fixture_difficulty_by_team). Absent or unknown means neutral, never
    a penalty: a missing fixture must leave the estimate exactly where it was,
    or a gameweek the calendar has not published yet quietly benches the squad.
    """
    if difficulty is None or isinstance(difficulty, bool):
        return 1.0
    try:
        d = float(difficulty)
    except (TypeError, ValueError):
        return 1.0
    weight = FIXTURE_WEIGHT.get(position_id(pm),
                                DEFAULT_FIXTURE_WEIGHT)
    return max(0.1, 1.0 + weight * (1.0 - 2.0 * min(1.0, max(0.0, d))))


def expected(pm, prob_pct, difficulty=None):
    """Expected points for one gameweek. `prob_pct` is 0-100, or None for "no idea".

    None means no data, not zero: treating an unknown as a certainty in either
    direction is how a diagnosis becomes a guess. The caller decides the prior
    (see lineup.caliber_prior) and passes it in.
    """
    if prob_pct is None:
        return None
    return (max(0.0, float(prob_pct)) / 100.0 * per_start(pm)
            * fixture_factor(pm, difficulty))

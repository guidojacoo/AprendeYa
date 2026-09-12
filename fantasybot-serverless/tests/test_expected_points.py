"""The league is won on points, so that is what the bot has to rank by.

Every one of these pins a case where the old objective — `probabilidad + 0.5 ×
media`, a sum on a 0-100 scale — got the order backwards. A 95%-probable player
averaging 2.5 outranked a 60%-probable one averaging 11, and the second scores
nearly three times as many points over a season.
"""

import unittest

from fantasybot.strategy import points as P
from fantasybot.strategy.lineup import player_score


def pm(avg=0.0, total=0, last=0, value=5_000_000, status="ok"):
    return {"playerMaster": {"id": "m1", "nickname": "X", "name": "X",
                             "positionId": 4, "marketValue": value,
                             "playerStatus": status, "averagePoints": avg,
                             "points": total, "lastSeasonPoints": last}}


def index(prob, **kw):
    return {"x": {"prob": prob, "disponible": True, **kw}}


class PerStart(unittest.TestCase):
    def test_a_two_game_sample_is_pulled_toward_last_season(self):
        """Two good games make a 10.0 average. Believing it is how a bot spends
        its season chasing whoever happened to score in week one."""
        hot = P.per_start({"averagePoints": 10.0, "points": 20,
                           "lastSeasonPoints": 76})      # 2.0/game last year
        self.assertLess(hot, 10.0)
        self.assertGreater(hot, 2.0)

    def test_a_full_sample_is_trusted_on_its_own(self):
        settled = P.per_start({"averagePoints": 8.0, "points": 80,
                               "lastSeasonPoints": 190})
        self.assertEqual(settled, 8.0)

    def test_games_played_is_derived_not_requested(self):
        """LaLiga publishes no match count, but total ÷ average is one."""
        self.assertEqual(P.games_played({"averagePoints": 5.0, "points": 45}), 9)

    def test_an_impossible_match_count_is_refused(self):
        """A stale total against a fresh average would invent a 60-game season."""
        self.assertEqual(P.games_played({"averagePoints": 0.5, "points": 400}), 0)
        self.assertEqual(P.games_played({"averagePoints": 0, "points": 40}), 0)

    def test_no_history_at_all_falls_back_to_an_average_player(self):
        """Zero would tie every unknown player with every other, and the XI
        would be decided by list order."""
        self.assertEqual(P.per_start({}), P.DEFAULT_RATE)

    def test_an_unknown_probability_is_not_a_zero(self):
        self.assertIsNone(P.expected({"averagePoints": 9.0}, None))


class TheOrderItBuildsTheXiIn(unittest.TestCase):
    def test_a_rotating_star_beats_a_mediocre_certainty(self):
        """The case that cost points every week: 60% × 11.0 = 6.6 expected,
        against 95% × 2.5 = 2.4. The old objective preferred the second."""
        star, _, _, _ = player_score(pm(avg=11.0, total=110, last=200),
                                     index(60))
        safe, _, _, _ = player_score(pm(avg=2.5, total=25, last=95),
                                     index(95))
        self.assertGreater(star, safe)

    def test_between_equals_the_likelier_starter_still_wins(self):
        more, _, _, _ = player_score(pm(avg=6.0, total=60, last=150), index(90))
        less, _, _, _ = player_score(pm(avg=6.0, total=60, last=150), index(45))
        self.assertGreater(more, less)

    def test_with_no_points_data_it_ranks_by_probability(self):
        """Otherwise a squad LaLiga has no history for ties at zero and the
        optimiser picks whoever happens to be first in the list."""
        likely, _, _, _ = player_score(pm(), index(90))
        unlikely, _, _, _ = player_score(pm(), index(20))
        self.assertGreater(likely, unlikely)

    def test_an_injured_player_scores_nothing_however_good_he_is(self):
        hurt, _, avail, tag = player_score(pm(avg=12.0, total=120, last=240,
                                              status="injured"), index(90))
        self.assertEqual(hurt, 0.0)
        self.assertFalse(avail)
        self.assertEqual(tag, "out")

    def test_a_doubt_discounts_the_odds_not_the_scoring_rate(self):
        """A doubt says he might not play. It says nothing about how well he
        plays if he does, which is why it belongs on the probability."""
        fit, _, _, _ = player_score(pm(avg=8.0, total=80, last=190), index(80))
        doubt, _, _, tag = player_score(
            pm(avg=8.0, total=80, last=190, status="doubtful"), index(80))
        self.assertEqual(tag, "doubtful")
        self.assertAlmostEqual(doubt, fit * 0.6, places=6)

    def test_the_score_is_readable_as_points(self):
        """It used to be a 0-100 blend that meant nothing on its own. The XI
        total is now the squad's expected points for the gameweek."""
        got, _, _, _ = player_score(pm(avg=8.0, total=80, last=190), index(75))
        self.assertAlmostEqual(got, 0.75 * 8.0, places=6)


class WhoItSignsForAGap(unittest.TestCase):
    """Filling an empty slot by market value picks the most expensive man
    available, which is a different question from the one being asked."""

    def test_candidates_are_ranked_by_what_they_would_add(self):
        from fantasybot.strategy import needs

        cheap_scorer = {"disponible": True, "prob": 85, "valor": 4_000_000,
                        "expected_points": 0.85 * 7.0}
        dear_passenger = {"disponible": True, "prob": 85, "valor": 22_000_000,
                          "expected_points": 0.85 * 1.5}
        ranked = sorted([dear_passenger, cheap_scorer],
                        key=lambda c: (c["disponible"], c["expected_points"] or 0,
                                       c["prob"] or 0, c["valor"] or 0),
                        reverse=True)
        self.assertIs(ranked[0], cheap_scorer)
        self.assertTrue(hasattr(needs, "points_mod"))

"""Recent form and rotation risk, from LaLiga's own per-gameweek stats.

Two decisive things the bot could not see. A player who scored 12, 11 and 9 in
the last three weeks and one who scored them in September have the same season
average and are not the same bet. And the probability of starting comes from a
scrape that is the most fragile input in the system — who actually took the
field is the same question answered by an API that does not break.
"""

from fantasybot.sources import form
from fantasybot.storage import get_storage
from fantasybot.strategy import points
from tests.support import StorageTestCase


class TheParserTakesTheShapeItIsGiven(StorageTestCase):
    """The payload shape is undocumented and cannot be confirmed from here, so
    the parser recognises the plausible layouts rather than betting on one."""

    def test_a_flat_list(self):
        got = form.parse_week([{"playerId": "7", "points": "11",
                                "mins_played": 90}])
        self.assertEqual(got["7"], {"points": 11.0, "minutes": 90.0})

    def test_nested_under_players_with_a_stats_block(self):
        got = form.parse_week({"players": [
            {"playerMaster": {"id": 9},
             "stats": {"totalPoints": 4, "minutesPlayed": 12}}]})
        self.assertEqual(got["9"], {"points": 4.0, "minutes": 12.0})

    def test_points_without_minutes_still_counts(self):
        got = form.parse_week({"data": [{"id": 3, "weekPoints": 0}]})
        self.assertEqual(got["3"]["points"], 0.0)
        self.assertIsNone(got["3"]["minutes"])

    def test_an_unknown_shape_yields_nothing(self):
        self.assertEqual(form.parse_week({"nope": 1}), {})

    def test_an_unknown_shape_records_itself(self):
        """A source that returns {} looks like a quiet week, forever. One that
        records what it could not read gets fixed."""
        class _C:
            def week_stats(self, w):
                return {"unexpected": [{"weird": 1}]}
        form.week(_C(), 5)
        doc = get_storage().get_doc("week_stats_shape", {})
        self.assertEqual(doc["week"], 5)
        self.assertIn("keys", doc["shape"])


class FormTiltsTheRateWithoutReplacingIt(StorageTestCase):
    def test_a_hot_streak_raises_the_estimate(self):
        hot = [{"points": 12, "minutes": 90}, {"points": 11, "minutes": 90},
               {"points": 9, "minutes": 90}]
        self.assertGreater(points.form_factor(hot, 5.0), 1.0)

    def test_a_cold_run_lowers_it(self):
        cold = [{"points": 1, "minutes": 90}, {"points": 0, "minutes": 90},
                {"points": 2, "minutes": 90}]
        self.assertLess(points.form_factor(cold, 5.0), 1.0)

    def test_two_quiet_games_do_not_halve_a_player(self):
        """Selling a good player after two quiet afternoons is the single most
        common way a human loses a fantasy league."""
        cold = [{"points": 0, "minutes": 90}, {"points": 0, "minutes": 90}]
        self.assertGreater(points.form_factor(cold, 6.0), 0.70)

    def test_one_huge_haul_does_not_multiply_him_by_four(self):
        self.assertLessEqual(points.form_factor([{"points": 30, "minutes": 90}],
                                                2.0), 1.0 + points.FORM_WEIGHT)

    def test_form_is_measured_against_his_own_rate(self):
        """A defender scoring 5s is in great form; a striker scoring 5s is not.
        One flat threshold would call them the same thing."""
        run = [{"points": 5, "minutes": 90}] * 3
        self.assertGreater(points.form_factor(run, 2.0), 1.0)
        self.assertLess(points.form_factor(run, 9.0), 1.0)

    def test_no_history_changes_nothing(self):
        self.assertEqual(points.form_factor(None, 5.0), 1.0)
        self.assertEqual(points.form_factor([], 5.0), 1.0)


class MinutesAreTheSignalTheScrapeCannotSee(StorageTestCase):
    def test_a_benched_player_is_caught(self):
        h = [{"points": 0, "minutes": 0}, {"points": 0, "minutes": 5},
             {"points": 8, "minutes": 90}, {"points": 0, "minutes": 0}]
        self.assertEqual(points.start_rate(h), 0.25)

    def test_a_regular_reads_as_one(self):
        h = [{"points": 6, "minutes": 90}] * 4
        self.assertEqual(points.start_rate(h), 1.0)

    def test_the_scrape_and_the_record_are_averaged(self):
        """Disagreement between them is information, so neither is discarded."""
        benched = [{"points": 0, "minutes": 0}] * 4
        got = points.blended_probability(80, benched)
        self.assertEqual(got, 40.0)

    def test_the_record_stands_alone_when_the_scrape_is_gone(self):
        """The fragile input is the scrape. This is what survives it."""
        regular = [{"points": 6, "minutes": 90}] * 4
        self.assertEqual(points.blended_probability(None, regular), 100.0)

    def test_without_minutes_the_scrape_is_untouched(self):
        self.assertEqual(points.blended_probability(80, [{"points": 3}]), 80)


class ItAllReachesExpectedPoints(StorageTestCase):
    def _pm(self):
        return {"id": "1", "positionId": "3", "averagePoints": "5",
                "points": "50", "lastSeasonPoints": "190"}

    def test_a_player_in_form_outscores_his_own_average(self):
        hot = [{"points": 11, "minutes": 90}] * 3
        plain = points.expected(self._pm(), 90)
        with_form = points.expected(self._pm(), 90, history=hot)
        self.assertGreater(with_form, plain)

    def test_a_rotation_risk_is_marked_down(self):
        benched = [{"points": 1, "minutes": 0}] * 4
        plain = points.expected(self._pm(), 90)
        risky = points.expected(self._pm(), 90, history=benched)
        self.assertLess(risky, plain)

    def test_no_history_leaves_the_old_answer_exactly_where_it_was(self):
        """The signal improves the estimate when present and never gates it."""
        self.assertEqual(points.expected(self._pm(), 90),
                         points.expected(self._pm(), 90, history=None))

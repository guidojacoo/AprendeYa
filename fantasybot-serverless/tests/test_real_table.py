"""Judge the opponent by how he is playing, not by what his squad cost.

Squad value says Madrid are hard, and that is defensible and beside the point:
an expensive squad losing every week is an easy fixture, and a cheap one
conceding nothing is not. The answer was already in reach and being thrown
away — `/stats/week/{n}` is the FIXTURE LIST, ten rows carrying localScore and
visitorScore, which is a results table waiting to be added up.

Value stays as the prior, because four gameweeks is a small sample and a club
can lose twice to the two best teams in Spain without becoming bad.
"""

from fantasybot.sources import standings
from tests.support import StorageTestCase


def _m(home, away, hs, as_, state="finished"):
    return {"local": home, "visitor": away, "localScore": hs,
            "visitorScore": as_, "matchState": state}


class TheTable(StorageTestCase):
    def test_it_adds_up_points_and_goals(self):
        got = standings.tally([[_m("1", "2", 3, 0)], [_m("2", "1", 1, 1)]])
        self.assertEqual(got["1"], {"points": 4, "played": 2, "gf": 4, "ga": 1})
        self.assertEqual(got["2"], {"points": 1, "played": 2, "gf": 1, "ga": 4})

    def test_an_unplayed_match_is_not_counted(self):
        got = standings.tally([[_m("1", "2", None, None, state="scheduled")]])
        self.assertEqual(got, {})

    def test_two_scores_mean_played_whatever_the_state_says(self):
        """The state field is undocumented; a result is not."""
        got = standings.tally([[_m("1", "2", 2, 1, state="7")]])
        self.assertEqual(got["1"]["points"], 3)

    def test_a_side_given_as_an_object_still_reads(self):
        got = standings.tally([[{"local": {"id": "9"}, "visitor": {"id": "4"},
                                 "localScore": 1, "visitorScore": 0}]])
        self.assertEqual(got["9"]["points"], 3)

    def test_junk_rows_are_skipped_not_fatal(self):
        got = standings.tally([[None, {"local": "1"}, _m("1", "2", 1, 0)]])
        self.assertEqual(got["1"]["played"], 1)


class Strength(StorageTestCase):
    def test_the_best_record_scores_one_and_the_worst_zero(self):
        table = standings.tally([[_m("1", "2", 3, 0)], [_m("1", "2", 2, 0)]])
        got = standings.strength_from(table)
        self.assertEqual(got["1"], 1.0)
        self.assertEqual(got["2"], 0.0)

    def test_goal_difference_separates_equal_points(self):
        table = {"a": {"points": 3, "played": 1, "gf": 4, "ga": 0},
                 "b": {"points": 3, "played": 1, "gf": 1, "ga": 0}}
        got = standings.strength_from(table)
        self.assertGreater(got["a"], got["b"])

    def test_a_club_with_no_games_is_absent_rather_than_zero(self):
        got = standings.strength_from({"a": {"points": 0, "played": 0,
                                             "gf": 0, "ga": 0}})
        self.assertEqual(got, {})

    def test_an_empty_table_is_empty_not_a_crash(self):
        self.assertEqual(standings.strength_from({}), {})


class ResultsTiltThePriorRatherThanReplacingIt(StorageTestCase):
    def test_one_game_barely_moves_it(self):
        got = standings.blend({"a": 0.0}, {"a": 1.0}, {"a": 1})
        self.assertGreater(got["a"], 0.7, "one bad afternoon is not a verdict")

    def test_a_full_sample_mostly_wins(self):
        got = standings.blend({"a": 0.0}, {"a": 1.0},
                              {"a": standings.FULL_SAMPLE})
        self.assertLess(got["a"], 0.4)

    def test_a_club_the_table_does_not_know_keeps_its_prior(self):
        got = standings.blend({}, {"a": 0.8}, {})
        self.assertEqual(got["a"], 0.8)

    def test_week_one_leaves_everything_exactly_as_it_was(self):
        prior = {"a": 0.9, "b": 0.2}
        self.assertEqual(standings.blend({}, prior, {}), prior)

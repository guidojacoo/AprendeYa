"""What it believed, and what actually happened.

The optimiser has always said what it expects the eleven to score. That number
went nowhere, so the model was never wrong about anything — and a model nobody
scores is a model that cannot improve. Both halves are cheap: the prediction is
already computed, and the results come from the same `weekPoints` read the form
model already makes.
"""

from fantasybot import memory
from fantasybot.storage import get_storage
from tests.support import StorageTestCase


def _xi(n=11, each=4.0):
    return {"formation": (4, 4, 2), "total": n * each,
            "goalkeeper": {"playerTeamId": "p0", "nombre": "P0", "score": each},
            "defender": [{"playerTeamId": f"d{i}", "nombre": f"D{i}",
                          "score": each} for i in range(4)],
            "midfield": [{"playerTeamId": f"m{i}", "nombre": f"M{i}",
                          "score": each} for i in range(4)],
            "striker": [{"playerTeamId": f"s{i}", "nombre": f"S{i}",
                         "score": each} for i in range(2)]}


class ItWritesDownWhatItExpects(StorageTestCase):
    def test_a_prediction_is_banked_with_the_whole_eleven(self):
        got = memory.predict(5, _xi())
        self.assertEqual(got["expected"], 44.0)
        self.assertEqual(len(got["xi"]), 11)
        self.assertIsNone(got["actual"])

    def test_re_optimising_before_kickoff_overwrites_it(self):
        """The prediction that matters is the team actually fielded."""
        memory.predict(5, _xi(each=4.0))
        got = memory.predict(5, _xi(each=6.0))
        self.assertEqual(got["expected"], 66.0)

    def test_a_settled_week_is_never_overwritten(self):
        memory.predict(5, _xi())
        memory.settle(5, {"p0": 9})
        got = memory.predict(5, _xi(each=99.0))
        self.assertEqual(got["actual"], 9.0, "a result is history")

    def test_no_week_no_prediction(self):
        self.assertIsNone(memory.predict(None, _xi()))


class ItSettlesAgainstRealPoints(StorageTestCase):
    def test_it_adds_up_what_the_fielded_eleven_scored(self):
        memory.predict(5, _xi())
        got = memory.settle(5, {"p0": 6, "d0": 2, "d1": 2, "d2": 2, "d3": 2,
                                "m0": 5, "m1": 5, "m2": 5, "m3": 5,
                                "s0": 8, "s1": 1})
        self.assertEqual(got["actual"], 43.0)
        self.assertEqual(got["error"], -1.0)
        self.assertEqual(got["settled_players"], 11)

    def test_a_player_we_lost_track_of_contributes_nothing_not_zero(self):
        """"Did not play" and "we cannot find him" are different, and only one
        of those is his fault."""
        memory.predict(5, _xi())
        got = memory.settle(5, {"p0": 6})
        self.assertEqual(got["actual"], 6.0)
        self.assertEqual(got["settled_players"], 1)

    def test_nothing_published_yet_leaves_it_open(self):
        memory.predict(5, _xi())
        got = memory.settle(5, {"nobody": 3})
        self.assertIsNone(got["actual"], "try again next tick")

    def test_settling_an_unknown_week_is_harmless(self):
        self.assertIsNone(memory.settle(99, {"p0": 3}))


class TheRunningScore(StorageTestCase):
    def test_no_settled_weeks_says_so(self):
        self.assertEqual(memory.accuracy()["weeks"], 0)

    def test_bias_is_the_part_that_is_actionable(self):
        """Consistently short means the estimates are too shy; consistently
        over means too generous. One week is noise, a run of them is a
        correction."""
        for wk in (3, 4, 5):
            memory.predict(wk, _xi(each=4.0))          # expects 44
            memory.settle(wk, {r["playerTeamId"]: 5.0
                               for line in ("goalkeeper",)
                               for r in [_xi()[line]]} |
                          {f"d{i}": 5.0 for i in range(4)} |
                          {f"m{i}": 5.0 for i in range(4)} |
                          {f"s{i}": 5.0 for i in range(2)})
        got = memory.accuracy()
        self.assertEqual(got["weeks"], 3)
        self.assertGreater(got["bias"], 0, "it was scoring more than expected")
        self.assertEqual(got["mean_abs_error"], 11.0)

    def test_the_ledger_and_the_predictions_do_not_collide(self):
        memory.record({"first_run": False, "added": ["Nuevo"], "removed": [],
                       "money_delta": -1})
        memory.predict(5, _xi())
        self.assertEqual(len(memory.recent()), 1)
        self.assertEqual(get_storage().get_doc(memory.PREDICTIONS)["5"]["expected"],
                         44.0)

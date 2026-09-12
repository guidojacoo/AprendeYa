"""The four things that decide whether the season ends well.

Each of these was a place where the bot answered a question next to the one it
was being asked: which XI, whom to sign, whom to sell, and what to spend on.
"""

import unittest

from fantasybot.strategy import points as P
from fantasybot.strategy import scoring, sell


def pm(pos=4, avg=6.0, total=60, last=160, value=8_000_000, status="ok",
       team_id="7"):
    return {"id": "m1", "nickname": "X", "name": "X", "positionId": pos,
            "marketValue": value, "playerStatus": status, "averagePoints": avg,
            "points": total, "lastSeasonPoints": last, "team": {"id": team_id}}


class TheOpponentHeFaces(unittest.TestCase):
    """A gameweek against the leaders is not a gameweek against the bottom club,
    and the bot used to field the same XI for both."""

    def test_an_easy_fixture_is_worth_more_than_a_hard_one(self):
        easy = P.expected(pm(), 80, difficulty=0.0)
        hard = P.expected(pm(), 80, difficulty=1.0)
        self.assertGreater(easy, hard)

    def test_it_matters_more_to_a_keeper_than_to_a_striker(self):
        """A keeper's afternoon is decided by whether his team keeps a clean
        sheet, which is mostly about the opponent. A striker still takes his
        chances. One flat adjustment would be wrong in both directions."""
        def swing(pos):
            return (P.expected(pm(pos=pos), 80, 0.0)
                    - P.expected(pm(pos=pos), 80, 1.0))
        self.assertGreater(swing(1), swing(4))   # portero vs delantero
        self.assertGreater(swing(2), swing(3))   # defensa vs medio

    def test_no_fixture_known_is_neutral_never_a_penalty(self):
        """A gameweek the calendar has not published yet must leave the estimate
        exactly where it was, not quietly bench the squad."""
        self.assertEqual(P.expected(pm(), 80), P.expected(pm(), 80, None))
        self.assertEqual(P.expected(pm(), 80), P.expected(pm(), 80, "raro"))

    def test_the_xi_uses_it(self):
        from fantasybot.strategy.lineup import player_score
        index = {"x": {"prob": 80, "disponible": True}}
        easy, _, _, _ = player_score({"playerTeamId": "p", "playerMaster": pm()},
                                     index, {"7": 0.0})
        hard, _, _, _ = player_score({"playerTeamId": "p", "playerMaster": pm()},
                                     index, {"7": 1.0})
        self.assertGreater(easy, hard)


class WhoASigningHasToBeat(unittest.TestCase):
    """"Better than an average starter" is no reason to sign anybody when your
    own line is already better than average."""

    def test_the_baseline_is_the_weakest_man_in_that_line(self):
        best = {"goalkeeper": {"score": 4.0},
                "defender": [{"score": 5.0}, {"score": 2.5}],
                "midfield": [{"score": 7.0}], "striker": [{"score": 6.0}]}
        got = scoring.replacement_from_xi(best)
        self.assertEqual(got["DEF"], 2.5, "the one he would actually displace")
        self.assertEqual(got["POR"], 4.0)

    def test_a_signing_is_scored_against_that_man(self):
        op = {"nombre": "X", "pos": "DEL", "margin_pct": 0.0,
              "buy_price": 8_000_000, "proyeccion": 8_000_000,
              "avg_points": 6.0, "season_points": 60, "last_season_points": 160}
        weak_line = scoring.score(op, prob=90, replacement={"DEL": 1.0})
        strong_line = scoring.score(op, prob=90, replacement={"DEL": 8.0})
        self.assertGreater(weak_line["score"], strong_line["score"])
        self.assertTrue(any("sube" in r for r in weak_line["reasons"]))
        self.assertTrue(any("no mejora" in r for r in strong_line["reasons"]))

    def test_an_empty_xi_falls_back_to_the_average(self):
        self.assertEqual(scoring.replacement_from_xi(None), {})
        self.assertEqual(scoring.replacement_from_xi({}), {})

    def test_it_reports_what_a_euro_buys(self):
        """Two decent signings can beat one expensive one, and only this number
        says so."""
        op = {"nombre": "X", "pos": "DEL", "margin_pct": 0.0,
              "buy_price": 10_000_000, "proyeccion": 10_000_000,
              "avg_points": 5.0, "season_points": 50, "last_season_points": 0}
        got = scoring.score(op, prob=100)
        self.assertEqual(got["expected_points"], 5.0)
        self.assertEqual(got["points_per_million"], 0.5)

    def test_the_money_headline_survives_the_extra_reason(self):
        """A line is appended after it, so "the last reason" stopped being "the
        money" the moment that happened."""
        op = {"nombre": "X", "pos": "DEL", "margin_pct": 20.0,
              "buy_price": 30_000_000, "proyeccion": 36_000_000,
              "avg_points": 9.0, "season_points": 90, "last_season_points": 0}
        got = scoring.score(op, prob=95, money=1_000_000)
        self.assertEqual(got["verdict"], "no alcanza")
        self.assertIn("No alcanza la caja", got["headline"])


class DeadCapital(unittest.TestCase):
    """A valuable man who never scores was invisible to every old rule, and he
    is exactly the one to sell: he keeps his price, so somebody will pay it."""

    TRENDS = {"x": {"valor": 9_000_000, "tendencia": 5}}

    def _team(self, **kw):
        return {"teamMoney": 30_000_000,
                "players": [{"playerTeamId": "pt1", "playerMaster": pm(**kw)}]}

    def _best(self):
        return {"goalkeeper": {"playerTeamId": "other"}, "defender": [],
                "midfield": [], "striker": [], "formation": (4, 3, 3),
                "payload": {"goalkeeper": "other", "defender": [],
                            "midfield": [], "striker": []}}

    def test_expensive_and_pointless_is_sold(self):
        out = sell.sell_candidates(
            self._team(avg=1.0, total=10, last=20, value=9_000_000),
            self._best(), self.TRENDS, prob_index={"x": {"prob": 30}})
        self.assertEqual(len(out), 1)
        self.assertIn("no puntúa para lo que vale", out[0]["reason"])

    def test_a_good_player_with_a_stale_probability_is_kept(self):
        """Probable-lineup data lags badly for a recent signing. A man who
        scores when he plays showing 3% is a stale feed, not dead capital."""
        out = sell.sell_candidates(
            self._team(avg=8.0, total=80, last=190, value=9_000_000),
            self._best(), self.TRENDS, prob_index={"x": {"prob": 3}})
        self.assertEqual(out, [])

    def test_a_cheap_non_scorer_is_left_alone(self):
        """Selling him buys nothing, and a bench needs filling too."""
        out = sell.sell_candidates(
            self._team(avg=0.5, total=5, last=10, value=1_000_000),
            self._best(), self.TRENDS, prob_index={"x": {"prob": 10}})
        self.assertEqual(out, [])

    def test_no_scoring_history_is_never_a_verdict(self):
        out = sell.sell_candidates(
            self._team(avg=0, total=0, last=0, value=9_000_000),
            self._best(), self.TRENDS, prob_index={"x": {"prob": 20}})
        self.assertEqual(out, [], "no evidence is not evidence of nothing")


class SpendingTheBudget(unittest.TestCase):
    def test_a_gap_is_filled_by_points_per_euro(self):
        """Filling the slot with the first affordable name spends the whole
        budget on one player when two cheaper ones score more between them."""
        from fantasybot.tick import _points_per_euro

        dear = {"expected_points": 6.0, "prob": 90}
        cheap = {"expected_points": 4.5, "prob": 85}
        self.assertGreater(_points_per_euro(cheap, 5_000_000),
                           _points_per_euro(dear, 20_000_000))

    def test_a_candidate_without_data_is_valued_as_ordinary(self):
        """Not as the best available, and not as worthless — either would let a
        missing field decide the signing."""
        from fantasybot.tick import _points_per_euro

        unknown = _points_per_euro({"prob": 50}, 5_000_000)
        self.assertGreater(unknown, 0)
        self.assertLess(unknown,
                        _points_per_euro({"expected_points": 6.0}, 5_000_000))

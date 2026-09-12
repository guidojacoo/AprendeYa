"""The number next to a player, and the sentences that justify it.

The score exists to be argued with. Every one of these pins a case where a
number alone would mislead: a fat margin on somebody who does not play, a
bargain nobody can pay for, a rise that is already turning over.
"""

import unittest

from fantasybot.strategy import scoring


def op(**kw):
    base = {"nombre": "Jugador", "margin_pct": 0.0, "buy_price": 5_000_000,
            "proyeccion": 5_000_000, "tendencia": None,
            "last_season_points": 0, "avg_points": 0.0, "season_points": 0,
            "pos": "DEL", "via": "SISTEMA"}
    base.update(kw)
    return base


class Score(unittest.TestCase):
    def test_the_margin_is_the_spine_of_it(self):
        poor = scoring.score(op(margin_pct=-15))["score"]
        flat = scoring.score(op(margin_pct=0))["score"]
        rich = scoring.score(op(margin_pct=15))["score"]
        self.assertLess(poor, flat)
        self.assertLess(flat, rich)

    def test_a_benchwarmer_is_marked_down_hard(self):
        """A player who does not start scores no points, however cheap — and the
        penalty scales with how good he is, because 15% of a nine-point player
        is a bigger loss than 15% of a two-point one."""
        good = {"avg_points": 9.0, "season_points": 90,
                "last_season_points": 200}
        starter = scoring.score(op(margin_pct=10, **good), prob=90)
        bench = scoring.score(op(margin_pct=10, **good), prob=15)
        self.assertGreater(starter["score"], bench["score"] + 30)
        self.assertIn("1.3 puntos", bench["headline"])

    def test_points_outrank_a_bargain(self):
        """The whole reason the score was rebuilt: the league is won on points,
        and a flip engine ranked a cheap non-scorer above a scoring starter."""
        scorer = scoring.score(op(margin_pct=1, avg_points=9.0,
                                  season_points=90, last_season_points=200),
                               prob=85)
        bargain = scoring.score(op(margin_pct=18, avg_points=1.5,
                                   season_points=15, last_season_points=40),
                                prob=85)
        self.assertGreater(scorer["score"], bargain["score"])

    def test_a_falling_value_is_called_what_it_is(self):
        falling = scoring.score(op(margin_pct=5, tendencia=-3))
        self.assertTrue(any("bajando" in r for r in falling["reasons"]))
        self.assertLess(falling["score"], scoring.score(op(margin_pct=5))["score"])

    def test_money_changes_the_verdict_and_not_the_score(self):
        """A good player you cannot pay for is still a good player. Folding the
        balance into the score would hide him the moment the cash dips."""
        rich = scoring.score(op(margin_pct=20, buy_price=9_000_000),
                             money=20_000_000)
        broke = scoring.score(op(margin_pct=20, buy_price=9_000_000),
                              money=1_000_000)
        self.assertEqual(rich["score"], broke["score"])
        self.assertTrue(rich["affordable"])
        self.assertFalse(broke["affordable"])
        self.assertEqual(broke["verdict"], "no alcanza")

    def test_every_score_carries_its_reasons(self):
        got = scoring.score(op(margin_pct=12), prob=80)
        self.assertTrue(got["reasons"])
        self.assertTrue(all(isinstance(r, str) and r for r in got["reasons"]))

    def test_it_stays_inside_the_scale(self):
        self.assertLessEqual(scoring.score(op(margin_pct=900))["score"], 100)
        self.assertGreaterEqual(scoring.score(op(margin_pct=-900))["score"], 0)

    def test_a_missing_price_does_not_crash_it(self):
        got = scoring.score(op(buy_price=None, proyeccion=None))
        self.assertIsInstance(got["score"], int)


class Rank(unittest.TestCase):
    def test_best_first_and_the_declined_are_kept(self):
        """The declined half is the point: the question you ask is about the
        player you did NOT get."""
        ranked = scoring.rank([op(nombre="Bueno", margin_pct=25),
                               op(nombre="Malo", margin_pct=-25)],
                              money=50_000_000)
        self.assertEqual([r["nombre"] for r in ranked], ["Bueno", "Malo"])
        self.assertEqual(ranked[1]["verdict"], "no vale la pena")

    def test_the_limit_trims_the_tail_not_the_head(self):
        ranked = scoring.rank([op(nombre=f"p{i}", margin_pct=i)
                               for i in range(10)], limit=3)
        self.assertEqual([r["nombre"] for r in ranked], ["p9", "p8", "p7"])

    def test_it_survives_an_empty_market(self):
        self.assertEqual(scoring.rank([]), [])


class ItReachesThePage(unittest.TestCase):
    """Scoring the market is worth nothing if the dashboard never sees it. The
    summary is the only thing stored, so that is where this has to hold."""

    def test_the_summary_carries_the_scored_market(self):
        from fantasybot import tick
        # Half-point steps so no two land on the same score: above +25% the
        # scale caps at 100 and the order is decided by price instead.
        market = scoring.rank([op(nombre=f"p{i}", margin_pct=i * 0.5)
                               for i in range(40)])
        got = tick._summarize({"money": 1, "market": market,
                               "flips": [], "lineup": {}}, {}, {})
        self.assertEqual(len(got["market"]), 30, "trimmed for a phone, not empty")
        self.assertEqual(got["market"][0]["nombre"], "p39")
        self.assertIn("reasons", got["market"][0])

    def test_the_log_names_both_sides_of_the_read(self):
        from unittest import mock

        from fantasybot import tick
        market = scoring.rank([op(nombre="Bueno", margin_pct=30),
                               op(nombre="Malo", margin_pct=-30)])
        with mock.patch.object(tick.events, "emit") as emit:
            tick._note_market_read({"market": market})
        title = emit.call_args[0][1]
        self.assertIn("Bueno", title)
        self.assertIn("Malo", emit.call_args[1]["detail"]["descartado"])

    def test_an_empty_market_logs_nothing(self):
        from unittest import mock

        from fantasybot import tick
        with mock.patch.object(tick.events, "emit") as emit:
            tick._note_market_read({})
        emit.assert_not_called()

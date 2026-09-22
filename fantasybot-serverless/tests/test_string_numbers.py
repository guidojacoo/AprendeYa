"""LaLiga sends numbers as strings, in the same payload as numbers.

The squad carries positionId "4" and marketValue "2683751" side by side. This is
not a curiosity: `round("2683751" * 1.15)` raises, and that one TypeError cost
the selling half of the bot. No reserve price could be computed, so nobody was
ever put on the market, so no offer ever arrived, so nothing ever sold — and the
review reported itself healthy throughout.

Every one of these runs the real code against the real shape.
"""

import unittest

from fantasybot.matching import num
from fantasybot.strategy import flip, lineup, needs, offers, sell


def player(pid="pt1", mid="m1", pos="4", value="2683751", status="ok"):
    """A squad member exactly as the API hands him over: everything a string."""
    return {"playerTeamId": pid,
            "playerMaster": {"id": mid, "nickname": f"J{mid}", "name": f"J{mid}",
                             "positionId": pos, "marketValue": value,
                             "playerStatus": status, "averagePoints": "4.5",
                             "points": "45", "lastSeasonPoints": "120"}}


class Num(unittest.TestCase):
    def test_it_reads_every_shape_lalig_a_sends(self):
        self.assertEqual(num("2683751"), 2683751)
        self.assertEqual(num(2683751), 2683751)
        self.assertEqual(num("2,683,751"), 2683751)
        self.assertEqual(num(" 12 "), 12)

    def test_junk_becomes_the_default_never_a_guess(self):
        self.assertEqual(num(None), 0)
        self.assertEqual(num("no", 7), 7)
        self.assertEqual(num({}, 3), 3)

    def test_a_bool_is_not_a_number(self):
        """True == 1 in Python, and a flag read as a price is a silent wrong
        answer rather than a loud one."""
        self.assertEqual(num(True), 0)
        self.assertEqual(num(False), 0)


class Selling(unittest.TestCase):
    def test_a_reserve_price_can_be_computed_at_all(self):
        """The bug was a crash on the string, not the exact number: a squad
        player outside the eleven now asks market value itself (see
        MOVABLE_ASK) rather than a premium over it, so the assertion is that
        this returns a real positive price and not that it raised."""
        price = offers.reserve_price(player(), set(), set())
        self.assertEqual(price, 2_683_751)

    def test_the_whole_squad_goes_on_the_market(self):
        """The bug, end to end: fifteen players, none listed, and the planner
        returned nothing at all."""
        squad = {"players": [player(f"pt{i}", f"m{i}") for i in range(15)]}
        rows = offers.plan_listings(squad, [], None, [])
        self.assertEqual(len(rows), 15)
        self.assertTrue(all(r["price"] > 0 for r in rows))


class TheRestOfTheReview(unittest.TestCase):
    def test_the_xi_prices_an_unmatched_player(self):
        """caliber_prior compares against 15_000_000, and a string is not
        comparable to an int — so one unmatched player took the XI down."""
        score, _, _, tag = lineup.player_score(player(), {})
        self.assertEqual(tag, "unknown")
        self.assertGreater(score, 0)

    def test_the_squad_census_counts_them(self):
        squad = {"players": [player("pt1", "m1", "1"), player("pt2", "m2", "4")]}
        self.assertEqual(needs.squad_counts(squad)["POR"], 1)

    def test_the_sell_advisor_compares_a_price(self):
        best = {"goalkeeper": {"playerTeamId": "other"}, "defender": [],
                "midfield": [], "striker": [],
                "payload": {"goalkeeper": "other", "defender": [],
                            "midfield": [], "striker": []}}
        got = sell.sell_candidates({"teamMoney": "5000000",
                                    "players": [player()]}, best,
                                   {"jm1": {"valor": 2_683_751, "tendencia": -50}})
        self.assertEqual(len(got), 1, "a falling value is still a sell signal")

    def test_the_bidder_can_price_a_listing(self):
        from fantasybot import bidding

        row = {"id": "m1", "salePrice": "2683751",
               "playerMaster": {"id": "p1", "marketValue": "2919201"}}
        client = type("C", (), {"market": lambda self, lid: [row]})()
        res = bidding.snipe("L", "m1", 4_000_000, client=client,
                            budget_seconds=0.2, log=lambda m: None)
        self.assertNotEqual(res["status"], "unpriced")

    def test_a_flip_prices_a_system_listing(self):
        el = {"id": "m1", "discr": "marketPlayerLeague", "salePrice": "2683751",
              "expirationDate": "2026-09-13T20:00:00+00:00",
              "playerMaster": {"id": "p1", "nickname": "Uno", "name": "Uno",
                               "positionId": "4", "marketValue": "2683751",
                               "lastSeasonPoints": "100", "averagePoints": "5",
                               "points": "50"}}
        trend = {"valor": 2_700_000, "tendencia": 2, "valor1": 2_650_000,
                 "valor3": 2_600_000, "valor7": 2_500_000}
        got = flip.evaluate(el, {"uno": trend}, horizon=3)
        self.assertIsNotNone(got)
        self.assertGreater(got["buy_price"], 0)

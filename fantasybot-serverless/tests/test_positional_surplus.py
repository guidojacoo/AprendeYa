"""Ten defenders when a formation fields five.

Reported live: "tenemos 10 defensores, 3 porteros, 7 mediocampos y 3 delanteros
... necesito que compre y venda jugadores todo el tiempo, que no se quede con
basura."

The pricing asked what a player was worth to us and answered one player at a
time. A seventh defender has perfectly good expected points in isolation — the
optimiser simply never fields him, because five better ones are ahead of him —
so he came out as "a bench player who is still an asset" at market value plus
fifteen per cent, and sat there all season. Six of that squad's twenty-three
were in that state, and the money in them was the money that ran out.

Dead capital is a property of the squad's SHAPE, not of the player, and only a
position-wide view can see it.
"""

from fantasybot.strategy import offers
from tests.support import StorageTestCase

POS = {"POR": 1, "DEF": 2, "MED": 3, "DEL": 4}


def _squad(counts):
    players, expected = [], {}
    for pos, n in counts.items():
        for i in range(n):
            ptid = f"{pos}{i}"
            players.append({"playerTeamId": ptid, "playerMaster": {
                "id": ptid, "nickname": ptid, "positionId": str(POS[pos]),
                "marketValue": "5000000", "averagePoints": "4",
                "points": "40", "playerStatus": "ok"}})
            expected[ptid] = 6.0 - i * 0.4
    return {"teamMoney": 0, "players": players}, expected


class WhoCannotPossiblyPlay(StorageTestCase):
    def test_the_reported_squad_frees_six(self):
        """3 POR / 10 DEF / 7 MED / 3 DEL, exactly as reported."""
        team, expected = _squad({"POR": 3, "DEF": 10, "MED": 7, "DEL": 3})
        got = offers.surplus_ids(team, expected)
        self.assertEqual(sorted(got),
                         ["DEF6", "DEF7", "DEF8", "DEF9", "MED6", "POR2"])

    def test_the_ones_cut_are_the_ones_we_would_field_last(self):
        team, expected = _squad({"POR": 1, "DEF": 8, "MED": 5, "DEL": 3})
        got = offers.surplus_ids(team, expected)
        self.assertNotIn("DEF0", got, "the best defender is never surplus")
        self.assertIn("DEF7", got, "the worst one is")

    def test_a_balanced_squad_has_no_surplus(self):
        team, expected = _squad({"POR": 2, "DEF": 6, "MED": 6, "DEL": 4})
        self.assertEqual(offers.surplus_ids(team, expected), set())

    def test_one_spare_per_line_is_kept(self):
        """Selling down to exactly the eleven is its own kind of broken."""
        team, expected = _squad({"POR": 2, "DEF": 6, "MED": 5, "DEL": 3})
        self.assertEqual(offers.surplus_ids(team, expected), set())

    def test_the_ceilings_come_from_the_real_formations(self):
        """Typed numbers go stale; the formation table is the truth."""
        self.assertEqual(offers._max_fieldable(False),
                         {"POR": 1, "DEF": 5, "MED": 5, "DEL": 3})
        self.assertEqual(offers._max_fieldable(True)["MED"], 6,
                         "4-6-0 and 3-6-1 field six midfielders")

    def test_a_coach_occupies_no_outfield_slot(self):
        team, expected = _squad({"DEF": 6})
        team["players"].append({"playerTeamId": "ent", "playerMaster": {
            "id": "ent", "positionId": "5", "marketValue": "1000000"}})
        self.assertNotIn("ent", offers.surplus_ids(team, expected))

    def test_without_expected_points_it_still_ranks_and_never_crashes(self):
        team, _ = _squad({"DEF": 9})
        self.assertEqual(len(offers.surplus_ids(team, None)), 3)


class SurplusIsPricedToLeave(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.team, self.expected = _squad(
            {"POR": 3, "DEF": 10, "MED": 7, "DEL": 3})
        self.surplus = offers.surplus_ids(self.team, self.expected)

    def _player(self, pid):
        return next(p for p in self.team["players"]
                    if p["playerMaster"]["id"] == pid)

    def test_a_surplus_defender_is_asked_UNDER_market_value(self):
        """The bug, in one assertion: he used to be asked +15%."""
        p = self._player("DEF9")
        self.assertLess(offers.premium_for(p, [], [], self.expected,
                                           self.surplus), 0)
        self.assertLess(
            offers.reserve_price(p, [], [], expected=self.expected,
                                 surplus=self.surplus),
            5_000_000)

    def test_a_needed_defender_keeps_his_premium(self):
        p = self._player("DEF0")
        self.assertGreater(offers.premium_for(p, [], [], self.expected,
                                              self.surplus), 0)

    def test_a_surplus_player_who_starts_is_still_protected(self):
        """Contradictory inputs must not sell the eleven.

        If the optimiser fields him, he is not surplus whatever the count says,
        and the XI premium has to win.
        """
        p = self._player("DEF9")
        self.assertEqual(
            offers.premium_for(p, ["DEF9"], [], self.expected, self.surplus),
            offers.XI_PREMIUM)

    def test_the_whole_squad_prices_without_surplus_unchanged(self):
        """Passing no surplus set reproduces the old behaviour exactly."""
        for p in self.team["players"]:
            self.assertEqual(
                offers.reserve_price(p, [], [], expected=self.expected),
                offers.reserve_price(p, [], [], expected=self.expected,
                                     surplus=()))

    def test_the_listing_plan_flags_why(self):
        rows = offers.plan_listings(self.team, [], best=None, sells=[],
                                    expected=self.expected)
        flagged = {r["nombre"] for r in rows if r.get("sobra_en_su_posicion")}
        self.assertEqual(flagged, self.surplus)

    def test_the_offer_handler_agrees_with_the_listing(self):
        """Two prices for one player is how a sale gets advertised then refused."""
        rows = {r["nombre"]: r["price"]
                for r in offers.plan_listings(self.team, [], best=None,
                                              sells=[], expected=self.expected)}
        reserves = offers.reserve_map(self.team, best=None, sells=[],
                                      expected=self.expected)
        for nombre, price in rows.items():
            self.assertEqual(reserves[nombre], price, nombre)


if __name__ == "__main__":
    import unittest
    unittest.main()

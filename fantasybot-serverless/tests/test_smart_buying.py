"""Why the squad ended up with ten defenders and three million euros.

Reported live: "tenemos 10 defensores ... necesito que compre y venda jugadores
todo el tiempo ... compras inteligentes."

`gain_from` measures what a signing ADDS to the eleven, and it is right about
that. What nothing measured is what a signing PUSHES OUT. On the reported squad
an eleventh defender scored a 1.60 gain against a fourth striker's 1.43 — so the
ranking actively PREFERRED the line that was already six players over its
ceiling, because a good defender displaces a weak starter in a five-man line
while the three-man attack was already decent.

Both cleared the bar, so both got bought, every week, into the same lines. Ten
defenders and three million euros is what that produces.

A signing costs its price plus whatever it freezes.
"""

from fantasybot import modes
from fantasybot.strategy import upgrades
from tests.support import StorageTestCase

POS = {"POR": 1, "DEF": 2, "MED": 3, "DEL": 4}


def _pm(pid, pos, avg, value=5_000_000):
    return {"id": pid, "nickname": pid, "positionId": str(POS[pos]),
            "marketValue": str(value), "averagePoints": str(avg),
            "points": str(avg * 10), "lastSeasonPoints": str(avg * 38),
            "playerStatus": "ok"}


def _squad(counts):
    players = []
    for pos, n in counts.items():
        for i in range(n):
            players.append({"playerTeamId": f"{pos}{i}",
                            "playerMaster": _pm(f"{pos}{i}", pos, 6.0 - i * 0.4)})
    return {"teamMoney": 30_000_000, "players": players}


REPORTED = {"POR": 3, "DEF": 10, "MED": 7, "DEL": 3}


class BuyingIntoAFullLineFreezesMoney(StorageTestCase):
    def test_an_eleventh_defender_freezes_capital(self):
        frozen, who = upgrades.freezes_capital(
            _squad(REPORTED), _pm("nuevo", "DEF", 7.0, 10_000_000))
        self.assertGreater(frozen, 0)
        self.assertIsNotNone(who)

    def test_a_fourth_striker_freezes_nothing(self):
        """Three forwards with a three-man attack: the line has room."""
        frozen, who = upgrades.freezes_capital(
            _squad(REPORTED), _pm("nuevo", "DEL", 7.0, 10_000_000))
        self.assertEqual((frozen, who), (0, None))

    def test_a_mediocre_signing_freezes_HIS_OWN_price(self):
        """If he is the one who would not fit, the frozen money is his.

        Ranking the newcomer with the rest instead of assuming he is the best
        is what keeps this honest — otherwise a bad buy looks cheap because it
        "displaces" somebody good.
        """
        frozen, who = upgrades.freezes_capital(
            _squad(REPORTED), _pm("malo", "DEF", 0.1, 9_000_000))
        self.assertTrue(who["es_el_que_ficho"])
        self.assertEqual(frozen, 9_000_000)

    def test_a_coach_is_not_an_outfield_slot(self):
        frozen, who = upgrades.freezes_capital(
            _squad(REPORTED), {"id": "e", "positionId": "5",
                               "marketValue": "1000000"})
        self.assertEqual((frozen, who), (0, None))


class TheRankingPrefersTheLineWithRoom(StorageTestCase):
    def setUp(self):
        super().setUp()
        modes.forget()
        self.team = _squad(REPORTED)

    def tearDown(self):
        modes.forget()
        super().tearDown()

    def _rank(self, *specs):
        ops, cards = [], {}
        for pid, pos, avg, price in specs:
            cards[pid] = _pm(pid, pos, avg, price)
            ops.append({"market_id": pid, "player_id": pid, "nombre": pid,
                        "buy_price": price, "via": "SISTEMA"})
        return upgrades.rank(ops, self.team, cards, money=30_000_000)

    def test_the_striker_now_outranks_the_eleventh_defender(self):
        """The bug, in one assertion. It used to be the other way round."""
        got = self._rank(("DEFnuevo", "DEF", 7.0, 10_000_000),
                         ("DELnuevo", "DEL", 7.0, 10_000_000))
        self.assertEqual(got[0]["nombre"], "DELnuevo")

    def test_the_gross_ratio_still_favoured_the_defender(self):
        """Proves the fix is the NET ratio and not some other difference."""
        got = {r["nombre"]: r for r in
               self._rank(("DEFnuevo", "DEF", 7.0, 10_000_000),
                          ("DELnuevo", "DEL", 7.0, 10_000_000))}
        self.assertGreater(got["DEFnuevo"]["gain_per_million"],
                           got["DELnuevo"]["gain_per_million"])
        self.assertLess(got["DEFnuevo"]["gain_per_million_neto"],
                        got["DELnuevo"]["gain_per_million_neto"])

    def test_a_line_with_room_charges_nothing_extra(self):
        got = self._rank(("DELnuevo", "DEL", 7.0, 10_000_000))[0]
        self.assertEqual(got["congela"], 0)
        self.assertEqual(got["gain_per_million"],
                         got["gain_per_million_neto"])

    def test_the_row_names_who_stops_fitting(self):
        """The page has to be able to say why, not just rank differently."""
        got = self._rank(("DEFnuevo", "DEF", 7.0, 10_000_000))[0]
        self.assertIn("nombre", got["desplaza"])

    def test_a_balanced_squad_is_unaffected(self):
        """The charge only exists where a line is actually full."""
        self.team = _squad({"POR": 2, "DEF": 5, "MED": 5, "DEL": 3})
        for row in self._rank(("d", "DEF", 7.0, 10_000_000),
                              ("m", "MED", 7.0, 10_000_000),
                              ("f", "DEL", 7.0, 10_000_000)):
            self.assertEqual(row["congela"], 0, row["nombre"])

    def test_trading_mode_still_ranks_by_margin_first(self):
        """The charge must not quietly override the mode."""
        modes.set_mode("dinero")
        ops = [{"market_id": "a", "player_id": "a", "nombre": "poco",
                "buy_price": 10_000_000, "margin_pct": 1.0},
               {"market_id": "b", "player_id": "b", "nombre": "mucho",
                "buy_price": 10_000_000, "margin_pct": 40.0}]
        cards = {"a": _pm("a", "DEL", 7.0, 10_000_000),
                 "b": _pm("b", "DEF", 7.0, 10_000_000)}
        got = upgrades.rank(ops, self.team, cards, money=30_000_000)
        self.assertEqual(got[0]["nombre"], "mucho")


if __name__ == "__main__":
    import unittest
    unittest.main()

"""Eleven starters beat three stars.

Fantasy scores an ELEVEN, and every slot pays the same. A squad with three
brilliant players and eight who do not start fields eight zeros, and loses to
eleven solid ones bought with the same money.

Two pieces of arithmetic were quietly arguing the other way:

  * `transfers` paired ONE sale with ONE buy, so selling a twenty-million
    forward to buy two ten-million starters was not a move it could express.
  * `best_plan` added up gains each computed against the CURRENT squad. Two
    signings who would fill the same weak slot were counted as filling two.

Both are answered by measuring the resulting eleven instead of summing marginal
gains.
"""

from fantasybot.strategy import depth
from tests.support import StorageTestCase


def _p(ptid, pos_id, avg, value=5_000_000, prob_ok=True):
    return {"playerTeamId": ptid,
            "playerMaster": {"id": f"pm-{ptid}", "nickname": ptid,
                             "positionId": str(pos_id),
                             "marketValue": str(value),
                             "averagePoints": str(avg), "points": str(avg * 10),
                             "lastSeasonPoints": str(avg * 38),
                             "playerStatus": "ok" if prob_ok else "injured"}}


def _squad(strikers_avg=(6, 6), spare=()):
    """One keeper, four defenders, four midfielders, two strikers."""
    squad = [_p("gk1", 1, 5)]
    squad += [_p(f"d{i}", 2, 4) for i in range(4)]
    squad += [_p(f"m{i}", 3, 4) for i in range(4)]
    squad += [_p(f"s{i}", 4, a) for i, a in enumerate(strikers_avg)]
    squad += list(spare)
    return {"teamMoney": 0, "players": squad}


class TheLeaksAreTheSlotsScoringNothing(StorageTestCase):
    def test_a_dead_slot_is_found(self):
        best = {"goalkeeper": {"playerTeamId": "gk1", "nombre": "P", "score": 4.0},
                "defender": [{"playerTeamId": "d1", "nombre": "D1", "score": 0.3,
                              "tag": "not_in_xi"}],
                "midfield": [], "striker": [], "missing": {}}
        got = depth.leaks(best)
        self.assertEqual([r["nombre"] for r in got], ["D1"])

    def test_an_empty_slot_is_the_worst_leak_of_all(self):
        best = {"goalkeeper": None, "defender": [], "midfield": [],
                "striker": [], "missing": {"striker": 2}}
        got = depth.leaks(best)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["expected"], 0.0)
        self.assertEqual(got[0]["tag"], "empty")

    def test_a_working_slot_is_not_a_leak(self):
        best = {"goalkeeper": {"playerTeamId": "gk1", "nombre": "P", "score": 6.0},
                "defender": [], "midfield": [], "striker": [], "missing": {}}
        self.assertEqual(depth.leaks(best), [])

    def test_no_lineup_is_not_a_crash(self):
        self.assertEqual(depth.leaks(None), [])


class TwoSigningsForOneSlotAreWorthOneSlot(StorageTestCase):
    """The arithmetic that made stacking look good."""

    def test_the_second_striker_for_the_same_slot_adds_almost_nothing(self):
        team = _squad(strikers_avg=(6, 6))
        star = _p("new1", 4, 12)["playerMaster"]
        other = _p("new2", 4, 12)["playerMaster"]
        one = depth.combo_gain(team, [star])
        two = depth.combo_gain(team, [star, other])
        self.assertGreater(one, 0)
        self.assertLess(two, one * 2,
                        "adding their separate gains would say twice as much")

    def test_replacing_a_forced_zero_beats_upgrading_someone_decent(self):
        """Eleven starters, in one assertion.

        With a bench, the optimiser simply changes formation around a bad
        player — which is it doing its job. The leak that actually costs points
        is a slot the squad is FORCED to fill badly, and fixing that is worth
        several times what polishing a working slot is worth.
        """
        def _exact(mid_avgs):
            return {"teamMoney": 0, "players":
                    [_p("gk1", 1, 5)] + [_p(f"d{i}", 2, 4) for i in range(4)]
                    + [_p(f"m{i}", 3, a) for i, a in enumerate(mid_avgs)]
                    + [_p(f"s{i}", 4, 5) for i in range(2)]}

        modest = _p("newM", 3, 5)["playerMaster"]
        forced_zero = depth.combo_gain(_exact([4, 4, 4, 0.1]), [modest])
        all_solid = depth.combo_gain(_exact([4, 4, 4, 4]), [modest])
        self.assertGreater(forced_zero, all_solid * 2,
                           "fixing a zero is worth several upgrades")

    def test_a_line_too_short_for_any_formation_is_a_real_hole(self):
        """Four midfielders cover 4-5-1; ONE does not cover anything."""
        thin = {"teamMoney": 0, "players": [
            _p("gk1", 1, 5)] + [_p(f"d{i}", 2, 4) for i in range(5)]
            + [_p("m0", 3, 4)] + [_p(f"s{i}", 4, 5) for i in range(2)]}
        mid = _p("newM", 3, 4)["playerMaster"]
        self.assertGreater(depth.combo_gain(thin, [mid]), 0)


class TheRebuildCanSellOneToBuyTwo(StorageTestCase):
    def _cards(self, *pms):
        return {str(pm["id"]): pm for pm in pms}

    def test_it_proposes_selling_one_for_two_starters(self):
        thin = _squad(strikers_avg=(6,))
        a = _p("A", 4, 6, value=6_000_000)["playerMaster"]
        b = _p("B", 3, 6, value=6_000_000)["playerMaster"]
        ranked = [{"market_id": "m1", "player_id": a["id"], "nombre": "A",
                   "buy_price": 6_000_000, "via": "SISTEMA"},
                  {"market_id": "m2", "player_id": b["id"], "nombre": "B",
                   "buy_price": 6_000_000, "via": "SISTEMA"}]
        sellable = [{"player_team_id": "d3", "nombre": "D3",
                     "raises": 14_000_000, "loss": 0.2}]
        got = depth.rebuild(thin, ranked, self._cards(a, b), sellable,
                            money=0, limit=3)
        self.assertTrue(got, "one sale must be able to fund two signings")
        self.assertEqual(len(got[0]["buy"]), 2)
        self.assertEqual(len(got[0]["sell"]), 1)
        self.assertIn("2 titulares por 1", got[0]["why"])

    def test_a_plan_that_costs_more_than_it_adds_is_refused(self):
        team = _squad()
        weak = _p("W", 4, 0)["playerMaster"]
        ranked = [{"market_id": "m1", "player_id": weak["id"], "nombre": "W",
                   "buy_price": 1_000_000, "via": "SISTEMA"}]
        sellable = [{"player_team_id": "s0", "nombre": "S0",
                     "raises": 9_000_000, "loss": 6.0}]
        got = depth.rebuild(team, ranked, self._cards(weak), sellable,
                            money=0)
        self.assertEqual(got, [])

    def test_a_signing_that_needs_no_sale_is_still_found(self):
        thin = _squad(strikers_avg=(6,))
        a = _p("A", 4, 7, value=3_000_000)["playerMaster"]
        ranked = [{"market_id": "m1", "player_id": a["id"], "nombre": "A",
                   "buy_price": 3_000_000, "via": "SISTEMA"}]
        got = depth.rebuild(thin, ranked, self._cards(a), [], money=5_000_000)
        self.assertTrue(got)
        self.assertEqual(got[0]["sell"], [])

    def test_nothing_affordable_is_an_empty_plan_not_an_error(self):
        team = _squad()
        a = _p("A", 4, 9, value=90_000_000)["playerMaster"]
        ranked = [{"market_id": "m1", "player_id": a["id"], "nombre": "A",
                   "buy_price": 90_000_000, "via": "SISTEMA"}]
        self.assertEqual(
            depth.rebuild(team, ranked, self._cards(a), [], money=1_000), [])


class TheMoneyHasToAddUp(StorageTestCase):
    """"Quiere vender un jugador de 500k y comprar 3 que suman 80 millones."

    Two separate promises, and only one of them was being kept.

    The budget arithmetic was right all along — nothing was ever proposed that
    the account could not pay for. What was wrong is that the search would
    staple a sale onto a purchase the cash already covered, because selling a
    500k reserve keeper costs almost no points and so never failed the net-gain
    test. It read as "sell him to afford them". It was not: it was the same
    purchase with a pointless disposal attached.

    So these tests pin both things at once — a sale only when a sale is needed,
    and a spend the plan's own numbers can pay for.
    """

    def _cards(self, *pms):
        return {str(pm["id"]): pm for pm in pms}

    def _market(self, prices):
        pms, ranked = [], []
        for i, price in enumerate(prices):
            pm = _p(f"N{i}", 4, 9, value=price)["playerMaster"]
            pms.append(pm)
            ranked.append({"market_id": f"m{i}", "player_id": pm["id"],
                           "nombre": f"N{i}", "buy_price": price,
                           "via": "SISTEMA"})
        return pms, ranked

    def test_a_sale_the_cash_did_not_need_is_not_attached(self):
        thin = _squad(strikers_avg=(6,))
        pms, ranked = self._market([22_000_000])
        cheap = [{"player_team_id": "bench", "nombre": "Suplente500k",
                  "raises": 500_000, "loss": 0.0}]
        got = depth.rebuild(thin, ranked, self._cards(*pms), cheap,
                            money=44_000_000, limit=3)
        self.assertTrue(got)
        for plan in got:
            self.assertEqual(plan["sell"], [],
                             "44M covers 22M — there is nothing to fund")

    def test_a_sale_the_cash_genuinely_needs_is_still_proposed(self):
        """The fence must not become a ban on selling."""
        thin = _squad(strikers_avg=(6,))
        pms, ranked = self._market([22_000_000])
        funder = [{"player_team_id": "d3", "nombre": "D3",
                   "raises": 25_000_000, "loss": 0.2}]
        got = depth.rebuild(thin, ranked, self._cards(*pms), funder,
                            money=1_000_000, limit=3)
        self.assertTrue(got, "1M cannot buy 22M without the sale")
        self.assertEqual(len(got[0]["sell"]), 1)

    def test_no_plan_spends_more_than_it_can_pay(self):
        """The invariant, over every plan the search returns."""
        thin = _squad(strikers_avg=(6,))
        pms, ranked = self._market([8_000_000, 9_000_000, 11_000_000,
                                    14_000_000, 22_000_000, 30_000_000])
        sellable = [{"player_team_id": "d3", "nombre": "D3",
                     "raises": 12_000_000, "loss": 0.2},
                    {"player_team_id": "m3", "nombre": "M3",
                     "raises": 6_000_000, "loss": 0.3}]
        for money in (0, 1_000_000, 20_000_000, 44_000_000):
            got = depth.rebuild(thin, ranked, self._cards(*pms), sellable,
                                money=money, limit=3)
            for plan in got:
                self.assertLessEqual(plan["spend"],
                                     plan["cash"] + plan["from_sales"],
                                     f"unpayable plan with {money} in hand")
                self.assertTrue(plan["affordable"])
                self.assertEqual(plan["cash"], money)

    def test_the_reserve_is_not_spendable(self):
        thin = _squad(strikers_avg=(6,))
        pms, ranked = self._market([9_000_000])
        got = depth.rebuild(thin, ranked, self._cards(*pms), [],
                            money=10_000_000, reserve=5_000_000)
        self.assertEqual(got, [], "only 5M is actually free")

    def test_the_same_signings_are_not_listed_twice(self):
        thin = _squad(strikers_avg=(6,))
        pms, ranked = self._market([8_000_000, 9_000_000, 11_000_000])
        sellable = [{"player_team_id": "d3", "nombre": "D3",
                     "raises": 12_000_000, "loss": 0.2}]
        got = depth.rebuild(thin, ranked, self._cards(*pms), sellable,
                            money=30_000_000, limit=3)
        keys = [tuple(sorted(b["market_id"] for b in p["buy"])) for p in got]
        self.assertEqual(len(keys), len(set(keys)))

    def test_what_a_sale_raises_is_net_of_the_haircut(self):
        thin = _squad(strikers_avg=(6,))
        pms, ranked = self._market([22_000_000])
        funder = [{"player_team_id": "d3", "nombre": "D3",
                   "raises": 25_000_000, "loss": 0.2}]
        got = depth.rebuild(thin, ranked, self._cards(*pms), funder,
                            money=1_000_000)
        self.assertTrue(got)
        self.assertEqual(got[0]["from_sales"], int(25_000_000 * 0.90))

    def test_a_player_we_could_not_price_is_never_bought(self):
        """`num` reads a missing field as 0, which would read as "free"."""
        thin = _squad(strikers_avg=(6,))
        pm = _p("Caro", 4, 12, value=40_000_000)["playerMaster"]
        for bad in (None, "", "n/a"):
            ranked = [{"market_id": "m1", "player_id": pm["id"],
                       "nombre": "Caro", "buy_price": bad, "via": "SISTEMA"}]
            got = depth.rebuild(thin, ranked, self._cards(pm), [], money=0)
            self.assertEqual(got, [], f"buy_price={bad!r} is not a free player")

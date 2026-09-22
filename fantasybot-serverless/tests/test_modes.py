"""Strategic modes, and proof that each one actually changes what the bot does.

A mode that renames the strategy without moving a single decision is worse than
no mode at all: it is a button that lies. So every test here asserts a DIFFERENT
OUTCOME between two modes on the same input, not that a setting was stored.

The safety rails are tested too, from the other direction: whatever the mode, a
plan still has to be affordable, and the eleven is never liquidated to bank a
paper profit.
"""

from fantasybot import modes
from fantasybot.strategy import depth, history, offers, upgrades
from tests.support import StorageTestCase


def _p(ptid, pos_id, avg, value=5_000_000, status="ok"):
    return {"playerTeamId": ptid,
            "playerMaster": {"id": f"pm-{ptid}", "nickname": ptid,
                             "positionId": str(pos_id),
                             "marketValue": str(value),
                             "averagePoints": str(avg), "points": str(avg * 10),
                             "lastSeasonPoints": str(avg * 38),
                             "playerStatus": status}}


class ModeSelection(StorageTestCase):
    def setUp(self):
        super().setUp()
        modes.forget()

    def tearDown(self):
        modes.forget()
        super().tearDown()

    def test_the_default_is_what_the_bot_always_did(self):
        self.assertEqual(modes.active(), "equilibrio")
        self.assertEqual(modes.knob("min_gain"), upgrades.MIN_GAIN)

    def test_switching_sticks(self):
        modes.set_mode("dinero")
        self.assertEqual(modes.active(), "dinero")

    def test_a_mode_nobody_recognises_falls_back_instead_of_crashing(self):
        """A typo in the settings table must not take the tick down."""
        from fantasybot.storage import get_storage
        get_storage().set_setting(modes.SETTING, "modo-inventado")
        modes.forget()
        self.assertEqual(modes.active(), "equilibrio")

    def test_an_unknown_mode_is_refused_at_the_door(self):
        with self.assertRaises(ValueError):
            modes.set_mode("turbo")

    def test_every_mode_defines_every_knob(self):
        """A knob missing from one mode would silently inherit and surprise."""
        expected = set(modes.MODES["equilibrio"])
        for name, spec in modes.MODES.items():
            self.assertEqual(set(spec), expected, f"{name} is missing knobs")


class TheModeChangesWhatItBuys(StorageTestCase):
    def setUp(self):
        super().setUp()
        modes.forget()

    def tearDown(self):
        modes.forget()
        super().tearDown()

    def _squad(self):
        return {"teamMoney": 50_000_000, "players":
                [_p("gk1", 1, 5)] + [_p(f"d{i}", 2, 4) for i in range(4)]
                + [_p(f"m{i}", 3, 4) for i in range(4)]
                + [_p(f"s{i}", 4, 5) for i in range(2)]}

    def _market(self):
        """A points player and a trade, deliberately opposed.

        `Puntero` improves the eleven and will not appreciate. `Chollo` barely
        plays but is priced well under where his trend is heading. A points
        posture must prefer the first; a trading posture, the second.
        """
        puntero = _p("Puntero", 4, 12, value=10_000_000)["playerMaster"]
        chollo = _p("Chollo", 4, 0.2, value=10_000_000)["playerMaster"]
        cards = {puntero["id"]: puntero, chollo["id"]: chollo}
        ops = [{"market_id": "m1", "player_id": puntero["id"],
                "nombre": "Puntero", "buy_price": 10_000_000,
                "via": "SISTEMA", "margin_pct": 0.5},
               {"market_id": "m2", "player_id": chollo["id"],
                "nombre": "Chollo", "buy_price": 10_000_000,
                "via": "SISTEMA", "margin_pct": 40.0}]
        return cards, ops

    def _ranked(self):
        cards, ops = self._market()
        return upgrades.rank(ops, self._squad(), cards=cards,
                             money=50_000_000)

    def test_points_mode_ranks_the_footballer_first(self):
        modes.set_mode("puntos")
        self.assertEqual(self._ranked()[0]["nombre"], "Puntero")

    def test_money_mode_ranks_the_trade_first(self):
        modes.set_mode("dinero")
        self.assertEqual(self._ranked()[0]["nombre"], "Chollo")

    def test_money_mode_would_sign_a_player_who_adds_no_points(self):
        """The whole point: a trade the points engine cannot express."""
        modes.set_mode("dinero")
        chollo = next(r for r in self._ranked() if r["nombre"] == "Chollo")
        self.assertTrue(upgrades.worth_signing(chollo))

    def test_points_mode_refuses_that_same_player(self):
        modes.set_mode("puntos")
        chollo = next(r for r in self._ranked() if r["nombre"] == "Chollo")
        self.assertFalse(upgrades.worth_signing(chollo))

    def test_money_mode_still_refuses_a_trade_with_no_margin(self):
        """Zero points bar is not "buy anyone"."""
        modes.set_mode("dinero")
        puntero = next(r for r in self._ranked() if r["nombre"] == "Puntero")
        self.assertFalse(upgrades.worth_signing(puntero),
                         "0.5% margin does not cover being wrong")

    def test_no_mode_can_make_an_unaffordable_signing_worth_it(self):
        """A rail, not a preference: this must hold in every mode."""
        for name in modes.names():
            modes.set_mode(name)
            row = {"affordable": False, "gain": 99.0, "margin_pct": 99.0}
            self.assertFalse(upgrades.worth_signing(row), name)


class TheModeChangesWhatItSells(StorageTestCase):
    def setUp(self):
        super().setUp()
        modes.forget()

    def tearDown(self):
        modes.forget()
        super().tearDown()

    def test_money_mode_discounts_the_bench_to_move_it(self):
        bench = _p("b1", 4, 3)
        modes.set_mode("equilibrio")
        normal = offers.premium_for(bench, [], [])
        modes.set_mode("dinero")
        self.assertLess(offers.premium_for(bench, [], []), normal)

    def test_money_mode_does_not_discount_the_eleven(self):
        """A trading mode that liquidates your starters is a relegation."""
        starter = _p("s1", 4, 8)
        modes.set_mode("equilibrio")
        normal = offers.premium_for(starter, ["s1"], [])
        modes.set_mode("dinero")
        self.assertEqual(offers.premium_for(starter, ["s1"], []), normal)

    def test_points_mode_makes_a_starter_effectively_unbuyable(self):
        starter = _p("s1", 4, 8)
        modes.set_mode("equilibrio")
        normal = offers.premium_for(starter, ["s1"], [])
        modes.set_mode("puntos")
        self.assertGreater(offers.premium_for(starter, ["s1"], []), normal)


class TakingTheProfit(StorageTestCase):
    """Bought at ten, worth fifteen, sell — the loop the user asked for."""

    def setUp(self):
        super().setUp()
        modes.forget()

    def tearDown(self):
        modes.forget()
        super().tearDown()

    def test_a_holding_that_ran_is_priced_to_leave(self):
        modes.set_mode("dinero")
        held = _p("h1", 4, 3, value=15_000_000)
        price = offers.reserve_price(held, [], [], paid=10_000_000)
        self.assertLess(price, 15_000_000 * 1.02,
                        "at target it is asked at market, with no extra premium"
                        " on top of the technical sale floor")
        self.assertGreaterEqual(price, 15_000_000)

    def test_a_holding_that_has_not_run_is_asked_at_market_not_above(self):
        """He is not for sale at a business premium either way: below target
        he simply is not being cashed in early, and MOVABLE_ASK means a
        non-eleven player never asks more than market value — plus the same
        small technical sale floor every listing carries (see
        SALE_FLOOR_CUSHION_PCT), never a real markup on top of it."""
        modes.set_mode("dinero")
        held = _p("h1", 4, 3, value=11_000_000)
        price = offers.reserve_price(held, [], [], paid=10_000_000)
        self.assertGreaterEqual(price, 11_000_000)
        self.assertLess(price, 11_000_000 * 1.02)

    def test_a_starter_is_never_cashed_in(self):
        modes.set_mode("dinero")
        held = _p("h1", 4, 8, value=15_000_000)
        self.assertFalse(offers.take_profit(held, ["h1"], 10_000_000))

    def test_without_an_entry_price_nothing_is_taken(self):
        """A player who came with the squad has no entry price.

        Reading that absence as zero would mark him infinitely profitable and
        sell him the instant any profit-taking mode came on.
        """
        modes.set_mode("dinero")
        held = _p("h1", 4, 3, value=15_000_000)
        self.assertFalse(offers.take_profit(held, [], None))

    def test_other_modes_never_take_profit(self):
        for name in ("equilibrio", "puntos"):
            modes.set_mode(name)
            held = _p("h1", 4, 3, value=99_000_000)
            self.assertFalse(offers.take_profit(held, [], 1_000_000), name)


class WhatWePaid(StorageTestCase):
    """Entry prices read off LaLiga's own feed, not stored by us."""

    def _act(self, atype, pid, amount, u1=7, u2=None, at="2026-09-01"):
        return {"activityTypeId": atype, "playerMasterId": pid,
                "amount": amount, "user1Id": u1, "user2Id": u2,
                "createdAt": at + "T10:00:00"}

    def test_an_open_position_reports_what_it_cost(self):
        got = history.paid_for_squad(
            [self._act(history.TYPE_MARKET_BUY, 100, 10_000_000)], 7)
        self.assertEqual(got, {"100": 10_000_000})

    def test_a_player_already_sold_is_not_a_holding(self):
        got = history.paid_for_squad([
            self._act(history.TYPE_MARKET_BUY, 100, 10_000_000, at="2026-09-01"),
            self._act(history.TYPE_MARKET_SELL, 100, 15_000_000, at="2026-09-05"),
        ], 7)
        self.assertEqual(got, {})

    def test_bought_twice_sold_once_reports_the_lot_still_open(self):
        """FIFO, the same way the P&L matches lots."""
        got = history.paid_for_squad([
            self._act(history.TYPE_MARKET_BUY, 100, 10_000_000, at="2026-09-01"),
            self._act(history.TYPE_MARKET_BUY, 100, 12_000_000, at="2026-09-03"),
            self._act(history.TYPE_MARKET_SELL, 100, 15_000_000, at="2026-09-05"),
        ], 7)
        self.assertEqual(got, {"100": 12_000_000})

    def test_a_rivals_purchase_is_not_ours(self):
        got = history.paid_for_squad(
            [self._act(history.TYPE_MARKET_BUY, 100, 10_000_000, u1=99)], 7)
        self.assertEqual(got, {})

    def test_junk_is_not_a_crash(self):
        self.assertEqual(history.paid_for_squad(None, 7), {})
        self.assertEqual(history.paid_for_squad([{}], 7), {})
        self.assertEqual(history.paid_for_squad([], "no soy un id"), {})


class TheModeNeverBreaksTheRails(StorageTestCase):
    def setUp(self):
        super().setUp()
        modes.forget()

    def tearDown(self):
        modes.forget()
        super().tearDown()

    def test_a_rebuild_plan_is_affordable_in_every_mode(self):
        team = {"teamMoney": 20_000_000, "players":
                [_p("gk1", 1, 5)] + [_p(f"d{i}", 2, 4) for i in range(4)]
                + [_p(f"m{i}", 3, 4) for i in range(4)] + [_p("s0", 4, 5)]}
        pm = _p("Caro", 4, 11, value=18_000_000)["playerMaster"]
        ranked = [{"market_id": "m1", "player_id": pm["id"], "nombre": "Caro",
                   "buy_price": 18_000_000, "via": "SISTEMA", "gain": 2.0}]
        for name in modes.names():
            modes.set_mode(name)
            for plan in depth.rebuild(team, ranked, {pm["id"]: pm}, [],
                                      money=20_000_000):
                self.assertLessEqual(plan["spend"],
                                     plan["cash"] + plan["from_sales"], name)


if __name__ == "__main__":
    import unittest
    unittest.main()

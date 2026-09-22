"""LaLiga refuses a listing at or below the player's live value, outright.

Reported live, the day MOVABLE_ASK shipped: every `list_squad` action failed —

    POST /market/sell -> 400 {"code":400,
      "message":"\"402687\" is not a valid sale price quantity for this player",
      "errorCode":"030.01.02"}

— for six different players in a row, first attempt, no exceptions. Caja
1,260,777€, flips 0, and nothing sold.

030.01.02 is the sale-side sibling of 030.01.01, the well-documented bid
floor: bidding.py already knew LaLiga answers a bid under the current value
with 400 "is not a valid money quantity for this player", because
`marketValue` is revalued through the day even while a listing's own
`salePrice` sits frozen. Nobody had connected that this must apply
symmetrically to the SALE price too, and MOVABLE_ASK — asking exactly market
value, or in BENCH_DISCOUNT/DUMP_DISCOUNT's case, below it — walked straight
into it. A hundred per cent of the day's sell attempts failed.

The fix is the same shape as the bid floor: reserve_price() never returns
less than value plus a small technical cushion (SALE_FLOOR_CUSHION_PCT),
whatever premium_for() decided. The discounts still exist and still mark a
player to prioritise — nothing else reads premium_for's return value — they
just cannot reach the number actually submitted to LaLiga any more.
"""

from fantasybot.strategy import offers
from tests.support import StorageTestCase


def _p(pid, value=402_687, status="ok"):
    return {"playerTeamId": pid,
            "playerMaster": {"id": pid, "nickname": pid, "positionId": "4",
                             "marketValue": str(value), "averagePoints": "0.5",
                             "playerStatus": status}}


class NoListingEverAsksAtOrBelowValue(StorageTestCase):
    """The invariant that would have prevented six failures in a row."""

    def test_a_plain_bench_player(self):
        price = offers.reserve_price(_p("b1"), [], [])
        self.assertGreater(price, 402_687)

    def test_a_bench_discounted_player(self):
        """BENCH_DISCOUNT (-8%) used to land the ask BELOW value directly —
        the clearest way to trigger 030.01.02."""
        price = offers.reserve_price(
            _p("b1"), [], [], expected={"b1": 0.2})
        self.assertGreater(price, 402_687)

    def test_a_positional_surplus_player(self):
        price = offers.reserve_price(_p("b1"), [], [], surplus={"b1"})
        self.assertGreater(price, 402_687)

    def test_an_out_of_league_player(self):
        """DUMP_DISCOUNT (-30%) was the worst case: 70% of value, nowhere
        near the floor."""
        price = offers.reserve_price(_p("b1", status="out_of_league"), [], [])
        self.assertGreater(price, 402_687)

    def test_an_already_flagged_sell_target(self):
        """SELLABLE_PREMIUM = 0.0 asked EXACTLY value with no cushion at
        all — the second most common way to hit the same 400."""
        price = offers.reserve_price(_p("b1"), [], {"b1"})
        self.assertGreater(price, 402_687)

    def test_a_holding_at_its_profit_target(self):
        """take_profit's own branch used to return `round(value)` directly,
        bypassing the floor entirely — the clearest single-line miss, since
        it short-circuits before premium_for or the floor clamp ever run."""
        from fantasybot import modes
        modes.forget()
        modes.set_mode("dinero")
        try:
            held = _p("b1", value=1_000_000)
            self.assertTrue(offers.take_profit(held, [], 500_000),
                            "the scenario must actually reach that branch")
            price = offers.reserve_price(held, [], [], paid=500_000)
        finally:
            modes.forget()
        self.assertGreater(price, 1_000_000)

    def test_every_reported_failing_value_is_now_safely_above_the_floor(self):
        """The six prices from the live log, replayed."""
        for value in (402_687, 447_673, 529_815, 726_159, 855_514, 2_003_565):
            price = offers.reserve_price(_p("x", value=value), [], [])
            self.assertGreater(price, value, value)


class TheFloorIsSmallOnPurpose(StorageTestCase):
    """A technical cushion, not a business decision — it must never meaningfully
    reduce what a genuine offer would have cleared."""

    def test_it_is_a_small_single_digit_percentage(self):
        price = offers.reserve_price(_p("b1", value=1_000_000), [], [])
        self.assertLess(price, 1_030_000, "more than 3% would not be a cushion")

    def test_the_eleven_is_unaffected_by_it(self):
        """The floor sits far under any real XI premium; it should never be
        the binding constraint for a starter."""
        starter = _p("s1", value=1_000_000)
        premium_price = offers.reserve_price(starter, ["s1"], [])
        self.assertGreaterEqual(premium_price, 1_390_000)


if __name__ == "__main__":
    import unittest
    unittest.main()

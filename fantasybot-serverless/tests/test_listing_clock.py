"""Why the bot never sold anybody.

Reported from the live team: "no vende jugadores nunca. tenemos 3 millones y
antes teníamos 80 millones."

The selling model was supposed to work like this: ask a premium, and if nobody
meets it, walk the price down day by day until the market does. The walking down
is the only part that ever actually sells a player nobody is fighting over.

It never ran once. LaLiga's market closes daily, so a player we are still trying
to sell is OFF the market for part of every day — and `_days_listed` forgot the
clock for anyone not listed at that instant. Every morning's re-listing started
again at day zero, `days_listed` never got past it, and the ask sat at value
+15% forever. Six simulated days asked 5,750,000 on day one and 5,750,000 on day
six.

Fixing the clock alone would have been worse than the bug it was aimed at: a
naive fix walks EVERY premium to zero over time, including the eleven's, so the
bot would answer a quiet week by offering its own starters at par.

That first fix (a premium that decayed towards zero over unsold days) was
superseded within the same session by a more direct one once it became clear
WHY nothing was selling: LaLiga's own market makes a standing offer on every
listing roughly once a day, and `accept_offer` is paid the OFFERED amount, not
the reserve — so the reserve is a threshold, never a price we collect. A
non-eleven player now asks exactly market value from the day he is listed
(`MOVABLE_ASK`), which makes "wait for it to decay" redundant: there is nothing
left to walk down. The eleven still never discounts, whatever `days_listed`
says — a starter's price is not stubbornness that time should wear down.
"""

from datetime import timedelta
from unittest import mock

from fantasybot import tick
from fantasybot.storage import get_storage, utcnow
from fantasybot.strategy import offers
from tests.support import StorageTestCase


def _p(pid, value=5_000_000):
    return {"playerTeamId": pid,
            "playerMaster": {"id": pid, "nickname": pid, "positionId": "4",
                             "marketValue": str(value), "averagePoints": "3",
                             "points": "30", "lastSeasonPoints": "100",
                             "playerStatus": "ok"}}


def _listing(pid):
    return {"discr": "marketPlayerTeam", "playerMaster": {"id": pid}}


class TheClockSurvivesTheMarketClosing(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.store = get_storage()
        self.team = {"teamMoney": 0, "players": [_p("b1")]}
        # Pinned: every _run must measure from the SAME origin, or the elapsed
        # days carry the microseconds each call spent getting there.
        self.t0 = utcnow()

    def _run(self, day, on_market):
        now = self.t0 + timedelta(days=day)
        with mock.patch.object(tick, "utcnow", lambda: now):
            return tick._days_listed(self.store, self.team,
                                     [_listing("b1")] if on_market else [])

    def test_a_lapsed_listing_does_not_reset_the_clock(self):
        """The bug, in one assertion."""
        self._run(0, True)
        self.assertEqual(self._run(1, False).get("b1"), 1.0)
        self.assertEqual(self._run(2, True).get("b1"), 2.0)

    def test_six_days_of_the_real_cycle_reach_six_days(self):
        self._run(0, True)
        for day in range(1, 7):
            self._run(day, day % 2 == 0)
        self.assertEqual(self._run(6, True).get("b1"), 6.0)

    def test_leaving_the_squad_does_end_it(self):
        """Sold, or taken by a clause: the next one starts fresh."""
        self._run(0, True)
        self.team = {"teamMoney": 0, "players": []}
        self.assertEqual(self._run(3, False), {})
        self.team = {"teamMoney": 0, "players": [_p("b1")]}
        self.assertEqual(self._run(4, True).get("b1"), 0.0)


class ThePriceIsAtMarketValueFromDayOne(StorageTestCase):
    """MOVABLE_ASK, not decay: there is nothing left to walk down."""

    def test_a_bench_player_asks_essentially_market_value_immediately(self):
        """Not EXACTLY market value: see SALE_FLOOR_CUSHION_PCT — LaLiga
        refuses a listing at or below the live value outright, so the floor
        sits a hair above it."""
        p = _p("b1")
        price = offers.reserve_price(p, [], [], days_listed=0)
        self.assertGreater(price, 5_000_000)
        self.assertLess(price, 5_000_000 * 1.02)

    def test_days_listed_no_longer_changes_the_ask(self):
        """The parameter is still accepted (for the page's own reporting) but
        no longer drives the price — MOVABLE_ASK already puts him at market
        value on day zero, so there is nothing for time to walk down."""
        p = _p("b1")
        self.assertEqual(offers.reserve_price(p, [], [], days_listed=0),
                         offers.reserve_price(p, [], [], days_listed=60))

    def test_it_never_goes_below_market_value(self):
        p = _p("b1")
        self.assertGreaterEqual(offers.reserve_price(p, [], [], days_listed=60),
                                5_000_000)

    def test_a_starter_is_never_discounted(self):
        """The eleven keeps its premium whatever days_listed says — a starter's
        price is not stubbornness that time should wear down."""
        p = _p("s1")
        self.assertEqual(offers.reserve_price(p, ["s1"], [], days_listed=0),
                         offers.reserve_price(p, ["s1"], [], days_listed=30))

    def test_the_premium_the_page_reports_matches_the_price_charged(self):
        """A reported premium that disagrees with the ask is a lie on screen.

        `premium_pct` is an INTEGER percentage for display, and no integer
        percentage reconstructs an arbitrary price to the exact euro — so
        this checks the two agree to within a rounding euro or two, not
        bit-for-bit. What it must never see again is the gap that motivated
        it: a price a full half a point off what the rounded number implies.
        """
        team = {"teamMoney": 0, "players": [_p("s1"), _p("b1")]}
        rows = offers.plan_listings(team, [], best=None, sells=[],
                                    listed_since={"s1": 30, "b1": 30})
        for row in rows:
            expected = round(row["value"] * (1 + row["premium_pct"] / 100))
            self.assertLessEqual(abs(row["price"] - expected), 2, row["nombre"])

    def test_an_out_of_league_player_still_meets_the_sale_floor(self):
        """DUMP_DISCOUNT still marks him to prioritise leaving, but the
        SUBMITTED price obeys the same floor as everybody else — the discount
        never reaches LaLiga as an actual below-value ask."""
        p = _p("x1")
        p["playerMaster"]["playerStatus"] = "out_of_league"
        self.assertGreaterEqual(offers.reserve_price(p, [], [], days_listed=30),
                                5_000_000)


class TheListedSinceDocumentStaysBounded(StorageTestCase):
    def test_players_who_left_are_cleaned_out(self):
        """The clock now outlives listings; it must not outlive the squad."""
        store = get_storage()
        team = {"teamMoney": 0, "players": [_p(f"p{i}") for i in range(3)]}
        tick._days_listed(store, team, [_listing(f"p{i}") for i in range(3)])
        self.assertEqual(len(store.get_doc("listed_since", {})), 3)
        smaller = {"teamMoney": 0, "players": [_p("p0")]}
        tick._days_listed(store, smaller, [])
        self.assertEqual(list(store.get_doc("listed_since", {})), ["p0"])

    def test_a_corrupt_timestamp_is_skipped_not_crashed(self):
        store = get_storage()
        store.put_doc("listed_since", {"b1": "no soy una fecha"})
        got = tick._days_listed(store, {"players": [_p("b1")]}, [])
        self.assertNotIn("b1", got)


if __name__ == "__main__":
    import unittest
    unittest.main()

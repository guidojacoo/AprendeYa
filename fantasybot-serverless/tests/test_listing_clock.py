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

Fixing the clock alone would have been worse than the bug: the decay runs to
zero, so the bot would have started offering its own STARTERS at par. So the
premium on the eleven no longer decays at all — a starter's price is not
stubbornness that time should wear down.
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


class ThePriceActuallyComesDown(StorageTestCase):
    def test_a_bench_player_reaches_market_value(self):
        p = _p("b1")
        day0 = offers.reserve_price(p, [], [], days_listed=0)
        day6 = offers.reserve_price(p, [], [], days_listed=6)
        self.assertEqual(day0, 5_750_000)
        self.assertEqual(day6, 5_000_000, "at par he finally sells")
        self.assertLess(day6, day0)

    def test_it_never_goes_below_market_value(self):
        """Walking down to par is patience; below it is a different decision."""
        p = _p("b1")
        self.assertEqual(offers.reserve_price(p, [], [], days_listed=60),
                         5_000_000)

    def test_a_starter_is_never_walked_down(self):
        """Fixing the clock made this reachable for the first time.

        The decay runs to zero, so without this the bot would answer a quiet
        week by offering its own eleven at par.
        """
        p = _p("s1")
        self.assertEqual(offers.reserve_price(p, ["s1"], [], days_listed=0),
                         offers.reserve_price(p, ["s1"], [], days_listed=30))

    def test_the_premium_the_page_reports_matches_the_price_charged(self):
        """A reported premium that disagrees with the ask is a lie on screen."""
        team = {"teamMoney": 0, "players": [_p("s1"), _p("b1")]}
        rows = offers.plan_listings(team, [], best=None, sells=[],
                                    listed_since={"s1": 30, "b1": 30})
        for row in rows:
            expected = round(row["value"] * (1 + row["premium_pct"] / 100))
            self.assertEqual(row["price"], expected, row["nombre"])

    def test_an_out_of_league_player_is_not_walked_UP(self):
        p = _p("x1")
        p["playerMaster"]["playerStatus"] = "out_of_league"
        self.assertLess(offers.reserve_price(p, [], [], days_listed=30),
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

"""A friend parks his squad on the market the way we park ours.

He is not selling. The listing is a standing ask, and he declines everything —
which is exactly what this bot does with its own squad. Bidding into that ties
up money in an auction nobody intends to conclude, and it is the wrong
transaction anyway: another manager's player is reached by paying his clause.

Three gates, because a rule added after a bid was queued still has to catch it:
the planner refuses to schedule one, the review sweeps the queue, and the bidder
re-reads the listing at the moment it would spend.
"""

from datetime import timedelta

from fantasybot import bidding, scheduler, tick
from fantasybot.storage import to_iso, utcnow
from tests.support import FakeClient, StorageTestCase


def _listing(mid, mine_of_a_rival):
    return {"id": mid,
            "discr": "marketPlayerTeam" if mine_of_a_rival else "marketPlayerLeague",
            "expirationDate": to_iso(utcnow() + timedelta(seconds=30)),
            "numberOfBids": 0, "salePrice": 5_000_000,
            "playerMaster": {"id": f"p{mid}", "nickname": f"J{mid}",
                             "marketValue": 5_000_000}}


class TheBidderRefusesAtTheMoneyMoment(StorageTestCase):
    def test_it_will_not_bid_on_another_managers_listing(self):
        client = FakeClient([_listing("m1", mine_of_a_rival=True)])
        res = bidding.snipe("L", "m1", 9_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "not_ours")
        self.assertEqual(client.bids, [], "not one euro into that auction")

    def test_a_laliga_listing_still_goes_through(self):
        client = FakeClient([_listing("m2", mine_of_a_rival=False)])
        res = bidding.snipe("L", "m2", 9_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "bid")
        self.assertEqual(len(client.bids), 1)


class TheQueueIsSwept(StorageTestCase):
    def _queue(self, mid):
        scheduler.schedule_bid("L1", mid, 5_000_000,
                               utcnow() + timedelta(hours=2), nombre=f"J{mid}")

    def test_a_bid_queued_before_the_rule_is_cancelled(self):
        """Filtering the planner only stops new ones. Three were already sitting
        in the queue, aimed at players friends had parked and never sell."""
        self._queue("m1")
        client = FakeClient([_listing("m1", mine_of_a_rival=True)])
        got = tick._cancel_offside_bids(client, "L1", log=lambda m: None)
        self.assertEqual(len(got["cancelled"]), 1)
        self.assertEqual(got["cancelled"][0]["nombre"], "Jm1")
        self.assertEqual([a for a in scheduler.pending(20)
                          if a["type"] == scheduler.BID], [])

    def test_a_bid_on_a_laliga_listing_is_left_alone(self):
        self._queue("m2")
        client = FakeClient([_listing("m2", mine_of_a_rival=False)])
        got = tick._cancel_offside_bids(client, "L1", log=lambda m: None)
        self.assertEqual(got["cancelled"], [])
        self.assertEqual(len([a for a in scheduler.pending(20)
                              if a["type"] == scheduler.BID]), 1)

    def test_a_listing_that_vanished_is_left_to_the_bidder(self):
        """Gone from the market is not this sweep's business: the bid will find
        it missing and stand down by itself."""
        self._queue("m3")
        got = tick._cancel_offside_bids(FakeClient([]), "L1", log=lambda m: None)
        self.assertEqual(got["cancelled"], [])

    def test_a_market_read_that_fails_cancels_nothing(self):
        class _Broken:
            def market(self, lid):
                raise RuntimeError("503")
        self._queue("m4")
        got = tick._cancel_offside_bids(_Broken(), "L1", log=lambda m: None)
        self.assertEqual(got["cancelled"], [])
        self.assertIn("503", got["error"])

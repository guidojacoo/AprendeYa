"""The bounded sniper: what a function does when it runs out of time.

`snipe()` is the only place in the serverless path that WAITS. Getting its exit
conditions wrong is how you either miss a close or bid twice, so each one is
pinned here.
"""

from datetime import timedelta

from fantasybot import bidding
from fantasybot.storage import utcnow
from tests.support import FakeClient, StorageTestCase, listing


class SnipeOutcomes(StorageTestCase):
    def test_waits_and_hands_back_when_the_close_is_far(self):
        close = utcnow() + timedelta(minutes=30)
        client = FakeClient([listing("m1", close.isoformat())])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=0.5, log=lambda m: None)
        self.assertEqual(res["status"], "waiting")
        self.assertEqual(client.bids, [],
                         "running out of budget must never send a bid")

    def test_bids_inside_the_final_window(self):
        close = utcnow() + timedelta(seconds=8)
        client = FakeClient([listing("m1", close.isoformat(), value=10_000_000)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=10, log=lambda m: None)
        self.assertEqual(res["status"], "bid")
        self.assertEqual(len(client.bids), 1)
        self.assertEqual(client.bids[0]["amount"],
                         10_000_000 + bidding.UNCONTESTED_CUSHION)

    def test_competition_makes_it_bid_early(self):
        close = utcnow() + timedelta(minutes=10)
        client = FakeClient([listing("m1", close.isoformat(),
                                     value=10_000_000, bids=2)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=10, log=lambda m: None)
        self.assertEqual(res["status"], "bid", "competition forces a move")
        self.assertEqual(client.bids[0]["amount"], 10_300_000)

    def test_cap_is_never_exceeded(self):
        close = utcnow() + timedelta(minutes=10)
        client = FakeClient([listing("m1", close.isoformat(),
                                     value=10_000_000, bids=5)])
        bidding.snipe("L", "m1", 10_050_000, client=client,
                      budget_seconds=10, log=lambda m: None)
        self.assertEqual(client.bids[0]["amount"], 10_050_000)

    def test_our_existing_bid_stops_it_before_anything_is_sent(self):
        close = utcnow() + timedelta(seconds=5)
        client = FakeClient([listing("m1", close.isoformat(),
                                     mine={"id": "b1", "money": 10_000_000})])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=10, log=lambda m: None)
        self.assertEqual(res["status"], "already")
        self.assertEqual(client.bids, [])

    def test_a_vanished_listing_is_not_an_error(self):
        client = FakeClient([])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "gone")

    def test_an_unpriced_listing_is_refused_not_guessed(self):
        close = utcnow() + timedelta(seconds=5)
        row = listing("m1", close.isoformat())
        row["salePrice"] = 0
        row["playerMaster"]["marketValue"] = 0
        client = FakeClient([row])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "unpriced")
        self.assertEqual(client.bids, [])

    def test_dry_run_decides_but_sends_nothing(self):
        close = utcnow() + timedelta(seconds=5)
        client = FakeClient([listing("m1", close.isoformat())])
        res = bidding.snipe("L", "m1", 11_000_000, client=client, dry_run=True,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "bid")
        self.assertTrue(res["dry_run"])
        self.assertEqual(client.bids, [])


class ApiErrorsDoNotWedgeTheQueue(StorageTestCase):
    def test_a_failing_bid_leaves_the_action_retryable(self):
        from fantasybot import scheduler
        from fantasybot.scheduler import TickContext
        from fantasybot.storage import PENDING

        close = utcnow() + timedelta(seconds=8)

        class Exploding(FakeClient):
            def make_bid(self, *a, **kw):
                raise RuntimeError("503 from LaLiga")

        client = Exploding([listing("m1", close.isoformat())])
        scheduler.schedule_bid("L", "m1", 11_000_000, close)
        ctx = TickContext(client=client, budget_seconds=15, log=lambda m: None)
        res = scheduler.run_due(ctx)
        self.assertEqual(res[0]["status"], PENDING,
                         "a transient API error must stay retryable")
        self.assertIn("503", res[0]["error"])

    def test_it_gives_up_after_max_attempts(self):
        from fantasybot import scheduler
        from fantasybot.scheduler import TickContext
        from fantasybot.storage import FAILED

        close = utcnow() + timedelta(seconds=8)

        class Exploding(FakeClient):
            def make_bid(self, *a, **kw):
                raise RuntimeError("permanently broken")

        client = Exploding([listing("m1", close.isoformat())])
        scheduler.schedule_bid("L", "m1", 11_000_000, close)
        last = None
        for _ in range(4):
            ctx = TickContext(client=client, budget_seconds=15,
                              max_attempts=3, log=lambda m: None)
            got = scheduler.run_due(ctx)
            if got:
                last = got[0]
        self.assertEqual(last["status"], FAILED,
                         "it must stop hammering an endpoint that keeps refusing")

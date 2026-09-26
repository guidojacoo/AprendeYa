"""Bidding five minutes out, and guarding the bid once it is in.

Fifteen seconds is sealed timing and it is also all the room a refused bid
gets: LaLiga says no, the next tick arrives after the close, the player is
gone. Five minutes turns one shot into three.

What that costs is the sealed part — rivals watch the bid count rise and still
have five minutes to answer — so the bid is no longer abandoned once placed.
These pin both halves: the margin, and the watch that pays for it.
"""

from datetime import timedelta

from fantasybot import bidding, config
from fantasybot.storage import utcnow
from tests.support import FakeClient, StorageTestCase, listing


class TheWindowIsFiveMinutes(StorageTestCase):
    def test_the_lead_is_ahead_of_the_window(self):
        """Otherwise the action wakes up already inside it and the margin the
        window was meant to create is gone before the first attempt."""
        self.assertGreater(config.BID_LEAD_SECONDS, config.BID_FINAL_SECONDS,
                           "a bid must be queued before its window opens")

    def test_it_bids_four_minutes_out(self):
        close = utcnow() + timedelta(seconds=240)
        client = FakeClient([listing("m1", close.isoformat(), value=10_000_000)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=10, log=lambda m: None)
        self.assertEqual(res["status"], "bid")
        self.assertEqual(client.bids[0]["amount"],
                         10_000_000 + bidding.UNCONTESTED_CUSHION)

    def test_it_still_waits_outside_the_window(self):
        close = utcnow() + timedelta(minutes=20)
        client = FakeClient([listing("m1", close.isoformat(), value=10_000_000)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=0.5, log=lambda m: None)
        self.assertEqual(res["status"], "waiting")
        self.assertEqual(client.bids, [])

    def test_a_refused_bid_leaves_time_for_another_attempt(self):
        """The point of the whole change, as arithmetic."""
        room = config.BID_FINAL_SECONDS
        attempts = room // config.CLOCK_INTERVAL_SECONDS
        self.assertGreaterEqual(attempts, 3,
                                "a refusal must have somewhere to be retried")


class GuardingTheBid(StorageTestCase):
    def _row(self, bids, mine_money, close=None):
        close = close or (utcnow() + timedelta(seconds=120))
        return listing("m1", close.isoformat(), value=10_000_000, bids=bids,
                       mine={"id": "b1", "money": mine_money})

    def test_alone_in_the_auction_it_changes_nothing(self):
        """One bid on the listing and it is ours. Raising here is bidding
        against ourselves — the exact arithmetic slip `_rivals_on` exists for."""
        client = FakeClient([self._row(bids=1, mine_money=10_000_010)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "guarding")
        self.assertEqual(res["rivals"], 0)
        self.assertEqual(client.bids, [])

    def test_a_rival_joining_raises_the_bid(self):
        client = FakeClient([self._row(bids=2, mine_money=10_000_010)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "raised")
        self.assertEqual(res["previous"], 10_000_010)
        # The contested price is the mode's (6% in "equilibrio"), and it is the
        # same number decide() picks for a fresh bid.
        expected = 10_000_000 + round(10_000_000 * bidding.contested_margin())
        self.assertEqual(client.modified[0]["money"], expected,
                         "the contested price, the same one decide() would pick")

    def test_it_never_raises_past_the_ceiling(self):
        client = FakeClient([self._row(bids=2, mine_money=10_000_010)])
        bidding.snipe("L", "m1", 11_000_000, ceiling=10_100_000, client=client,
                      budget_seconds=5, log=lambda m: None)
        self.assertEqual(client.modified[0]["money"], 10_100_000)

    def test_it_never_lowers_a_bid(self):
        """Already above the contested price: leave it. There is no way down."""
        client = FakeClient([self._row(bids=2, mine_money=10_900_000)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "guarding")
        self.assertEqual(client.modified, [])

    def test_a_bid_with_no_id_is_left_alone(self):
        """Cancel-and-rebid would drop us out of the auction between the two
        calls. A lower bid beats no bid."""
        close = utcnow() + timedelta(seconds=120)
        client = FakeClient([listing("m1", close.isoformat(), value=10_000_000,
                                     bids=2, mine={"money": 10_000_010})])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "guarding")
        self.assertEqual(client.modified, [])
        self.assertEqual(client.bids, [])

    def test_the_watch_stays_on_until_the_close(self):
        client = FakeClient([self._row(bids=1, mine_money=10_000_010)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertTrue(res["retry"], "a bid placed early has to be watched")

    def test_after_the_close_it_lets_go(self):
        client = FakeClient([self._row(bids=1, mine_money=10_000_010,
                                       close=utcnow() - timedelta(seconds=5))])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertFalse(res["retry"])

    def test_a_dry_run_decides_the_raise_and_sends_nothing(self):
        client = FakeClient([self._row(bids=2, mine_money=10_000_010)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client, dry_run=True,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "raised")
        self.assertTrue(res["dry_run"])
        self.assertEqual(client.modified, [])


class TheRetryBudgetCountsRefusals(StorageTestCase):
    """Guarding claims the action once a minute. If the limit counted claims,
    a bid that went in perfectly would burn its whole retry budget watching
    itself — and the first transient network error after that would fail it
    for good."""

    def test_a_run_that_worked_does_not_spend_a_retry(self):
        from fantasybot import scheduler
        from fantasybot.storage import PENDING, get_storage

        store = get_storage()
        calls = []

        @scheduler.executor("guardy")
        def _guardy(ctx, action):
            calls.append(1)
            if len(calls) <= 4:
                return {"retry": True, "status": "guarding"}
            raise RuntimeError("the network blinked")

        scheduler.schedule("guardy", {}, execute_at=utcnow(),
                           idempotency_key="guardy:1")
        ctx = scheduler.TickContext(budget_seconds=30, log=lambda m: None)
        last = None
        for _ in range(5):
            for a in store.due_actions():
                last = scheduler._run_one(store, a, ctx, utcnow(),
                                          lambda m: None)
        self.assertEqual(last["status"], PENDING,
                         "one failure after four good runs is not a dead action")
        self.assertEqual(last["failures"], 1)


class ABidThatAppearsMidPoll(StorageTestCase):
    def test_it_is_guarded_like_any_other(self):
        """Another tick placed it, or a send we thought had failed landed. We
        are in the auction either way, so walking away is the one wrong move."""
        close = utcnow() + timedelta(minutes=20)
        client = FakeClient([listing("m1", close.isoformat(), value=10_000_000)])
        original = client.market

        def _market(league_id):
            rows = original(league_id)
            if client.market_calls > 1:
                return [listing("m1", close.isoformat(), value=10_000_000,
                                bids=2, mine={"id": "b1", "money": 10_000_010})]
            return rows

        client.market = _market
        res = bidding.snipe("L", "m1", 11_000_000, client=client, poll=0.01,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "raised")
        self.assertEqual(client.bids, [], "it must never bid a second time")

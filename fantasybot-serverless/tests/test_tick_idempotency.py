"""The scenario the whole architecture exists to survive.

  TICK #1  -> finds a queued bid that is due -> places it
  TICK #2  -> same action, same listing      -> places NOTHING

A cron that fires twice, a GitHub Actions run that overlaps the next one, a
Vercel retry, a human hitting the endpoint — all of them land here, and none of
them may cost a second bid.
"""

from datetime import timedelta

from fantasybot import scheduler
from fantasybot.scheduler import TickContext
from fantasybot.storage import DONE, PENDING, utcnow
from tests.support import FakeClient, StorageTestCase, listing


class TickRunsADueBidExactlyOnce(StorageTestCase):
    def setUp(self):
        super().setUp()
        # A listing closing in 10 seconds: due now, and inside the bid window.
        self.close = utcnow() + timedelta(seconds=10)
        self.client = FakeClient([listing("m1", self.close.isoformat(),
                                          value=10_000_000, bids=1)])
        scheduler.schedule_bid("L1", "m1", 12_000_000, self.close, nombre="Tester")

    def _tick(self):
        ctx = TickContext(client=self.client, budget_seconds=20, log=lambda m: None)
        return scheduler.run_due(ctx)

    def test_first_tick_bids_second_tick_does_not(self):
        first = self._tick()
        self.assertEqual(len(self.client.bids), 1, "tick #1 must place the bid")
        self.assertEqual(first[0]["status"], DONE)
        self.assertEqual(first[0]["result"]["status"], "bid")

        # The listing now carries OUR bid, exactly as LaLiga would report it.
        self.client.set_market([listing("m1", self.close.isoformat(),
                                        value=10_000_000, bids=1,
                                        mine={"id": "bid-1", "money": 10_300_000})])
        second = self._tick()
        self.assertEqual(len(self.client.bids), 1, "tick #2 must NOT bid again")
        self.assertEqual(second, [], "a done action is not due any more")

    def test_replanning_the_same_listing_does_not_requeue_it(self):
        self._tick()
        self.assertEqual(len(self.client.bids), 1)
        # A later review re-proposes the same player. Same close time -> same key.
        scheduler.schedule_bid("L1", "m1", 12_000_000, self.close, nombre="Tester")
        self.assertEqual(self._tick(), [])
        self.assertEqual(len(self.client.bids), 1)

    def test_two_ticks_racing_the_same_action_bid_once(self):
        """Both ticks see the action as due; only the one that CLAIMS it acts."""
        due = self.store.due_actions()
        self.assertEqual(len(due), 1)
        a, b = dict(due[0]), dict(due[0])

        winners = [self.store.claim_action(a), self.store.claim_action(b)]
        self.assertEqual(winners, [True, False],
                         "exactly one tick may claim an action")

    def test_a_crashed_tick_does_not_bid_twice_on_retry(self):
        """The amnesia case: the bid WAS sent, then the tick died before it could
        record that. The lease expires, the action comes back as due — and the
        re-read of the listing is what stops a second bid."""
        due = self.store.due_actions()
        self.store.claim_action(due[0], lease_seconds=-1)   # claimed, then died
        self.client.make_bid("L1", "m1", 10_300_000)        # ...but it HAD bid
        self.client.set_market([listing("m1", self.close.isoformat(),
                                        value=10_000_000, bids=1,
                                        mine={"id": "bid-1"})])

        results = self._tick()
        self.assertEqual(len(self.client.bids), 1,
                         "the re-read must catch our own existing bid")
        self.assertEqual(results[0]["result"]["status"], "guarding",
                         "it must not re-bid, because OUR bid is already there")
        self.assertEqual(results[0]["status"], PENDING,
                         "and it stays queued to watch that bid to the close")


class ExpiredActionsAreSkipped(StorageTestCase):
    def test_a_bid_whose_listing_already_closed_is_not_sent(self):
        closed = utcnow() - timedelta(hours=2)
        client = FakeClient([listing("m9", closed.isoformat())])
        scheduler.schedule_bid("L1", "m9", 1_000_000, closed)
        ctx = TickContext(client=client, budget_seconds=20, log=lambda m: None)
        results = scheduler.run_due(ctx)
        self.assertEqual(len(client.bids), 0)
        self.assertEqual(results[0]["status"], "skipped")


class TickRespectsItsBudget(StorageTestCase):
    def test_it_stops_claiming_when_out_of_time(self):
        close = utcnow() + timedelta(seconds=5)
        for i in range(3):
            scheduler.schedule_bid("L1", f"m{i}", 1_000_000, close)
        client = FakeClient([])
        ctx = TickContext(client=client, budget_seconds=0.1, log=lambda m: None)
        self.assertEqual(scheduler.run_due(ctx), [],
                         "an out-of-time tick must leave the queue untouched")
        self.assertEqual(len(self.store.pending_actions()), 3)

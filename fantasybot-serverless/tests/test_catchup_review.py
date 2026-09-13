"""A review that thinks and never spends earns a second attempt.

Forty-five million in the bank, zero bids scheduled, and a note reading
"Revisión acortada por tiempo: gap_signings, bids". The expensive part — reading
the market, pricing every candidate, ranking the upgrades — had all been done,
and the clock ran out one step before the only step that puts money to work. The
next review was an hour away; the listings it meant to bid on close sooner.
"""

from fantasybot import scheduler, tick
from fantasybot.storage import utcnow
from tests.support import StorageTestCase


class CatchUp(StorageTestCase):
    def _pending_reviews(self):
        return [a for a in scheduler.pending(50)
                if a.get("type") == scheduler.REVIEW]

    def test_dropping_a_spending_phase_queues_a_re_run(self):
        got = tick._schedule_catchup(["gap_signings", "bids"], utcnow())
        self.assertIsNotNone(got)
        self.assertEqual(got["phases"], ["gap_signings", "bids"])
        self.assertEqual(len(self._pending_reviews()), 1)

    def test_dropping_only_the_cheap_tail_queues_nothing(self):
        """`sources` is a health check. Losing it costs a warning, not a signing."""
        self.assertIsNone(tick._schedule_catchup(["sources", "matchday"],
                                                 utcnow()))
        self.assertEqual(self._pending_reviews(), [])

    def test_a_complete_review_queues_nothing(self):
        self.assertIsNone(tick._schedule_catchup([], utcnow()))

    def test_it_retries_once_an_hour_not_once_a_tick(self):
        """Otherwise a review that always runs long re-queues itself forever."""
        now = utcnow()
        for _ in range(4):
            tick._schedule_catchup(["bids"], now)
        self.assertEqual(len(self._pending_reviews()), 1)

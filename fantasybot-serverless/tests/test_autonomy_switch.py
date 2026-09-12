"""The observe-only switch has to hold where money moves.

FANTASYBOT_AUTO_EXECUTE is what you reach for to watch the bot for a day before
trusting it with your squad. It used to stop the lineup and nothing else: bids
were still queued, and the executor still sent them for real. A safety switch
that does not stop the irreversible half is not a safety switch.
"""

from datetime import timedelta

from fantasybot import config, scheduler, tick
from fantasybot.scheduler import TickContext
from fantasybot.storage import utcnow
from tests.support import FakeClient, StorageTestCase, listing


class _Flags(StorageTestCase):
    def setUp(self):
        super().setUp()
        # NOT `self._saved`: StorageTestCase uses that name, and quietly
        # overwriting it left its own cleanup unable to restore the temp paths.
        self._saved_flags = (config.AUTO_EXECUTE, config.AUTO_BIDS,
                             config.AUTO_LINEUP)
        self.addCleanup(self._restore_flags)

    def _restore_flags(self):
        (config.AUTO_EXECUTE, config.AUTO_BIDS,
         config.AUTO_LINEUP) = self._saved_flags

    def _set(self, execute=True, bids=True):
        config.AUTO_EXECUTE, config.AUTO_BIDS = execute, bids


class QueuedBidsRespectTheSwitch(_Flags):
    def _run_due_bid(self):
        close = utcnow() + timedelta(seconds=8)
        client = FakeClient([listing("m1", close.isoformat(),
                                     value=10_000_000)])
        scheduler.schedule_bid("L", "m1", 12_000_000, close)
        ctx = TickContext(client=client, budget_seconds=15, log=lambda m: None)
        results = scheduler.run_due(ctx)
        return client, results

    def test_it_bids_when_autonomy_is_on(self):
        self._set(execute=True, bids=True)
        client, results = self._run_due_bid()
        self.assertEqual(len(client.bids), 1)
        self.assertFalse(results[0]["result"].get("dry_run"))

    def test_auto_execute_off_stops_the_bid(self):
        """The regression: this used to send the bid anyway."""
        self._set(execute=False, bids=True)
        client, results = self._run_due_bid()
        self.assertEqual(client.bids, [],
                         "AUTO_EXECUTE=false must stop money moving")
        self.assertTrue(results[0]["result"].get("dry_run"))

    def test_auto_bids_off_stops_the_bid(self):
        self._set(execute=True, bids=False)
        client, _ = self._run_due_bid()
        self.assertEqual(client.bids, [])

    def test_an_action_queued_while_on_does_not_fire_after_you_turn_it_off(self):
        """The switch is re-checked when the bid would be SENT, not only when it
        was planned — otherwise turning autonomy off leaves live orders behind."""
        close = utcnow() + timedelta(seconds=8)
        self._set(execute=True, bids=True)
        scheduler.schedule_bid("L", "m1", 12_000_000, close)

        self._set(execute=False, bids=True)          # you change your mind
        client = FakeClient([listing("m1", close.isoformat())])
        ctx = TickContext(client=client, budget_seconds=15, log=lambda m: None)
        scheduler.run_due(ctx)
        self.assertEqual(client.bids, [])


class PlanningRespectsTheSwitch(_Flags):
    def test_nothing_is_queued_while_autonomy_is_off(self):
        self._set(execute=False, bids=True)
        self.assertFalse(tick.bids_allowed())

    def test_bids_allowed_needs_both_flags(self):
        for execute, bids, expected in ((True, True, True), (False, True, False),
                                        (True, False, False), (False, False, False)):
            self._set(execute=execute, bids=bids)
            self.assertEqual(tick.bids_allowed(), expected,
                             f"execute={execute} bids={bids}")

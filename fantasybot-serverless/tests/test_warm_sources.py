"""Keeping the slow sources out of the fast path.

Scraping futbolfantasy cold costs about 25 seconds — being polite means a 1.2s
floor between requests, and the probable-lineups index is twenty pages. Paying
that inside a review left the review with nothing to decide with, and three real
runs went past the 60-second ceiling Vercel kills functions at: 56s, 58s, 61s,
and one that reached 191s. Another is still marked `running` because it was
killed mid-write.

So the scraping is its own job, on its own schedule, with a whole tick to itself.
"""

from datetime import timedelta
from unittest import mock

from fantasybot import config, net, scheduler, tick
from fantasybot.scheduler import TickContext
from fantasybot.storage import FAILED, RUNNING, to_iso, utcnow
from tests.support import StorageTestCase


class WarmJob(StorageTestCase):
    def _run(self, **stubs):
        defaults = {"trends_index": {"a": 1}, "probable_lineups": {"b": 2},
                    "next_kickoff": "2026-09-13T19:00:00+00:00",
                    "next_gameweek_kickoff": None}
        defaults.update(stubs)
        patches = [
            mock.patch("fantasybot.sources.market_trends.trends_index",
                       return_value=defaults["trends_index"]),
            mock.patch("fantasybot.sources.lineups.probable_lineups",
                       return_value=defaults["probable_lineups"]),
            mock.patch("fantasybot.sources.matchday.next_kickoff",
                       return_value=defaults["next_kickoff"]),
            mock.patch("fantasybot.sources.matchday.next_gameweek_kickoff",
                       return_value=defaults["next_gameweek_kickoff"]),
        ]
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        return tick._execute_warm(ctx, {"payload": {}})

    def test_it_refreshes_every_source(self):
        res = self._run()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["warmed"]["trends"], 1)
        self.assertEqual(res["warmed"]["lineups"], 1)

    def test_it_records_when_it_last_ran(self):
        self._run()
        self.assertIsNotNone(self.store.get_doc("last_warm_at"))

    def test_it_clears_the_fetch_deadline_afterwards(self):
        """A deadline left behind would silently refuse every later fetch."""
        self._run()
        self.assertIsNone(net._remaining())

    def test_the_deadline_is_cleared_even_when_a_source_explodes(self):
        """A deadline left behind would silently refuse every later fetch."""
        with mock.patch("fantasybot.sources.market_trends.trends_index",
                        side_effect=RuntimeError("503")):
            ctx = TickContext(budget_seconds=20, log=lambda m: None)
            with self.assertRaises(RuntimeError):
                tick._execute_warm(ctx, {"payload": {}})
        self.assertIsNone(net._remaining())


class WarmScheduling(StorageTestCase):
    def test_it_queues_a_refresh_when_none_has_run(self):
        tick._schedule_warm(self.store)
        queued = self.store.pending_actions()
        self.assertEqual([a["type"] for a in queued], [scheduler.WARM])

    def test_it_does_not_queue_one_that_is_not_due(self):
        self.store.put_doc("last_warm_at", to_iso(utcnow()))
        tick._schedule_warm(self.store)
        self.assertEqual(self.store.pending_actions(), [])

    def test_it_queues_again_once_the_interval_has_passed(self):
        self.store.put_doc("last_warm_at", to_iso(
            utcnow() - timedelta(seconds=config.WARM_INTERVAL + 60)))
        tick._schedule_warm(self.store)
        self.assertEqual(len(self.store.pending_actions()), 1)

    def test_two_calls_in_the_same_hour_queue_one_job(self):
        tick._schedule_warm(self.store)
        tick._schedule_warm(self.store)
        self.assertEqual(len(self.store.pending_actions()), 1)


class StaleExecutions(StorageTestCase):
    """A run left `running` was killed before it could write its own ending. It
    is not in progress and never will be — but it sits at the top of the
    dashboard looking like it is, and skews every "when did this last run"."""

    def test_an_abandoned_run_is_marked_failed(self):
        eid = self.store.start_execution("tick:db")
        rows = self.store.get_doc("executions", [])
        rows[-1]["started_at"] = to_iso(utcnow() - timedelta(minutes=30))
        self.store.put_doc("executions", rows)

        tick._close_stale_executions(self.store)
        row = next(r for r in self.store.recent_executions() if r["id"] == eid)
        self.assertEqual(row["status"], FAILED)
        self.assertIn("cortada", row["error"])

    def test_a_run_that_just_started_is_left_alone(self):
        eid = self.store.start_execution("tick:db")
        tick._close_stale_executions(self.store)
        row = next(r for r in self.store.recent_executions() if r["id"] == eid)
        self.assertEqual(row["status"], RUNNING)

    def test_a_finished_run_is_not_touched(self):
        eid = self.store.start_execution("tick:db")
        self.store.finish_execution(eid, "done")
        tick._close_stale_executions(self.store)
        row = next(r for r in self.store.recent_executions() if r["id"] == eid)
        self.assertEqual(row["status"], "done")

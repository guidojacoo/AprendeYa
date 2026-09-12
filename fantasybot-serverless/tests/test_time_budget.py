"""Degrading instead of dying.

Vercel kills a function at 60 seconds with an HTML error page and no chance to
clean up — which is what the first real deployment hit: the review spent longer
than that and the dashboard showed "Unexpected token 'A'", because the caller was
handed HTML where it expected JSON.

Nothing catches being killed. The only defence is never getting there, so both
halves of the run give up early instead: fetches refuse past a deadline, and a
failed source is a missing signal rather than an exception. An agent that decides
with one fewer input still plays the gameweek; one that raises does nothing.
"""

import time
import unittest
from unittest import mock

from fantasybot import cache, net
from tests.support import StorageTestCase


class FetchDeadline(unittest.TestCase):
    def tearDown(self):
        net.clear_deadline()

    def test_past_the_deadline_it_does_not_even_try(self):
        net.set_deadline(time.monotonic() - 1)
        with mock.patch.object(net.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(net.DeadlineExceeded):
                net.get("https://www.futbolfantasy.com/x")
        urlopen.assert_not_called()

    def test_a_request_never_outlives_the_run(self):
        """A 20s default timeout inside a run with 4s left would be killed
        mid-request; the socket timeout is clamped to what is actually left."""
        net.set_deadline(time.monotonic() + 4)
        seen = {}

        class FakeResp:
            status = 200
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return FakeResp()

        with mock.patch.object(net.urllib.request, "urlopen", fake_urlopen):
            net.get("https://example.com/x", timeout=20)
        self.assertLessEqual(seen["timeout"], 4)

    def test_with_no_deadline_nothing_changes(self):
        seen = {}

        class FakeResp:
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return FakeResp()

        with mock.patch.object(net.urllib.request, "urlopen", fake_urlopen):
            net.get("https://example.com/x", timeout=20)
        self.assertEqual(seen["timeout"], 20)


class CacheSurvivesABrokenSource(StorageTestCase):
    def test_a_failing_producer_returns_the_default(self):
        def boom():
            raise RuntimeError("futbolfantasy redesigned their HTML")
        self.assertEqual(cache.cached("k", 60, boom, default={}), {})

    def test_a_deadline_during_a_scrape_is_a_missing_signal(self):
        def boom():
            raise net.DeadlineExceeded("out of time")
        self.assertEqual(cache.cached("k", 60, boom, default=[]), [])

    def test_it_does_not_poison_the_cache_with_the_default(self):
        """A failure must not be memoised as if it were a real answer."""
        cache.cached("k", 60, lambda: (_ for _ in ()).throw(RuntimeError()),
                     default={})
        self.assertEqual(cache.cached("k", 60, lambda: {"real": 1}), {"real": 1})

    def test_a_working_producer_is_unaffected(self):
        calls = []
        def producer():
            calls.append(1)
            return {"v": 1}
        self.assertEqual(cache.cached("k", 60, producer, default={}), {"v": 1})
        self.assertEqual(cache.cached("k", 60, producer, default={}), {"v": 1})
        self.assertEqual(len(calls), 1, "the second read must be a cache hit")


class SourcesDegradeQuietly(StorageTestCase):
    def test_probable_lineups_returns_an_empty_index_when_the_site_is_down(self):
        from fantasybot.sources import lineups
        with mock.patch.object(lineups, "team_slugs",
                               side_effect=RuntimeError("503")):
            self.assertEqual(lineups.probable_lineups(), {})

    def test_market_trends_returns_an_empty_list_when_the_site_is_down(self):
        from fantasybot.sources import market_trends
        with mock.patch.object(market_trends.net, "get",
                               side_effect=net.DeadlineExceeded("x")):
            self.assertEqual(market_trends.market_trends(), [])

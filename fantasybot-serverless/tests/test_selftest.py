"""The diagnosis itself must not be the thing that breaks.

You run this when something is already wrong, so it has two jobs beyond probing:
never let one broken dependency hide the others, and never take so long that
Vercel kills it and serves HTML where the panel expects a report.
"""

import time
import unittest
from unittest import mock

from fantasybot import config, selftest
from tests.support import StorageTestCase


class _NoNetwork(StorageTestCase):
    """Tests must not reach the internet.

    `selftest.run()` probes the scraped sources for real, which in a test means
    a blocked connection and several seconds of waiting for it to fail. Stubbed
    here so the suite stays fast and works offline — the probe's own logic is
    tested separately, without a socket.
    """

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(
            selftest, "_sources",
            return_value=(selftest.OK, "stub: fuentes OK"))
        patch.start()
        self.addCleanup(patch.stop)
        llm = mock.patch.object(
            selftest, "_llm",
            return_value=(selftest.WARN, "stub: sin LLM"))
        llm.start()
        self.addCleanup(llm.stop)


class Probes(_NoNetwork):
    def test_a_broken_probe_does_not_stop_the_others(self):
        with mock.patch.object(selftest, "_storage",
                               side_effect=RuntimeError("boom")):
            report = selftest.run(budget_seconds=20)
        names = [c["check"] for c in report["checks"]]
        self.assertIn("Almacenamiento (Supabase)", names)
        self.assertGreater(len(names), 3, "the rest must still be reported")
        first = report["checks"][0]
        self.assertEqual(first["status"], selftest.FAIL)
        self.assertIn("boom", first["detail"])

    def test_it_reports_counts_and_a_verdict(self):
        report = selftest.run(budget_seconds=20)
        self.assertEqual(set(report["counts"]),
                         {selftest.OK, selftest.WARN, selftest.FAIL})
        self.assertEqual(report["ok"], report["counts"][selftest.FAIL] == 0)
        self.assertIn("bien", report["summary"])

    def test_api_probes_are_skipped_once_rather_than_failing_five_times(self):
        """With no session there is no client, and five identical failures tell
        you nothing the first one did not."""
        report = selftest.run(budget_seconds=20)
        details = [c["check"] for c in report["checks"]]
        self.assertNotIn("Tu usuario", details)
        self.assertNotIn("Mercado", details)

    def test_it_stops_when_the_budget_is_gone(self):
        report = selftest.run(budget_seconds=0)
        self.assertTrue(all(c["status"] == selftest.WARN
                            for c in report["checks"]))
        self.assertTrue(any("Sin tiempo" in c["detail"]
                            for c in report["checks"]))

    def test_it_clears_the_fetch_deadline_afterwards(self):
        """A deadline left set would silently refuse every later scrape."""
        from fantasybot import net
        selftest.run(budget_seconds=5)
        self.assertIsNone(net._remaining())

    def test_the_deadline_is_cleared_even_when_a_probe_explodes(self):
        from fantasybot import net
        with mock.patch.object(selftest, "_run",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                selftest.run(budget_seconds=5)
        self.assertIsNone(net._remaining())


class AutonomyProbe(_NoNetwork):
    def setUp(self):
        super().setUp()
        self._saved_flags = {n: getattr(config, n) for n in
                             ("AUTO_EXECUTE", "AUTO_LINEUP", "AUTO_BIDS",
                              "AUTO_SELLS", "AUTO_CLAUSES", "AUTO_SHIELD")}
        self.addCleanup(lambda: [setattr(config, n, v)
                                 for n, v in self._saved_flags.items()])

    def test_observe_only_mode_is_called_out(self):
        config.AUTO_EXECUTE = False
        status, detail = selftest._autonomy()
        self.assertEqual(status, selftest.WARN)
        self.assertIn("no actúa", detail)

    def test_full_autonomy_reads_as_ok(self):
        for n in self._saved_flags:
            setattr(config, n, True)
        status, detail = selftest._autonomy()
        self.assertEqual(status, selftest.OK)
        self.assertIn("completa", detail)

    def test_a_missing_flag_is_named(self):
        for n in self._saved_flags:
            setattr(config, n, True)
        config.AUTO_CLAUSES = False
        status, detail = selftest._autonomy()
        self.assertEqual(status, selftest.WARN)
        self.assertIn("cláusulas", detail)


class SchedulerProbe(_NoNetwork):
    def test_no_executions_names_the_likely_cause(self):
        status, detail = selftest._scheduler()
        self.assertEqual(status, selftest.FAIL)
        self.assertIn("BOT_CRON_SECRET", detail)

    def test_a_recent_execution_is_ok(self):
        self.store.finish_execution(self.store.start_execution("tick"), "done")
        status, _ = selftest._scheduler()
        self.assertEqual(status, selftest.OK)


class SourcesProbe(StorageTestCase):
    """The scraped-source probe, checked without a socket."""

    def _probe(self, trends, lineups):
        with mock.patch("fantasybot.sources.market_trends.trends_index",
                        return_value=dict.fromkeys(range(trends))), \
             mock.patch("fantasybot.sources.lineups.probable_lineups",
                        return_value=dict.fromkeys(range(lineups))):
            return selftest._sources()

    def test_healthy_counts_pass(self):
        self.assertEqual(self._probe(400, 300)[0], selftest.OK)

    def test_a_collapsed_count_is_a_warning_not_a_silent_pass(self):
        """A redesigned page returns an empty index and everything DEGRADES —
        flips stop being found, the optimiser falls back to priors — while the
        bot looks like it is working. The count is the only signal."""
        status, detail = self._probe(3, 300)
        self.assertEqual(status, selftest.WARN)
        self.assertIn("flojas", detail)

    def test_both_sources_down_is_still_only_a_warning(self):
        """The bot keeps playing with less information; that is not a failure."""
        self.assertEqual(self._probe(0, 0)[0], selftest.WARN)

"""The diagnosis itself must not be the thing that breaks.

You run this when something is already wrong, so it has two jobs beyond probing:
never let one broken dependency hide the others, and never take so long that
Vercel kills it and serves HTML where the panel expects a report.
"""

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

    def test_an_automated_wake_up_is_ok(self):
        self.store.finish_execution(self.store.start_execution("tick:db"),
                                    "done")
        status, detail = selftest._scheduler()
        self.assertEqual(status, selftest.OK)
        self.assertIn("db", detail)

    def test_hand_triggered_runs_alone_are_not_a_working_scheduler(self):
        """A run you clicked looks exactly like a scheduled one in the log. If
        every recent execution came from a person, nothing is automating this —
        and saying OK there is the diagnosis lying."""
        for _ in range(3):
            self.store.finish_execution(
                self.store.start_execution("tick:unknown"), "done")
        status, detail = selftest._scheduler()
        self.assertEqual(status, selftest.WARN)
        self.assertIn("a mano", detail)

    def test_the_github_clock_counts_as_automated(self):
        self.store.finish_execution(self.store.start_execution("tick:github"),
                                    "done")
        self.assertEqual(selftest._scheduler()[0], selftest.OK)


class DatabaseClockProbe(StorageTestCase):
    """The migration ships a placeholder URL you are meant to replace. Left in,
    pg_net posts to a domain that does not resolve, every minute, forever — and
    the bot is never woken. That must never read as OK."""

    HEALTHY_CLOCK = {"scheduled": True, "active": True, "runs_last_hour": 59,
                     "http_ok": 59, "http_failed": 0}

    def setUp(self):
        super().setUp()
        from fantasybot import config
        self._saved_secret = config.BOT_CRON_SECRET
        config.BOT_CRON_SECRET = "el-de-vercel"
        self.addCleanup(lambda: setattr(config, "BOT_CRON_SECRET",
                                        self._saved_secret))

    def _probe(self, rows, clock="healthy"):
        """`rows` is the scheduler_config row; `clock` is what the database says
        about the cron JOB. They are independent, which is the whole point."""
        status = self.HEALTHY_CLOCK if clock == "healthy" else clock
        # Unless a case is about the secret, every row carries the right one —
        # otherwise each of these tests would fail on the secret check first and
        # never reach the thing it was written to check.
        rows = [{"bot_secret": "el-de-vercel", **r} for r in rows]

        def fake_request(method, path, params=None, body=None, prefer=None):
            if path.startswith("rpc/"):
                if status is None:
                    raise RuntimeError("function does not exist")
                return status
            return rows

        with mock.patch.object(self.store, "_request", fake_request,
                               create=True):
            with mock.patch.object(type(self.store), "kind", "supabase"):
                return selftest._db_scheduler()

    def test_the_placeholder_is_a_failure_not_a_pass(self):
        status, detail = self._probe([{"app_url": "https://TU-APP.vercel.app",
                                       "enabled": True}])
        self.assertEqual(status, selftest.FAIL)
        self.assertIn("placeholder", detail)
        self.assertIn("update public.scheduler_config", detail)

    def test_a_real_url_passes(self):
        status, detail = self._probe([{"app_url": "https://real.vercel.app",
                                       "enabled": True}])
        self.assertEqual(status, selftest.OK)
        self.assertIn("cada minuto", detail)
        self.assertIn("59", detail, "it should quote how often it actually ran")

    def test_a_config_row_without_a_cron_job_is_a_failure(self):
        """The row existing is not the clock running. That distinction cost a
        night: scheduler_config was there, every check went green, and
        cron.schedule had never been run."""
        status, detail = self._probe(
            [{"app_url": "https://real.vercel.app", "enabled": True}],
            clock={"scheduled": False})
        self.assertEqual(status, selftest.FAIL)
        self.assertIn("EL CRON NO ESTÁ CREADO", detail)

    def test_a_cron_that_barely_runs_is_flagged(self):
        status, detail = self._probe(
            [{"app_url": "https://real.vercel.app", "enabled": True}],
            clock={"scheduled": True, "active": True, "runs_last_hour": 2})
        self.assertEqual(status, selftest.WARN)
        self.assertIn("2 veces", detail)

    def test_a_mismatched_secret_is_named_outright(self):
        """Otherwise the report says "the calls fail" and leaves you comparing
        two opaque strings by hand, across two web consoles."""
        status, detail = self._probe([{"app_url": "https://real.vercel.app",
                                       "enabled": True,
                                       "bot_secret": "el-viejo"}])
        self.assertEqual(status, selftest.FAIL)
        self.assertIn("no coincide", detail)

    def test_an_empty_secret_is_named_outright(self):
        status, detail = self._probe([{"app_url": "https://real.vercel.app",
                                       "enabled": True, "bot_secret": ""}])
        self.assertEqual(status, selftest.FAIL)
        self.assertIn("401", detail)

    def test_failing_calls_with_the_right_secret_point_elsewhere(self):
        """Once the secret is known to match, repeating "it is probably the
        secret" is the diagnosis lying — the remaining causes are the URL
        pointing at another deployment, or Vercel refusing before our code."""
        status, detail = self._probe(
            [{"app_url": "https://real.vercel.app", "enabled": True}],
            clock={"scheduled": True, "active": True, "runs_last_hour": 60,
                   "http_ok": 0, "http_failed": 60})
        self.assertEqual(status, selftest.FAIL)
        self.assertIn("Deployment Protection", detail)

    def test_a_missing_status_function_asks_for_the_migration(self):
        status, detail = self._probe(
            [{"app_url": "https://real.vercel.app", "enabled": True}],
            clock=None)
        self.assertEqual(status, selftest.WARN)
        self.assertIn("0003", detail)

    def test_disabled_is_reported(self):
        status, _ = self._probe([{"app_url": "https://real.vercel.app",
                                  "enabled": False}])
        self.assertEqual(status, selftest.WARN)

    def test_an_empty_url_is_a_failure(self):
        self.assertEqual(self._probe([{"app_url": "", "enabled": True}])[0],
                         selftest.FAIL)


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


class SelfHealingClock(StorageTestCase):
    """The bot repairing the database's clock.

    An unreplaced placeholder fails in the worst possible way: pg_net posts to a
    domain that does not resolve, every minute, forever, and nothing is ever
    woken. No error, no symptom, just a bot that quietly does nothing. A running
    function knows its own address, so it can fix the row — but only when nobody
    chose that value on purpose.
    """

    def setUp(self):
        super().setUp()
        from fantasybot import config, tick
        self.tick, self.config = tick, config
        self._saved_url = config.VERCEL_APP_URL
        self._saved_secret = config.BOT_CRON_SECRET
        config.VERCEL_APP_URL = "https://real.vercel.app"
        config.BOT_CRON_SECRET = "el-de-vercel"
        self.addCleanup(lambda: setattr(config, "VERCEL_APP_URL",
                                        self._saved_url))
        self.addCleanup(lambda: setattr(config, "BOT_CRON_SECRET",
                                        self._saved_secret))

    def _table(self, stored, stored_secret=None):
        """A stand-in that behaves like the row, not like a mock.

        The repair reads the row back after writing it, so a fake that answers
        the second GET with the value from before the PATCH would report every
        repair as failed — and, worse, a fake that ignores the PATCH entirely
        would hide exactly the bug this read-back exists to catch.
        """
        from fantasybot import config
        secret = (stored_secret if stored_secret is not None
                  else config.BOT_CRON_SECRET)
        row = None if stored is None else {"app_url": stored,
                                           "bot_secret": secret}
        calls = []

        def fake_request(method, path, params=None, body=None, prefer=None):
            calls.append((method, dict(params or {}), body))
            if method == "GET":
                return [] if row is None else [dict(row)]
            if method == "PATCH" and row is not None:
                row.update({k: v for k, v in (body or {}).items()
                            if k != "updated_at"})
            return None

        return row, calls, fake_request

    def _heal(self, stored, stored_secret=None, calls_out=None):
        _, calls, fake_request = self._table(stored, stored_secret)
        with mock.patch.object(type(self.store), "kind", "supabase"), \
             mock.patch.object(self.store, "_request", fake_request,
                               create=True):
            self.tick._heal_scheduler_url(self.store)
        if calls_out is not None:
            calls_out.extend(calls)
        return [b for m, _p, b in calls if m == "PATCH"]

    def test_it_replaces_a_placeholder(self):
        written = self._heal("https://TU-APP.vercel.app")
        self.assertEqual(written[0]["app_url"], "https://real.vercel.app")

    def test_it_syncs_a_mismatched_secret(self):
        """Fifteen 401s in fifteen minutes is what a wrong secret looks like:
        the clock runs, every call is refused, nothing surfaces."""
        written = self._heal("https://real.vercel.app",
                             stored_secret="el-viejo")
        self.assertEqual(written[0]["bot_secret"], "el-de-vercel")
        self.assertNotIn("app_url", written[0],
                         "a URL somebody chose must be left alone")

    def test_a_matching_secret_is_not_rewritten(self):
        self.assertEqual(self._heal("https://real.vercel.app",
                                    stored_secret="el-de-vercel"), [])

    def test_it_reads_the_row_the_clock_reads(self):
        """fantasybot_wake() selects id = 1. Inspecting "the first row" and
        writing to id = 1 are the same row until the table holds two, and then
        the repair reports success having changed nothing."""
        calls = []
        self._heal("https://TU-APP.vercel.app", calls_out=calls)
        gets = [params for m, params, _b in calls if m == "GET"]
        self.assertTrue(gets)
        for params in gets:
            self.assertEqual(params.get("id"), "eq.1")

    def test_it_mints_a_secret_when_neither_side_has_one(self):
        """Without this the clock is locked out for good: nothing to sync, and
        every call refused by a deployment that cannot recognise it."""
        self.config.BOT_CRON_SECRET = ""
        written = self._heal("https://real.vercel.app", stored_secret="")
        minted = written[0]["bot_secret"]
        self.assertGreaterEqual(len(minted), 20)
        # The guard accepts it from then on; test_api_auth covers that end.

    def test_it_leaves_a_stored_secret_alone_when_vercel_has_none(self):
        """The deployment then accepts the stored one instead of overwriting a
        working key with a fresh one every time a page is loaded."""
        self.config.BOT_CRON_SECRET = ""
        self.assertEqual(self._heal("https://real.vercel.app",
                                    stored_secret="el-que-ya-funciona"), [])

    def test_a_write_that_does_not_land_is_not_announced(self):
        """A PATCH matching no row succeeds — 200, empty, no complaint. Saying
        "repaired" then sends you looking in the wrong place for a night."""
        def fake_request(method, path, params=None, body=None, prefer=None):
            if method == "GET":
                return [{"app_url": "https://TU-APP.vercel.app",
                         "bot_secret": "el-de-vercel"}]
            return None          # the PATCH changes nothing

        with mock.patch.object(type(self.store), "kind", "supabase"), \
             mock.patch.object(self.store, "_request", fake_request,
                               create=True):
            self.assertIs(self.tick._heal_scheduler_url(self.store), False)

    def test_it_reports_whether_it_wrote(self):
        """The health endpoint surfaces this, so it has to be truthful: a repair
        that happened and one that was unnecessary must not look the same."""
        from fantasybot import tick as tick_mod

        def run(stored):
            _row, _calls, fake_request = self._table(stored)
            with mock.patch.object(type(self.store), "kind", "supabase"), \
                 mock.patch.object(self.store, "_request", fake_request,
                                   create=True):
                return tick_mod._heal_scheduler_url(self.store)

        self.assertIs(run("https://TU-APP.vercel.app"), True)
        self.assertIs(run("https://ya-estaba-bien.vercel.app"), False)
        self.assertIs(run(None), False)

    def test_it_replaces_an_empty_url(self):
        self.assertTrue(self._heal(""))

    def test_it_never_overrides_a_url_somebody_chose(self):
        """Two deployments can share one database; hijacking the other one's
        scheduler would be worse than the problem being solved."""
        self.assertEqual(self._heal("https://otra-app.vercel.app"), [])

    def test_a_missing_migration_is_not_an_error(self):
        self.assertEqual(self._heal(None), [])

    def test_it_does_nothing_without_a_known_address(self):
        self.config.VERCEL_APP_URL = ""
        with mock.patch.object(self.config, "self_url", return_value=""):
            self.assertEqual(self._heal("https://TU-APP.vercel.app"), [])


class SelfUrl(unittest.TestCase):
    def test_it_prefers_the_stable_production_domain(self):
        import os
        from fantasybot import config
        saved = config.VERCEL_APP_URL
        config.VERCEL_APP_URL = ""
        try:
            with mock.patch.dict(os.environ, {
                    "VERCEL_PROJECT_PRODUCTION_URL": "stable.vercel.app",
                    "VERCEL_URL": "deploy-abc123.vercel.app"}, clear=False):
                self.assertEqual(config.self_url(), "https://stable.vercel.app")
        finally:
            config.VERCEL_APP_URL = saved

    def test_it_adds_the_scheme_vercel_leaves_off(self):
        import os
        from fantasybot import config
        saved = config.VERCEL_APP_URL
        config.VERCEL_APP_URL = ""
        try:
            with mock.patch.dict(os.environ,
                                 {"VERCEL_PROJECT_PRODUCTION_URL": "x.vercel.app",
                                  "VERCEL_URL": ""}, clear=False):
                self.assertTrue(config.self_url().startswith("https://"))
        finally:
            config.VERCEL_APP_URL = saved

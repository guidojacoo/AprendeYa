"""The tick as a whole: cadence, locking, and never dying silently."""

from datetime import timedelta
from unittest import mock

from fantasybot import config, tick
from fantasybot.storage import to_iso, utcnow
from tests.support import FakeClient, StorageTestCase


class TickAlwaysReports(StorageTestCase):
    def test_a_broken_review_still_returns_a_summary(self):
        """A tick that dies without a trace is a bot you cannot debug, so the
        failure path has to produce the same shape as the success path."""
        with mock.patch.object(tick, "run_review",
                               side_effect=RuntimeError("LaLiga is down")):
            out = tick.run(log=lambda m: None)
        self.assertFalse(out["ok"])
        self.assertIn("LaLiga is down", out["error"])
        self.assertIn("traceback", out)
        self.assertEqual(self.store.recent_executions()[0]["status"], "failed")

    def test_a_quiet_tick_records_an_execution_anyway(self):
        with mock.patch.object(tick, "run_review",
                               return_value={"status": "skipped"}):
            out = tick.run(log=lambda m: None)
        self.assertTrue(out["ok"])
        self.assertEqual(out["actions"], [])
        self.assertIsNone(out["next_deadline"])
        self.assertEqual(self.store.recent_executions()[0]["status"], "done")


class ReviewCadence(StorageTestCase):
    def test_it_is_skipped_until_the_interval_has_passed(self):
        self.store.put_doc("last_review_at", to_iso(utcnow()))
        ctx = tick.TickContext(budget_seconds=10, log=lambda m: None)
        ctx.holder = "t1"
        out = tick.run_review(ctx)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["reason"], "not due")

    def test_force_overrides_the_cadence(self):
        self.store.put_doc("last_review_at", to_iso(utcnow()))
        self.store.set_setting("review_interval", 999999)
        ctx = tick.TickContext(client=FakeClient(), budget_seconds=10,
                               log=lambda m: None)
        ctx.holder = "t1"
        with mock.patch.object(tick.agent_mod, "review") as review:
            review.side_effect = RuntimeError("reached the review")
            with self.assertRaises(RuntimeError):
                tick.run_review(ctx, force=True)

    def test_a_second_tick_will_not_review_concurrently(self):
        self.store.acquire_lock(tick.REVIEW_LOCK, 60, "the-other-tick")
        ctx = tick.TickContext(budget_seconds=10, log=lambda m: None)
        ctx.holder = "me"
        out = tick.run_review(ctx, force=True)
        self.assertEqual(out["reason"], "another tick is reviewing")

    def test_the_lock_is_released_even_when_the_review_blows_up(self):
        ctx = tick.TickContext(client=FakeClient(), budget_seconds=10,
                               log=lambda m: None)
        ctx.holder = "me"
        with mock.patch.object(tick.agent_mod, "review",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                tick.run_review(ctx, force=True)
        self.assertTrue(self.store.acquire_lock(tick.REVIEW_LOCK, 60, "next-tick"),
                        "a crashed review must not wedge every later tick")


class MissingCredentials(StorageTestCase):
    def test_no_tokens_fails_the_tick_loudly(self):
        """With no LaLiga session the bot cannot do anything — and must SAY so,
        in the execution row, rather than logging a quiet no-op forever."""
        out = tick.run(force_review=True, log=lambda m: None)
        self.assertFalse(out["ok"])
        self.assertIn("token", out["error"].lower())
        self.assertEqual(self.store.recent_executions()[0]["status"], "failed")


class SniperMode(StorageTestCase):
    def test_it_does_not_spend_its_budget_reviewing(self):
        with mock.patch.object(tick, "run_review") as review:
            out = tick.run(mode="sniper", log=lambda m: None)
        review.assert_not_called()
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "sniper")


class SleepHint(StorageTestCase):
    def test_none_when_nothing_is_queued(self):
        self.assertIsNone(tick._sleep_hint())

    def test_seconds_until_the_next_action(self):
        from fantasybot import scheduler
        scheduler.schedule_bid("L", "m1", 1_000_000,
                               utcnow() + timedelta(seconds=300))
        hint = tick._sleep_hint()
        # 300s to the close, minus the 60s lead the bid action is queued at.
        self.assertTrue(230 <= hint <= 245, f"unexpected hint: {hint}")

    def test_never_negative(self):
        from fantasybot import scheduler
        scheduler.schedule_bid("L", "m1", 1_000_000,
                               utcnow() - timedelta(hours=1))
        self.assertEqual(tick._sleep_hint(), 0)


class Settings(StorageTestCase):
    def test_a_db_setting_beats_the_env_default(self):
        self.assertEqual(tick.setting("review_interval"), config.REVIEW_INTERVAL)
        self.store.set_setting("review_interval", 120)
        self.assertEqual(tick.setting("review_interval"), 120)

    def test_an_unknown_setting_is_none(self):
        self.assertIsNone(tick.setting("does_not_exist"))

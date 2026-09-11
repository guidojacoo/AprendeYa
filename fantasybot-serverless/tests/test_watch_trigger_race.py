"""Regression: `fantasybot watch --run`/`--hermes` must not be able to fire a real
agent cycle (money, and in hermes mode LLM tokens) concurrently with another one
already in flight (the "Launch agent" button, or a second `watch` process on the
same team). `cmd_watch`'s auto-fire used to call `subprocess.run` directly,
completely bypassing `monitor`'s `_RUNNING`/`_RUN_LOCK` guard -- only the button
was ever protected. Both paths now go through the single guarded `monitor.trigger()`.
"""

import threading
import types
import unittest
from unittest import mock

from fantasybot import cli, monitor


class TriggerIsMutuallyExclusive(unittest.TestCase):
    def setUp(self):
        monitor._RUNNING = False

    def tearDown(self):
        monitor._RUNNING = False

    def test_fires_and_returns_true_when_idle(self):
        with mock.patch.object(monitor.threading, "Thread") as Thread:
            ok = monitor.trigger("agent")
        self.assertTrue(ok)
        Thread.assert_called_once()

    def test_returns_false_without_firing_when_already_running(self):
        monitor._RUNNING = True
        with mock.patch.object(monitor.threading, "Thread") as Thread:
            ok = monitor.trigger("agent")
        self.assertFalse(ok)
        Thread.assert_not_called()


class WatchAutoFireGoesThroughTrigger(unittest.TestCase):
    """`cmd_watch`'s `fire()` must call `monitor.trigger(mode)`, not spawn its own
    subprocess -- that's the actual concurrency hole this fix closes."""

    def _run_cmd_watch(self, run=False, hermes=False):
        args = types.SimpleNamespace(host="127.0.0.1", port=9137, run=run,
                                     hermes=hermes, no_open=True)
        fake_srv = mock.Mock()

        def sleep_se(secs):
            if secs == 1.2:   # fire()'s pre-connect delay: proceed immediately
                return None
            raise KeyboardInterrupt   # the final wait loop: exit cmd_watch right away

        with mock.patch.object(monitor, "serve", return_value=(fake_srv, "http://x")), \
             mock.patch.object(monitor, "trigger") as trig, \
             mock.patch.object(cli.time, "sleep", side_effect=sleep_se), \
             mock.patch.object(threading.Thread, "start",
                               lambda self: self._target and self._target()), \
             mock.patch("builtins.print"):
            cli.cmd_watch(args)
        return trig

    def test_run_flag_triggers_deterministic_agent_via_monitor(self):
        trig = self._run_cmd_watch(run=True)
        trig.assert_called_once_with("agent")

    def test_hermes_flag_triggers_hermes_via_monitor(self):
        trig = self._run_cmd_watch(hermes=True)
        trig.assert_called_once_with("hermes")

    def test_no_flags_never_triggers(self):
        trig = self._run_cmd_watch()
        trig.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""What a tick writes to its history, sixty times an hour, forever.

The dashboard went completely blank -- no balance, no squad, no queue, just
"StorageUnavailable: GET executions: The read operation timed out" -- and the
cause was self-inflicted. Every diagnostic added to the tick response this
session (the bid funnel, the clause defence, the rival breakdown, the recorded
payload shapes) also went into the execution row's summary. Ten rows of that is
megabytes, and `select *` over them timed out.

The analysis is a snapshot and already lives in `last_report`. The row keeps
what a HISTORY needs: did it work, how long, what did it do, what broke.
"""

from fantasybot import tick
from tests.support import StorageTestCase


class TheRowKeepsTheFactsAndDropsTheAnalysis(StorageTestCase):
    def _summary(self):
        return {
            "ok": True, "mode": "tick", "duration_seconds": 19.1,
            "actions": [{"key": "bid:1", "status": "done"}],
            "clock": {"runs_last_hour": 60},
            "review": {
                "status": "ok", "seconds": 21.3, "skipped": [], "money": 44_000_000,
                "bids": {"scheduled": [{"market_id": "m1"}, {"market_id": "m2"}],
                         "funnel": {"en el mercado": 20}},
                # The heavy ones, all of which live in `last_report` already.
                "squad": {"counts": {"POR": 2}, "position_ids": ["1"] * 40},
                "market": [{"nombre": f"p{i}"} for i in range(30)],
                "upgrades": [{"nombre": f"u{i}"} for i in range(10)],
                "defense": {"exposed": [{"nombre": f"d{i}"} for i in range(16)],
                            "activity_shape": {"biggest": [{"amount": 1}] * 4}},
            },
        }

    def test_the_operational_facts_survive(self):
        got = tick._slim(self._summary())
        self.assertEqual(got["ok"], True)
        self.assertEqual(got["duration_seconds"], 19.1)
        self.assertEqual(got["actions"], [{"key": "bid:1", "status": "done"}])
        self.assertEqual(got["clock"], {"runs_last_hour": 60})

    def test_the_review_keeps_its_shape_not_its_contents(self):
        got = tick._slim(self._summary())["review"]
        self.assertEqual(got["status"], "ok")
        self.assertEqual(got["seconds"], 21.3)
        self.assertEqual(got["money"], 44_000_000)
        self.assertEqual(got["scheduled_bids"], 2,
                         "what it bought is the one analysis number a history "
                         "actually wants")

    def test_the_heavy_analysis_is_dropped(self):
        got = tick._slim(self._summary())["review"]
        for gone in ("market", "upgrades", "defense", "squad", "bids"):
            self.assertNotIn(gone, got, f"{gone} belongs in last_report")

    def test_a_failure_keeps_its_cause(self):
        got = tick._slim({"ok": False, "error": "boom", "traceback": "line 1",
                          "review": {"status": "failed"}})
        self.assertEqual(got["error"], "boom")
        self.assertEqual(got["traceback"], "line 1")

    def test_the_row_stays_small(self):
        import json
        got = json.dumps(tick._slim(self._summary()))
        self.assertLess(len(got), 2000,
                        "ten of these is what the dashboard reads in one query")

    def test_a_review_that_never_ran_is_passed_through(self):
        self.assertIsNone(tick._slim({"ok": True, "review": None}).get("review"))
        self.assertEqual(tick._slim({"ok": True})["ok"], True)

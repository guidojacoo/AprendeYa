"""Tests for fantasybot.sources.value_history — daily value snapshots + a pure trend
helper (banking LaLiga's official marketValue so we can later compute trends ourselves).

Pure functions (snapshot_from_players, trend) take date strings in — no datetime.now() —
so they're fully deterministic. save/load do the only I/O (JSON files in a dir).
"""

import json
import os
import tempfile
import unittest

from fantasybot.sources import value_history as vh


class TestSnapshotFromPlayers(unittest.TestCase):
    def test_compacts_and_coerces(self):
        players = [
            {"id": "68", "marketValue": "51354003", "playerStatus": "ok"},
            {"id": 99, "marketValue": 2000000, "playerStatus": "injured"},
        ]
        snap = vh.snapshot_from_players(players)
        self.assertEqual(snap, {
            "68": {"v": 51354003, "s": "ok"},
            "99": {"v": 2000000, "s": "injured"},
        })
        # marketValue string was coerced to int
        self.assertIsInstance(snap["68"]["v"], int)

    def test_skips_idless_and_valueless(self):
        players = [
            {"marketValue": "100", "playerStatus": "ok"},          # no id
            {"id": "7", "playerStatus": "ok"},                     # no value
            {"id": "8", "marketValue": None, "playerStatus": "ok"},  # null value
            {"id": "9", "marketValue": "", "playerStatus": "ok"},   # empty value
            {"id": "10", "marketValue": "500", "playerStatus": "ok"},
        ]
        snap = vh.snapshot_from_players(players)
        self.assertEqual(snap, {"10": {"v": 500, "s": "ok"}})

    def test_missing_status_defaults_none(self):
        snap = vh.snapshot_from_players([{"id": "1", "marketValue": "5"}])
        self.assertEqual(snap, {"1": {"v": 5, "s": None}})

    def test_empty_input(self):
        self.assertEqual(vh.snapshot_from_players([]), {})
        self.assertEqual(vh.snapshot_from_players(None), {})

    def test_is_pure_does_not_mutate(self):
        players = [{"id": "1", "marketValue": "5", "playerStatus": "ok"}]
        vh.snapshot_from_players(players)
        self.assertEqual(players, [{"id": "1", "marketValue": "5", "playerStatus": "ok"}])


class TestSaveLoad(unittest.TestCase):
    def test_save_writes_and_returns_path(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "snaps")  # not created yet
            path = vh.save_snapshot(sub, "2026-08-20", {"1": {"v": 5, "s": "ok"}})
            self.assertEqual(path, os.path.join(sub, "2026-08-20.json"))
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"1": {"v": 5, "s": "ok"}})

    def test_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            vh.save_snapshot(d, "2026-08-20", {"1": {"v": 5, "s": "ok"}})
            self.assertEqual(vh.load_snapshot(d, "2026-08-20"),
                             {"1": {"v": 5, "s": "ok"}})

    def test_load_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(vh.load_snapshot(d, "2020-01-01"), {})

    def test_load_unreadable_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "2026-08-20.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            self.assertEqual(vh.load_snapshot(d, "2026-08-20"), {})

    def test_prune_removes_old_keeps_recent_and_ignores_others(self):
        with tempfile.TemporaryDirectory() as d:
            # An old snapshot (well beyond KEEP_DAYS), a recent one, and a non-snapshot file.
            vh.save_snapshot(d, "2026-01-01", {"a": {"v": 1, "s": "ok"}})  # old
            other = os.path.join(d, "notes.txt")
            with open(other, "w", encoding="utf-8") as fh:
                fh.write("keep me")
            bad = os.path.join(d, "2026-13-99.json")  # looks like json but bad date
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("{}")
            # Saving today prunes anything older than KEEP_DAYS relative to today.
            vh.save_snapshot(d, "2026-08-20", {"b": {"v": 2, "s": "ok"}})

            names = set(os.listdir(d))
            self.assertIn("2026-08-20.json", names)   # today kept
            self.assertNotIn("2026-01-01.json", names)  # old pruned
            self.assertIn("notes.txt", names)          # non-snapshot untouched
            self.assertIn("2026-13-99.json", names)    # unparseable date untouched

    def test_prune_keeps_within_window(self):
        with tempfile.TemporaryDirectory() as d:
            # 39 days before today (< KEEP_DAYS=40) must survive.
            vh.save_snapshot(d, "2026-07-12", {"a": {"v": 1, "s": "ok"}})
            vh.save_snapshot(d, "2026-08-20", {"b": {"v": 2, "s": "ok"}})
            names = set(os.listdir(d))
            self.assertIn("2026-07-12.json", names)
            self.assertIn("2026-08-20.json", names)


class TestTrend(unittest.TestCase):
    def _setup(self, d):
        vh.save_snapshot(d, "2026-08-10", {"1": {"v": 1000, "s": "ok"},
                                           "2": {"v": 500, "s": "ok"}})
        vh.save_snapshot(d, "2026-08-20", {"1": {"v": 1200, "s": "ok"},
                                           "2": {"v": 400, "s": "ok"}})

    def test_rise(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup(d)
            r = vh.trend(d, "1", "2026-08-20", 10)
            self.assertEqual(r, {"value": 1200, "prev": 1000,
                                 "delta": 200, "pct": 20.0})

    def test_fall(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup(d)
            r = vh.trend(d, "2", "2026-08-20", 10)
            self.assertEqual(r, {"value": 400, "prev": 500,
                                 "delta": -100, "pct": -20.0})

    def test_now_snapshot_override_avoids_reload(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup(d)
            # Pass today's snapshot directly (e.g. freshly fetched, not yet on disk).
            r = vh.trend(d, "1", "2026-08-20", 10,
                         now_snapshot={"1": {"v": 1500, "s": "ok"}})
            self.assertEqual(r["value"], 1500)
            self.assertEqual(r["delta"], 500)

    def test_missing_prev_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup(d)
            self.assertIsNone(vh.trend(d, "1", "2026-08-20", 999))

    def test_missing_player_in_prev(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup(d)
            self.assertIsNone(vh.trend(d, "3", "2026-08-20", 10))

    def test_missing_player_today(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup(d)
            self.assertIsNone(vh.trend(d, "3", "2026-08-20", 10,
                                       now_snapshot={"1": {"v": 1}}))

    def test_prev_zero_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            vh.save_snapshot(d, "2026-08-10", {"1": {"v": 0, "s": "ok"}})
            vh.save_snapshot(d, "2026-08-20", {"1": {"v": 1200, "s": "ok"}})
            self.assertIsNone(vh.trend(d, "1", "2026-08-20", 10))


if __name__ == "__main__":
    unittest.main()

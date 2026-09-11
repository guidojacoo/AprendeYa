"""Regression: llm-rec recommendation tasks must expire on their own.

They have no natural completion signal (unlike deterministic gap/clause/sell tasks, which
get reconciled each run), so without an age-based sweep they pile up in tasks.json until
someone deletes them by hand. `state.expire_tasks(prefix, max_age_days)` drops the stale
ones and leaves everything else alone.
"""

import os
import tempfile
import unittest
from unittest import mock

from fantasybot import state

DAY = 86400
NOW = 1_800_000_000  # fixed "now" so the test doesn't depend on the clock


class ExpireTasks(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "tasks.json")
        p = mock.patch.object(state, "TASKS_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)

    def _seed(self, tasks):
        state.save_tasks(tasks)

    def _keys(self):
        return {t.get("key") for t in state.load_tasks()}

    def test_drops_only_old_llm_recs(self):
        self._seed([
            {"id": 1, "key": "llm-rec:clause:aaa", "created": NOW - 20 * DAY},  # stale -> gone
            {"id": 2, "key": "llm-rec:sell:bbb", "created": NOW - 2 * DAY},     # fresh -> kept
            {"id": 3, "key": "clause:123", "created": NOW - 40 * DAY},          # not llm-rec -> kept
        ])
        removed = state.expire_tasks("llm-rec:", 14, now=NOW)
        self.assertEqual(removed, 1)
        self.assertEqual(self._keys(), {"llm-rec:sell:bbb", "clause:123"})

    def test_missing_created_is_treated_as_ancient(self):
        self._seed([{"id": 1, "key": "llm-rec:x", "text": "old"}])  # no 'created'
        self.assertEqual(state.expire_tasks("llm-rec:", 14, now=NOW), 1)
        self.assertEqual(state.load_tasks(), [])

    def test_boundary_just_under_ttl_is_kept(self):
        self._seed([{"id": 1, "key": "llm-rec:x", "created": NOW - 14 * DAY + 60}])
        self.assertEqual(state.expire_tasks("llm-rec:", 14, now=NOW), 0)

    def test_no_write_when_nothing_expires(self):
        self._seed([{"id": 1, "key": "llm-rec:x", "created": NOW}])
        mtime_before = os.path.getmtime(self.path)
        self.assertEqual(state.expire_tasks("llm-rec:", 14, now=NOW), 0)
        self.assertEqual(os.path.getmtime(self.path), mtime_before)  # untouched


if __name__ == "__main__":
    unittest.main()

"""The persistence contract every backend must honour.

These are the rules the serverless design rests on. If any of them breaks, the
bot can bid twice, lose a token refresh, or wedge itself behind a dead lock.
"""

from datetime import timedelta

from fantasybot.storage import DONE, RUNNING, parse_iso, utcnow
from tests.support import StorageTestCase


class Documents(StorageTestCase):
    def test_roundtrip_and_default(self):
        self.assertEqual(self.store.get_doc("nope", {"d": 1}), {"d": 1})
        self.store.put_doc("tasks", [{"id": 1, "text": "x"}])
        self.assertEqual(self.store.get_doc("tasks"), [{"id": 1, "text": "x"}])

    def test_overwrite_replaces_not_merges(self):
        self.store.put_doc("bids", {"a": 1})
        self.store.put_doc("bids", {"b": 2})
        self.assertEqual(self.store.get_doc("bids"), {"b": 2})


class Cache(StorageTestCase):
    def test_hit_then_expiry(self):
        self.store.cache_put("k", {"v": 1}, ttl_seconds=60)
        self.assertEqual(self.store.cache_get("k"), {"v": 1})
        self.store.cache_put("k", {"v": 1}, ttl_seconds=0)
        self.assertIsNone(self.store.cache_get("k"))

    def test_miss_is_none(self):
        self.assertIsNone(self.store.cache_get("never-written"))


class Events(StorageTestCase):
    def test_chronological_and_capped(self):
        for i in range(5):
            self.store.emit_event({"iso": f"2026-01-0{i + 1}T00:00:00+00:00",
                                   "kind": "note", "title": f"e{i}"})
        got = self.store.load_events(limit=3)
        self.assertEqual([e["title"] for e in got], ["e2", "e3", "e4"])


class MarketSnapshots(StorageTestCase):
    def test_roundtrip_and_prune(self):
        self.store.put_market_snapshot("2026-01-01", {"1": {"v": 100}})
        self.store.put_market_snapshot("2026-03-01", {"1": {"v": 200}},
                                       keep_days=10)
        self.assertEqual(self.store.get_market_snapshot("2026-03-01"),
                         {"1": {"v": 200}})
        # January is far past the 10-day window, so the newer write dropped it.
        self.assertEqual(self.store.get_market_snapshot("2026-01-01"), {})


class ScheduledActions(StorageTestCase):
    def _schedule(self, key="bid:L:m1:close", when=None, **kw):
        return self.store.schedule_action(
            "bid", {"market_id": "m1"},
            execute_at=when or (utcnow() - timedelta(seconds=5)),
            idempotency_key=key, **kw)

    def test_same_key_does_not_queue_twice(self):
        a = self._schedule()
        b = self._schedule()
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.store.pending_actions()), 1)

    def test_finished_action_is_not_resurrected(self):
        a = self._schedule()
        self.store.claim_action(a)
        self.store.finish_action(a, DONE, result={"status": "bid"})
        again = self._schedule()
        self.assertEqual(again["status"], DONE)
        self.assertEqual(self.store.due_actions(), [])

    def test_claim_succeeds_once(self):
        a = self._schedule()
        first = dict(a)
        second = dict(a)
        self.assertTrue(self.store.claim_action(first))
        self.assertFalse(self.store.claim_action(second))

    def test_expired_lease_is_reclaimable(self):
        a = self._schedule()
        self.assertTrue(self.store.claim_action(a, lease_seconds=-1))
        self.assertEqual(a["status"], RUNNING)
        # The tick that held it died; the next one must be able to take over.
        due = self.store.due_actions()
        self.assertEqual(len(due), 1)
        self.assertTrue(self.store.claim_action(due[0]))

    def test_future_actions_are_not_due(self):
        self._schedule(key="later", when=utcnow() + timedelta(minutes=10))
        self.assertEqual(self.store.due_actions(), [])
        self.assertEqual(len(self.store.pending_actions()), 1)

    def test_next_deadline_is_the_earliest(self):
        soon = utcnow() + timedelta(minutes=2)
        self._schedule(key="a", when=utcnow() + timedelta(minutes=9))
        self._schedule(key="b", when=soon)
        self.assertEqual(parse_iso(self.store.next_deadline().isoformat()),
                         parse_iso(soon.isoformat()))

    def test_cancel_only_touches_pending(self):
        self._schedule(key="x")
        self.assertTrue(self.store.cancel_action("x"))
        self.assertFalse(self.store.cancel_action("x"))
        self.assertEqual(self.store.due_actions(), [])


class Locks(StorageTestCase):
    def test_mutual_exclusion_and_expiry(self):
        self.assertTrue(self.store.acquire_lock("review", 60, "tick-a"))
        self.assertFalse(self.store.acquire_lock("review", 60, "tick-b"))
        self.store.release_lock("review", "tick-a")
        self.assertTrue(self.store.acquire_lock("review", 60, "tick-b"))

    def test_dead_holder_does_not_wedge_the_bot(self):
        self.assertTrue(self.store.acquire_lock("review", -1, "dead-tick"))
        self.assertTrue(self.store.acquire_lock("review", 60, "live-tick"))

    def test_reentrant_for_the_same_holder(self):
        self.assertTrue(self.store.acquire_lock("review", 60, "me"))
        self.assertTrue(self.store.acquire_lock("review", 60, "me"))


class Executions(StorageTestCase):
    def test_start_and_finish_are_visible(self):
        eid = self.store.start_execution("tick")
        self.store.finish_execution(eid, DONE, summary={"ok": True})
        rows = self.store.recent_executions()
        self.assertEqual(rows[0]["status"], DONE)
        self.assertEqual(rows[0]["summary"], {"ok": True})


class Settings(StorageTestCase):
    def test_set_and_read_back(self):
        self.store.set_setting("review_interval", 900)
        self.assertEqual(self.store.get_settings()["review_interval"], 900)

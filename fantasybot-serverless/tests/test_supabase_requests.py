"""How the Supabase backend builds its requests.

Every other test runs against LocalStorage, because that is what makes them
runnable with no database and no network. The gap that leaves is exactly the
PostgREST wire format — and that is where a bug hid: `+` was in the list of
characters passed through literally, so an ISO timestamp's "+00:00" offset
reached Postgres as " 00:00" and every timestamp filter was rejected. The queue
was broken against Supabase and perfectly green in CI.

These tests exercise URL and payload construction with no network at all.
"""

import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone

from fantasybot.storage import PENDING, RUNNING, to_iso
from fantasybot.storage.supabase import SupabaseStorage, _project_url

NOW = datetime(2026, 9, 12, 0, 21, 8, 371615, tzinfo=timezone.utc)


class UrlBuilding(unittest.TestCase):
    def setUp(self):
        self.store = SupabaseStorage(url="https://x.supabase.co", key="k",
                                     scope="default")

    def _params(self, url):
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    def test_a_timestamp_offset_survives_the_round_trip(self):
        """The regression. A literal `+` in a query string decodes to a space."""
        iso = to_iso(NOW)
        self.assertIn("+00:00", iso)
        url = self.store._build_url("scheduled_actions",
                                    {"execute_at": f"lte.{iso}"})
        self.assertNotIn("+00:00", url, "the offset must be percent-encoded")
        self.assertIn("%2B00:00", url)
        self.assertEqual(self._params(url)["execute_at"][0], f"lte.{iso}")

    def test_no_bare_plus_reaches_the_wire(self):
        url = self.store._build_url("events", {"ts": f"gte.{to_iso(NOW)}",
                                               "select": "*"})
        query = urllib.parse.urlparse(url).query
        self.assertNotIn("+", query,
                         "a raw '+' in a query string is read as a space")

    def test_postgrest_operators_stay_readable(self):
        """`or=(...)` and `select=*` must NOT be encoded — PostgREST parses them
        as syntax, so over-encoding breaks it just as surely as under-encoding."""
        url = self.store._build_url("scheduled_actions", {
            "select": "*",
            "or": f"(status.eq.{PENDING},and(status.eq.{RUNNING},"
                  f"locked_until.lt.{to_iso(NOW)}))",
        })
        self.assertIn("select=*", url)
        self.assertIn("or=(status.eq.pending,and(status.eq.running,", url)

    def test_the_scope_filter_is_always_applied(self):
        """Rows are namespaced; a query that forgets the scope would read another
        team's state out of the same free project."""
        captured = {}
        self.store._request = lambda m, p, params=None, **kw: captured.update(
            {"path": p, "params": params}) or []
        self.store._select("events", {"kind": "eq.bid"})
        self.assertEqual(captured["params"]["scope"], "eq.default")

    def test_dates_and_colons_are_not_mangled(self):
        url = self.store._build_url("market_snapshots", {"day": "eq.2026-09-12"})
        self.assertIn("day=eq.2026-09-12", url)


class ProjectUrl(unittest.TestCase):
    def test_rest_suffix_is_not_doubled(self):
        for raw in ("https://x.supabase.co", "https://x.supabase.co/",
                    "https://x.supabase.co/rest/v1", "https://x.supabase.co/rest/v1/"):
            store = SupabaseStorage(url=raw, key="k")
            self.assertEqual(store.rest, "https://x.supabase.co/rest/v1",
                             f"failed for {raw!r}")


class DueFilter(unittest.TestCase):
    """The claim query is the heart of exactly-once. Its shape is worth pinning."""

    def setUp(self):
        self.store = SupabaseStorage(url="https://x.supabase.co", key="k")

    def test_it_claims_pending_and_reclaims_expired_leases(self):
        f = self.store._due_filter(NOW)
        self.assertEqual(f["execute_at"], f"lte.{to_iso(NOW)}")
        self.assertIn(f"status.eq.{PENDING}", f["or"])
        self.assertIn(f"status.eq.{RUNNING}", f["or"])
        self.assertIn("locked_until.lt.", f["or"],
                      "a tick that died must not hold its action forever")

    def test_the_filter_encodes_cleanly(self):
        url = self.store._build_url("scheduled_actions",
                                    self.store._due_filter(NOW))
        self.assertNotIn("+", urllib.parse.urlparse(url).query)

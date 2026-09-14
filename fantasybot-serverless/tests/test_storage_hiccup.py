"""A slow database is not a broken bot.

Two 504s from Supabase in half an hour took the whole execution down with them,
each one a red "Falló la ejecución" on the dashboard and a step toward a phone
buzzing. On a free tier woken sixty times an hour, a busy minute is a thing that
happens — and the next tick is a minute away.

What must not change: a query we got WRONG still fails loudly, because that one
will be wrong again next minute too.
"""

import urllib.error
from unittest import mock

from fantasybot.storage import StorageError, StorageUnavailable
from fantasybot.storage import supabase as sb
from tests.support import StorageTestCase


def _http_error(code, body=b'{"message":"Gateway Timeout"}'):
    return urllib.error.HTTPError("http://x", code, "err", {},
                                  __import__("io").BytesIO(body))


class ATimeoutIsItsOwnKindOfFailure(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.store = sb.SupabaseStorage.__new__(sb.SupabaseStorage)
        self.store.rest = "http://db/rest/v1"
        self.store.key = "k"
        self.store.scope = "test"

    def _call(self, side_effect):
        with mock.patch.object(sb.urllib.request, "urlopen",
                               side_effect=side_effect), \
             mock.patch.object(sb.time, "sleep"):
            return self.store._request("GET", "agent_state")

    def test_a_504_raises_unavailable_not_a_plain_error(self):
        with self.assertRaises(StorageUnavailable):
            self._call(_http_error(504))

    def test_a_dropped_connection_is_the_same_kind_of_thing(self):
        with self.assertRaises(StorageUnavailable):
            self._call(urllib.error.URLError("connection reset"))

    def test_a_bad_query_still_fails_loudly(self):
        """A 400 will be a 400 again next minute. That is a bug, not a hiccup."""
        with self.assertRaises(StorageError) as caught:
            self._call(_http_error(400, b'{"message":"invalid input syntax"}'))
        self.assertNotIsInstance(caught.exception, StorageUnavailable)

    def test_it_gives_up_before_it_eats_the_tick(self):
        """Three attempts at fifteen seconds is forty-five, and the whole tick
        has about fifty. A slow database used to take the function with it."""
        self.assertLess(sb.TIMEOUT * (sb.RETRIES + 1), 30)
        self.assertLessEqual(sb.RETRY_BUDGET, 25)

    def test_the_backoff_is_seconds_not_half_seconds(self):
        """A gateway timeout means the far side is busy. Coming back in half a
        second asks the same busy thing the same question."""
        self.assertGreaterEqual(sb.BACKOFF[0], 1.0)
        self.assertGreater(sb.BACKOFF[-1], sb.BACKOFF[0])

    def test_it_retries_and_then_succeeds(self):
        import io
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'[{"v":1}]'
        ok.__exit__.return_value = False
        got = self._call([_http_error(504), ok])
        self.assertEqual(got, [{"v": 1}])
        del io

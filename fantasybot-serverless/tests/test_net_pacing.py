"""Regression: the scraped site gets a pacing floor, the API does not.

Parallel workers fetching match pages and probable lineups hammered
futbolfantasy into rate-limiting us. One request at a time, spaced, keeps the
scrape welcome — while API calls, which are urgent, stay at full speed.
"""

import unittest
from unittest import mock

from fantasybot import net


class PacingIsOnlyForTheScrapedSite(unittest.TestCase):

    def setUp(self):
        net._last_hit.clear()
        self.addCleanup(net._last_hit.clear)

    def _waits_for(self, url, calls=2):
        """Seconds slept across `calls` consecutive fetches of the same host."""
        reloj = {"t": 100.0}
        dormido = []

        def fake_sleep(segundos):
            dormido.append(segundos)
            reloj["t"] += segundos

        with mock.patch.object(net.time, "monotonic", lambda: reloj["t"]), \
             mock.patch.object(net.time, "sleep", fake_sleep):
            for _ in range(calls):
                net._pace(url)
        return dormido

    def test_second_hit_on_the_scraped_site_waits(self):
        dormido = self._waits_for("https://www.futbolfantasy.com/partidos/x")
        self.assertEqual(len(dormido), 1)
        self.assertAlmostEqual(sum(dormido), net.THROTTLE_SECONDS, places=6)

    def test_the_api_is_never_slowed_down(self):
        # An urgent bid must not queue behind a scrape.
        self.assertEqual(
            self._waits_for("https://api.laligafantasymarca.com/stats/v1/x", calls=5),
            [])

    def test_subdomains_of_a_throttled_host_are_throttled_too(self):
        self.assertEqual(len(self._waits_for("https://cdn.futbolfantasy.com/a.png")), 1)


if __name__ == "__main__":
    unittest.main()

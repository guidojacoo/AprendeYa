"""Regression: a flip must not project appreciation the source says is already reversing.

A paying user saw Julián Álvarez suggested as a "+1% flip" while his value was falling.
Real futbolfantasy data: he rose all week (valor7 69.6M -> valor 71.2M) but PEAKED and
turned down today (valor1 71.29M > valor 71.22M; tendencia -1, negative acceleration).
The flip engine averaged only the 3d/7d windows, missed the fresh reversal, projected a
rise and recommended buying him at the top. The sell engine already trusts `tendencia`;
the flip engine must respect that same fresh signal.
"""

import os
import tempfile
import unittest
from unittest import mock

os.environ["FANTASYBOT_HOME"] = tempfile.mkdtemp(prefix="fb-fliptrend-")

from fantasybot.strategy import flip  # noqa: E402

# Real numbers pulled from futbolfantasy for Julián Álvarez (2026-08-19).
_JULIAN = {"valor": 71_224_797, "valor1": 71_293_084, "valor2": 70_814_658,
           "valor3": 70_546_928, "valor7": 69_606_904, "tendencia": -1,
           "aceleracion": -441_365}

# A genuine riser: up over every window, positive tendencia, up on the day too.
_RISER = {"valor": 30_000_000, "valor1": 29_900_000, "valor3": 29_000_000,
          "valor7": 28_000_000, "tendencia": 14}


class ProjectRespectsFreshTrend(unittest.TestCase):
    def test_falling_now_is_not_projected_up(self):
        # tendencia < 0 -> don't extrapolate the stale 3/7d rise.
        self.assertLessEqual(flip.project(_JULIAN, 7), _JULIAN["valor"])

    def test_last_day_drop_alone_gates_projection(self):
        # Even without a tendencia read, a value that fell over the last day must not be
        # projected upward.
        t = dict(_JULIAN)
        t["tendencia"] = None
        self.assertLessEqual(flip.project(t, 7), t["valor"])

    def test_genuine_riser_still_projected_up(self):
        # The gate must NOT neuter real opportunities: a clear riser still projects up.
        self.assertGreater(flip.project(_RISER, 7), _RISER["valor"])


class EvaluateDropsFallingFlip(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(flip, "match_name", lambda n, nm, i: self._trend)
        p.start()
        self.addCleanup(p.stop)

    def _element(self, value):
        return {"id": "m1", "discr": "marketPlayerLeague", "salePrice": value,
                "playerMaster": {"id": "pX", "nickname": "Julián Álvarez",
                                 "name": "Julián Álvarez", "marketValue": value,
                                 "positionId": 4}}

    def test_falling_player_is_not_a_positive_flip(self):
        self._trend = _JULIAN
        r = flip.evaluate(self._element(_JULIAN["valor"]), {}, horizon=7)
        self.assertIsNotNone(r)
        self.assertLessEqual(r["margin"], 0)          # buying at value can't turn a profit

    def test_rising_player_is_still_a_positive_flip(self):
        self._trend = _RISER
        r = flip.evaluate(self._element(_RISER["valor"]), {}, horizon=7)
        self.assertIsNotNone(r)
        self.assertGreater(r["margin"], 0)            # genuine flip survives


if __name__ == "__main__":
    unittest.main()

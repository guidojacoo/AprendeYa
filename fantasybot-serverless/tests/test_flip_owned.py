"""Regression: flip opportunities must exclude players you already own.

A paying user reported the agent suggesting he "sign" Aubameyang — a player already
in his squad (it showed up in his market, e.g. because he'd listed it). `opportunities`
now takes the owned ids and filters them out.
"""

import os
import tempfile
import unittest
from unittest import mock

os.environ["FANTASYBOT_HOME"] = tempfile.mkdtemp(prefix="fb-flip-")

from fantasybot.strategy import flip  # noqa: E402

_TREND = {"valor": 30_000_000, "valor3": 29_000_000, "valor7": 28_000_000, "tendencia": 14}


class _Client:
    def market(self, lid):
        return [{"id": "m1", "discr": "marketPlayerLeague",
                 "playerMaster": {"id": "pOWN", "nickname": "Aubameyang",
                                  "name": "Aubameyang", "marketValue": 30_000_000,
                                  "positionId": 4}}]


class FlipExcludesOwned(unittest.TestCase):
    def setUp(self):
        for target, val in [("trends_index", lambda *a, **k: {"aubameyang": _TREND}),
                            ("match_name", lambda n, nm, i: _TREND)]:
            p = mock.patch.object(flip, target, val)
            p.start()
            self.addCleanup(p.stop)

    def test_owned_player_is_not_recommended(self):
        self.assertEqual([o["nombre"] for o in flip.opportunities(_Client(), "L")],
                         ["Aubameyang"])                       # visible when not owned
        self.assertEqual(flip.opportunities(_Client(), "L", owned={"pOWN"}), [])  # filtered


if __name__ == "__main__":
    unittest.main()

"""Regression: the deterministic buyout-clause recommender must not suggest signing a
benchwarmer to fill a gap. A paying user was told to buy Juan Musso (Atlético's ~10%-to-
start backup keeper) — a keeper who won't play scores 0, so recommending his (multi-million)
buyout just wastes money. clause_targets now drops targets whose starting probability is
clearly low (unknown probability is kept, not penalised)."""

import os
import tempfile
import unittest
from unittest import mock

os.environ["FANTASYBOT_HOME"] = tempfile.mkdtemp(prefix="fb-clauseprob-")

from fantasybot import agent  # noqa: E402


def _musso_market():
    return [{"discr": "marketPlayerTeam", "id": "m1",
             "playerMaster": {"id": "pM", "nickname": "J. Musso", "name": "Juan Musso",
                              "positionId": 1},
             "playerTeam": {"buyoutClause": 7_000_000,
                            "buyoutClauseLockedEndTime": "2026-09-01T21:00:00+02:00"}}]


_TEAM = {"teamMoney": 100_000_000, "players": []}


class ClauseTargetsRespectStartingProbability(unittest.TestCase):
    def setUp(self):
        # Isolate the prob filter from gap detection: POR is a gap here.
        p = mock.patch.object(agent.needs_mod, "gaps", lambda t: ["POR"])
        p.start()
        self.addCleanup(p.stop)

    def test_benchwarmer_is_not_recommended(self):
        targets = agent.clause_targets(_musso_market(), _TEAM, {"juan musso": {"prob": 10}})
        self.assertEqual(targets, [])                       # 10% keeper dropped

    def test_probable_starter_is_recommended(self):
        targets = agent.clause_targets(_musso_market(), _TEAM, {"juan musso": {"prob": 90}})
        self.assertEqual(len(targets), 1)                   # a real starter survives
        self.assertEqual(targets[0]["prob"], 90)

    def test_unknown_probability_is_kept(self):
        # A name that doesn't match the probable-lineup index (prob None) must NOT be
        # dropped — we don't penalise a match miss.
        targets = agent.clause_targets(_musso_market(), _TEAM, {})
        self.assertEqual(len(targets), 1)
        self.assertIsNone(targets[0]["prob"])


if __name__ == "__main__":
    unittest.main()

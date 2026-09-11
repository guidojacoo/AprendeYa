"""Regression: when a rival's player is ON SALE, recommend the bid, not the clause.

A clause is a ~1.67x premium over value and stays locked for days; a sale listing is
open right now and starts at his value. The advisor used to offer only the clause
route for players owned by other managers — recommending a 4.5M buyout on a keeper
his owner had listed at 2.7M. When both routes exist, the cheaper one wins, and the
report still carries both prices so the caller can see the alternative.
"""

import os
import tempfile
import unittest
from unittest import mock

os.environ["FANTASYBOT_HOME"] = tempfile.mkdtemp(prefix="fb-cheaper-")

from fantasybot import agent  # noqa: E402
from fantasybot.strategy import needs  # noqa: E402


def _keeper(status=None, sale=None):
    el = {"discr": "marketPlayerTeam", "id": "m9",
          "playerMaster": {"id": "pK", "nickname": "Portero", "name": "Un Portero",
                           "positionId": 1, "marketValue": 2_700_000,
                           "playerStatus": "ok"},
          "playerTeam": {"buyoutClause": 4_500_000,
                         "buyoutClauseLockedEndTime": "2026-09-01T21:00:00+02:00"},
          "expirationDate": "2026-08-23T14:00:00+02:00"}
    if status:
        el["status"] = status
    if sale is not None:
        el["salePrice"] = sale
    return el


class _Client:
    def __init__(self, entries):
        self._entries = entries

    def market(self, league_id):
        return self._entries


_TEAM = {"teamMoney": 100_000_000, "players": []}


class CandidatesPreferTheCheaperRoute(unittest.TestCase):
    def test_on_sale_beats_the_clause(self):
        cands = needs.candidates(_Client([_keeper("on_sale", 2_700_000)]), "L",
                                 "POR", prob_index={}, money=100_000_000)
        self.assertEqual(cands[0]["via"], "PUJA")
        self.assertEqual(cands[0]["price"], 2_700_000)
        self.assertEqual(cands[0]["clause"], 4_500_000)  # the alternative, visible

    def test_not_on_sale_keeps_the_clause_route(self):
        cands = needs.candidates(_Client([_keeper()]), "L",
                                 "POR", prob_index={}, money=100_000_000)
        self.assertEqual(cands[0]["via"], "CLAUSULA")
        self.assertEqual(cands[0]["price"], 4_500_000)

    def test_urgency_raises_the_bid_cap_but_never_the_clause(self):
        client = _Client([_keeper("on_sale", 2_700_000)])
        with mock.patch.object(needs, "gaps", lambda t: ["POR"]), \
             mock.patch.object(needs, "probable_lineups", lambda: {}):
            report = needs.advise(client, "L", _TEAM, days_to_matchday=0.5)
        c = report["suggestions"]["POR"][0]
        self.assertGreater(c["max_bid"], c["price"])  # PUJA: urgency applies


class ClauseTargetsFlagTheCheaperRoute(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(agent.needs_mod, "gaps", lambda t: ["POR"])
        p.start()
        self.addCleanup(p.stop)

    def test_target_carries_the_saving(self):
        targets = agent.clause_targets([_keeper("on_sale", 2_700_000)], _TEAM, {})
        self.assertTrue(targets[0]["cheaper_via_bid"])
        self.assertEqual(targets[0]["saving_vs_clause"], 1_800_000)

    def test_no_sale_no_flag(self):
        targets = agent.clause_targets([_keeper()], _TEAM, {})
        self.assertFalse(targets[0]["cheaper_via_bid"])
        self.assertEqual(targets[0]["saving_vs_clause"], 0)


class ReviewFeedbackRegressions(unittest.TestCase):
    """The three review findings on this PR, frozen as tests."""

    def test_venta_alcanzable_con_clausula_impagable(self):
        # An affordable open sale must surface even when the clause is out of reach.
        el = _keeper("on_sale", 2_700_000)
        el["playerTeam"]["buyoutClause"] = 900_000_000   # nadie paga eso
        with mock.patch.object(agent.needs_mod, "gaps", lambda t: ["POR"]):
            targets = agent.clause_targets([el], _TEAM, {})
        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0]["cheaper_via_bid"])

    def test_add_task_refresca_texto_y_fecha_con_la_misma_clave(self):
        import os
        import tempfile
        from fantasybot import state
        old = state.TASKS_PATH
        state.TASKS_PATH = os.path.join(tempfile.mkdtemp(), "tasks.json")
        try:
            state.add_task("Buyout X for 4,500,000", due="2026-09-01", key="clause:x")
            t = state.add_task("Bid for X: ON SALE at 2,700,000",
                               due="2026-08-25", key="clause:x")
            self.assertEqual(t["text"], "Bid for X: ON SALE at 2,700,000")
            self.assertEqual(t["due"], "2026-08-25")
            self.assertEqual(len(state.load_tasks()), 1)   # misma tarea, no otra
        finally:
            state.TASKS_PATH = old


if __name__ == "__main__":
    unittest.main()

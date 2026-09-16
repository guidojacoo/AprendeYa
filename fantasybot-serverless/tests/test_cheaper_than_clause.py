"""A rival's player is signed by paying his clause, and by nothing else.

This file used to pin the opposite: when another manager had a player ON SALE,
prefer the bid, because a clause is a ~1.67x premium and the listing is open now
at his value. That was cheaper and it is no longer the rule — the owner of this
bot wants exactly two ways in, and bidding against another manager is not one of
them:

    LaLiga's own listings  ->  bid
    another manager's man  ->  his buyout clause

The cost is real and worth naming: a rival whose clause we cannot afford but
whose listing we could have won is now out of reach entirely. The sale price is
still read and carried, as context on the page, never as a plan.
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


class ARivalsPlayerIsAlwaysAClause(unittest.TestCase):
    def test_an_open_sale_does_not_become_a_bid(self):
        """The old rule, inverted. He is listed at 2.7M and his clause is 4.5M,
        and we pay the clause."""
        cands = needs.candidates(_Client([_keeper("on_sale", 2_700_000)]), "L",
                                 "POR", prob_index={}, money=100_000_000)
        self.assertEqual(cands[0]["via"], "CLAUSULA")
        self.assertEqual(cands[0]["price"], 4_500_000)

    def test_no_candidate_is_ever_offered_as_a_bid(self):
        for el in (_keeper("on_sale", 2_700_000), _keeper()):
            cands = needs.candidates(_Client([el]), "L", "POR",
                                     prob_index={}, money=100_000_000)
            self.assertNotEqual(cands[0]["via"], "PUJA")

    def test_not_on_sale_keeps_the_clause_route(self):
        cands = needs.candidates(_Client([_keeper()]), "L",
                                 "POR", prob_index={}, money=100_000_000)
        self.assertEqual(cands[0]["via"], "CLAUSULA")
        self.assertEqual(cands[0]["price"], 4_500_000)

    def test_urgency_never_raises_what_we_pay_for_a_clause(self):
        """A clause has one price. Being in a hurry does not change it."""
        client = _Client([_keeper("on_sale", 2_700_000)])
        with mock.patch.object(needs, "gaps", lambda t: ["POR"]), \
             mock.patch.object(needs, "probable_lineups", lambda: {}):
            report = needs.advise(client, "L", _TEAM, days_to_matchday=0.5)
        c = report["suggestions"]["POR"][0]
        self.assertEqual(c["via"], "CLAUSULA")
        self.assertEqual(c["max_bid"], c["price"])


class ClauseTargetsNeverDivertToABid(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(agent.needs_mod, "gaps", lambda t: ["POR"])
        p.start()
        self.addCleanup(p.stop)

    def test_a_listed_rival_is_still_a_clause_target(self):
        """The diversion mattered: `_plan_clauses` SKIPS anything flagged
        cheaper-via-bid, so with bidding on rivals removed a flagged player
        would have been signed by neither route."""
        targets = agent.clause_targets([_keeper("on_sale", 2_700_000)], _TEAM, {})
        self.assertEqual(len(targets), 1)
        self.assertFalse(targets[0]["cheaper_via_bid"])

    def test_the_sale_price_is_still_visible(self):
        targets = agent.clause_targets([_keeper("on_sale", 2_700_000)], _TEAM, {})
        self.assertEqual(targets[0]["on_sale_at"], 2_700_000)

    def test_no_sale_no_flag(self):
        targets = agent.clause_targets([_keeper()], _TEAM, {})
        self.assertFalse(targets[0]["cheaper_via_bid"])


class ReviewFeedbackRegressions(unittest.TestCase):
    """The three review findings on this PR, frozen as tests."""

    def test_an_unaffordable_clause_puts_him_out_of_reach(self):
        """The price of the rule, stated rather than hidden. His owner has him
        listed at 2.7M and we could win that listing — but his clause is 900M,
        and the clause is the only door we use."""
        el = _keeper("on_sale", 2_700_000)
        el["playerTeam"]["buyoutClause"] = 900_000_000
        with mock.patch.object(agent.needs_mod, "gaps", lambda t: ["POR"]):
            targets = agent.clause_targets([el], _TEAM, {})
        self.assertEqual(targets, [])

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

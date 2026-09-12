"""A market row the API did not promise.

`KeyError: 'discr'` killed the whole review, every time it ran: nothing was
listed, no reserve prices were written, and a squad standing on the market had
nobody reading its offers. Three places subscripted the field directly while the
rest of the codebase already read it defensively.
"""

import unittest
from unittest import mock

from fantasybot import agent
from fantasybot.strategy import flip, needs
from tests.support import StorageTestCase


TREND = {"valor": 5_000_000, "tendencia": 1, "valor1": 4_900_000,
         "valor3": 4_800_000, "valor7": 4_700_000}


def row(discr=None, **kw):
    el = {"id": "m1",
          "playerMaster": {"id": "p1", "nickname": "Uno", "name": "Uno",
                           "positionId": 4, "marketValue": 5_000_000,
                           "lastSeasonPoints": 100, "averagePoints": 5.0,
                           "points": 50},
          "salePrice": 4_800_000, "expirationDate": "2026-09-13T20:00:00+00:00"}
    if discr is not None:
        el["discr"] = discr
    el.update(kw)
    return el


class ARowWithNoDiscr(unittest.TestCase):
    def test_evaluating_it_does_not_raise(self):
        got = flip.evaluate(row(), {"uno": TREND}, horizon=3)
        self.assertIsNone(got, "unknown owner and no clause: nothing to act on")

    def test_a_system_listing_still_evaluates(self):
        got = flip.evaluate(row(discr="marketPlayerLeague"), {"uno": TREND},
                            horizon=3)
        self.assertIsNotNone(got)
        self.assertEqual(got["via"], "SISTEMA")

    def test_one_bad_row_does_not_lose_the_good_ones(self):
        """The failure that mattered: the review died on the first odd row and
        every opportunity behind it went unseen."""
        class _C:
            def market(self, lid):
                return [row(), row(discr="marketPlayerLeague")]
        with mock.patch.object(flip, "trends_index", return_value={"uno": TREND}):
            ops = flip.opportunities(_C(), "L1")
        self.assertEqual(len(ops), 1)

    def test_it_is_never_treated_as_a_clause_target(self):
        """Paying a clause is irreversible, so an unreadable owner is skipped
        rather than assumed."""
        team = {"teamMoney": 50_000_000, "players": []}
        with mock.patch.object(agent.needs_mod, "gaps", return_value=["DEL"]):
            targets = agent.clause_targets([row()], team, {})
        self.assertEqual(targets, [])

    def test_the_needs_advisor_survives_it_too(self):
        class _C:
            def market(self, lid):
                return [row(), row(discr="marketPlayerLeague")]
        got = needs.candidates(_C(), "L1", "DEL", prob_index={},
                               money=50_000_000)
        self.assertEqual([c["via"] for c in got], ["SISTEMA"],
                         "the readable listing survives, the odd one is dropped")


class AReviewThatRaisesIsNotADeadTick(StorageTestCase):
    """It used to take everything with it: the health note, the token check and
    the scheduler repair all skipped, and the tick recorded as a failure."""

    def test_the_tick_survives_and_says_what_broke(self):
        from fantasybot import tick

        with mock.patch.object(tick, "run_review",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(tick, "handle_offers", return_value={}), \
             mock.patch.object(tick, "run_llm_strategy", return_value={}):
            summary = tick.run(log=lambda m: None, source="test")

        # Contained but not quiet: the tick finishes its other work instead of
        # dying halfway through it, and still reports failure so the scheduler's
        # job goes red. A green light over a broken mechanism hides for days.
        self.assertFalse(summary["ok"])
        self.assertIn("boom", summary["error"])
        self.assertEqual(summary["review"]["status"], "error")
        self.assertIn("traceback", summary["review"])
        self.assertIn("pending", summary, "the rest of the tick still ran")

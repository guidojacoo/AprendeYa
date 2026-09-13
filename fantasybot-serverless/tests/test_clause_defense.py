"""Putting our best players out of reach — the move that decides leagues.

A rival does not have to outplay you. He pays the clause on your best forward
once, and it is a two-sided swing: you lose the points, he gains them. Any
manager paying attention raises clauses on his stars in week one. The bot never
did, which meant it was playing a strictly harder game than everyone else.
"""

from fantasybot import config
from fantasybot.strategy import clausedefense as cd
from tests.support import StorageTestCase


def _p(pid, value, clause, ptid=None, shielded=False):
    return {"playerTeamId": ptid or f"pt-{pid}", "isShielded": shielded,
            "buyoutClause": clause,
            "playerMaster": {"id": pid, "nickname": f"J{pid}",
                             "positionId": "3", "marketValue": value}}


class WhoIsActuallyExposed(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.team = {"teamMoney": 20_000_000, "players": [
            _p("1", 8_000_000, 9_000_000),     # reachable, our best
            _p("2", 3_000_000, 4_000_000),     # reachable, bench
            _p("3", 12_000_000, 40_000_000),   # already out of reach
        ]}
        self.risk = {"pt-1": 4.2, "pt-2": 0.1, "pt-3": 3.0}

    def test_a_clause_beyond_the_field_is_not_exposure(self):
        rows = cd.exposed(self.team, 10_000_000, self.risk)
        self.assertNotIn("J3", [r["nombre"] for r in rows],
                         "a 40M clause in a 10M league is already safe")

    def test_worst_loss_first(self):
        rows = cd.exposed(self.team, 10_000_000, self.risk)
        self.assertEqual(rows[0]["nombre"], "J1")

    def test_the_target_clears_the_richest_rival_with_a_margin(self):
        """Their cash moves. A clause that exactly matches today's reach is
        unprotected the moment somebody sells a player."""
        got = cd.target_for(9_000_000, 8_000_000, 10_000_000)
        self.assertEqual(got, round(10_000_000 * (1 + cd.SAFETY_MARGIN)))

    def test_an_unknown_field_spends_nothing(self):
        self.assertIsNone(cd.target_for(9_000_000, 8_000_000, 0))

    def test_the_target_is_never_under_the_player_value(self):
        got = cd.target_for(1_000, 30_000_000, 100_000)
        self.assertEqual(got, 30_000_000)


class WhatItSpends(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.team = {"players": [_p("1", 8_000_000, 9_000_000)]}
        self.risk = {"pt-1": 4.2}

    def test_with_no_measured_price_it_probes_instead_of_guessing(self):
        got = cd.plan(self.team, 10_000_000, 50_000_000, self.risk,
                      cost_ratio=None)
        self.assertEqual(got["mode"], "probe")
        self.assertEqual(len(got["raises"]), 1)

    def test_a_probe_is_small(self):
        got = cd.plan(self.team, 10_000_000, 50_000_000, self.risk)
        self.assertLessEqual(got["raises"][0]["cost"], cd.PROBE_BUDGET)

    def test_once_priced_it_defends_for_real(self):
        got = cd.plan(self.team, 10_000_000, 50_000_000, self.risk,
                      cost_ratio=0.1)
        self.assertEqual(got["mode"], "on")
        self.assertEqual(got["raises"][0]["nombre"], "J1")

    def test_it_will_not_defend_a_player_who_scores_nothing(self):
        """Losing a bench player costs about what replacing him costs."""
        got = cd.plan(self.team, 10_000_000, 50_000_000, {"pt-1": 0.1},
                      cost_ratio=0.1)
        self.assertEqual(got["raises"], [])

    def test_it_never_spends_more_than_its_share_of_the_bank(self):
        got = cd.plan(self.team, 10_000_000, 1_000_000, self.risk,
                      cost_ratio=1.0)
        self.assertEqual(got["raises"], [],
                         "defending a squad you can no longer improve is how "
                         "you finish fourth with everyone intact")


class LearningTheRealPrice(StorageTestCase):
    def test_it_reads_the_bill_off_the_account(self):
        """The endpoint returns the new clause, never what it charged."""
        self.assertEqual(
            cd.measure_ratio(50_000_000, 49_900_000, 9_000_000, 10_000_000),
            0.1)

    def test_a_raise_that_looks_free_is_refused(self):
        self.assertIsNone(
            cd.measure_ratio(50_000_000, 50_000_000, 9_000_000, 10_000_000))

    def test_an_impossible_ratio_is_refused(self):
        """A wrong ratio banked here silently mis-prices every later decision,
        so an unbelievable one is thrown away rather than trusted."""
        self.assertIsNone(
            cd.measure_ratio(50_000_000, 20_000_000, 9_000_000, 10_000_000))

    def test_a_clause_that_did_not_move_is_not_a_measurement(self):
        self.assertIsNone(
            cd.measure_ratio(50_000_000, 49_000_000, 9_000_000, 9_000_000))


class TheDefenceIsOnByDefault(StorageTestCase):
    def test_raising_clauses_is_enabled(self):
        self.assertTrue(config.AUTO_RAISE_CLAUSE)

    def test_paying_clauses_is_enabled(self):
        """The strongest move on the board was switched off."""
        self.assertTrue(config.AUTO_CLAUSES)

    def test_the_free_defensive_move_is_enabled(self):
        self.assertTrue(config.AUTO_SHIELD)

    def test_one_player_can_never_take_the_whole_bank(self):
        self.assertTrue(0 < config.MAX_CLAUSE_SHARE < 1)

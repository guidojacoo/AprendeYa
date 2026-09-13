"""Putting our best players out of reach — the move that decides leagues.

A rival does not have to outplay you. He pays the clause on your best forward
once, and it is a two-sided swing: you lose the points, he gains them. Any
manager paying attention raises clauses on his stars in week one. The bot never
did, which meant it was playing a strictly harder game than everyone else.
"""

from fantasybot import config
from fantasybot.storage import utcnow
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


class TheEndpointActsOnARosterSlot(StorageTestCase):
    """LaLiga keys every write about OUR OWN player on the playerTeamId, not on
    the playerMaster id: sell_player does, shield_player does, and this one does
    too. Given the footballer's id it answers 404 Not Found, which is what it
    did the first time it ran live."""

    def test_the_raise_is_sent_with_the_player_team_id(self):
        from fantasybot import config, tick
        from fantasybot.scheduler import TickContext

        sent = []

        class _C:
            def default_ids(self):
                return "L1", "T1"

            def team(self, lid, tid):
                return {"teamMoney": 50_000_000, "players": [
                    {"playerTeamId": "pt-99", "buyoutClause": 9_000_000,
                     "playerMaster": {"id": "2533", "nickname": "Uno",
                                      "marketValue": 8_000_000}}]}

            def increase_buyout_clause(self, lid, pid, amount):
                sent.append(pid)
                return {"ok": True}

        flags = (config.AUTO_RAISE_CLAUSE, config.AUTO_EXECUTE)
        config.AUTO_RAISE_CLAUSE = config.AUTO_EXECUTE = True
        self.addCleanup(lambda: setattr(config, "AUTO_RAISE_CLAUSE", flags[0]))
        self.addCleanup(lambda: setattr(config, "AUTO_EXECUTE", flags[1]))

        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        ctx.get_client = lambda: _C()
        tick._execute_raise_clause(ctx, {"payload": {
            "league_id": "L1", "player_id": "2533", "player_team_id": "pt-99",
            "nombre": "Uno", "target": 13_500_000, "clause": 9_000_000}})
        self.assertEqual(sent, ["pt-99"],
                         "sent with the footballer's id it answers 404")


class APermanentRefusalIsNotRetried(StorageTestCase):
    """A 404 is the same answer three times in six seconds: three identical
    error events, three notifications, no new information."""

    def test_a_404_fails_the_action_immediately(self):
        from fantasybot import scheduler
        from fantasybot.api import FantasyError
        from fantasybot.scheduler import TickContext
        from fantasybot.storage import FAILED, get_storage

        @scheduler.executor("refused")
        def _refused(ctx, action):
            raise FantasyError("POST /increase -> 404: Not Found", status=404)

        scheduler.schedule("refused", {}, execute_at=utcnow(),
                           idempotency_key="refused:1")
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        store = get_storage()
        res = [scheduler._run_one(store, a, ctx, utcnow(), lambda m: None)
               for a in store.due_actions()][0]
        self.assertEqual(res["status"], FAILED)
        self.assertTrue(res["permanent"])
        self.assertEqual(res["failures"], 1)

    def test_a_503_still_gets_another_try(self):
        from fantasybot import scheduler
        from fantasybot.api import FantasyError
        from fantasybot.scheduler import TickContext
        from fantasybot.storage import PENDING, get_storage

        @scheduler.executor("flaky")
        def _flaky(ctx, action):
            raise FantasyError("GET /market -> 503: upstream", status=503)

        scheduler.schedule("flaky", {}, execute_at=utcnow(),
                           idempotency_key="flaky:1")
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        store = get_storage()
        res = [scheduler._run_one(store, a, ctx, utcnow(), lambda m: None)
               for a in store.due_actions()][0]
        self.assertEqual(res["status"], PENDING)
        self.assertFalse(res["permanent"])

    def test_rate_limiting_is_worth_waiting_out(self):
        from fantasybot.api import FantasyError
        self.assertFalse(FantasyError("slow down", status=429).permanent)
        self.assertFalse(FantasyError("timeout", status=408).permanent)
        self.assertTrue(FantasyError("nope", status=400).permanent)
        self.assertFalse(FantasyError("no status at all").permanent)

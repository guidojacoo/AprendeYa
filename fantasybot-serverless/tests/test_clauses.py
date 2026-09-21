"""Paying a rival's buyout clause — the bot's only irreversible spend.

Planning happens up to a day before the clause unlocks, and the world moves in
between: the player can be bought by someone else, his clause can rise, our
balance can fall because we won a bid. So every assumption is re-checked against
the live API at the moment of payment, and anything that no longer holds means we
stand down.

A skipped clause costs nothing. An over-paid one cannot be undone.
"""

from datetime import timedelta
from unittest import mock

from fantasybot import config, scheduler, tick
from fantasybot.scheduler import TickContext
from fantasybot.storage import to_iso, utcnow
from tests.support import StorageTestCase


class ClauseClient:
    def __init__(self, money=50_000_000, clause=20_000_000, unlock=None,
                 owned=(), on_market=True):
        self.money, self.clause, self.unlock = money, clause, unlock
        self.owned, self.on_market = list(owned), on_market
        self.paid = []

    def default_ids(self):
        return "L1", "T1"

    def team(self, lid, tid):
        return {"teamMoney": self.money,
                "players": [{"playerMaster": {"id": pid}} for pid in self.owned]}

    def market(self, lid):
        if not self.on_market:
            return []
        return [{"id": "mkt1", "discr": "marketPlayerTeam",
                 "playerMaster": {"id": "p9", "nickname": "Objetivo"},
                 "playerTeam": {"buyoutClause": self.clause,
                                "buyoutClauseLockedEndTime": self.unlock}}]

    def pay_buyout_clause(self, lid, player_id, amount):
        self.paid.append({"player_id": player_id, "amount": amount})
        return {"ok": True}


class Paying(StorageTestCase):
    def setUp(self):
        super().setUp()
        self._flags = (config.AUTO_CLAUSES, config.AUTO_EXECUTE,
                       config.CASH_RESERVE, config.MAX_CLAUSE)
        config.AUTO_CLAUSES = config.AUTO_EXECUTE = True
        config.CASH_RESERVE = config.MAX_CLAUSE = 0
        self.addCleanup(self._restore)

    def _restore(self):
        (config.AUTO_CLAUSES, config.AUTO_EXECUTE,
         config.CASH_RESERVE, config.MAX_CLAUSE) = self._flags

    def test_the_share_is_re_checked_when_the_money_actually_moves(self):
        """The balance moves between the plan and the payment — we win a bid, a
        clause is paid — and this is the last point at which refusing is free."""
        client = ClauseClient(money=12_000_000, clause=10_000_000)
        res = self._run(client, max_pay=11_000_000)
        self.assertEqual(res["status"], "too_expensive")
        self.assertEqual(client.paid, [],
                         "10M of a 12M balance leaves nothing to play with")

    def _run(self, client, max_pay=22_000_000):
        unlock = utcnow() - timedelta(seconds=5)
        scheduler.schedule(
            scheduler.CLAUSE,
            {"league_id": "L1", "player_id": "p9", "nombre": "Objetivo",
             "planned_clause": 20_000_000, "max_pay": max_pay},
            execute_at=unlock, idempotency_key="clause:L1:p9:x")
        ctx = TickContext(client=client, budget_seconds=20, log=lambda m: None)
        return scheduler.run_due(ctx)[0]["result"]

    def test_it_pays_when_everything_still_holds(self):
        c = ClauseClient()
        self.assertEqual(self._run(c)["status"], "paid")
        self.assertEqual(c.paid, [{"player_id": "p9", "amount": 20_000_000}])

    def test_it_pays_the_CURRENT_clause_not_the_planned_one(self):
        """His value drifts; the number that matters is the one on the API now."""
        c = ClauseClient(clause=20_500_000)
        self.assertEqual(self._run(c)["status"], "paid")
        self.assertEqual(c.paid[0]["amount"], 20_500_000)

    def test_it_stands_down_if_we_already_own_him(self):
        c = ClauseClient(owned=["p9"])
        res = self._run(c)
        self.assertEqual(res["status"], "already_owned")
        self.assertEqual(c.paid, [])

    def test_it_refuses_a_clause_that_rose_past_the_cap(self):
        c = ClauseClient(clause=30_000_000)
        res = self._run(c)
        self.assertEqual(res["status"], "too_expensive")
        self.assertEqual(c.paid, [])

    def test_it_stands_down_if_he_left_the_market(self):
        c = ClauseClient(on_market=False)
        self.assertEqual(self._run(c)["status"], "gone")
        self.assertEqual(c.paid, [])

    def test_a_still_locked_clause_is_retried_not_failed(self):
        c = ClauseClient(unlock=to_iso(utcnow() + timedelta(minutes=30)))
        res = self._run(c)
        self.assertEqual(res["status"], "locked")
        self.assertTrue(res["retry"])
        self.assertEqual(c.paid, [])
        self.assertEqual(len(self.store.pending_actions()), 1,
                         "it must stay queued for when the window opens")

    def test_the_cash_reserve_is_never_breached(self):
        config.CASH_RESERVE = 35_000_000
        c = ClauseClient(money=50_000_000, clause=20_000_000)
        res = self._run(c)
        self.assertEqual(res["status"], "insufficient_funds")
        self.assertEqual(c.paid, [])

    def test_autonomy_off_stops_the_payment(self):
        config.AUTO_CLAUSES = False
        c = ClauseClient()
        self.assertEqual(self._run(c)["status"], "skipped")
        self.assertEqual(c.paid, [])

    def test_dry_run_stops_the_payment(self):
        c = ClauseClient()
        scheduler.schedule(
            scheduler.CLAUSE,
            {"league_id": "L1", "player_id": "p9", "nombre": "X",
             "max_pay": 99_000_000},
            execute_at=utcnow() - timedelta(seconds=1),
            idempotency_key="clause:L1:p9:dry")
        ctx = TickContext(client=c, budget_seconds=20, dry_run=True,
                          log=lambda m: None)
        scheduler.run_due(ctx)
        self.assertEqual(c.paid, [])


class Planning(StorageTestCase):
    def setUp(self):
        super().setUp()
        self._flags = (config.AUTO_CLAUSES, config.AUTO_EXECUTE,
                       config.CASH_RESERVE, config.MAX_CLAUSE)
        config.AUTO_CLAUSES = config.AUTO_EXECUTE = True
        config.CASH_RESERVE = config.MAX_CLAUSE = 0
        self.addCleanup(self._restore)

    def _restore(self):
        (config.AUTO_CLAUSES, config.AUTO_EXECUTE,
         config.CASH_RESERVE, config.MAX_CLAUSE) = self._flags

    def _plan(self, target, money=50_000_000):
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        return tick._plan_clauses(ctx, "L1", {"teamMoney": money},
                                  {"clause_targets": [target]})

    def _target(self, **kw):
        base = {"player_id": "p1", "nombre": "Uno", "pos": "DEL",
                "clause": 10_000_000,
                "unlock": to_iso(utcnow() + timedelta(hours=4))}
        base.update(kw)
        return base

    def test_a_worthwhile_target_is_queued_at_its_unlock(self):
        res = self._plan(self._target())
        self.assertEqual(len(res["queued"]), 1)
        queued = self.store.pending_actions()[0]
        self.assertEqual(queued["type"], scheduler.CLAUSE)
        self.assertEqual(queued["payload"]["max_pay"], 11_000_000,
                         "a 10% allowance for a routine value bump")

    def test_a_target_cheaper_to_bid_for_is_left_alone(self):
        """Paying a ~1.67x clause premium for someone already listed at his value
        is burning money."""
        res = self._plan(self._target(cheaper_via_bid=True))
        self.assertEqual(res["queued"], [])
        self.assertIn("cheaper to bid", res["skipped"][0]["why"])

    def test_a_clause_beyond_the_balance_is_skipped(self):
        res = self._plan(self._target(clause=80_000_000))
        self.assertEqual(res["queued"], [])
        self.assertIn("beyond", res["skipped"][0]["why"])

    def test_the_reserve_shrinks_what_we_will_commit_to(self):
        config.CASH_RESERVE = 45_000_000
        res = self._plan(self._target(clause=10_000_000), money=50_000_000)
        self.assertEqual(res["queued"], [])

    def test_max_clause_caps_a_single_payment(self):
        config.MAX_CLAUSE = 5_000_000
        self.assertEqual(self._plan(self._target())["queued"], [])

    def test_the_allowance_never_exceeds_what_we_can_afford(self):
        """The SHARE rule, isolated from the floor.

        Both fences apply to a real plan; this one is about the share, so the
        floor is pinned out of the way rather than silently deciding the
        outcome. Before the floor existed it was zero and did so invisibly.
        """
        with mock.patch.object(tick.modes, "cash_floor", lambda: 0):
            self._plan(self._target(clause=10_000_000), money=17_000_000)
        self.assertEqual(self.store.pending_actions()[0]["payload"]["max_pay"],
                         int(17_000_000 * config.MAX_CLAUSE_SHARE))

    def test_the_floor_is_never_spent_through(self):
        """The fence that actually bounds a SEQUENCE of clauses.

        `clause_share` is a share of what is LEFT, so it cannot bound one: at
        60% each payment leaves 40%, and four of them take eighty million to
        two. That is what drained the account, and only an absolute number
        stops it.
        """
        with mock.patch.object(tick.modes, "cash_floor", lambda: 10_000_000):
            res = self._plan(self._target(clause=9_000_000), money=12_000_000)
        self.assertEqual(res["queued"], [],
                         "9M would leave 3M, under the 10M floor")

    def test_one_player_cannot_take_most_of_the_bank(self):
        """A euro ceiling set in August is meaningless by November, so the real
        limit is a share. Paying 96% of the balance for one player does not buy
        a player — it costs the next two, because nothing is left to answer
        with."""
        res = self._plan(self._target(clause=10_000_000), money=10_400_000)
        self.assertEqual(res["queued"], [])

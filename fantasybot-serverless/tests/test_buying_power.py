"""It had money and scheduled nothing.

"Tiene saldo y no programa pujas ni clausulazos."

The live review that answered why, verbatim from its funnel:

    anuncios de LaLiga: 11 · no me alcanza: 7 · suman muy poco: 4
    valen la pena: 0 · mejor gana: 4.36 · modo: equilibrio

with 1,260,777 € in the bank and a squad worth 241,287,572 €. Two rules made
every one of those eleven unaffordable:

  * a ten-million cash floor, so the spendable balance was zero;
  * and only cash counted — while LaLiga lets a squad bid up to its cash plus
    20% of its value, about 48M here.

The credit is real and so is its catch — a gameweek that starts negative scores
zero — so it is fenced: only what the bench can repay, only when two of
LaLiga's daily offers fit before the next first kick-off, and only once an
offer has actually been seen arriving.
"""

import unittest
from datetime import timedelta

from fantasybot import config, modes, scheduler, tick
from fantasybot.scheduler import TickContext
from fantasybot.storage import to_iso, utcnow
from fantasybot.strategy import finance
from tests.support import StorageTestCase

CASH, SQUAD = 1_260_777, 241_287_572


class ThePower(unittest.TestCase):
    def _power(self, **kw):
        args = {"cash": CASH, "team_value": SQUAD, "liquid": 60_000_000,
                "credit_use": 0.5, "offers_proven": True,
                "gameweek_start": to_iso(utcnow() + timedelta(days=4))}
        args.update(kw)
        return finance.buying_power(args.pop("cash"), args.pop("team_value"),
                                    args.pop("liquid"), **args)

    def test_the_live_case_can_bid(self):
        p = self._power()
        self.assertEqual(p["credit_line"], round(SQUAD * 0.20))
        self.assertEqual(p["credit"], round(SQUAD * 0.20 * 0.5))
        self.assertGreater(p["spend_total"], 20_000_000)
        self.assertEqual(p["spend_cash"], CASH, "no floor eats the cash any more")

    def test_no_credit_until_an_offer_has_been_seen(self):
        p = self._power(offers_proven=False)
        self.assertEqual(p["credit"], 0)
        self.assertIn("oferta", p["why_no_credit"])

    def test_no_credit_without_a_clock(self):
        self.assertEqual(self._power(gameweek_start=None)["credit"], 0)

    def test_the_bench_bounds_the_debt(self):
        p = self._power(liquid=10_000_000, credit_use=1.0)
        self.assertEqual(p["credit"], 8_500_000)

    def test_money_already_owed_comes_off_what_can_be_repaid(self):
        p = self._power(cash=-5_000_000, liquid=10_000_000, credit_use=1.0)
        self.assertEqual(p["credit"], 3_500_000)
        self.assertEqual(p["spend_cash"], 0)

    def test_promised_money_is_not_spent_twice(self):
        p = self._power(committed=10_000_000)
        self.assertEqual(p["committed"], 10_000_000)
        self.assertEqual(p["spend_total"],
                         CASH + round(SQUAD * 0.20 * 0.5) - 10_000_000)

    def test_credit_only_where_it_can_be_repaid_in_time(self):
        start = utcnow() + timedelta(days=3)
        ok = lambda close: finance.credit_ok_for(to_iso(close), to_iso(start))  # noqa: E731
        self.assertTrue(ok(start - timedelta(hours=60)))
        self.assertFalse(ok(start - timedelta(hours=20)),
                         "one daily offer is not enough margin")
        self.assertTrue(ok(start + timedelta(hours=2)),
                        "negative during a gameweek is harmless")
        self.assertFalse(finance.credit_ok_for(None, to_iso(start)))


def _upgrade(mid, price, gain, closes):
    return {"via": tick.SYSTEM_LISTING, "market_id": mid, "nombre": mid,
            "player_id": f"p-{mid}", "buy_price": price, "gain": gain,
            "gain_per_million": gain / (price / 1e6),
            "gain_per_million_neto": gain / (price / 1e6),
            "expires_at": to_iso(closes), "margin_pct": 0}


class _EmptyMarket:
    """For the margin-ordered fallback, which reads the market itself."""

    def market(self, lid):
        return []


class ThePlannerBidsWithIt(StorageTestCase):
    def setUp(self):
        super().setUp()
        saved = (config.AUTO_EXECUTE, config.AUTO_BIDS, config.AUTO_CREDIT)
        config.AUTO_EXECUTE = config.AUTO_BIDS = config.AUTO_CREDIT = True
        self.addCleanup(lambda: (setattr(config, "AUTO_EXECUTE", saved[0]),
                                 setattr(config, "AUTO_BIDS", saved[1]),
                                 setattr(config, "AUTO_CREDIT", saved[2])))
        modes.forget()
        self.addCleanup(modes.forget)
        self.start = utcnow() + timedelta(days=5)
        self.store.put_doc("gameweek_start", {"at": to_iso(self.start)})
        self.store.put_doc("sale_floors", {
            f"b{i}": {"value": 8_000_000, "xi": False} for i in range(8)})
        self.team = {"teamMoney": CASH, "teamValue": SQUAD, "players": []}

    def _plan(self, upgrades):
        ctx = TickContext(budget_seconds=30, log=lambda m: None)
        report = {"upgrades": upgrades, "flips": []}
        power = tick._buying_power(self.team)
        return tick._plan_bids(ctx, _EmptyMarket(), "L1", self.team, report,
                               power=power)

    def test_the_live_market_gets_a_bid(self):
        self.store.put_doc("offers_seen", {"at": to_iso(utcnow())})
        res = self._plan([_upgrade("m1", 20_000_000, 4.36,
                                   utcnow() + timedelta(days=1))])
        self.assertEqual(len(res["scheduled"]), 1, res["funnel"])
        self.assertEqual(res["funnel"]["no me alcanza"], 0)
        self.assertLessEqual(res["scheduled"][0]["ceiling"],
                             res["power"]["spend_total"])

    def test_without_proof_of_selling_only_cash_counts(self):
        res = self._plan([_upgrade("m1", 20_000_000, 4.36,
                                   utcnow() + timedelta(days=1))])
        self.assertEqual(res["scheduled"], [])
        self.assertEqual(res["funnel"]["no me alcanza"], 1)
        self.assertTrue(res["funnel"]["sin crédito porque"])

    def test_an_auction_too_close_to_the_gameweek_needs_cash(self):
        self.store.put_doc("offers_seen", {"at": to_iso(utcnow())})
        res = self._plan([_upgrade("m1", 20_000_000, 4.36,
                                   self.start - timedelta(hours=10))])
        self.assertEqual(res["scheduled"], [])

    def test_a_listing_already_bid_on_is_not_planned_again(self):
        self.store.put_doc("offers_seen", {"at": to_iso(utcnow())})
        closes = utcnow() + timedelta(days=1)
        first = self._plan([_upgrade("m1", 20_000_000, 4.36, closes)])
        self.assertEqual(len(first["scheduled"]), 1)
        again = self._plan([_upgrade("m1", 20_000_000, 4.36, closes)])
        self.assertEqual(again["scheduled"], [])
        self.assertEqual(again["power"]["committed"], 20_000_000)


class TheClauseWindow(unittest.TestCase):
    def test_shut_for_the_24_hours_before_a_gameweek(self):
        start = utcnow() + timedelta(hours=10)
        is_open, reopens = tick.clause_window(to_iso(start))
        self.assertFalse(is_open)
        self.assertEqual(reopens, start)

    def test_open_otherwise_and_open_without_a_clock(self):
        self.assertTrue(tick.clause_window(
            to_iso(utcnow() + timedelta(hours=30)))[0])
        self.assertTrue(tick.clause_window(None)[0])


class _RivalClient:
    """A league of us plus one rival whose squad carries clauses."""

    def __init__(self, clause=5_000_000, lock=None, shielded_until=None,
                 money=50_000_000):
        self.clause, self.lock, self.shield = clause, lock, shielded_until
        self.money, self.paid, self.calls = money, [], 0

    def default_ids(self):
        return "L1", "T1"

    def league_teams(self, lid):
        self.calls += 1
        return [{"id": "T1", "teamMoney": self.money},
                {"id": "R1", "manager": {"managerName": "rival"}}]

    def team(self, lid, tid):
        self.calls += 1
        if tid == "T1":
            return {"teamMoney": self.money, "players": []}
        return {"players": [{
            "playerTeamId": "slot-77", "buyoutClause": self.clause,
            "buyoutClauseLockedEndTime": self.lock,
            "isShielded": bool(self.shield), "shieldedEndDate": self.shield,
            "playerMaster": {"id": "p77", "nickname": "Crack", "positionId": 4,
                             "marketValue": 4_000_000, "averagePoints": 7}}]}

    def pay_buyout_clause(self, lid, slot, amount):
        self.paid.append((slot, amount))
        return {"ok": True}


class ClausulazosOnRivalSquads(StorageTestCase):
    def setUp(self):
        super().setUp()
        saved = (config.AUTO_CLAUSES, config.AUTO_EXECUTE)
        config.AUTO_CLAUSES = config.AUTO_EXECUTE = True
        self.addCleanup(lambda: (setattr(config, "AUTO_CLAUSES", saved[0]),
                                 setattr(config, "AUTO_EXECUTE", saved[1])))

    def test_every_rival_squad_is_a_source_of_targets_and_it_is_cached(self):
        from fantasybot.strategy import raids
        c = _RivalClient()
        teams = raids.fetch_rival_squads(c, "L1", "T1")
        self.assertEqual([t["team_id"] for t in teams], ["R1"])
        calls = c.calls
        raids.fetch_rival_squads(c, "L1", "T1")
        self.assertEqual(c.calls, calls, "a second read inside the TTL is free")
        got = raids.candidates(teams, owned_ids=set())
        self.assertEqual(got[0]["player_team_id"], "slot-77")
        self.assertEqual(got[0]["owner_team_id"], "R1")
        self.assertEqual(raids.candidates(teams, owned_ids={"p77"}), [])
        self.assertEqual(raids.candidates(teams, set(), max_clause=1), [])

    def test_a_shield_or_a_lock_pushes_the_payable_instant(self):
        from fantasybot.strategy import raids
        later = utcnow() + timedelta(hours=20)
        row = {"buyoutClauseLockedEndTime": to_iso(utcnow() - timedelta(days=1)),
               "isShielded": True, "shieldedEndDate": to_iso(later)}
        self.assertEqual(raids.payable_from(row), later)

    def _queue(self, owner="R1"):
        scheduler.schedule(
            scheduler.CLAUSE,
            {"league_id": "L1", "player_id": "p77", "player_team_id": "slot-77",
             "owner_team_id": owner, "nombre": "Crack",
             "planned_clause": 5_000_000, "max_pay": 5_500_000},
            execute_at=utcnow() - timedelta(seconds=1),
            idempotency_key="clause:L1:p77:x")

    def _run(self, client):
        ctx = TickContext(client=client, budget_seconds=20, log=lambda m: None)
        return scheduler.run_due(ctx)[0]["result"]

    def test_it_pays_on_the_slot_read_from_the_owners_squad(self):
        c = _RivalClient()
        self._queue()
        self.assertEqual(self._run(c)["status"], "paid")
        self.assertEqual(c.paid, [("slot-77", 5_000_000)],
                         "the slot id, never the footballer's")

    def test_a_shielded_player_is_waited_for(self):
        c = _RivalClient(shielded_until=to_iso(utcnow() + timedelta(hours=5)))
        self._queue()
        res = self._run(c)
        self.assertEqual(res["status"], "shielded")
        self.assertTrue(res["retry"])
        self.assertEqual(c.paid, [])

    def test_the_window_is_respected_without_a_single_request(self):
        self.store.put_doc("gameweek_start",
                           {"at": to_iso(utcnow() + timedelta(hours=5))})
        c = _RivalClient()
        self._queue()
        res = self._run(c)
        self.assertEqual(res["status"], "window_closed")
        self.assertEqual(c.calls, 0)

    def test_laligas_window_refusal_is_a_wait_not_a_failure(self):
        class Refusing(_RivalClient):
            def pay_buyout_clause(self, lid, slot, amount):
                raise RuntimeError('400 {"errorCode":"030.01.17"}')
        self._queue()
        res = self._run(Refusing())
        self.assertEqual(res["status"], "window_closed")
        self.assertTrue(res["retry"])

    def test_the_planner_lands_a_payment_after_the_window(self):
        start = utcnow() + timedelta(hours=6)
        self.store.put_doc("gameweek_start", {"at": to_iso(start)})
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        team = {"teamMoney": 50_000_000, "players": []}
        report = {"clause_targets": [{
            "player_id": "p77", "player_team_id": "slot-77",
            "owner_team_id": "R1", "owner": "rival", "nombre": "Crack",
            "pos": "DEL", "clause": 5_000_000, "gain": 2.0,
            "unlock": to_iso(utcnow() - timedelta(days=1))}]}
        res = tick._plan_clauses(ctx, "L1", team, report)
        self.assertEqual(len(res["queued"]), 1, res)
        action = self.store.pending_actions()[0]
        self.assertGreaterEqual(action["execute_at"], to_iso(start))
        self.assertEqual(action["payload"]["player_team_id"], "slot-77")
        self.assertEqual(action["payload"]["owner_team_id"], "R1")


class TheDailyReward(StorageTestCase):
    class _C:
        def __init__(self, redeemed=0, refuse=False):
            self.redeemed, self.refuse = redeemed, refuse
            self.money, self.claims, self.checks = 1_000_000, 0, 0

        def default_ids(self):
            return "L1", "T1"

        def check_daily_reward(self, lid, tid):
            self.checks += 1
            if self.refuse:
                raise RuntimeError('400 {"errorCode":"050.01.04"}')
            return {"teamId": 1, "dailyRewardsRedeemed": self.redeemed}

        def claim_daily_reward(self, lid, tid):
            self.claims += 1
            self.money += 100_000

        def team_money(self, tid):
            return {"teamMoney": self.money}

    def setUp(self):
        super().setUp()
        saved = (config.AUTO_DAILY_REWARD, config.AUTO_EXECUTE)
        config.AUTO_DAILY_REWARD = config.AUTO_EXECUTE = True
        self.addCleanup(lambda: (setattr(config, "AUTO_DAILY_REWARD", saved[0]),
                                 setattr(config, "AUTO_EXECUTE", saved[1])))

    def _ctx(self, c):
        return TickContext(client=c, budget_seconds=20, log=lambda m: None)

    def test_it_claims_once_a_day(self):
        c = self._C()
        got = tick.claim_daily_reward(self._ctx(c))
        self.assertEqual(got, {"status": "claimed", "amount": 100_000})
        self.assertIsNone(tick.claim_daily_reward(self._ctx(c)))
        self.assertEqual((c.claims, c.checks), (1, 1),
                         "the rest of the day costs no requests")

    def test_already_taken_is_stamped_not_retried(self):
        c = self._C(refuse=True)
        self.assertEqual(tick.claim_daily_reward(self._ctx(c))["status"],
                         "already_claimed")
        self.assertIsNone(tick.claim_daily_reward(self._ctx(c)))
        self.assertEqual(c.checks, 1)


class RaisingAClauseSendsWhatToPay(StorageTestCase):
    def test_half_of_the_rise_is_what_is_paid(self):
        sent = []

        class _C:
            CLAUSE_FACTOR = 2

            def default_ids(self):
                return "L1", "T1"

            def team(self, lid, tid):
                return {"teamMoney": 50_000_000, "players": [
                    {"playerTeamId": "pt-9", "buyoutClause": 9_000_000,
                     "playerMaster": {"id": "p9", "marketValue": 8_000_000}}]}

            def increase_buyout_clause(self, lid, ptid, amount):
                sent.append((ptid, amount))

        saved = (config.AUTO_RAISE_CLAUSE, config.AUTO_EXECUTE)
        config.AUTO_RAISE_CLAUSE = config.AUTO_EXECUTE = True
        self.addCleanup(lambda: (setattr(config, "AUTO_RAISE_CLAUSE", saved[0]),
                                 setattr(config, "AUTO_EXECUTE", saved[1])))
        ctx = TickContext(client=_C(), budget_seconds=20, log=lambda m: None)
        tick._execute_raise_clause(ctx, {"payload": {
            "league_id": "L1", "player_id": "p9", "player_team_id": "pt-9",
            "nombre": "Nueve", "target": 13_000_001}})
        self.assertEqual(sent, [("pt-9", 2_000_001)],
                         "pay X and the clause rises by 2X; rounded up")


if __name__ == "__main__":
    unittest.main()


class SellingToPayAClause(StorageTestCase):
    """A clause needs cash in hand — LaLiga lends nothing for one. When the best
    target is out of the bank's reach, the bench players who score least are
    the ones whose floor drops until the money is there."""

    def setUp(self):
        super().setUp()
        saved = (config.AUTO_CLAUSES, config.AUTO_EXECUTE)
        config.AUTO_CLAUSES = config.AUTO_EXECUTE = True
        self.addCleanup(lambda: (setattr(config, "AUTO_CLAUSES", saved[0]),
                                 setattr(config, "AUTO_EXECUTE", saved[1])))
        modes.forget()
        self.addCleanup(modes.forget)

    def _team(self):
        def p(i, value):
            return {"playerTeamId": f"pt{i}",
                    "playerMaster": {"id": f"p{i}", "marketValue": value}}
        return {"teamMoney": 1_000_000,
                "players": [p(1, 30_000_000), p(2, 9_000_000), p(3, 8_000_000),
                            p(4, 2_000_000)]}

    def test_the_least_useful_bench_funds_the_best_target(self):
        report = {"clause_targets": [{"player_id": "x", "clause": 10_000_000,
                                      "gain": 2.0, "gain_per_million": 0.2}]}
        best = {"payload": {"goalkeeper": "pt1", "defender": [],
                            "midfield": [], "striker": []}}
        got = tick._clause_funding(self._team(), report, best,
                                   expected={"pt2": 0.1, "pt3": 0.5, "pt4": 0.0})
        self.assertNotIn("p1", got, "never the eleven")
        self.assertIn("p4", got, "the one who scores nothing goes first")
        self.assertTrue({"p2", "p3"} <= got,
                        "until the clause clears the share fence too")

    def test_a_target_that_adds_little_funds_nothing(self):
        report = {"clause_targets": [{"player_id": "x", "clause": 10_000_000,
                                      "gain": 0.1}]}
        self.assertEqual(tick._clause_funding(self._team(), report), set())


class OneRowPerTarget(unittest.TestCase):
    def test_the_squad_read_wins_and_the_market_adds_its_sale(self):
        from fantasybot import agent
        merged = agent._merge_targets(
            [{"player_id": "p1", "clause": 1, "market_id": "m1",
              "sale_price": 900, "gain_per_million": 0.1}],
            [{"player_id": "p1", "clause": 2, "player_team_id": "s1",
              "gain_per_million": 0.1},
             {"player_id": "p2", "clause": 3, "gain_per_million": 0.9}])
        self.assertEqual([t["player_id"] for t in merged], ["p2", "p1"])
        p1 = merged[1]
        self.assertEqual((p1["clause"], p1["player_team_id"], p1["market_id"]),
                         (2, "s1", "m1"))


class TheGameweekClock(unittest.TestCase):
    """Read from LaLiga's own calendar: the current gameweek if it has not
    started, the next one if it has."""

    class _C:
        def __init__(self, weeks):
            self.weeks = weeks

        def current_week(self):
            return {"weekNumber": 7}

        def calendar(self, week=None):
            return [{"matchDate": to_iso(d)} for d in self.weeks.get(week, [])]

    def test_a_gameweek_under_way_points_at_the_next_one(self):
        now = utcnow()
        nxt = now + timedelta(days=5)
        c = self._C({7: [now - timedelta(hours=3), now + timedelta(hours=20)],
                     8: [nxt + timedelta(hours=2), nxt]})
        at, source = tick._gameweek_start(c, now=now)
        self.assertEqual((at, source), (to_iso(nxt), "calendario"))

    def test_a_gameweek_not_yet_started_is_the_one(self):
        now = utcnow()
        first = now + timedelta(hours=30)
        c = self._C({7: [first + timedelta(hours=3), first]})
        self.assertEqual(tick._gameweek_start(c, now=now)[0], to_iso(first))

    def test_the_scrape_is_the_fallback(self):
        now = utcnow()
        later = now + timedelta(days=2)
        report = {"matchday": {"gameweek_kickoff": to_iso(later)}}
        self.assertEqual(tick._gameweek_start(self._C({}), report, now=now),
                         (to_iso(later), "futbolfantasy"))

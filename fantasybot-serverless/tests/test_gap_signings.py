"""Signing for a position that stops the eleven being fielded at all.

This is the difference between a bot that trades well and one that wins. The flip
engine only bids on PROFITABLE resales, so a squad missing a goalkeeper would sit
there correctly declining to overpay — fielding ten men and losing points every
single gameweek. An empty slot costs more than a bad margin.

BLOCKING gaps only. Being a substitute short is a different question with a
different answer: it is insurance, it has a price, and the upgrade engine puts an
actual number on it rather than a squad-size rule of thumb spending real money on
a backup who would score 0.4 on the weeks he plays.
"""

from datetime import timedelta

from fantasybot import config, scheduler, state, tick
from fantasybot.scheduler import TickContext
from fantasybot.storage import to_iso, utcnow
from tests.support import StorageTestCase


class GapSignings(StorageTestCase):
    NAMES = ("AUTO_BIDS", "AUTO_EXECUTE", "CASH_RESERVE")

    def setUp(self):
        super().setUp()
        self._flags = {n: getattr(config, n) for n in self.NAMES}
        config.AUTO_BIDS = config.AUTO_EXECUTE = True
        config.CASH_RESERVE = 0
        self.addCleanup(lambda: [setattr(config, n, v)
                                 for n, v in self._flags.items()])

    def _cand(self, **kw):
        base = {"nombre": "Portero", "market_id": "mk1", "player_id": "p1",
                "via": "SISTEMA", "price": 5_000_000, "max_bid": 6_000_000,
                "prob": 85, "disponible": True, "affordable": True,
                "expires": to_iso(utcnow() + timedelta(hours=6))}
        base.update(kw)
        return base

    def _plan(self, cands, money=50_000_000, gaps=("POR",)):
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        report = {"blocking_gaps": dict.fromkeys(gaps, 1),
                  "needs": {"gaps": dict.fromkeys(gaps, 1),
                            "suggestions": {gaps[0]: cands}}}
        return tick._plan_gap_signings(ctx, "L1", {"teamMoney": money}, report)

    def test_it_bids_for_a_missing_goalkeeper(self):
        res = self._plan([self._cand()])
        self.assertEqual(len(res["queued"]), 1)
        self.assertEqual(res["committed"], 6_000_000)
        queued = self.store.pending_actions()[0]
        self.assertEqual(queued["type"], scheduler.BID)
        self.assertEqual(queued["payload"]["max_bid"], 6_000_000)

    def test_it_closes_the_task_that_asked_a_human_to_do_it(self):
        """An autonomous bot must not leave a to-do saying "sign a goalkeeper"
        for something it just did."""
        state.add_task("Sign POR: you're short.", key="gap:POR")
        self.assertEqual(len(state.pending_tasks()), 1)
        self._plan([self._cand()])
        self.assertEqual(state.pending_tasks(), [])

    def test_it_skips_a_player_who_will_not_start(self):
        res = self._plan([self._cand(prob=10)])
        self.assertEqual(res["queued"], [])
        # Spanish on purpose: this string is shown on the page, not logged.
        self.assertIn("titular que entre en la caja",
                      res["skipped"][0]["why"])

    def test_it_skips_an_unavailable_player(self):
        self.assertEqual(self._plan([self._cand(disponible=False)])["queued"], [])

    def test_it_leaves_the_clause_route_to_the_clause_planner(self):
        """agent.clause_targets already filters on exactly these positions;
        planning it twice would queue two ways of buying the same player."""
        self.assertEqual(self._plan([self._cand(via="CLAUSULA")])["queued"], [])

    def test_a_listing_with_no_close_time_cannot_be_timed(self):
        self.assertEqual(self._plan([self._cand(expires=None)])["queued"], [])

    def test_it_will_not_commit_more_than_the_balance(self):
        self.assertEqual(self._plan([self._cand()], money=1_000_000)["queued"], [])

    def test_the_cash_reserve_is_respected(self):
        config.CASH_RESERVE = 46_000_000
        self.assertEqual(self._plan([self._cand()], money=50_000_000)["queued"], [])

    def test_it_takes_the_first_viable_candidate_not_the_cheapest(self):
        """The list arrives sorted by availability then starting probability, so
        the first one that fits is the best one that fits."""
        res = self._plan([self._cand(nombre="Suplente", prob=20),
                          self._cand(nombre="Titular", market_id="mk2", prob=90)])
        self.assertEqual(res["queued"][0]["nombre"], "Titular")

    def test_two_gaps_share_one_budget(self):
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        report = {"blocking_gaps": {"POR": 1, "DEL": 1}, "needs": {
            "gaps": {"POR": 1, "DEL": 1},
            "suggestions": {
                "POR": [self._cand(max_bid=8_000_000)],
                "DEL": [self._cand(market_id="mk2", nombre="Delantero",
                                   max_bid=8_000_000)]}}}
        res = tick._plan_gap_signings(ctx, "L1", {"teamMoney": 10_000_000}, report)
        self.assertEqual(len(res["queued"]), 1,
                         "the second gap must not spend money the first took")
        self.assertEqual(res["committed"], 8_000_000)

    def test_no_gaps_means_nothing_to_do(self):
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        res = tick._plan_gap_signings(ctx, "L1", {"teamMoney": 1},
                                      {"needs": {"gaps": {}}})
        self.assertEqual(res["queued"], [])

    def test_autonomy_off_queues_nothing(self):
        config.AUTO_EXECUTE = False
        res = self._plan([self._cand()])
        self.assertEqual(res["mode"], "off")
        self.assertEqual(self.store.pending_actions(), [])

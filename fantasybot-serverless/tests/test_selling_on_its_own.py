"""Selling on its own, not just publishing.

"Tiene que poder vender solo y no solo publicar."

The squad stood on the market all season and nothing sold, for two reasons
that compounded:

  * The offers were never read. LaLiga's market row carries `numberOfOffers` —
    a count — and nothing else; the offers hang off the player's slot at
    `/league/{id}/playerTeam/{slot}/offer`. The bot looked for an `offers` list
    on the row, found none, and concluded nobody had bid.
  * Even read, most would have been refused. The ask has to sit above value
    (LaLiga refuses a listing at or under it), and the ask was also the bar for
    accepting — so LaLiga's own daily offer, which lands around value, could
    never clear it for the very players the bot wanted gone.

The live row that proved the first one, verbatim in the fields that matter:
`"numberOfOffers": 0, "directOffer": false`, and no offers key at all.
"""

import unittest
from datetime import timedelta

from fantasybot import config, scheduler, tick
from fantasybot.scheduler import TickContext
from fantasybot.storage import to_iso, utcnow
from fantasybot.strategy import finance
from fantasybot.strategy import offers
from tests.support import StorageTestCase


def _row(pid="p1", slot="pt-1", market_id="m1", count=1, value=10_000_000,
         seller="T1", money=None):
    row = {"id": market_id, "discr": "marketPlayerTeam",
           "playerMaster": {"id": pid, "nickname": f"J{pid}",
                            "marketValue": value},
           "playerTeam": {"playerTeamId": slot, "buyoutClause": value},
           "numberOfOffers": count, "directOffer": False,
           "salePrice": round(value * 1.01)}
    if money is not None:
        row["sellerTeam"] = {"id": seller, "teamMoney": money}
    return row


class ReadingTheOffersWhereLaLigaKeepsThem(unittest.TestCase):
    def test_a_bare_list_best_first_and_only_pending(self):
        got = offers.parse_player_offers([
            {"id": "a", "money": "9000000", "status": "pending"},
            {"id": "b", "money": 9_500_000},
            {"id": "c", "money": 12_000_000, "status": "rejected"},
            {"id": None, "money": 1},
        ])
        self.assertEqual([o["id"] for o in got], ["b", "a"])
        self.assertEqual(got[1]["money"], 9_000_000, "numbers arrive as strings")

    def test_a_wrapped_list(self):
        for key in ("offers", "data", "elements", "items"):
            got = offers.parse_player_offers({key: [{"id": "o", "money": 5}]})
            self.assertEqual(len(got), 1, key)

    def test_nothing_is_nothing(self):
        self.assertEqual(offers.parse_player_offers(None), [])
        self.assertEqual(offers.parse_player_offers({}), [])

    def test_isFromMarket_decides_who_is_paying(self):
        self.assertTrue(offers.is_system_offer({"isFromMarket": True,
                                                "buyerTeam": {"id": "x"}}))
        self.assertFalse(offers.is_system_offer({"isFromMarket": False}))

    def test_the_seller_is_us_and_says_nothing_about_the_buyer(self):
        self.assertTrue(offers.is_system_offer({"id": 1, "money": 1,
                                                "sellerTeam": {"id": "T1"}}))


class OnlyTheListingsWithOffersCostARequest(unittest.TestCase):
    class _C:
        def __init__(self, answer):
            self.asked, self.answer = [], answer

        def player_offers(self, lid, slot):
            self.asked.append(slot)
            return self.answer

    def test_rows_with_offers_are_read_and_the_rest_are_not(self):
        c = self._C([{"id": "o1", "money": 10_000_000}])
        market = [_row("p1", "pt-1", count=1), _row("p2", "pt-2", count=0),
                  _row("rival", "pt-9", count=3)]
        rep = offers.attach_offers(c, "L", market, {"p1", "p2"})
        self.assertEqual(c.asked, ["pt-1"], "one request, for the one with offers")
        self.assertEqual(market[0]["offers"][0]["id"], "o1")
        self.assertNotIn("offers", market[2], "a rival's listing is not ours")
        self.assertEqual(rep["ofertas"], 1)

    def test_a_row_that_does_not_say_is_polled_but_not_every_tick(self):
        c = self._C([])
        row = _row("p1", "pt-1")
        row.pop("numberOfOffers")
        polled = {}
        offers.attach_offers(c, "L", [row], {"p1"}, polled=polled, now_ts=1000)
        offers.attach_offers(c, "L", [row], {"p1"}, polled=polled, now_ts=1100)
        offers.attach_offers(c, "L", [row], {"p1"}, polled=polled, now_ts=2000)
        self.assertEqual(len(c.asked), 2)

    def test_a_failing_read_is_reported_not_raised(self):
        class Broken:
            def player_offers(self, lid, slot):
                raise RuntimeError("503")
        rep = offers.attach_offers(Broken(), "L", [_row()], {"p1"})
        self.assertTrue(rep["errores"])


class WhatWeTakeIsNotWhatWeAsk(unittest.TestCase):
    def _p(self, pid="p1", ptid="pt-1", value=10_000_000, status=None):
        return {"playerTeamId": ptid,
                "playerMaster": {"id": pid, "nickname": pid,
                                 "marketValue": value, "playerStatus": status}}

    def test_a_useful_bench_player_goes_for_about_value(self):
        floor, _ = offers.accept_floor(self._p(), set(), set())
        self.assertEqual(floor, 9_800_000)
        ask = offers.reserve_price(self._p(), set(), set())
        self.assertGreater(ask, 10_000_000, "the ask stays above value (030.01.02)")

    def test_dead_money_goes_a_little_under(self):
        floor, why = offers.accept_floor(self._p(), set(), {"p1"})
        self.assertEqual(floor, 9_300_000)
        self.assertEqual(why, "dinero parado")

    def test_a_starter_still_costs_his_premium(self):
        floor, why = offers.accept_floor(self._p(), {"pt-1"}, set())
        self.assertEqual(floor, 14_000_000)
        self.assertEqual(why, "titular")

    def test_a_player_funding_a_signing(self):
        floor, _ = offers.accept_floor(self._p(), {"pt-1"}, set(), fund_ids={"p1"})
        self.assertEqual(floor, 9_500_000, "even a starter, when the signing is worth more")

    def test_out_of_the_league(self):
        floor, _ = offers.accept_floor(self._p(status="out_of_league"), set(), set())
        self.assertEqual(floor, 7_000_000)

    def test_the_floors_map_carries_what_the_debt_manager_needs(self):
        team = {"players": [self._p("p1", "pt-1"), self._p("p2", "pt-2")]}
        best = {"payload": {"goalkeeper": "pt-1", "defender": [],
                            "midfield": [], "striker": []}}
        got = offers.sale_floors(team, best, expected={"pt-1": 5.0, "pt-2": 0.4})
        self.assertTrue(got["p1"]["xi"])
        self.assertFalse(got["p2"]["xi"])
        self.assertEqual(got["p2"]["ptid"], "pt-2")
        self.assertEqual(got["p1"]["pts"], 5.0)


class DecidingWithFloors(unittest.TestCase):
    FLOORS = {"p1": {"ask": 10_100_000, "floor": 9_800_000, "xi": False,
                     "value": 10_000_000, "ptid": "pt-1", "why": "suplente útil"}}

    def _decide(self, offer):
        row = _row()
        row["offers"] = [offer]
        return offers.evaluate_offers(None, [row], reserves={"p1": 10_100_000},
                                      floors=self.FLOORS)[0]

    def test_laligas_offer_at_99_percent_now_sells(self):
        """Under the old rule this was below the ask and never sold."""
        d = self._decide({"id": "o1", "money": 9_900_000})
        self.assertEqual(d["action"], offers.ACCEPT)
        self.assertTrue(d["de_laliga"])

    def test_laligas_lowball_is_held_not_declined(self):
        d = self._decide({"id": "o1", "money": 8_000_000})
        self.assertEqual(d["action"], offers.HOLD)

    def test_a_rivals_lowball_is_declined(self):
        d = self._decide({"id": "o1", "money": 8_000_000,
                          "buyerTeam": {"id": "T9",
                                        "manager": {"managerName": "rival"}}})
        self.assertEqual(d["action"], offers.DECLINE)
        self.assertEqual(d["comprador"], "rival")


class TheDebtManager(unittest.TestCase):
    def _d(self, name, amount, value, xi=False, pts=0.0):
        return {"action": offers.HOLD, "nombre": name, "amount": amount,
                "value": value, "in_xi": xi, "pts": pts, "best": True}

    def test_pressure_tightens_with_the_clock(self):
        now = utcnow()
        start = lambda h: to_iso(now + timedelta(hours=h))  # noqa: E731
        self.assertIsNone(finance.pressure(5, start(10), now))
        self.assertEqual(finance.pressure(-1, start(100), now), finance.IN_DEBT)
        self.assertEqual(finance.pressure(-1, start(45), now), finance.URGENT)
        self.assertEqual(finance.pressure(-1, start(10), now), finance.PANIC)
        self.assertEqual(finance.pressure(-1, None, now), finance.URGENT)
        self.assertEqual(finance.pressure(-1, start(-5), now), finance.IN_DEBT,
                         "a start that has passed is a gameweek under way")

    def test_it_sells_the_bench_first_and_stops_when_positive(self):
        ds = [self._d("titular", 20_000_000, 20_000_000, xi=True, pts=6),
              self._d("suplente", 4_000_000, 4_200_000),
              self._d("otro", 3_000_000, 3_100_000)]
        finance.settle_debt(ds, -3_500_000, finance.URGENT)
        sold = [d["nombre"] for d in ds if d["action"] == "accept"]
        self.assertEqual(sold, ["suplente"])

    def test_the_eleven_is_only_touched_in_panic(self):
        ds = [self._d("titular", 20_000_000, 20_000_000, xi=True, pts=6)]
        finance.settle_debt(ds, -5_000_000, finance.URGENT)
        self.assertEqual(ds[0]["action"], offers.HOLD)
        finance.settle_debt(ds, -5_000_000, finance.PANIC)
        self.assertEqual(ds[0]["action"], "accept")

    def test_a_calm_bank_changes_nothing(self):
        ds = [self._d("suplente", 4_000_000, 4_200_000)]
        finance.settle_debt(ds, 1_000_000, finance.CALM)
        self.assertEqual(ds[0]["action"], offers.HOLD)


class _OfferClient:
    def __init__(self, market, answers, money=1_000_000):
        self._market, self.answers, self.money = market, answers, money
        self.accepted, self.declined, self.asked = [], [], []

    def default_ids(self):
        return "L1", "T1"

    def market(self, lid):
        return self._market

    def player_offers(self, lid, slot):
        self.asked.append(slot)
        return self.answers.get(slot, [])

    def team_money(self, tid):
        return {"teamMoney": self.money}

    def accept_offer(self, lid, market_id, offer_id, money):
        self.accepted.append((market_id, offer_id, money))

    def decline_offer(self, lid, market_id, offer_id):
        self.declined.append((market_id, offer_id))


class TheTickSells(StorageTestCase):
    def setUp(self):
        super().setUp()
        saved = (config.AUTO_SELLS, config.DECLINE_LOWBALLS)
        config.AUTO_SELLS = config.DECLINE_LOWBALLS = True
        self.addCleanup(lambda: setattr(config, "AUTO_SELLS", saved[0]))
        self.addCleanup(lambda: setattr(config, "DECLINE_LOWBALLS", saved[1]))
        self.store.put_doc("league_ids", ["L1", "T1"])
        self.store.put_doc("reserves", {"p1": 10_100_000, "p2": 30_300_000})
        self.store.put_doc("sale_floors", {
            "p1": {"ask": 10_100_000, "floor": 9_800_000, "xi": False,
                   "value": 10_000_000, "ptid": "pt-1", "pts": 0.2},
            "p2": {"ask": 42_000_000, "floor": 42_000_000, "xi": True,
                   "value": 30_000_000, "ptid": "pt-2", "pts": 6.0}})

    def _tick(self, client):
        ctx = TickContext(client=client, budget_seconds=30, log=lambda m: None)
        return tick.handle_offers(ctx)

    def test_laligas_offer_is_read_from_the_slot_and_accepted(self):
        client = _OfferClient(
            [_row("p1", "pt-1", "m1", count=1),
             _row("p2", "pt-2", "m2", count=0, value=30_000_000)],
            {"pt-1": [{"id": "o1", "money": 9_950_000, "status": "pending",
                       "isFromMarket": True}]})
        res = self._tick(client)
        self.assertEqual(client.asked, ["pt-1"])
        self.assertEqual(client.accepted, [("m1", "o1", 9_950_000)])
        self.assertEqual(len(res["accepted"]), 1)
        self.assertTrue(self.store.get_doc("offers_seen"),
                        "the proof the credit line waits for")
        reviews = [a for a in self.store.pending_actions()
                   if a["type"] == scheduler.REVIEW]
        self.assertEqual(len(reviews), 1, "money landed: think again now")

    def test_negative_before_the_gameweek_sells_the_starter_if_it_must(self):
        self.store.put_doc("gameweek_start",
                           {"at": to_iso(utcnow() + timedelta(hours=10))})
        client = _OfferClient(
            [_row("p2", "pt-2", "m2", count=1, value=30_000_000)],
            {"pt-2": [{"id": "o9", "money": 29_000_000, "isFromMarket": True}]},
            money=-12_000_000)
        res = self._tick(client)
        self.assertEqual(res["pressure"], finance.PANIC)
        self.assertEqual(client.accepted, [("m2", "o9", 29_000_000)],
                         "a gameweek that starts negative scores zero")
        self.assertEqual(res["money_after"], 17_000_000)

    def test_the_same_offer_is_held_with_a_healthy_bank(self):
        client = _OfferClient(
            [_row("p2", "pt-2", "m2", count=1, value=30_000_000)],
            {"pt-2": [{"id": "o9", "money": 29_000_000, "isFromMarket": True}]},
            money=5_000_000)
        res = self._tick(client)
        self.assertEqual(client.accepted, [])
        self.assertEqual(client.declined, [], "LaLiga's offer is held, not declined")
        self.assertEqual(len(res["held"]), 1)

    def test_new_laliga_listings_wake_a_review(self):
        system = {"id": "s1", "discr": "marketPlayerLeague",
                  "playerMaster": {"id": "x1", "marketValue": 1}}
        client = _OfferClient([system], {})
        self._tick(client)          # first read only remembers
        self.assertEqual(self.store.pending_actions(), [])
        client._market = [system, {**system, "id": "s2"}]
        self._tick(client)
        self.assertEqual([a["type"] for a in self.store.pending_actions()],
                         [scheduler.REVIEW])


if __name__ == "__main__":
    unittest.main()

"""Buying to win, not to trade.

The bot ranked the market by projected resale profit and never asked whether the
player would take the field. That is a trader's question, and a trader finishes
the season rich and second. Every case here is one the margin ordering got wrong.
"""

import unittest

from fantasybot.strategy import upgrades


def card(pid, pos, value, avg, status="ok"):
    """A player as the API sends him — numbers as strings."""
    return {"id": pid, "nickname": pid, "name": pid, "positionId": str(pos),
            "marketValue": str(value), "playerStatus": status,
            "averagePoints": str(avg), "points": str(avg * 10),
            "lastSeasonPoints": "80"}


def squad(avg=3):
    """A legal, unremarkable XI plus a bench: 1-4-4-3 and one spare."""
    rows = [(1, 1), (2, 4), (3, 4), (4, 3)]
    players, n = [], 0
    for pos, count in rows:
        for _ in range(count):
            n += 1
            players.append({"playerTeamId": f"pt{n}",
                            "playerMaster": card(f"p{n}", pos, 5_000_000, avg)})
    return {"teamMoney": "20000000", "players": players}


def op(pid, price, margin=0.0):
    return {"player_id": pid, "market_id": f"m-{pid}", "nombre": pid,
            "buy_price": price, "margin_pct": margin}


class WhatASigningIsWorth(unittest.TestCase):
    def test_a_better_player_than_my_worst_starter_is_worth_points(self):
        team = squad(avg=3)
        cards = {"star": card("star", 4, 9_000_000, 9)}
        got = upgrades.rank([op("star", 9_000_000)], team, cards, prob_index={})
        self.assertGreater(got[0]["gain"], 1.0)

    def test_a_worse_player_is_worth_nothing_however_cheap(self):
        """He never makes the XI, so he scores nothing. The old ordering bought
        him for his margin."""
        team = squad(avg=6)
        cards = {"dud": card("dud", 4, 500_000, 1)}
        got = upgrades.rank([op("dud", 500_000, margin=30.0)], team, cards,
                            prob_index={})
        self.assertEqual(got[0]["gain"], 0.0)
        self.assertFalse(upgrades.worth_signing(got[0]))

    def test_points_beat_margin_when_they_disagree(self):
        team = squad(avg=3)
        cards = {"star": card("star", 4, 9_000_000, 9),
                 "dud": card("dud", 4, 500_000, 1)}
        ranked = upgrades.rank([op("dud", 500_000, margin=30.0),
                                op("star", 9_000_000, margin=0.0)],
                               team, cards, prob_index={})
        self.assertEqual(ranked[0]["nombre"], "star")

    def test_cheap_and_good_beats_dear_and_good(self):
        """Two signings that each add a point beat one that adds one and a half
        for twice the price. The budget is finite."""
        team = squad(avg=3)
        cards = {"dear": card("dear", 4, 16_000_000, 9),
                 "cheap": card("cheap", 4, 4_000_000, 8)}
        ranked = upgrades.rank([op("dear", 16_000_000), op("cheap", 4_000_000)],
                               team, cards, prob_index={})
        self.assertEqual(ranked[0]["nombre"], "cheap")

    def test_a_player_we_already_own_is_not_a_signing(self):
        team = squad()
        cards = {"p1": card("p1", 1, 5_000_000, 3)}
        self.assertEqual(upgrades.rank([op("p1", 5_000_000)], team, cards,
                                       prob_index={}), [])

    def test_an_injured_signing_adds_nothing(self):
        team = squad(avg=3)
        cards = {"hurt": card("hurt", 4, 9_000_000, 9, status="injured")}
        got = upgrades.rank([op("hurt", 9_000_000)], team, cards, prob_index={})
        self.assertEqual(got[0]["gain"], 0.0)


class AGapIsJustALargeGain(unittest.TestCase):
    def test_a_keeper_for_a_squad_with_none_is_worth_the_whole_xi(self):
        """No goalkeeper means no legal XI at all, so the first one is worth
        more than any striker — and the gap rule falls out of the same measure
        instead of needing one of its own."""
        team = squad()
        team["players"] = [p for p in team["players"]
                           if p["playerMaster"]["positionId"] != "1"]
        cards = {"gk": card("gk", 1, 1_000_000, 3)}
        got = upgrades.rank([op("gk", 1_000_000)], team, cards, prob_index={})
        self.assertGreater(got[0]["gain"], 10.0)


class SpendingTheBudget(unittest.TestCase):
    def test_it_stops_at_the_balance(self):
        rows = [{"nombre": "a", "buy_price": 8_000_000, "gain": 2.0,
                 "gain_per_million": 0.25, "affordable": True},
                {"nombre": "b", "buy_price": 8_000_000, "gain": 1.5,
                 "gain_per_million": 0.19, "affordable": True}]
        picked = upgrades.best_plan(rows, 10_000_000)
        self.assertEqual([p["nombre"] for p in picked], ["a"])

    def test_the_cash_reserve_is_not_spent(self):
        rows = [{"nombre": "a", "buy_price": 9_000_000, "gain": 2.0,
                 "gain_per_million": 0.22, "affordable": True}]
        self.assertEqual(upgrades.best_plan(rows, 10_000_000,
                                            reserve=5_000_000), [])

    def test_noise_is_not_worth_money(self):
        """Below the bar the difference is noise in the starting probabilities,
        and acting on noise spends real euros."""
        rows = [{"nombre": "a", "buy_price": 1_000_000, "gain": 0.05,
                 "gain_per_million": 0.05, "affordable": True}]
        self.assertEqual(upgrades.best_plan(rows, 10_000_000), [])


class SellingToFundASigning(unittest.TestCase):
    """The bot could only buy what its balance covered, so a squad holding a
    nine-million bench player who scores nothing was locked out of every real
    starter on the market. Selling him IS the transfer."""

    def _team(self):
        team = squad(avg=3)
        # A dear passenger: expensive, and he never makes the XI because the
        # squad already fields four better strikers.
        team["players"].append({
            "playerTeamId": "pt-dead",
            "playerMaster": card("dead", 4, 9_000_000, 0)})
        team["teamMoney"] = "1000000"
        return team

    def test_a_bench_player_costs_nothing_to_lose(self):
        rows = upgrades.sellable(self._team(), prob_index={})
        dead = next(r for r in rows if r["nombre"] == "dead")
        self.assertEqual(dead["loss"], 0.0)
        self.assertEqual(rows[0]["loss"], 0.0, "cheapest to give up comes first")

    def test_a_starter_costs_real_points_to_lose(self):
        rows = upgrades.sellable(squad(avg=5), prob_index={})
        self.assertTrue(any(r["loss"] > 0 for r in rows))

    def test_the_sale_unlocks_a_signing_the_cash_could_not_reach(self):
        team = self._team()
        cards = {"star": card("star", 4, 9_000_000, 9)}
        ranked = upgrades.rank([op("star", 9_000_000)], team, cards,
                               money=1_000_000, prob_index={})
        got = upgrades.transfers(ranked, team, money=1_000_000, prob_index={})
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["buy"], got[0]["sell"]), ("star", "dead"))
        self.assertGreater(got[0]["net_gain"], 1.0)

    def test_a_sale_that_costs_more_than_the_signing_adds_is_refused(self):
        """Not a transfer — a downgrade with extra steps."""
        team = squad(avg=9)
        team["teamMoney"] = "0"
        ranked = [{"nombre": "meh", "market_id": "m", "buy_price": 5_000_000,
                   "gain": 0.3}]
        self.assertEqual(
            upgrades.transfers(ranked, team, money=0, prob_index={}), [])

    def test_something_already_affordable_is_not_a_transfer(self):
        team = self._team()
        team["teamMoney"] = "50000000"
        cards = {"star": card("star", 4, 9_000_000, 9)}
        ranked = upgrades.rank([op("star", 9_000_000)], team, cards,
                               money=50_000_000, prob_index={})
        self.assertEqual(
            upgrades.transfers(ranked, team, money=50_000_000, prob_index={}), [])

    def test_the_ask_is_discounted_to_what_would_actually_be_paid(self):
        """A reserve is an ASK. A plan funded by the ask funds itself with money
        nobody has offered."""
        rows = upgrades.sellable(self._team(), prob_index={})
        dead = next(r for r in rows if r["nombre"] == "dead")
        self.assertLess(dead["raises"], dead["value"])

"""Standing listings and the offers they attract.

This is the code that decides when a player leaves the squad, so the thresholds
are pinned exhaustively rather than sampled. Everything here is pure: squad and
market data in, decisions out, no I/O and no clock.
"""

import unittest

from fantasybot.strategy import offers


def player(pid="p1", ptid=None, value=10_000_000, status=None, nickname="Jugador"):
    return {"playerTeamId": ptid or f"pt-{pid}",
            "playerMaster": {"id": pid, "nickname": nickname,
                             "marketValue": value, "playerStatus": status}}


def listing_row(pid="p1", market_id="m1", offer_list=None, **kw):
    row = {"id": market_id, "discr": "marketPlayerTeam",
           "playerMaster": {"id": pid, "nickname": "Jugador",
                            "marketValue": kw.get("value", 10_000_000)}}
    if offer_list is not None:
        row["offers"] = offer_list
    return row


def best_xi(*player_team_ids):
    """A lineup payload shaped the way payload_ids() reads it.

    `goalkeeper` is a single id, not a list — the other three lines are lists.
    """
    ids = list(player_team_ids)
    return {"payload": {"goalkeeper": ids[0], "defender": ids[1:],
                        "midfield": [], "striker": []}}


class ReservePrice(unittest.TestCase):
    def test_a_starter_costs_a_premium(self):
        p = player(ptid="pt-1")
        self.assertEqual(offers.reserve_price(p, {"pt-1"}, set()), 14_000_000)

    def test_a_bench_player_is_asked_at_market_value_not_a_premium(self):
        """The reserve is a THRESHOLD; `accept_offer` pays the offered amount,
        not the reserve. A premium on a player we want to move buys nothing and
        only costs the sale — see MOVABLE_ASK."""
        self.assertEqual(offers.reserve_price(player(), set(), set()), 10_000_000)

    def test_a_flagged_sell_target_goes_at_market_value(self):
        self.assertEqual(offers.reserve_price(player(), set(), {"p1"}), 10_000_000)

    def test_a_player_out_of_the_league_is_dumped_at_a_discount(self):
        """His value is collapsing — holding out for a premium loses money."""
        p = player(status="out_of_league")
        self.assertEqual(offers.reserve_price(p, {"pt-p1"}, set()), 7_000_000)

    def test_out_of_league_beats_being_a_starter(self):
        p = player(ptid="pt-1", status="out_of_league")
        self.assertEqual(offers.reserve_price(p, {"pt-1"}, set()), 7_000_000)

    def test_a_valueless_player_has_no_reserve(self):
        self.assertEqual(offers.reserve_price(player(value=0), set(), set()), 0)


class PlanListings(unittest.TestCase):
    def test_the_whole_squad_goes_up_including_starters(self):
        """Listing is not selling: a starter's reserve is high enough that only a
        genuinely good offer moves him, and an unmet listing costs nothing."""
        team = {"players": [player("p1", "pt-1"), player("p2", "pt-2")]}
        plan = offers.plan_listings(team, market=[], best=best_xi("pt-1"))
        self.assertEqual({r["player_id"] for r in plan}, {"p1", "p2"})
        by_id = {r["player_id"]: r for r in plan}
        self.assertEqual(by_id["p1"]["price"], 14_000_000)
        self.assertTrue(by_id["p1"]["in_xi"])
        self.assertEqual(by_id["p2"]["price"], 10_000_000)

    def test_already_listed_players_are_not_listed_again(self):
        team = {"players": [player("p1"), player("p2")]}
        plan = offers.plan_listings(team, market=[listing_row("p1")])
        self.assertEqual([r["player_id"] for r in plan], ["p2"])

    def test_it_uses_the_roster_slot_id_for_the_sale(self):
        team = {"players": [player("p1", "pt-99")]}
        self.assertEqual(offers.plan_listings(team, [])[0]["player_team_id"],
                         "pt-99")

    def test_players_below_the_floor_are_skipped(self):
        team = {"players": [player("p1", value=50_000)]}
        self.assertEqual(offers.plan_listings(team, []), [])

    def test_an_empty_squad_is_not_an_error(self):
        self.assertEqual(offers.plan_listings({}, []), [])


class EvaluateOffers(unittest.TestCase):
    def _decide(self, offer_money, *, in_xi=False, value=10_000_000):
        team = {"players": [player("p1", "pt-1", value=value)]}
        market = [listing_row("p1", offer_list=[{"id": "o1", "money": offer_money}],
                              value=value)]
        best = best_xi("pt-1") if in_xi else None
        return offers.evaluate_offers(team, market, best=best)[0]

    def test_an_offer_at_the_reserve_is_accepted(self):
        d = self._decide(10_000_000)
        self.assertEqual(d["action"], offers.ACCEPT)
        self.assertEqual(d["offer_id"], "o1")

    def test_an_offer_one_euro_short_is_declined(self):
        self.assertEqual(self._decide(9_999_999)["action"], offers.DECLINE)

    def test_a_starter_is_not_sold_at_a_bench_price(self):
        self.assertEqual(self._decide(10_000_000, in_xi=True)["action"],
                         offers.DECLINE)

    def test_a_starter_goes_for_a_real_premium(self):
        self.assertEqual(self._decide(14_000_000, in_xi=True)["action"],
                         offers.ACCEPT)

    def test_only_the_best_offer_can_be_accepted(self):
        """Two good offers on one player must not sell him twice."""
        team = {"players": [player("p1", "pt-1")]}
        market = [listing_row("p1", offer_list=[
            {"id": "low", "money": 12_000_000},
            {"id": "high", "money": 13_000_000}])]
        decided = offers.evaluate_offers(team, market)
        accepted = [d for d in decided if d["action"] == offers.ACCEPT]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["offer_id"], "high")
        self.assertEqual(accepted[0]["amount"], 13_000_000)

    def test_a_rivals_listing_is_none_of_our_business(self):
        team = {"players": [player("p1")]}
        market = [listing_row("someone-else",
                              offer_list=[{"id": "o1", "money": 99_000_000}])]
        self.assertEqual(offers.evaluate_offers(team, market), [])

    def test_a_listing_with_no_offers_produces_no_decision(self):
        team = {"players": [player("p1")]}
        self.assertEqual(offers.evaluate_offers(team, [listing_row("p1")]), [])

    def test_a_single_offer_object_is_understood_too(self):
        """The API has been seen returning one offer as an object, not a list."""
        team = {"players": [player("p1")]}
        market = [listing_row("p1")]
        market[0]["offer"] = {"id": "o1", "money": 12_000_000}
        decided = offers.evaluate_offers(team, market)
        self.assertEqual(decided[0]["action"], offers.ACCEPT)

    def test_an_offer_without_an_id_is_ignored_not_guessed_at(self):
        """Acting on an offer we cannot identify is how you accept the wrong one."""
        team = {"players": [player("p1")]}
        market = [listing_row("p1", offer_list=[{"money": 99_000_000}])]
        self.assertEqual(offers.evaluate_offers(team, market), [])

    def test_a_valueless_player_is_never_sold_by_accident(self):
        """Reserve 0 must not mean "accept anything" — it means we cannot price
        him, so we decline rather than give him away."""
        team = {"players": [player("p1", value=0)]}
        market = [listing_row("p1", offer_list=[{"id": "o1", "money": 1}],
                              value=0)]
        self.assertEqual(offers.evaluate_offers(team, market)[0]["action"],
                         offers.DECLINE)


class ReserveMap(unittest.TestCase):
    """The reserve prices a review caches so every later tick can price an
    incoming offer without re-optimising the lineup."""

    def test_it_prices_the_whole_squad(self):
        team = {"players": [player("p1", "pt-1"), player("p2", "pt-2")]}
        got = offers.reserve_map(team, best=best_xi("pt-1"))
        self.assertEqual(got, {"p1": 14_000_000, "p2": 10_000_000})

    def test_a_cached_reserve_wins_over_recomputing(self):
        """The cached number was priced against a freshly optimised XI; this tick
        has not paid for one, so it must not quietly disagree."""
        team = {"players": [player("p1", "pt-1")]}
        market = [listing_row("p1", offer_list=[{"id": "o1", "money": 12_000_000}])]
        # No `best` passed, so recomputing would call it a bench player (11.5M)
        # and accept. The cached reserve says he is a starter.
        decided = offers.evaluate_offers(team, market,
                                         reserves={"p1": 14_000_000})
        self.assertEqual(decided[0]["action"], offers.DECLINE)
        self.assertEqual(decided[0]["reserve"], 14_000_000)

    def test_a_player_missing_from_the_cache_is_never_given_away(self):
        team = {"players": [player("p1")]}
        market = [listing_row("p1", offer_list=[{"id": "o1", "money": 1}])]
        decided = offers.evaluate_offers(team, market, reserves={})
        self.assertEqual(decided[0]["action"], offers.DECLINE)

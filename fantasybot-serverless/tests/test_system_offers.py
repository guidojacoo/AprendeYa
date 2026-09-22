"""LaLiga itself is the buyer, and the reserve is a threshold, not a price.

Reported live: "que compre y venda a laliga, al publicar en el mercado el
mercado le hace una oferta cada 24hs y muchas veces es mas del valor del
jugador. tiene que ver eso tambien."

Two things follow from that, and neither was true before this change.

`accept_offer` is called with the OFFERED amount (`d["amount"]`), never with
our reserve. So the reserve only ever decides yes or no — a lower ask never
earns us less, it just widens which offers clear the bar. Asking a bench
player at market value plus fifteen per cent bought nothing: LaLiga's own
standing offer usually lands somewhere near value, sometimes above it and
sometimes not quite there, and the fifteen per cent premium simply declined
the offers in between for no extra euro ever collected. A player outside the
eleven now asks market value and nothing more (MOVABLE_ASK), so the daily
system offer clears far more often.

And the offers themselves needed a name. LaLiga's standing offer carries no
bidder — no user, no manager, no team — because it is not one; a rival's bid
always does. `is_system_offer` reads that absence, so a decision can say
whether the money that bought a player was a rival's or the market's own.
"""

from fantasybot.strategy import offers
from tests.support import StorageTestCase


def _p(pid, value=10_000_000, status="ok"):
    return {"playerTeamId": pid,
            "playerMaster": {"id": pid, "nickname": pid, "positionId": "4",
                             "marketValue": str(value), "averagePoints": "3",
                             "playerStatus": status}}


def _row(pid, offer_list, value=10_000_000):
    return {"id": f"mk-{pid}", "discr": "marketPlayerTeam",
            "playerMaster": {"id": pid, "nickname": pid, "marketValue": value},
            "offers": offer_list}


class WhoMadeTheOffer(StorageTestCase):
    def test_an_offer_with_no_bidder_fields_is_the_system(self):
        self.assertTrue(offers.is_system_offer({"id": "o1", "money": 1}))

    def test_an_offer_naming_a_manager_is_human(self):
        self.assertFalse(offers.is_system_offer(
            {"id": "o1", "money": 1, "userId": 42}))

    def test_an_offer_naming_a_team_is_human(self):
        self.assertFalse(offers.is_system_offer(
            {"id": "o1", "money": 1, "buyerTeam": {"id": 7}}))

    def test_a_zero_or_empty_bidder_field_still_reads_as_system(self):
        """Present-but-empty is not the same as naming somebody."""
        self.assertTrue(offers.is_system_offer(
            {"id": "o1", "money": 1, "userId": None, "team": {}}))

    def test_junk_is_not_a_crash(self):
        self.assertFalse(offers.is_system_offer(None))
        self.assertFalse(offers.is_system_offer("not a dict"))

    def test_the_flag_travels_with_the_parsed_offer(self):
        parsed = offers._offers_on(
            _row("p1", [{"id": "o1", "money": 1_000_000}]))
        self.assertTrue(parsed[0]["de_laliga"])
        parsed = offers._offers_on(
            _row("p1", [{"id": "o1", "money": 1_000_000, "userId": 5}]))
        self.assertFalse(parsed[0]["de_laliga"])

    def test_the_decision_reports_who_paid(self):
        team = {"players": [_p("p1")]}
        market = [_row("p1", [{"id": "o1", "money": 10_000_000}])]
        got = offers.evaluate_offers(team, market)[0]
        self.assertTrue(got["de_laliga"])


class TheReserveIsAThresholdNotAPrice(StorageTestCase):
    """MOVABLE_ASK: `accept_offer` pays the offered amount, so a bench premium
    only ever costs a sale, never earns one."""

    def test_a_bench_player_asks_exactly_market_value(self):
        self.assertEqual(offers.reserve_price(_p("b1"), [], []), 10_000_000)

    def test_the_daily_system_offer_at_value_now_clears(self):
        """Value + 15% would have declined this; value clears it exactly."""
        team = {"players": [_p("p1")]}
        market = [_row("p1", [{"id": "o1", "money": 10_000_000}])]
        got = offers.evaluate_offers(team, market)[0]
        self.assertEqual(got["action"], offers.ACCEPT)

    def test_an_offer_above_value_is_still_accepted_and_we_keep_the_upside(self):
        """A higher premium never bought MORE money — accept_offer is paid
        whatever was offered, so the offer's own size is the only upside."""
        team = {"players": [_p("p1")]}
        market = [_row("p1", [{"id": "o1", "money": 12_000_000}])]
        got = offers.evaluate_offers(team, market)[0]
        self.assertEqual(got["action"], offers.ACCEPT)
        self.assertEqual(got["amount"], 12_000_000)

    def test_a_starter_still_needs_a_real_offer(self):
        """The eleven is not movable stock; MOVABLE_ASK does not touch it."""
        team = {"players": [_p("p1")]}
        market = [_row("p1", [{"id": "o1", "money": 10_000_000}])]
        best = {"payload": {"goalkeeper": None, "defender": ["p1"],
                            "midfield": [], "striker": []}}
        got = offers.evaluate_offers(team, market, best=best)[0]
        self.assertEqual(got["action"], offers.DECLINE)


if __name__ == "__main__":
    import unittest
    unittest.main()

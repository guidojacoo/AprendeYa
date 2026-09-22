"""Two keepers who never play, asked at a premium, all season.

They were not in the XI and no advisor had flagged them, so they fell to the
default tier: "bench player who is still an asset", market value PLUS fifteen
per cent. Nobody pays a premium for a footballer who will not take the field,
so the listings never sold and the money stayed locked in them.

A man who does not play is not an asset. He is cash we have not collected, and
the reasoning is the one DUMP_DISCOUNT already applied to a player who left the
league: waiting does not make him worth more.

`premium_for` still marks him with a NEGATIVE premium — he is still the one to
prioritise selling. What that premium can no longer do is push the actual
LISTED PRICE below market value: live evidence showed LaLiga refuses that
outright with 400 `030.01.02`, the sale-side sibling of the bid floor. So
`reserve_price` floors every player at value plus a small cushion (see
SALE_FLOOR_CUSHION_PCT); the discount is real for RANKING and PRIORITY, never
for the number actually submitted.
"""

from fantasybot.strategy import offers
from tests.support import StorageTestCase


def _keeper(ptid, pid, value=700_000):
    return {"playerTeamId": ptid,
            "playerMaster": {"id": pid, "nickname": f"Portero{pid}",
                             "positionId": "1", "marketValue": str(value),
                             "playerStatus": "ok"}}


class APlayerWhoDoesNotPlayIsPricedToLeave(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.bench = _keeper("pt-1", "k1")
        self.starter = _keeper("pt-2", "k2", value=5_000_000)

    def test_a_substitute_is_asked_under_his_value(self):
        got = offers.premium_for(self.bench, xi_ids=set(), sell_ids=set(),
                                 expected={"pt-1": 0.4})
        self.assertEqual(got, offers.BENCH_DISCOUNT)
        self.assertLess(got, 0, "a premium is what kept him unsold")

    def test_the_reserve_never_lands_below_market_value(self):
        """The bug that follows this one: LaLiga refuses a listing below
        value outright (400, 030.01.02). The discount marks him to prioritise;
        it may not push the submitted price under the floor."""
        price = offers.reserve_price(self.bench, set(), set(),
                                     expected={"pt-1": 0.4})
        self.assertGreaterEqual(price, 700_000)

    def test_without_the_signal_nothing_changes(self):
        """No expected points means judge him as before, not dump him."""
        self.assertEqual(
            offers.premium_for(self.bench, set(), set(), expected=None),
            offers.SQUAD_PREMIUM)

    def test_a_real_squad_player_keeps_his_premium(self):
        got = offers.premium_for(self.starter, set(), set(),
                                 expected={"pt-2": 4.5})
        self.assertEqual(got, offers.SQUAD_PREMIUM)

    def test_a_starter_is_still_expensive_to_prise_away(self):
        got = offers.premium_for(self.starter, xi_ids={"pt-2"}, sell_ids=set(),
                                 expected={"pt-2": 0.1})
        self.assertEqual(got, offers.XI_PREMIUM,
                         "being in the XI settles it before anything else")

    def test_out_of_the_league_still_wins(self):
        gone = _keeper("pt-3", "k3")
        gone["playerMaster"]["playerStatus"] = "out_of_league"
        self.assertEqual(
            offers.premium_for(gone, set(), set(), expected={"pt-3": 0.0}),
            offers.DUMP_DISCOUNT)

    def test_the_listing_plan_carries_what_he_is_expected_to_score(self):
        team = {"players": [self.bench]}
        plan = offers.plan_listings(team, [], expected={"pt-1": 0.4})
        self.assertEqual(plan[0]["expected_points"], 0.4)
        self.assertGreaterEqual(plan[0]["premium_pct"], 0,
                                "the floor wins; the discount cannot go below it")


class TheThresholdIsAboutPlayingNotAboutBeingGood(StorageTestCase):
    def test_a_genuine_substitute_falls_under_it(self):
        self.assertLess(0.4, offers.MIN_USEFUL_POINTS)

    def test_a_modest_starter_clears_it(self):
        self.assertGreater(2.5, offers.MIN_USEFUL_POINTS)

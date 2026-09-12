"""A squad standing on the market that sells nothing.

The offer handler runs on every tick and reads one thing: the reserve price of
each player. That was written inside the listing phase — and the listing phase is
time-boxed, dropped whenever the review is running late against the sixty seconds
Vercel allows. So a shortened review left the bot unable to judge a single offer
until a full one came round. The asks were out there; nobody was reading the
replies.
"""

from unittest import mock

from fantasybot import config, tick
from tests.support import StorageTestCase


class _Client:
    def __init__(self):
        self.market_calls = 0

    def market(self, lid):
        self.market_calls += 1
        return []


def _team():
    return {"teamMoney": 5_000_000, "players": [
        {"playerTeamId": "pt1",
         "playerMaster": {"id": "m1", "nickname": "Uno", "name": "Uno",
                          "positionId": 4, "marketValue": 3_000_000,
                          "playerStatus": "ok"}}]}


class ReservesAreWrittenBeforeAnythingCanBeDropped(StorageTestCase):
    def test_storing_them_needs_no_autonomy_and_no_listing_phase(self):
        client = _Client()
        tick._store_reserves(client, "L1", _team(), None, [])
        self.assertTrue(self.store.get_doc("reserves", {}),
                        "the offer handler has nothing to run on without these")

    def test_listing_reuses_the_market_read_instead_of_repeating_it(self):
        """One request per review, not two: the review is racing a 60s ceiling."""
        client = _Client()
        market, days = tick._store_reserves(client, "L1", _team(), None, [])
        ctx = mock.Mock(dry_run=False)
        saved = config.AUTO_LIST
        config.AUTO_LIST = False
        try:
            tick._plan_listings(ctx, client, "L1", _team(), None, [],
                                market, days)
        finally:
            config.AUTO_LIST = saved
        self.assertEqual(client.market_calls, 1)

    def test_listing_alone_still_works_when_called_without_them(self):
        """The CLI and the tests call it directly; it must not need a caller to
        have done the work first."""
        client = _Client()
        ctx = mock.Mock(dry_run=False)
        saved = config.AUTO_LIST
        config.AUTO_LIST = False
        try:
            got = tick._plan_listings(ctx, client, "L1", _team(), None, [])
        finally:
            config.AUTO_LIST = saved
        self.assertEqual(got["mode"], "off")
        self.assertTrue(self.store.get_doc("reserves", {}))


class TheReportSaysWhatItDropped(StorageTestCase):
    def test_skipped_phases_reach_the_dashboard(self):
        """It was only ever an event, which scrolls away. "No listó a nadie"
        needs a reason that is still there when you go looking."""
        got = tick._summarize({"money": 1, "lineup": {}}, {}, {},
                              skipped_phases=["listings", "clauses"])
        self.assertEqual(got["skipped_phases"], ["listings", "clauses"])


class WhyNobodyGoesUp(StorageTestCase):
    """"Listed 0" over a squad with nobody on the market is a silent refusal,
    and a silent refusal is indistinguishable from a switch being off."""

    def _squad(self, value):
        return {"teamMoney": 0, "players": [
            {"playerTeamId": "pt1",
             "playerMaster": {"id": "m1", "nickname": "Uno", "positionId": 4,
                              "marketValue": value, "playerStatus": "ok"}}]}

    def test_a_player_with_no_price_is_counted_and_named(self):
        got = tick._listing_skips(self._squad(None), [], [])
        self.assertEqual(got, {"sin valor de mercado en la ficha": 1})

    def test_a_price_below_the_floor_is_a_different_reason(self):
        got = tick._listing_skips(self._squad(1_000), [], [])
        self.assertEqual(got, {"reserva por debajo del mínimo": 1})

    def test_one_already_on_the_market_is_not_a_failure(self):
        market = [{"discr": "marketPlayerTeam", "playerMaster": {"id": "m1"}}]
        got = tick._listing_skips(self._squad(5_000_000), market, [])
        self.assertEqual(got, {"ya estaba en el mercado": 1})

    def test_a_player_going_up_is_not_counted_at_all(self):
        got = tick._listing_skips(self._squad(5_000_000), [],
                                  [{"player_id": "m1"}])
        self.assertEqual(got, {})

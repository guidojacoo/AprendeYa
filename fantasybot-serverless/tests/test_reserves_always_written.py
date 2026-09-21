"""A squad standing on the market that sells nothing.

The offer handler runs on every tick and reads one thing: the reserve price of
each player. That was written inside the listing phase — and the listing phase is
time-boxed, dropped whenever the review is running late against the sixty seconds
Vercel allows. So a shortened review left the bot unable to judge a single offer
until a full one came round. The asks were out there; nobody was reading the
replies.
"""

from datetime import timedelta
from unittest import mock

from fantasybot import config, tick
from fantasybot.storage import get_storage, to_iso, utcnow
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
        market, days, expected, paid = tick._store_reserves(
            client, "L1", _team(), None, [])
        ctx = mock.Mock(dry_run=False)
        saved = config.AUTO_LIST
        config.AUTO_LIST = False
        try:
            tick._plan_listings(ctx, client, "L1", _team(), None, [],
                                market, days, expected=expected, paid=paid)
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


class AListingThatDidNotTake(StorageTestCase):
    """The squad read as listed at one moment and absent from the market five
    minutes later. "LaLiga refused this" and "the listing expired" are different
    problems, and they are indistinguishable if nobody checks.

    The check used to be a second market read per listing, immediately after
    the call. That cost 2.1 reads per player — about fifty-six seconds of
    network to list eighteen of them, against a budget that ends at fifty — so
    the queue died half-drained every tick and most of the squad never reached
    the market at all.

    So it is answered against the next market read instead, which the review
    makes anyway. Same verdict, no per-player cost, and it also catches a
    listing LaLiga accepted and then dropped an hour later — which a check made
    four seconds after the call never could.
    """

    def _action(self, pid="m1"):
        return {"payload": {"league_id": "L1", "player_team_id": "pt1",
                            "player_id": pid, "nombre": "Uno",
                            "price": 3_000_000, "value": 2_600_000}}

    class _Client:
        def __init__(self):
            self.sold = []
            self.reads = 0

        def sell_player(self, lid, ptid, price):
            self.sold.append((ptid, price))
            return {"ok": True}

        def market(self, lid):
            self.reads += 1
            return []

    def setUp(self):
        super().setUp()
        self.store = get_storage()
        tick.forget_market()

    def tearDown(self):
        tick.forget_market()
        super().tearDown()

    def _list_one(self, pid="m1"):
        client = self._Client()
        ctx = mock.Mock(dry_run=False)
        ctx.out_of_time.return_value = False
        ctx.get_client.return_value = client
        saved = config.AUTO_LIST
        config.AUTO_LIST = True
        try:
            return client, tick._execute_listing(ctx, self._action(pid))
        finally:
            config.AUTO_LIST = saved

    def _listing(self, pid):
        return {"discr": "marketPlayerTeam", "playerMaster": {"id": pid}}

    def test_listing_one_player_costs_one_market_read(self):
        """The whole point of the change."""
        client, got = self._list_one()
        self.assertEqual(got["status"], "listed")
        self.assertEqual(client.reads, 1)

    def test_listing_ten_players_still_costs_one_market_read(self):
        """A tick shares one snapshot; it used to be two calls a head."""
        client = self._Client()
        ctx = mock.Mock(dry_run=False)
        ctx.out_of_time.return_value = False
        ctx.get_client.return_value = client
        saved = config.AUTO_LIST
        config.AUTO_LIST = True
        try:
            for i in range(10):
                tick._execute_listing(ctx, self._action(f"m{i}"))
        finally:
            config.AUTO_LIST = saved
        self.assertEqual(client.reads, 1)
        self.assertEqual(len(client.sold), 10)

    def test_a_listing_that_appears_is_confirmed(self):
        self._list_one()
        got = tick._reconcile_listings(self.store, [self._listing("m1")])
        self.assertEqual(got["confirmed"], 1)
        self.assertEqual(got["refused"], [])
        self.assertEqual(self.store.get_doc("listing_attempts", {}), {},
                         "a settled attempt is not carried forever")

    def test_a_listing_that_does_not_appear_is_reported_refused(self):
        self._list_one()
        # Past the grace period: absent now means refused, not lagging.
        old = to_iso(utcnow() - timedelta(seconds=tick.LISTING_GRACE_SECONDS + 60))
        attempts = self.store.get_doc("listing_attempts", {})
        attempts["m1"]["at"] = old
        self.store.put_doc("listing_attempts", attempts)
        got = tick._reconcile_listings(self.store, [])
        self.assertEqual([r["nombre"] for r in got["refused"]], ["Uno"])

    def test_a_listing_still_settling_is_not_called_refused(self):
        """Absent one minute later is lag, not a refusal."""
        self._list_one()
        got = tick._reconcile_listings(self.store, [])
        self.assertEqual(got["refused"], [])
        self.assertEqual(got["pending"], 1, "still being watched")

    def test_nothing_attempted_is_not_a_crash(self):
        self.assertEqual(tick._reconcile_listings(self.store, []), {})

"""The squad has to be on the market at ALL times.

Reported live, twice: "sigue sin poner el equipo en venta. todo el equipo tiene
que estar en venta en el mercado en todo momento."

The listing action was keyed by CALENDAR DATE — `list:<league>:<player>:<date>`
— and a finished action with a known key is a permanent no-op. That is the
anti-double-bid rule, and it was doing its job on the wrong thing.

LaLiga's listings lapse when the market closes. From that moment the squad was
off the market, and every hourly review that tried to put it back was swallowed
by the key until midnight. Simulated over one day: listed at 00h, lapsed at 14h,
and the planner correctly queued all of them at 15h, 16h, 17h and every hour
after — not one reached the market. Ten hours a day with nothing for sale.

Double-listing never needed that key to be prevented: the planner skips anyone
currently on the market, and the executor re-checks against the tick's snapshot.
"""

from datetime import timedelta
from unittest import mock

from fantasybot import scheduler, tick
from fantasybot.scheduler import TickContext
from fantasybot.storage import get_storage, to_iso, utcnow
from tests.support import StorageTestCase

SQUAD = 4


class _Client:
    def __init__(self, refuse=()):
        self.on_market: set = set()
        self.refuse = set(refuse)
        self.sell_calls = 0

    def market(self, lid):
        return [{"discr": "marketPlayerTeam", "playerMaster": {"id": p}}
                for p in sorted(self.on_market)]

    def sell_player(self, lid, ptid, price):
        self.sell_calls += 1
        pid = str(ptid).replace("pt", "m")
        if pid not in self.refuse:
            self.on_market.add(pid)
        return {"ok": True}


def _team():
    return {"teamMoney": 10_000_000, "players": [
        {"playerTeamId": f"pt{i}", "playerMaster": {
            "id": f"m{i}", "nickname": f"J{i}", "positionId": 2,
            "marketValue": "5000000", "averagePoints": "4",
            "playerStatus": "ok"}} for i in range(SQUAD)]}


class _Day(StorageTestCase):
    """Drives whole hours of the real cycle: review, queue, execute."""

    def setUp(self):
        super().setUp()
        self.store = get_storage()
        self.client = _Client()
        self.t0 = utcnow().replace(minute=0, second=0, microsecond=0)

    def _hour(self, hour, lapse=False):
        now = self.t0 + timedelta(hours=hour)
        if lapse:
            self.client.on_market.clear()
        with mock.patch.object(tick, "utcnow", lambda: now), \
             mock.patch("fantasybot.storage.local.utcnow", lambda: now):
            ctx = TickContext(budget_seconds=50, log=lambda m: None)
            ctx.get_client = lambda: self.client
            tick.forget_market()
            res = tick._plan_listings(ctx, self.client, "L1", _team(), None, [])
            scheduler.run_due(ctx, now=now, log=lambda m: None)
        return res


class ALapsedListingGoesStraightBackUp(_Day):
    def test_the_squad_is_on_the_market_again_the_same_review(self):
        """The bug, in one assertion."""
        self._hour(0)
        self.assertEqual(len(self.client.on_market), SQUAD)
        self._hour(1, lapse=True)
        self.assertEqual(len(self.client.on_market), SQUAD,
                         "it lapsed and was re-listed in the same review")

    def test_a_whole_day_never_leaves_the_squad_unlisted(self):
        counts = []
        for hour in range(12):
            self._hour(hour, lapse=(hour in (4, 9)))
            counts.append(len(self.client.on_market))
        self.assertEqual(counts, [SQUAD] * 12, f"off the market: {counts}")

    def test_it_does_not_re_list_somebody_already_up(self):
        """Re-listing hourly must not mean listing twice."""
        self._hour(0)
        before = self.client.sell_calls
        for hour in range(1, 5):
            self._hour(hour)
        self.assertEqual(self.client.sell_calls, before,
                         "nobody lapsed, so nobody needed re-listing")


class ARefusalIsNotRetriedForever(_Day):
    def setUp(self):
        super().setUp()
        self.client = _Client(refuse={"m0"})

    def _reconcile(self, hour):
        now = self.t0 + timedelta(hours=hour)
        with mock.patch.object(tick, "utcnow", lambda: now):
            return tick._reconcile_listings(self.store, self.client.market("L1"))

    def _age_attempts(self, hour=0):
        """Push the attempts past the grace period without waiting.

        Aged against t0, not against the wall clock: the reconciliation runs on
        the patched clock, and mixing the two makes an attempt look like it was
        recorded in the future.
        """
        attempts = self.store.get_doc("listing_attempts", {}) or {}
        at = (self.t0 + timedelta(hours=hour)
              - timedelta(seconds=tick.LISTING_GRACE_SECONDS + 60))
        for row in attempts.values():
            row["at"] = to_iso(at)
        self.store.put_doc("listing_attempts", attempts)

    def test_a_refused_player_is_reported(self):
        self._hour(0)
        self._age_attempts()
        got = self._reconcile(0)
        self.assertEqual([r["player_id"] for r in got["refused"]], ["m0"])

    def test_he_is_held_back_instead_of_retried_every_hour(self):
        self._hour(0)
        self._age_attempts()
        self._reconcile(0)
        held = tick._listing_backoff(self.store)
        self.assertIn("m0", held)
        res = self._hour(1)
        self.assertNotIn("m0", [r["player_id"] for r in res["listed"]])

    def test_the_wait_grows_with_each_refusal(self):
        strikes = {"m0": {"count": 1, "last": to_iso(self.t0)}}
        self.store.put_doc("listing_strikes", strikes)
        first = tick._listing_backoff(self.store)["m0"]["until"]
        strikes["m0"]["count"] = 3
        self.store.put_doc("listing_strikes", strikes)
        self.assertGreater(tick._listing_backoff(self.store)["m0"]["until"],
                           first)

    def test_the_wait_is_capped(self):
        self.store.put_doc("listing_strikes",
                           {"m0": {"count": 99, "last": to_iso(self.t0)}})
        until = tick._listing_backoff(self.store)["m0"]["until"]
        cap = self.t0 + timedelta(hours=tick.LISTING_BACKOFF_MAX_HOURS + 1)
        self.assertLess(until, to_iso(cap))

    def test_one_listing_that_works_clears_the_record(self):
        self.store.put_doc("listing_strikes",
                           {"m0": {"count": 4, "last": to_iso(utcnow())}})
        self.store.put_doc("listing_attempts",
                           {"m0": {"at": to_iso(utcnow()), "nombre": "J0",
                                   "price": 1}})
        tick._reconcile_listings(
            self.store, [{"discr": "marketPlayerTeam",
                          "playerMaster": {"id": "m0"}}])
        self.assertEqual(self.store.get_doc("listing_strikes", {}), {})

    def test_a_listing_judged_too_late_is_never_called_refused(self):
        """A listing that ran its course looks exactly like a rejection.

        Every listing ends by lapsing, so judging one an hour after the fact
        would bench the whole squad for doing nothing wrong. This is the guard
        that made the lapse tests above pass.
        """
        self._hour(0)
        attempts = self.store.get_doc("listing_attempts", {}) or {}
        stale = self.t0 - timedelta(seconds=tick.LISTING_VERDICT_MAX_SECONDS + 60)
        for row in attempts.values():
            row["at"] = to_iso(stale)
        self.store.put_doc("listing_attempts", attempts)
        with mock.patch.object(tick, "utcnow", lambda: self.t0):
            got = tick._reconcile_listings(self.store, [])
        self.assertEqual(got["refused"], [])
        self.assertEqual(self.store.get_doc("listing_strikes", {}), {})

    def test_everybody_else_still_goes_up(self):
        """One refusal must not stop the other three."""
        self._hour(0)
        self.assertEqual(self.client.on_market, {"m1", "m2", "m3"})


if __name__ == "__main__":
    import unittest
    unittest.main()
